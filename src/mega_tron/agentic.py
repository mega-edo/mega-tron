"""Agentic skill search — LLM-driven rerank on top of the cosine prefilter.

Pipeline:

    A. Cosine prefilter over the cache (``MEGA_PREFILTER``, default 200).
       Skills with mega_meta.status == "archived" are dropped here so the
       LLM never wastes tokens on them.
    B. LLM call #1: shown the top-K metadata one-liners + the query, returns
       {"pick": [...], "need_to_read": [...]} between sentinels. The
       ``pick`` list is capped at ``MEGA_SHORTLIST`` (default 20).
    C. LLM call #2 (only if ``need_to_read`` non-empty): shown the previous
       response + concatenated SKILL.md bodies for the requested names,
       returns the final {"pick": [...]}. Read count capped at
       ``MEGA_READ_MAX`` (default 5).
    D. Caller (Router.rank) materializes the pick into RankedSkill
       objects via the cache.

Errors are *fail-open*: any LLMBackendError or parse failure falls back
to the cosine-only top-N. The hook never breaks because routing got
smart.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mega_tron._env import read_int_env
from mega_tron._sentinel import extract_json, make_sentinels
from mega_tron.llm_backends import LLMBackend, LLMBackendError

if TYPE_CHECKING:  # pragma: no cover
    import numpy as np

    from mega_tron.cache import Cache, CacheEntry


PICK_SENTINEL_START, PICK_SENTINEL_END = make_sentinels("PICK")
HYDE_SENTINEL_START, HYDE_SENTINEL_END = make_sentinels("HYDE")

DEFAULT_TOP = 200
DEFAULT_SHORTLIST = 20  # LLM step B pick-list cap
DEFAULT_TOP_K = 5  # final ordered pick size — what the LLM is asked to return
DEFAULT_MAX_READS = 5  # step C body-load cap
DEFAULT_TIMEOUT_S = 30
DEFAULT_PREPEND_K = 3  # how many of the picks make it into the must-use prefix
DEFAULT_HYDE_N = 3  # max hypothetical descriptions in multi-perspective mode
DEFAULT_PER_PERSPECTIVE = 100  # cosine top-K per hypothetical before union
# Conditional-agentic gate — skip the LLM when the cosine prefilter is so
# confident in a single dominant skill that LLM rerank can only hurt.
# Signal: top1_cos - topK_cos. >=0.08 reliably marks single-skill on
# SkillRet-Embedding-style score distributions.
DEFAULT_CONFIDENCE_GAP = 0.08


def _is_quiet() -> bool:
    return os.environ.get("MEGA_QUIET", "").strip() not in ("", "0")


@dataclass(frozen=True)
class AgenticResult:
    """Output of one agentic search pass."""

    picks: list[str]
    calls: int
    reads: list[str]
    fell_back: bool
    error: str | None = None


def _system_prompt_pick(shortlist: int, max_reads: int, top_k: int) -> str:
    """Step B system prompt.

    The model is asked for an *ordered* list of length ``top_k`` (most
    relevant first). ``shortlist`` is the hard cap if it wants to surface
    backup candidates beyond ``top_k``. ``max_reads`` bounds the
    ``need_to_read`` request.

    Multi-skill framing matters: a SkillRet ablation showed the LLM
    confidently demotes co-relevant skills for k≥2 queries — k=3 ranker
    miss rate was 41% in v4. The prompt now explicitly states that a task
    may need several coordinated skills and asks the model to include ALL
    skills addressing ANY aspect of the task, not just the dominant one.

    Candidates are pre-sorted by cosine similarity (most-similar first);
    revealing that ordering gives the LLM a prior to weight against.
    """
    return (
        "You are a skill router scoring candidate skills against a user "
        "task. A task often requires SEVERAL skills working together "
        "(e.g. deploy + monitor + log). Your job is to rank EVERY skill "
        "that addresses ANY aspect of the task — not just the single "
        "best-fitting skill.\n\n"
        "The candidate list below is pre-sorted by semantic similarity "
        "to the query (most similar first); treat that as a prior but "
        "feel free to reorder when the SKILL metadata gives you a "
        "better signal.\n\n"
        f"Return the top {top_k} skills in DESCENDING order of "
        "relevance. If multiple skills cover different sub-needs of the "
        "task, ALL of them belong in the list — do not discard a "
        "relevant skill just because another one is more central. You "
        f"may extend up to {shortlist} skills total to surface backup "
        f"candidates. If you cannot decide between candidates from "
        f"metadata alone, list up to {max_reads} of them under "
        '"need_to_read" and we will fetch their full SKILL.md so you '
        "can re-pick on the next turn.\n\n"
        "Reply with ONE JSON object between the sentinels — nothing else "
        "between them, and no commentary inside the JSON.\n\n"
        f"{PICK_SENTINEL_START}\n"
        '{"pick": ["name1", "name2"], "need_to_read": ["name3"]}\n'
        f"{PICK_SENTINEL_END}\n\n"
        "Empty `need_to_read` is fine. Names must come from the candidate list."
    )


def _system_prompt_body_first(top_k: int, n_bodies: int, n_backup: int) -> str:
    """Body-first pipeline prompt.

    Replaces the metadata-only step B that the SkillRet ablation showed to
    be net-negative (k=2/3 NDCG regressed because one-liners hid signal).
    The LLM gets full SKILL.md bodies for the top-N cosine candidates plus
    metadata one-liners for the rest of the prefilter — so it can verify
    relevance from real content and still pull a backup candidate when a
    body-read skill clearly does not fit.

    Cosine ordering is a strong prior here (SkillRet-Embedding-0.6B is
    task-fine-tuned), so the prompt explicitly says "use bodies to refine,
    not overturn" — over-thinking with bodies regresses the strong prior.
    """
    return (
        "You are a skill router. A coding task often requires SEVERAL "
        "skills working together (e.g. deploy + monitor + log). Your job: "
        "rank EVERY skill that addresses ANY aspect of the task — not "
        "just the single best-fit.\n\n"
        f"You are shown the FULL SKILL.md body for the top {n_bodies} "
        "candidates (pre-ranked by semantic similarity — this is a strong "
        f"prior tuned for skill retrieval) and METADATA ONE-LINERS for "
        f"{n_backup} backup candidates ranked below. Treat the body order "
        "as the default; only reorder when a body clearly reveals "
        "stronger or weaker fit than its position suggests. Pull a backup "
        "candidate up only if a body-read candidate clearly does not fit "
        "the task and the backup metadata is a clearly better match.\n\n"
        f"Return the top {top_k} skills in DESCENDING order of "
        "relevance. If multiple skills cover different sub-needs of the "
        "task, ALL of them belong in the list — do not discard a "
        "relevant skill just because another one is more central.\n\n"
        "Reply with ONE JSON object between the sentinels — nothing else "
        "between them, and no commentary inside the JSON.\n\n"
        f"{PICK_SENTINEL_START}\n"
        '{"pick": ["name1", "name2"]}\n'
        f"{PICK_SENTINEL_END}\n\n"
        "Names must come from the candidate list (body or backup)."
    )


def _system_prompt_disambig(top_k: int) -> str:
    """Step C system prompt — final ordered pick after body reads.

    Same multi-skill framing as the pick prompt: a task can legitimately
    need several skills; co-relevant skills must not be dropped from the
    final pick just because the body of one read-skill turned out very
    strong.
    """
    return (
        "You previously asked to read the full SKILL.md for some skills. "
        "Their bodies are included below. Remember: a task can require "
        "SEVERAL skills working together. Keep every skill that "
        "addresses ANY aspect of the task — do not drop a relevant skill "
        "just because a deeper read confirmed another one is strongest.\n\n"
        "Return the FINAL ordered list of the top "
        f"{top_k} relevant skills (descending relevance), replacing or "
        "supplementing your earlier picks as appropriate.\n\n"
        "Reply with ONE JSON object between the sentinels, no other content.\n\n"
        f"{PICK_SENTINEL_START}\n"
        '{"pick": ["name1", "name2"]}\n'
        f"{PICK_SENTINEL_END}"
    )


def _system_prompt_decompose(hyde_n: int) -> str:
    """Step 0 (multi-perspective) system prompt.

    Asks the model to anticipate which *kinds* of skills the user will
    need and write a hypothetical SKILL.md ``description`` field for each.
    Each hypothetical is then embedded separately and used as its own
    cosine prefilter query — multi-vector retrieval rather than the
    single-intent baseline. The HyDE framing closes the
    task → SKILL-description semantic gap that hurts plain query embedding.

    The model writes 1 to ``hyde_n`` hypotheticals — many tasks are
    single-skill and forcing N>1 invents noise.
    """
    return (
        "You are a skill router. The user has a coding task. Anticipate "
        "which kinds of skills they will need.\n\n"
        f"Write 1 to {hyde_n} hypothetical SKILL.md `description` fields "
        "— one per distinct sub-skill the task requires. Each description "
        "must follow this exact archetype format:\n\n"
        "  USE WHEN: <trigger conditions; archetype, not specific case>\n"
        "  PREFER OVER: <which alternative skills/tools this replaces>\n"
        "  AVOID IF: <conditions to skip this skill>\n\n"
        "Constraints per hypothetical:\n"
        "  - ≤ 600 characters total\n"
        "  - All three lines present\n"
        "  - Archetype-level (\"webhook signature validation\"), NOT "
        "specific (\"the SPI-83 endpoint\")\n"
        "  - Do NOT invent skill names — describe the skill's purpose\n"
        "  - Do NOT solve the task; describe the skill that solves it\n\n"
        "If the task is genuinely single-skill, return only ONE "
        "hypothetical. Quality over quantity.\n\n"
        "Reply with ONE JSON object between the sentinels — nothing else "
        "between them.\n\n"
        f"{HYDE_SENTINEL_START}\n"
        '{"hypotheticals": ["USE WHEN: ...\\nPREFER OVER: ...\\nAVOID IF: ...", "..."]}\n'
        f"{HYDE_SENTINEL_END}"
    )


def render_metadata_line(entry: "CacheEntry") -> str:
    """One-liner shown to the LLM. ~20-80 tok per skill."""
    desc = (entry.description or "").strip().replace("\n", " ")
    if len(desc) > 240:
        desc = desc[:237] + "..."
    h = entry.helpful_count
    ha = entry.harmful_count
    status = entry.status or "active"
    return f"- {entry.name}: {desc} (h={h} ha={ha} {status})"


class AgenticSearch:
    """Runs the 4-step agentic pipeline against a warmed :class:`Cache`."""

    def __init__(
        self,
        backend: LLMBackend,
        *,
        top: int | None = None,
        shortlist: int | None = None,
        top_k: int | None = None,
        max_reads: int | None = None,
        timeout_s: int | None = None,
        multi_perspective: bool = False,
        always_read: bool = False,
        hyde_n: int | None = None,
        per_perspective: int | None = None,
        skip_when_confident: bool = True,
        confidence_gap: float | None = None,
        body_first: bool = False,
        body_read_n: int | None = None,
        pin_cosine_top1: bool = True,
    ) -> None:
        self.backend = backend
        self.top = top if top is not None else read_int_env("MEGA_PREFILTER", DEFAULT_TOP)
        self.shortlist = (
            shortlist
            if shortlist is not None
            else read_int_env("MEGA_SHORTLIST", DEFAULT_SHORTLIST)
        )
        self.top_k = (
            top_k
            if top_k is not None
            else read_int_env("MEGA_TOP_K", DEFAULT_TOP_K)
        )
        self.max_reads = (
            max_reads
            if max_reads is not None
            else read_int_env("MEGA_READ_MAX", DEFAULT_MAX_READS)
        )
        self.timeout_s = (
            timeout_s
            if timeout_s is not None
            else read_int_env("MEGA_TIMEOUT_S", DEFAULT_TIMEOUT_S)
        )
        # multi_perspective decomposes the query into 1-N hypothetical
        # SKILL descriptions, embeds each, and unions the top-K of each.
        # always_read forces step C to run on every query by promoting
        # step-B picks into need_to_read when the LLM didn't request any.
        self.multi_perspective = multi_perspective
        self.always_read = always_read
        self.hyde_n = (
            hyde_n
            if hyde_n is not None
            else read_int_env("MEGA_HYDE_N", DEFAULT_HYDE_N)
        )
        self.per_perspective = (
            per_perspective
            if per_perspective is not None
            else read_int_env("MEGA_PER_PERSPECTIVE", DEFAULT_PER_PERSPECTIVE)
        )
        # Conditional-agentic gate: when the prefilter shows a clear
        # single-skill winner (top1 - topK > confidence_gap), bypass the
        # LLM rerank — it cannot beat a strong embedder on k=1 queries
        # and only adds latency / cost.
        self.skip_when_confident = skip_when_confident
        self.confidence_gap = (
            confidence_gap
            if confidence_gap is not None
            else DEFAULT_CONFIDENCE_GAP
        )
        # Body-first pipeline: bypass the metadata-only step B and pass
        # full SKILL.md bodies for the top-N cosine candidates plus
        # metadata for the rest of the shortlist in a single LLM call.
        # `body_read_n` controls how many bodies are included (default =
        # max_reads so the existing read-budget knob also tunes
        # body-first).
        self.body_first = body_first
        self.body_read_n = (
            body_read_n
            if body_read_n is not None
            else self.max_reads
        )
        # Hybrid (recommended when body_first is on): force position 0 to
        # the cosine top-1 candidate, then let the LLM rank #2..#K. A
        # strong embedder's top-1 is more reliable than the LLM's #1
        # picked from bodies alone.
        self.pin_cosine_top1 = pin_cosine_top1

    # ---- Step 0 (optional): HyDE decompose ----------------------------------

    def decompose(self, query: str) -> list[str]:
        """Ask the LLM for 1..hyde_n hypothetical SKILL descriptions.

        Returns an empty list on backend / parse failure — the caller can
        then fall back to single-vector prefilter using the raw query.
        """
        try:
            raw = self.backend.chat(
                _system_prompt_decompose(self.hyde_n),
                f"USER TASK:\n{query.strip()}",
                timeout_s=self.timeout_s,
            )
        except LLMBackendError as e:
            if not _is_quiet():
                print(f"[agentic decompose] backend failed: {e}", file=sys.stderr)
            return []
        parsed = extract_json(
            raw,
            start=HYDE_SENTINEL_START,
            end=HYDE_SENTINEL_END,
            fallback_key="hypotheticals",
        )
        if not parsed:
            if not _is_quiet():
                print(
                    f"[agentic decompose] could not parse: {raw[:120]!r}",
                    file=sys.stderr,
                )
            return []
        hyps = parsed.get("hypotheticals")
        if not isinstance(hyps, list):
            return []
        out: list[str] = []
        for h in hyps[: self.hyde_n]:
            if isinstance(h, str) and h.strip():
                # Soft cap matches the prompt constraint (≤600 chars).
                out.append(h.strip()[:600])
        return out

    def prefilter_multi(
        self,
        cache: "Cache",
        query_vecs: "np.ndarray",
    ) -> list["CacheEntry"]:
        """Multi-vector prefilter: take cosine top-K per query vector, then
        union the results (dedupe by name, preserve best score per entry).

        Args:
            cache: warm :class:`Cache`.
            query_vecs: shape ``(N_hypothetical, dim)`` array of
                L2-normalized vectors — one per perspective.
        """
        import numpy as np

        entries = [e for e in cache.entries() if (e.status or "active") != "archived"]
        if not entries:
            return []
        full = np.stack([e.embedding for e in entries])
        dim = full.shape[1]
        nameonly = np.stack(
            [
                e.embedding_name if e.embedding_name is not None
                else np.zeros(dim, dtype=np.float32)
                for e in entries
            ]
        )
        Q = query_vecs.reshape(-1, dim)
        # Score every entry × every perspective; take max over perspectives
        # so an entry that's a great match for ANY hypothetical ranks high.
        scores_full = full @ Q.T  # (N, P)
        scores_name = nameonly @ Q.T
        scores = np.maximum(scores_full, scores_name).max(axis=1)  # (N,)

        # Per-perspective top-K, then union.
        seen: set[int] = set()
        picked: list[int] = []
        for p in range(Q.shape[0]):
            per = np.maximum(scores_full[:, p], scores_name[:, p])
            order = np.argsort(-per)[: self.per_perspective]
            for idx in order:
                if idx not in seen:
                    seen.add(int(idx))
                    picked.append(int(idx))
        # Sort the union by the cross-perspective max score so the LLM
        # sees the strongest candidates first.
        picked.sort(key=lambda i: -float(scores[i]))
        # Hard cap at `self.top` so we don't blow up the LLM context.
        picked = picked[: self.top]
        return [entries[i] for i in picked]

    # ---- Step A: cosine prefilter --------------------------------------------

    def prefilter(self, cache: "Cache", query_vec: "np.ndarray") -> list["CacheEntry"]:
        """Top-N cosine candidates, archived skills excluded."""
        import numpy as np

        entries = [e for e in cache.entries() if (e.status or "active") != "archived"]
        if not entries:
            return []
        full = np.stack([e.embedding for e in entries])
        # Some entries may lack a name vector; fall back to zero.
        dim = full.shape[1]
        nameonly = np.stack(
            [
                e.embedding_name if e.embedding_name is not None
                else np.zeros(dim, dtype=np.float32)
                for e in entries
            ]
        )
        q = query_vec.reshape(-1)
        scores = np.maximum(full @ q, nameonly @ q)
        order = np.argsort(-scores)[: self.top]
        return [entries[i] for i in order]

    # ---- Step B + C: LLM picks -----------------------------------------------

    def pick(
        self,
        query: str,
        candidates: list["CacheEntry"],
        *,
        skill_body_loader=None,
    ) -> AgenticResult:
        """Return the final pick list. Fails open to candidates' top names.

        Args:
            query: the user prompt.
            candidates: prefiltered :class:`CacheEntry` list (length ≤ self.top).
            skill_body_loader: callable ``name -> str | None`` for step C.
                Defaults to reading ``<skill_dir>/SKILL.md`` off disk.
        """
        if not candidates:
            return AgenticResult(picks=[], calls=0, reads=[], fell_back=False)

        by_name = {e.name: e for e in candidates}
        loader = skill_body_loader or _default_skill_body_loader

        # Step B: pick + read-decision.
        system_prompt = _system_prompt_pick(self.shortlist, self.max_reads, self.top_k)
        user_msg = self._format_pick_prompt(query, candidates)
        try:
            raw = self.backend.chat(
                system_prompt, user_msg, timeout_s=self.timeout_s
            )
        except LLMBackendError as e:
            return self._fail_open(candidates, str(e))

        parsed = extract_json(
            raw, start=PICK_SENTINEL_START, end=PICK_SENTINEL_END, fallback_key="pick"
        )
        if not parsed:
            return self._fail_open(candidates, f"could not parse step B output: {raw[:120]!r}")
        picks = _string_list(parsed.get("pick"))
        need_to_read = _string_list(parsed.get("need_to_read"))

        # Filter to names the model is actually allowed to use, then enforce
        # Apply the shortlist and read-max caps. Shortlist applies to
        # `pick` because that's the LLM's confident output; `need_to_read`
        # has its own cap (`max_reads`) since those translate to extra
        # body loads.
        picks = [n for n in picks if n in by_name][: self.shortlist]
        need_to_read = [n for n in need_to_read if n in by_name][: self.max_reads]

        # always_read: if the LLM didn't request any reads but we're asked
        # to always run step C, promote the top picks into need_to_read so
        # step C runs and re-ranks with full-body context.
        if self.always_read and not need_to_read and picks:
            need_to_read = picks[: self.max_reads]

        calls = 1
        reads_done: list[str] = []

        # Step C: disambiguate by reading SKILL.md bodies.
        if need_to_read:
            bodies: list[tuple[str, str]] = []
            for name in need_to_read:
                entry = by_name[name]
                body = loader(entry)
                if body:
                    bodies.append((name, body))
                    reads_done.append(name)
            if bodies:
                user_msg2 = self._format_disambig_prompt(query, picks, bodies)
                try:
                    raw2 = self.backend.chat(
                        _system_prompt_disambig(self.top_k), user_msg2, timeout_s=self.timeout_s
                    )
                except LLMBackendError as e:
                    # Step C failed — keep the step B picks rather than dropping.
                    return AgenticResult(
                        picks=picks,
                        calls=calls,
                        reads=reads_done,
                        fell_back=True,
                        error=f"step C: {e}",
                    )
                calls = 2
                parsed2 = extract_json(
                    raw2,
                    start=PICK_SENTINEL_START,
                    end=PICK_SENTINEL_END,
                    fallback_key="pick",
                )
                if parsed2:
                    final_picks = [
                        n for n in _string_list(parsed2.get("pick")) if n in by_name
                    ]
                    if final_picks:
                        picks = final_picks

        if not _is_quiet():
            print(
                f"[agentic] backend={self.backend.name} calls={calls} "
                f"reads={len(reads_done)} picks={picks}",
                file=sys.stderr,
            )
        return AgenticResult(picks=picks, calls=calls, reads=reads_done, fell_back=False)

    # ---- Step B' (body-first): single LLM call with bodies ------------------

    def pick_body_first(
        self,
        query: str,
        candidates: list["CacheEntry"],
        *,
        skill_body_loader=None,
    ) -> AgenticResult:
        """Body-first pipeline — single LLM call with SKILL.md bodies.

        Bypasses the metadata-only step B. The top ``body_read_n``
        cosine candidates are loaded with their full SKILL.md bodies and
        shown to the LLM; the rest of the prefilter is shown as metadata
        one-liners so the LLM can still pull a backup when a body-read
        candidate clearly misses. One LLM call returns the final
        ordered pick.

        Falls open to cosine top-K if the loader / backend fails.
        """
        if not candidates:
            return AgenticResult(picks=[], calls=0, reads=[], fell_back=False)

        loader = skill_body_loader or _default_skill_body_loader
        by_name = {e.name: e for e in candidates}

        n_bodies = min(self.body_read_n, len(candidates))
        body_entries = candidates[:n_bodies]
        backup_entries = candidates[n_bodies : self.shortlist]

        bodies: list[tuple[str, str]] = []
        reads_done: list[str] = []
        for entry in body_entries:
            body = loader(entry)
            if body:
                bodies.append((entry.name, body))
                reads_done.append(entry.name)

        # If we somehow could not load ANY bodies, fall back to step B so
        # the pipeline still produces a result.
        if not bodies:
            return self._fail_open(candidates, "body-first: no bodies loaded")

        system_prompt = _system_prompt_body_first(
            self.top_k, len(bodies), len(backup_entries)
        )
        user_msg = self._format_body_first_prompt(query, bodies, backup_entries)
        try:
            raw = self.backend.chat(
                system_prompt, user_msg, timeout_s=self.timeout_s
            )
        except LLMBackendError as e:
            return self._fail_open(candidates, str(e))

        parsed = extract_json(
            raw, start=PICK_SENTINEL_START, end=PICK_SENTINEL_END, fallback_key="pick"
        )
        if not parsed:
            return self._fail_open(
                candidates, f"body-first: parse failed: {raw[:120]!r}"
            )
        picks = [n for n in _string_list(parsed.get("pick")) if n in by_name][: self.shortlist]
        if not picks:
            return self._fail_open(candidates, "body-first: empty picks")

        if not _is_quiet():
            print(
                f"[agentic body-first] backend={self.backend.name} "
                f"reads={len(reads_done)} picks={picks}",
                file=sys.stderr,
            )
        return AgenticResult(picks=picks, calls=1, reads=reads_done, fell_back=False)

    # ---- Prompt formatting ---------------------------------------------------

    def _format_pick_prompt(self, query: str, candidates: list["CacheEntry"]) -> str:
        lines = [render_metadata_line(e) for e in candidates]
        return (
            f"USER TASK:\n{query.strip()}\n\n"
            f"CANDIDATE SKILLS (cosine top-{len(candidates)}):\n"
            + "\n".join(lines)
        )

    def _format_body_first_prompt(
        self,
        query: str,
        bodies: list[tuple[str, str]],
        backup_entries: list["CacheEntry"],
    ) -> str:
        parts = [f"USER TASK:\n{query.strip()}", ""]
        parts.append(
            f"TOP {len(bodies)} CANDIDATES (full SKILL.md bodies, "
            "ordered by cosine similarity — strongest match first):"
        )
        parts.append("")
        for name, body in bodies:
            parts.append(f"===== {name} =====")
            if len(body) > 6000:
                body = body[:6000] + "\n...[truncated]..."
            parts.append(body)
            parts.append("")
        if backup_entries:
            parts.append(
                f"BACKUP CANDIDATES ({len(backup_entries)} skills, "
                "metadata one-liners only — pull from these only if a "
                "body-read candidate clearly does not fit):"
            )
            for entry in backup_entries:
                parts.append(render_metadata_line(entry))
        return "\n".join(parts)

    def _format_disambig_prompt(
        self, query: str, prior_picks: list[str], bodies: list[tuple[str, str]]
    ) -> str:
        parts = [f"USER TASK:\n{query.strip()}", ""]
        if prior_picks:
            parts.append("PRIOR PICKS: " + ", ".join(prior_picks))
        parts.append("FULL SKILL.md BODIES BELOW.\n")
        for name, body in bodies:
            parts.append(f"===== {name} =====")
            # Keep bodies bounded — agentic search isn't a code summarizer.
            if len(body) > 6000:
                body = body[:6000] + "\n...[truncated]..."
            parts.append(body)
            parts.append("")
        return "\n".join(parts)

    # ---- Helpers -------------------------------------------------------------

    def _fail_open(
        self, candidates: list["CacheEntry"], error: str
    ) -> AgenticResult:
        """Backend or parser failed — give caller the cosine top-N names."""
        fallback_n = min(DEFAULT_PREPEND_K, len(candidates))
        return AgenticResult(
            picks=[e.name for e in candidates[:fallback_n]],
            calls=0,
            reads=[],
            fell_back=True,
            error=error,
        )


def _string_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [v.strip() for v in value if isinstance(v, str) and v.strip()]


def _default_skill_body_loader(entry: "CacheEntry") -> str | None:
    """Default Step C SKILL.md loader."""
    try:
        skill_md = entry.skill_dir / "SKILL.md"
        if not skill_md.exists():
            return None
        return skill_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


__all__ = [
    "AgenticResult",
    "AgenticSearch",
    "render_metadata_line",
    "PICK_SENTINEL_START",
    "PICK_SENTINEL_END",
    "DEFAULT_TOP",
    "DEFAULT_SHORTLIST",
    "DEFAULT_TOP_K",
    "DEFAULT_MAX_READS",
]
