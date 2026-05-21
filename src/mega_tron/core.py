"""MegaCore — host-agnostic library facade for mega-tron.

This module is the **only** import a host adapter needs. Everything
Codex/Claude/Hermes-specific composes around it. The public surface is
intentionally small:

- :func:`route` — rank skills for a query (delegates to :class:`Router`).
- :func:`record_verdict` / :func:`record_verdicts` — persist a HELPFUL /
  HARMFUL / NEUTRAL / INCONCLUSIVE judgement against a skill.
- :func:`regressions` — list skills whose helpful/harmful trend recently
  flipped. *Stub in v1.1; powered by the SQLite store in v1.3.*
- :func:`stats` — per-skill counters. *Frontmatter-backed in v1.1;
  SQLite-backed in v1.3.*
- :func:`warmup` / :func:`warmup_if_stale` — embedding cache lifecycle.

Phase 1 (v1.1) delivers the *shape* of the facade so Hermes (or any
future host) can already write::

    from mega_tron import MegaCore, Verdict
    core = MegaCore(skills_dirs=[...])
    ranked = core.route("validate webhook signature", top_k=5)
    core.record_verdict(Verdict(
        skill_name="webhook-signer",
        verdict="HELPFUL",
        host="hermes",
        reason="diff +12 -3 in src/auth/, test passed",
        session_id="0193-...",
    ))

Under the hood today, ``record_verdict`` still writes through
:func:`mega_tron.verdicts.mega_meta.apply_evaluations` — the existing
frontmatter mutation. Phase 2 (v1.2) will introduce the SQLite store and
upgrade ``record_verdict`` to dual-write, then Phase 3 (v1.3) wires
:func:`regressions` to the time-series. The facade's signature stays
constant across those phases.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Literal

if TYPE_CHECKING:
    from mega_tron.agentic import AgenticSearch
    from mega_tron.cache import Cache
    from mega_tron.config import Config
    from mega_tron.embedder import Embedder
    from mega_tron.verdicts.mega_meta import EvaluationOutcome
    from mega_tron.router import RankedSkill
    from mega_tron.verdicts.store import Store


HostName = Literal["codex", "claude_code", "hermes", "other"]
VerdictLabel = Literal["HELPFUL", "HARMFUL", "NEUTRAL", "INCONCLUSIVE"]
RegressionClass = Literal["regressed", "broken", "unused", "stable"]


@dataclass(frozen=True)
class Verdict:
    """One per-skill judgement emitted at the end of a session.

    Wire format that host adapters pass to :meth:`MegaCore.record_verdict`.
    Whatever the host's transcript schema looks like — Codex JSONL,
    Claude Code's transcript-tail, Hermes's tool-call audit — adapters
    convert it into one or more :class:`Verdict` objects.

    Attributes:
        skill_name: the SKILL.md ``name:`` field (or directory basename
            when ``name:`` is absent). Must match an existing skill.
        verdict: one of ``HELPFUL`` (skill clearly contributed),
            ``HARMFUL`` (skill led the model astray), ``NEUTRAL`` (ran
            but did not move the needle), ``INCONCLUSIVE`` (no evidence
            either way; no-op).
        host: which host runtime produced this verdict. Required so the
            shared store can answer "show me only Hermes-side verdicts".
        reason: short natural-language evidence citation, max ~150 chars.
            Stored as a context string and embedded for task-specific
            ranker bonuses. ``None`` is allowed but discouraged — the
            evidence is what makes the verdict actionable.
        session_id: opaque session identifier from the host. Used to
            dedup retries via the SQLite ``UNIQUE(session_id,
            skill_name, host)`` constraint (Phase 2+).
        occurred_at: when the verdict was decided. ``None`` lets the
            store stamp ``datetime.utcnow()`` at insert time, which is
            the right default for live hook events. Migrations and
            replays should pass an explicit timestamp.
        extras: free-form key/value bag persisted into the store's
            ``extras_json`` column (Phase 2+). Use for host-specific
            metadata that doesn't fit the schema yet.
    """

    skill_name: str
    verdict: VerdictLabel
    host: HostName
    reason: str | None = None
    session_id: str | None = None
    occurred_at: datetime | None = None
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Regression:
    """One row of :meth:`MegaCore.regressions` output.

    Surfaces skills whose helpful/harmful trend recently flipped — the
    signal Hermes's own Curator cannot produce because it has no per-
    verdict time series. Phase 1 only ships the dataclass; Phase 3
    (v1.3) populates it from the SQLite store.

    Classifications (first match wins, ordered by severity):

    - ``broken``     — was working (helpful_baseline >= min_invocations)
                       and now mostly harmful (harm-ratio >= 0.5).
    - ``regressed``  — was working, now zero helpful and at least one
                       harmful, OR sharp helpful drop with no harm.
    - ``unused``     — too few recent invocations to judge (false-
                       positive guard).
    - ``stable``     — no actionable trend.

    Default :meth:`MegaCore.regressions` emits only ``broken`` and
    ``regressed``.
    """

    skill_name: str
    classification: RegressionClass
    helpful_recent: int
    helpful_baseline: int
    harmful_recent: int
    harmful_baseline: int
    window_days: int
    last_helpful_at: datetime | None
    last_harmful_at: datetime | None
    detail: str


@dataclass(frozen=True)
class StatRow:
    """One row of :meth:`MegaCore.stats` output."""

    skill_name: str
    helpful_count: int
    harmful_count: int
    status: str
    last_updated: str | None
    host: str | None = None  # populated when by_host=True


class MegaCore:
    """Host-agnostic facade over the mega-tron router + verdict pipeline.

    Constructed once per host process. Routing delegates to the existing
    :class:`Router`; verdict recording delegates to
    :func:`mega_tron.verdicts.mega_meta.apply_evaluations` in Phase 1 and
    additionally writes the SQLite store in Phase 2+.

    Args:
        skills_dirs: list of directories to scan for ``SKILL.md`` files.
            ``None`` resolves via :func:`config.discover_skill_dirs`.
        embedder: optional pre-built :class:`Embedder`. ``None`` builds
            via :func:`embedder.make_embedder` using the configured
            model (default ``ThakiCloud/SKILLRET-Embedding-0.6B``).
        cache: optional pre-built :class:`Cache`. ``None`` builds the
            default cache path (``~/.cache/mega-tron/<model>.npz``).
        store: SQLite verdict store. ``None`` auto-discovers the
            default-path store (:func:`config.store_path`) and uses it
            iff the file already exists — so a fresh install keeps
            Phase-1 frontmatter-only behaviour and a migrated install
            silently picks up dual-write. Pass an explicit
            :class:`mega_tron.verdicts.store.Store` to force-enable.
        config: optional :class:`Config` overriding env-var defaults.
            ``None`` reads the user config file lazily as needed.
    """

    def __init__(
        self,
        skills_dirs: list[Path] | None = None,
        *,
        embedder: "Embedder | None" = None,
        cache: "Cache | None" = None,
        store: "Store | None" = None,
        config: "Config | None" = None,
    ) -> None:
        from mega_tron.cache import Cache as _Cache
        from mega_tron.config import Config as _Config
        from mega_tron.config import discover_skill_dirs, store_path
        from mega_tron.embedder import fingerprint_of, make_embedder
        from mega_tron.router import Router

        self._config = config or _Config.load()
        if skills_dirs is None:
            self._skills_dirs = [
                Path(p) for p in discover_skill_dirs(config=self._config)
            ]
        else:
            self._skills_dirs = [Path(p) for p in skills_dirs]
        self._embedder = embedder if embedder is not None else make_embedder(
            self._config.embedder_model
        )
        if cache is not None:
            self._cache = cache
        else:
            fp = fingerprint_of(self._embedder)
            cache_path = Path.home() / ".cache" / "mega-tron" / f"{fp}.npz"
            self._cache = _Cache(path=cache_path)
        # Store auto-discovery: when no explicit Store was passed, look
        # at the default path. Use it iff the file already exists — that
        # way Phase-1 users (no migration yet, no store file on disk)
        # keep frontmatter-only writes, and Phase-2-migrated users
        # automatically get dual-write the moment they construct a
        # MegaCore. Tests can force-disable by passing a Store with a
        # path under a tmp dir.
        if store is not None:
            self._store = store
        else:
            default_path = store_path()
            if default_path.exists():
                from mega_tron.verdicts.store import Store as _Store

                self._store = _Store(default_path)
            else:
                self._store = None
        self._router = Router(
            skills_dirs=self._skills_dirs,
            embedder=self._embedder,
            cache=self._cache,
        )

    # ------------------------------------------------------------------ #
    # Routing
    # ------------------------------------------------------------------ #

    def route(
        self,
        query: str,
        *,
        top_k: int = 5,
        prefilter: int | None = None,
        agentic: "AgenticSearch | None" = None,
    ) -> "list[RankedSkill]":
        """Rank skills for ``query`` and return the top-K.

        Pure delegation to :meth:`Router.rank`. Identical semantics —
        this exists so host adapters import :class:`MegaCore` only and
        never touch :class:`Router` directly, keeping the package's
        public surface narrow.
        """
        return self._router.rank(
            query, top_k=top_k, prefilter=prefilter, agentic=agentic
        )

    def warmup(self) -> tuple[int, int, list]:
        """Force a full embedding cache warmup. See :meth:`Router.warmup`."""
        return self._router.warmup()

    def warmup_if_stale(self) -> tuple[int, int, list] | None:
        """Re-warm only when a ``SKILL.md`` has changed since cache mtime."""
        return self._router.warmup_if_stale()

    # ------------------------------------------------------------------ #
    # Verdicts
    # ------------------------------------------------------------------ #

    def record_verdict(self, v: Verdict) -> None:
        """Persist a single verdict.

        Writes to *both* the SQLite store (when present) and the
        SKILL.md ``mega_meta:`` frontmatter block. Frontmatter stays
        canonical for legacy readers until v1.5 retires dual-write —
        from Phase 2 onward SQLite is the time-series source of truth
        and frontmatter is the derived view.
        """
        self.record_verdicts([v])

    def record_verdicts(self, vs: Iterable[Verdict]) -> "EvaluationOutcome":
        """Persist a batch of verdicts. Atomic per-skill.

        Dual-write semantics in v1.2+:

        - When :attr:`_store` is non-``None`` (the configured store path
          exists), each verdict is first inserted into the SQLite
          ``verdicts`` table with its full ``host`` / ``occurred_at`` /
          ``extras`` metadata. The store handles UNIQUE-based retry
          dedup internally.
        - The frontmatter ``mega_meta:`` block is then refreshed via
          :func:`apply_evaluations` — this is where cumulative counters
          (helpful_count / harmful_count / status / contexts) live.

        When :attr:`_store` is ``None`` (fresh install, no migration
        yet) only the frontmatter write fires — time-series analysis
        is skipped but the cumulative counters still update.
        """
        from mega_tron.verdicts.mega_meta import apply_evaluations

        items = list(vs)
        if not items:
            from mega_tron.verdicts.mega_meta import EvaluationOutcome

            return EvaluationOutcome(
                updated=0,
                skipped_inconclusive=0,
                skipped_missing=0,
                skipped_invalid=0,
                errors=[],
                applied=[],
            )

        # ---- SQLite write (Phase 2+, conditional on store presence) ----
        if self._store is not None:
            for v in items:
                # Resolve the skill_dir for migration / first-seen
                # provenance. Best-effort: if the skill isn't found in
                # any registered root the store still records the
                # verdict (skill_dir defaults to empty in the upsert).
                skill_dir = self._resolve_skill_dir(v.skill_name)
                self._store.record_verdict(
                    skill_name=v.skill_name,
                    verdict=v.verdict,
                    host=v.host,
                    reason=v.reason,
                    session_id=v.session_id,
                    occurred_at=v.occurred_at,
                    skill_dir=str(skill_dir) if skill_dir else None,
                    extras=v.extras or None,
                )

        # ---- Frontmatter write (legacy / derived-view path) ----
        # The legacy mutation path takes a list of dicts plus a single
        # session_id. We honour each verdict's session_id when present;
        # the common case is a single Stop hook batch sharing one
        # session, so we pull the first non-None.
        session_id = next(
            (v.session_id for v in items if v.session_id), None
        )
        evaluations = [
            {"skill": v.skill_name, "verdict": v.verdict, "reason": v.reason}
            for v in items
        ]
        # Skills can live across multiple roots (Codex + Claude + custom);
        # apply_evaluations currently expects one root. Use the first
        # registered dir as the canonical write target — the same
        # convention :class:`Router.load_skills` already uses ("first
        # dir wins" on name collisions).
        target_dir = (
            self._skills_dirs[0]
            if self._skills_dirs
            else Path.home() / ".codex" / "skills"
        )
        return apply_evaluations(
            skills_dir=target_dir,
            evaluations=evaluations,
            session_id=session_id,
        )

    # ------------------------------------------------------------------ #
    # Analytics (stubs in Phase 1; powered by the store in Phase 3)
    # ------------------------------------------------------------------ #

    def regressions(
        self,
        *,
        window_days: int = 30,
        min_invocations: int = 5,
        host: str | None = None,
        include_all: bool = False,
    ) -> list[Regression]:
        """List skills whose helpful/harmful trend recently flipped.

        Delegates to :func:`mega_tron.verdicts.regressions.compute` against
        the configured store. Returns ``[]`` when no store is wired
        (pre-migration install), keeping the call safe to make from
        any host adapter regardless of migration state.

        Default output emits only actionable classes (``broken``,
        ``regressed``). Pass ``include_all=True`` to also see
        ``unused`` and ``stable`` rows for debugging.
        """
        if self._store is None:
            return []
        from mega_tron.verdicts.regressions import compute

        return compute(
            self._store,
            window_days=window_days,
            min_invocations=min_invocations,
            host=host,
            include_all=include_all,
        )

    def stats(
        self,
        *,
        by_host: bool = False,
        top: int | None = None,
    ) -> list[StatRow]:
        """Per-skill counter snapshot.

        Cumulative counters (helpful / harmful / status / last_updated)
        come from SKILL.md ``mega_meta:`` frontmatter — the single source
        of truth. ``by_host=True`` additionally pivots the SQLite
        ``verdicts`` table to emit one row per (skill, host) pair so the
        same view also surfaces per-runtime activity; the per-host
        counts are time-series-true counts, while the skill's overall
        ``status`` / ``last_updated`` still come from frontmatter.
        """
        from mega_tron.verdicts.mega_meta import read_meta

        # Frontmatter scan — first-dir-wins on name collision (matches
        # Router.load_skills convention).
        metas: dict[str, "MegaMeta"] = {}
        for root in self._skills_dirs:
            if not root.exists():
                continue
            for entry in sorted(root.iterdir()):
                if not entry.is_dir():
                    continue
                skill_md = entry / "SKILL.md"
                if not skill_md.exists():
                    continue
                if entry.name in metas:
                    continue
                try:
                    metas[entry.name] = read_meta(skill_md)
                except Exception:
                    continue

        if not by_host or self._store is None:
            rows = [
                StatRow(
                    skill_name=name,
                    helpful_count=m.helpful_count,
                    harmful_count=m.harmful_count,
                    status=m.status,
                    last_updated=m.last_updated,
                    host=None,
                )
                for name, m in metas.items()
            ]
            if top is not None:
                rows.sort(
                    key=lambda r: (r.helpful_count - r.harmful_count),
                    reverse=True,
                )
                rows = rows[:top]
            return rows

        # by_host=True with a configured store: pivot per (skill, host).
        # One SQL pass over `verdicts`. Status / last_updated come from
        # frontmatter (which is per-skill, not per-host) so the host
        # rows of the same skill share the same status.
        with self._store._connect() as conn:
            cur = conn.execute(
                """
                SELECT skill_name, host,
                       SUM(CASE WHEN verdict='HELPFUL' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN verdict='HARMFUL' THEN 1 ELSE 0 END),
                       MAX(occurred_at)
                FROM verdicts
                GROUP BY skill_name, host
                ORDER BY skill_name, host
                """
            )
            host_rows = cur.fetchall()
        rows = [
            StatRow(
                skill_name=name,
                helpful_count=int(h or 0),
                harmful_count=int(x or 0),
                status=metas[name].status if name in metas else "active",
                last_updated=last,
                host=host,
            )
            for name, host, h, x, last in host_rows
        ]
        if top is not None:
            rows.sort(
                key=lambda r: (r.helpful_count - r.harmful_count),
                reverse=True,
            )
            rows = rows[:top]
        return rows

    def _resolve_skill_dir(self, skill_name: str) -> Path | None:
        """Find the directory containing ``<skill_name>/SKILL.md`` across
        the registered roots. First-dir-wins matches Router's collision
        rule. Returns ``None`` when the skill isn't on disk anywhere —
        the store still accepts the verdict (skill_dir defaults to '')
        so a Hermes-side verdict for a skill that lives only in
        ``~/.hermes/skills/`` is recorded even when this MegaCore wasn't
        constructed with that root."""
        for root in self._skills_dirs:
            candidate = root / skill_name
            if (candidate / "SKILL.md").exists():
                return candidate
        return None

    # ------------------------------------------------------------------ #
    # Compat / introspection
    # ------------------------------------------------------------------ #

    def export_frontmatter(
        self, *, skills_dirs: list[Path] | None = None
    ) -> int:
        """No-op since the SQLite ``skill_state`` derived cache was
        retired. Cumulative counters now live in SKILL.md ``mega_meta:``
        frontmatter as their single source of truth — host Stop hooks
        write them directly via :func:`mega_tron.verdicts.mega_meta.apply_evaluations`.

        Returns ``0`` always. Kept on the public surface as a back-compat
        shim so external tooling that called it doesn't crash.
        """
        del skills_dirs
        return 0

    @property
    def skills_dirs(self) -> list[Path]:
        """The list of skill roots this core was constructed with."""
        return list(self._skills_dirs)

    @property
    def store(self) -> "Store | None":
        """The configured SQLite :class:`Store`, or ``None`` on a
        pre-migration install. Host adapters that need direct query
        access (Hermes hints emitter, ``mega-tron regressions`` CLI)
        read this; do not mutate it through this property."""
        return self._store

    @classmethod
    def from_config(cls, config: "Config | None" = None) -> "MegaCore":
        """Construct a :class:`MegaCore` using only user-config defaults."""
        return cls(config=config)


__all__ = [
    "HostName",
    "MegaCore",
    "Regression",
    "RegressionClass",
    "StatRow",
    "Verdict",
    "VerdictLabel",
]
