"""Router — rank skills by semantic similarity to a query.

The Embedder yields L2-normalized vectors, so cosine similarity reduces
to a plain dot product. With ~100 skills × 1024 dim that's a single
(1, dim) × (dim, N) matmul — sub-millisecond on CPU.

When a skill carries ``mega_meta:`` evaluation evidence (from the Stop
hook's self-evaluation), :func:`ranker.adjusted_score` blends in a small
Bayesian count bonus + a task-specific context-match boost + status
filter. Cold-start skills are unaffected (the blend reduces to pure
semantic when no evidence exists). See ``ranker.py``.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from mega_tron.cache import (
    Cache,
    CacheEntry,
    file_sha16,
    make_sync_row,
    skill_priority_key,
)
from mega_tron.dynamic_k import DynamicKConfig, dynamic_k, profile_for
from mega_tron.verdicts.mega_meta import MegaMeta
from mega_tron.pre_flight import (
    ValidationError,
    normalize_desc,
    parse_frontmatter,
    validate,
)
from mega_tron.tokens import count_skill_tokens

if TYPE_CHECKING:
    import numpy as np

    from mega_tron.agentic import AgenticSearch
    from mega_tron.embedder import Embedder


@dataclass(frozen=True)
class Skill:
    """A pre-flight-validated SKILL.md ready for embedding and staging."""

    name: str
    skill_dir: Path  # original directory (symlink target)
    description: str
    desc_tok: int  # name + description char count // CHARS_PER_TOKEN
    sha: str  # 16-hex content SHA of SKILL.md (cache key)
    # Schema v3: NL evaluation contexts (≤3 each) from mega_meta YAML.
    helpful_contexts: tuple[str, ...] = field(default_factory=tuple)
    harmful_contexts: tuple[str, ...] = field(default_factory=tuple)
    # Schema v4: mega_meta counters + status, carried so the Cache can store
    # them and Router.rank reads from CacheEntry without re-parsing YAML.
    helpful_count: int = 0
    harmful_count: int = 0
    status: str = "active"
    consecutive_harmful: int = 0


@dataclass(frozen=True)
class RankedSkill:
    """A Skill with its query similarity score and origin Cache entry."""

    skill: Skill
    score: float

    @property
    def name(self) -> str:
        return self.skill.name


def load_skills(
    skills_dir: Path | list[Path],
    invalid: list | None = None,
) -> list[Skill]:
    """Walk one or more skill roots, validate each SKILL.md, return Codex-
    compatible Skills.

    Each Skill carries its ``mega_meta:`` helpful/harmful contexts (if
    present) so the Cache can embed them in the same batched call as
    description+name.

    Accepts either a single Path or a list — multi-dir lets the router
    cover ``~/.claude/skills``, ``~/.codex/skills``, and any
    user-registered roots in one warmup.

    When two or more SKILL.md files share a ``name:`` field, winner
    selection uses :func:`mega_tron.cache.skill_priority_key` — the same
    key :meth:`Cache.compact_skills` uses for semantic-near-duplicate
    clusters. Order: status (active > suspect > archived) → net verdict
    score (helpful − harmful) → SKILL.md mtime. Losers are appended to
    ``invalid`` with a one-line reason that names the winner so the user
    can see why the collision was resolved that way.

    Args:
        skills_dir: directory (or list of directories) whose immediate
            children are skill folders, each containing a SKILL.md.
        invalid: optional list — appended with ValidationError for any
            skill that fails pre-flight checks or that loses a name-
            collision tiebreak against a same-named sibling.
    """
    if isinstance(skills_dir, (list, tuple)):
        dirs = list(skills_dir)
    else:
        dirs = [skills_dir]

    # Two-pass: gather every valid candidate first, then resolve
    # name-collisions by priority. One-pass first-dir-wins discarded the
    # verdict signal — a 100-HELPFUL skill in ~/.codex/skills used to
    # lose to a stale duplicate in ~/.claude/skills just because the
    # claude dir was discovered first.
    @dataclass
    class _Candidate:
        skill: Skill
        skill_md: Path

    by_name: dict[str, list[_Candidate]] = {}
    for root in dirs:
        if not root.exists():
            continue
        for entry in sorted(root.iterdir()):
            skill_md = entry / "SKILL.md"
            if not entry.is_dir() or not skill_md.exists():
                continue
            err = validate(skill_md)
            if err is not None:
                if invalid is not None:
                    invalid.append(err)
                continue
            fm, _ = parse_frontmatter(skill_md)
            name = str(fm.get("name") or entry.name).strip()
            desc = normalize_desc(fm.get("description"))
            meta = MegaMeta.from_dict(fm.get("mega_meta") or {})
            skill = Skill(
                name=name,
                skill_dir=entry.resolve(),
                description=desc,
                desc_tok=count_skill_tokens(name, desc),
                sha=file_sha16(skill_md),
                helpful_contexts=tuple(meta.helpful_contexts),
                harmful_contexts=tuple(meta.harmful_contexts),
                helpful_count=meta.helpful_count,
                harmful_count=meta.harmful_count,
                status=meta.status,
                consecutive_harmful=meta.consecutive_harmful,
            )
            by_name.setdefault(name, []).append(
                _Candidate(skill=skill, skill_md=skill_md)
            )

    out: list[Skill] = []
    for name, cands in by_name.items():
        if len(cands) == 1:
            out.append(cands[0].skill)
            continue
        ranked = sorted(
            cands,
            key=lambda c: skill_priority_key(
                status=c.skill.status,
                helpful_count=c.skill.helpful_count,
                harmful_count=c.skill.harmful_count,
                skill_md_path=c.skill_md,
            ),
            reverse=True,
        )
        winner = ranked[0]
        out.append(winner.skill)
        if invalid is not None:
            for loser in ranked[1:]:
                invalid.append(
                    ValidationError(
                        skill_md=loser.skill_md,
                        reason=(
                            f"duplicate skill name {name!r} "
                            f"(kept: {winner.skill_md}, "
                            f"reason: status={winner.skill.status} "
                            f"verdict_score="
                            f"{winner.skill.helpful_count - winner.skill.harmful_count})"
                        ),
                    )
                )
    return out


def _eval_blend_default() -> bool:
    """Read MEGA_EVAL_BLEND; default ON."""
    raw = os.environ.get("MEGA_EVAL_BLEND", "").strip().lower()
    if not raw:
        return True
    return raw not in ("0", "false", "no", "off")


class Router:
    """Rank skills by cosine similarity to a query, optionally blended with
    per-skill evaluation evidence from `mega_meta:` YAML.
    """

    def __init__(
        self,
        skills_dir: "Path | list[Path] | None" = None,
        embedder: "Embedder | None" = None,
        cache: "Cache | None" = None,
        use_eval: bool | None = None,
        *,
        skills_dirs: "list[Path] | None" = None,
    ) -> None:
        """Construct a router.

        ``skills_dir`` accepts either a single Path or a list of Paths.
        The dedicated ``skills_dirs`` kwarg is the same thing under a
        clearer name; pass whichever reads cleaner at the call site.
        ``None`` is also accepted — callers that build the dir list
        lazily (e.g. the CLI when ``--skills-dir`` is omitted) can
        delegate to :func:`mega_tron.config.discover_skill_dirs`.
        """
        if skills_dirs is not None:
            self.skills_dirs: list[Path] = [Path(p) for p in skills_dirs]
        elif skills_dir is None:
            self.skills_dirs = []
        elif isinstance(skills_dir, (list, tuple)):
            self.skills_dirs = [Path(p) for p in skills_dir]
        else:
            self.skills_dirs = [Path(skills_dir)]
        # Single-dir alias; first entry is the "primary" dir for callers
        # that only need one path.
        self.skills_dir = self.skills_dirs[0] if self.skills_dirs else None
        if embedder is None or cache is None:
            raise ValueError("Router requires both `embedder` and `cache`.")
        self.embedder = embedder
        self.cache = cache
        self.use_eval = _eval_blend_default() if use_eval is None else bool(use_eval)
        self._loaded = False
        # Set by :meth:`rank` when called with ``dynamic=True`` so hosts
        # can ship the chosen K + branch reason as telemetry. ``None``
        # when the most recent rank() ran with ``dynamic=False`` (or
        # hasn't been called yet).
        self.last_dynamic: tuple[int, str] | None = None

    def warmup(self) -> tuple[int, int, list]:
        """Load skills from every registered dir, sync the cache, persist.
        Idempotent. Returns ``(n_re_embedded, n_reused, invalid_errors)``.

        :func:`load_skills` walks every entry in ``self.skills_dirs`` in
        order, dedupes by ``name:`` field (first dir wins), and the
        cache holds the union. The cache is keyed on per-skill SHA so
        re-running ``warmup`` after a SKILL.md edit only re-embeds the
        changed file — the rest are reused regardless of which root
        they live under.
        """
        invalid: list = []
        skills = load_skills(self.skills_dirs, invalid=invalid)
        self.cache.load()
        rows = [
            make_sync_row(
                s.name,
                s.skill_dir,
                s.description,
                s.desc_tok,
                s.sha,
                list(s.helpful_contexts),
                list(s.harmful_contexts),
                helpful_count=s.helpful_count,
                harmful_count=s.harmful_count,
                status=s.status,
                consecutive_harmful=s.consecutive_harmful,
            )
            for s in skills
        ]
        n_new, n_reused = self.cache.sync(rows, self.embedder)
        self.cache.save()
        self._loaded = True
        return n_new, n_reused, invalid

    def warmup_if_stale(self) -> tuple[int, int, list] | None:
        """Re-run :meth:`warmup` only if any SKILL.md across any registered
        dir has an mtime newer than the cache file.

        Multi-dir: the freshest SKILL.md anywhere triggers a refresh, but
        the actual re-embed is still SHA-keyed so unchanged skills stay
        warm. With no cache file at all we always do a full warmup.
        """
        cache_mtime = (
            self.cache.path.stat().st_mtime if self.cache.path.exists() else 0.0
        )
        if cache_mtime == 0.0:
            return self.warmup()
        if not self.skills_dirs:
            return None
        latest_mtime = 0.0
        for root in self.skills_dirs:
            if not root.exists():
                continue
            for entry in root.iterdir():
                skill_md = entry / "SKILL.md"
                if entry.is_dir() and skill_md.exists():
                    mtime = skill_md.stat().st_mtime
                    if mtime > latest_mtime:
                        latest_mtime = mtime
        if latest_mtime == 0.0:
            return None
        if latest_mtime > cache_mtime:
            return self.warmup()
        return None

    def _ensure_warm(self) -> None:
        if not self._loaded:
            self.warmup()

    def rank(
        self,
        query: str,
        top_k: int = 5,
        *,
        prefilter: int | None = None,
        agentic: "AgenticSearch | None" = None,
        dynamic: bool = False,
        dynamic_cfg: DynamicKConfig | None = None,
    ) -> list[RankedSkill]:
        """Rank cached skills.

        - If ``agentic`` is provided, runs the agentic pipeline:
          cosine prefilter → LLM pick → optional SKILL.md read → final
          pick. Returns RankedSkill objects in the order the LLM chose,
          with score carried from the cosine prefilter. (Agentic uses
          its own prefilter knob via :attr:`AgenticSearch.top`; the
          ``prefilter`` arg here is ignored when agentic is set.)
        - Otherwise: semantic max(cos) blended with mega_meta evaluation
          evidence per :func:`ranker.adjusted_score` when ``use_eval``
          is True. When ``prefilter`` is set, only the top-N cosine
          candidates are passed to the eval-blend rerank — this matters
          at scale where blending the whole cache lets weak-evidence
          skills jump ranks.

        ``top_k`` is the hard cap. ``dynamic=True`` enables the
        :mod:`dynamic_k` policy: the actual returned count is chosen
        from the score distribution (0 for null-prompts, more for
        ambiguous ones, gap-cut otherwise), clamped down to ``top_k``.
        The last decision is exposed on ``self.last_dynamic`` as
        ``(k, reason)`` for telemetry. Agentic path ignores ``dynamic``.
        """
        import numpy as np

        self._ensure_warm()
        entries = self.cache.entries()
        if not entries:
            return []
        # Asymmetric embedders (e.g. SkillRet-Embedding-0.6B) need an
        # instruction prefix on QUERIES but plain text on documents. We
        # honor a `query_prefix` attribute on the embedder so cache builds
        # stay prefix-free while query embeds get the prompted form.
        prefix = getattr(self.embedder, "query_prefix", None) or ""
        q_text = (prefix + query) if prefix else query
        q_vec = self.embedder.embed([q_text])  # (1, dim)

        if agentic is not None:
            return self._agentic_rank(query, q_vec[0], top_k, agentic)

        full = self.cache.embeddings_matrix()
        nameonly = self.cache.name_embeddings_matrix()
        scores_full = (full @ q_vec.T).ravel()
        scores_name = (nameonly @ q_vec.T).ravel()
        semantic = np.maximum(scores_full, scores_name)

        # Optional cosine prefilter cut before the eval-blend rerank.
        # When unset, the blend runs over every entry in the cache.
        if prefilter is not None and prefilter < len(entries):
            cos_order = np.argsort(-semantic)[:prefilter]
            candidate_entries = [entries[i] for i in cos_order]
            candidate_semantic = semantic[cos_order]
        else:
            candidate_entries = list(entries)
            candidate_semantic = semantic

        if self.use_eval:
            from mega_tron.ranker import adjusted_score

            q_vec_flat = q_vec[0]
            # Verdict-embedding lookup: ask the on-disk verdict store
            # for the top-K most semantically similar past verdicts,
            # then partition by polarity per skill. Empty / unavailable
            # store → all maxes default to 0 and the related-verdict
            # signal contributes nothing (backward compat for installs
            # with no embedding store yet).
            related_helpful: dict[str, float] = {}
            related_harmful: dict[str, float] = {}
            try:
                from mega_tron.verdicts.embeddings import (
                    VerdictEmbeddingsStore,
                )
                from mega_tron.embedder import fingerprint_of

                ves = VerdictEmbeddingsStore(
                    fingerprint=fingerprint_of(self.embedder)
                )
                if len(ves) > 0:
                    # Pull a generous top-K so the per-skill max is
                    # actually a max over the relevant verdicts for
                    # this skill (small K can miss the relevant one
                    # behind unrelated near-duplicates).
                    related = ves.query(q_vec_flat, top_k=40)
                    for _vid, sk, label, cos in related:
                        if label == "HELPFUL":
                            if cos > related_helpful.get(sk, 0.0):
                                related_helpful[sk] = cos
                        elif label == "HARMFUL":
                            if cos > related_harmful.get(sk, 0.0):
                                related_harmful[sk] = cos
            except Exception:
                # Embedding store unavailable (fresh install, npz on
                # different host, etc.) — fall through with empty maps.
                pass

            blended = np.empty(len(candidate_entries), dtype=np.float32)
            for i, entry in enumerate(candidate_entries):
                meta = self._meta_from_entry(entry)
                blended[i] = adjusted_score(
                    semantic=float(candidate_semantic[i]),
                    query_vec=q_vec_flat,
                    meta=meta,
                    helpful_ctx_embs=entry.embedding_helpful_ctxs,
                    harmful_ctx_embs=entry.embedding_harmful_ctxs,
                    related_helpful_max=related_helpful.get(entry.name, 0.0),
                    related_harmful_max=related_harmful.get(entry.name, 0.0),
                )
            scores = blended
        else:
            scores = candidate_semantic

        order = np.argsort(-scores)

        if dynamic:
            sorted_scores = [float(scores[i]) for i in order]
            # Auto-select an embedder profile (with abs_floor) when the
            # caller didn't pin a config. Falls back to the bare default
            # (no floor) for unknown embedders — see
            # mega_tron.dynamic_k.EMBEDDER_PROFILES.
            cfg = dynamic_cfg
            if cfg is None:
                model_id = getattr(self.embedder, "model_id", None)
                cfg = profile_for(model_id)
            k, reason = dynamic_k(sorted_scores, cfg=cfg)
            self.last_dynamic = (k, reason)
            cut = min(k, top_k)
        else:
            self.last_dynamic = None
            cut = top_k
        return [
            self._make_ranked(candidate_entries[i], float(scores[i]))
            for i in order[:cut]
        ]

    def _agentic_rank(
        self,
        query: str,
        q_vec: "np.ndarray",
        top_k: int,
        agentic: "AgenticSearch",
    ) -> list[RankedSkill]:
        """Run the agentic pipeline. Falls back to cosine on backend errors.

        Optional multi-perspective path: if
        ``agentic.multi_perspective`` is on, an extra LLM call
        decomposes the query into 1..hyde_n hypothetical SKILL
        descriptions; each is embedded and used as its own cosine
        query, with per-perspective top-Ks unioned into the candidate
        pool. Falls back to single-vector prefilter if decompose
        returns nothing.

        Conditional-agentic gate: when the cosine top-1 dominates the
        top-K by ``confidence_gap``, return the cosine top-K directly
        and skip the LLM call entirely.
        """
        candidates = self._build_candidates(query, q_vec, agentic)
        if not candidates:
            return []
        # Cosine score for display/telemetry — reuses the same matmul
        # logic so the returned RankedSkill carries a comparable score.
        cos_scores = {e.name: float(q_vec @ e.embedding) for e in candidates}

        # Conditional gate: if top1 - topK_cos > threshold, the prefilter
        # is confident in a single dominant skill; LLM rerank is
        # net-negative on these. Honor multi_perspective + always_read
        # toggles (those force the agentic path even when confident).
        if (
            agentic.skip_when_confident
            and not agentic.multi_perspective
            and not agentic.always_read
            and len(candidates) >= top_k
        ):
            cos_sorted = sorted(cos_scores.values(), reverse=True)
            gap = cos_sorted[0] - cos_sorted[top_k - 1]
            if gap >= agentic.confidence_gap:
                return [
                    self._make_ranked(e, cos_scores[e.name])
                    for e in candidates[:top_k]
                ]

        # Body-first bypasses the metadata pick. cosine top-N bodies →
        # single LLM call → final pick. Useful when one-liners are too
        # sparse to disambiguate but the SKILL.md bodies aren't.
        if agentic.body_first:
            result = agentic.pick_body_first(query, candidates)
        else:
            result = agentic.pick(query, candidates)
        by_name = {e.name: e for e in candidates}
        cosine_top1 = candidates[0].name  # candidates already sorted by cosine

        # Optional hybrid: pin the cosine top-1 at position 0 and let the
        # LLM rank #2..#K. A strong embedder's top-1 is more reliable
        # than the LLM's #1 picked from bodies alone.
        ordered_names: list[str] = []
        if agentic.body_first and agentic.pin_cosine_top1:
            ordered_names.append(cosine_top1)
        seen: set[str] = set(ordered_names)
        for name in result.picks:
            if name in seen or name not in by_name:
                continue
            seen.add(name)
            ordered_names.append(name)
            if len(ordered_names) >= top_k:
                break

        return [
            self._make_ranked(by_name[n], cos_scores.get(n, 0.0))
            for n in ordered_names[:top_k]
        ]

    def _build_candidates(
        self,
        query: str,
        q_vec: "np.ndarray",
        agentic: "AgenticSearch",
    ) -> list[CacheEntry]:
        """Build the candidate pool for agentic step B.

        Honors ``multi_perspective``: when on, decompose the query into
        hypothetical SKILL descriptions and union per-perspective top-Ks.
        Empty decompose → fall back to single-vector prefilter so the
        pipeline degrades gracefully on backend failure.
        """
        if not agentic.multi_perspective:
            return agentic.prefilter(self.cache, q_vec)
        hypotheticals = agentic.decompose(query)
        if not hypotheticals:
            return agentic.prefilter(self.cache, q_vec)
        hyp_vecs = self.embedder.embed(hypotheticals)  # (N_hyp, dim)
        return agentic.prefilter_multi(self.cache, hyp_vecs)

    def _meta_from_entry(self, entry: CacheEntry) -> MegaMeta:
        """Reconstruct a MegaMeta view straight out of CacheEntry — no YAML I/O.

        Counts/status are cached inside the .npz. Cache invalidation is
        SHA-keyed, and ``update_meta()`` rewrites SKILL.md (flipping the
        SHA), so the cached row stays coherent with the file on disk.
        """
        return MegaMeta(
            helpful_count=entry.helpful_count,
            harmful_count=entry.harmful_count,
            helpful_contexts=list(entry.helpful_contexts),
            harmful_contexts=list(entry.harmful_contexts),
            status=entry.status,
            consecutive_harmful=entry.consecutive_harmful,
        )

    def score(self, query: str, skill: Skill) -> float:
        """Single-skill similarity — used by mega-symphony's hybrid scoring hook.

        Note: this re-embeds the skill if it's not in the cache yet, but does
        NOT persist. For batched hybrid scoring, call warmup() first. Returns
        pure semantic — no evaluation blend (the caller composes their own
        hybrid score using `mega_meta` directly).
        """
        self._ensure_warm()
        entry = self._entry_for(skill)
        q_vec = self.embedder.embed([query])[0]
        return float(q_vec @ entry.embedding)

    def _entry_for(self, skill: Skill) -> CacheEntry:
        for entry in self.cache.entries():
            if entry.name == skill.name and entry.sha == skill.sha:
                return entry
        text = f"{skill.name}\n{skill.description}"
        vec = self.embedder.embed([text])[0]
        entry = CacheEntry(
            name=skill.name,
            embedding=vec,
            sha=skill.sha,
            skill_dir=skill.skill_dir,
            description=skill.description,
            desc_tok=skill.desc_tok,
        )
        self.cache.upsert(entry)
        return entry

    def _make_ranked(self, entry: CacheEntry, score: float) -> RankedSkill:
        skill = Skill(
            name=entry.name,
            skill_dir=entry.skill_dir,
            description=entry.description,
            desc_tok=entry.desc_tok,
            sha=entry.sha,
            helpful_contexts=tuple(entry.helpful_contexts),
            harmful_contexts=tuple(entry.harmful_contexts),
        )
        return RankedSkill(skill=skill, score=score)


def warmup_if_stale(router: "Router") -> tuple[int, int, list] | None:
    """Module-level alias for Router.warmup_if_stale for convenient imports."""
    return router.warmup_if_stale()
