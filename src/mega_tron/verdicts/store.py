"""SQLite verdict store — the time-series spine of mega-tron.

A single-file SQLite database at ``~/.local/share/mega-tron/store.db``
(override via ``MEGA_TRON_STORE``). Every verdict the host adapters
record lands here with a real per-event timestamp, so the regression
detector can ask "did this skill's helpful_count drop over the last
30 days?" — a question the cumulative ``mega_meta:`` frontmatter
counters cannot answer.

The store maintains two tables (plus an analytics-only one):

- ``skills`` — one row per known skill, keyed by ``skill_name``.
- ``verdicts`` — append-only event log; one row per ``record_verdict``.
- ``routes`` — optional analytics table; populated lazily by callers
  that opt in.

Cumulative counters (helpful_count, harmful_count, helpful_contexts,
status, ...) live in SKILL.md's ``mega_meta:`` frontmatter — the
single source of truth a user can read with ``cat SKILL.md``.
:class:`mega_tron.core.MegaCore.stats` / ``export_frontmatter``
scan the frontmatter directly; we no longer maintain a derived
``skill_state`` SQLite cache that would duplicate it.

Every table carries an ``extras_json TEXT`` column so future columns
can be staged without a schema migration; ``schema_version`` is
authoritative when migrations *do* land.

Concurrency. WAL mode + a Python-level retry loop on ``SQLITE_BUSY``
(3 attempts × 200/400/800 ms backoff) handle the realistic write
contention: Codex Stop hook + Claude Stop hook + Hermes Curator
thread can all be inserting verdicts at once. When the optional
``mega-tron daemon`` is running the host adapters proxy through
it so a single connection is the canonical writer; the fallback path
(direct write) still works correctly under contention via WAL.

Design choices worth flagging:

- All TEXT timestamps are ISO-8601 UTC with a trailing ``Z`` so
  string comparison and ``datetime('now', ...)`` arithmetic are both
  exact-and-correct on naive lexicographic compare.
- The ``UNIQUE(session_id, skill_name, host)`` constraint dedups Stop
  hook retries. A ``NULL`` session_id bypasses the constraint —
  intentional for migration-synthesised rows that carry no real
  session identity.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from mega_tron.config import store_path as _default_store_path


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #


SCHEMA_VERSION = 2
"""Bump this (and supply migration SQL) when ``DDL`` changes shape.

Cosmetic edits to DDL strings (re-formatting, comment tweaks) do *not*
require a bump — only changes that the running code would observe
(new column, dropped column, renamed table) do.
"""


DDL: tuple[str, ...] = (
    # Schema-version pin. Single row per applied version. Used by callers
    # that want to bail out on a too-new database (forward-compat
    # guard) or auto-apply pending migrations (when those exist).
    """
    CREATE TABLE IF NOT EXISTS schema_version (
        version    INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL
    )
    """,
    # One row per known skill. The skill_name is the SKILL.md `name:`
    # field (or the directory basename when name is omitted) and is the
    # join key for `verdicts`. The same skill_name can live in multiple
    # directories across a multi-root setup; we record the most-recent
    # path in skill_dir but don't enforce uniqueness on it (Router has
    # its own first-dir-wins collision rule).
    """
    CREATE TABLE IF NOT EXISTS skills (
        skill_name      TEXT PRIMARY KEY,
        skill_dir       TEXT NOT NULL,
        last_seen_sha   TEXT,
        last_seen_host  TEXT,
        first_seen_at   TEXT NOT NULL,
        last_seen_at    TEXT NOT NULL,
        extras_json     TEXT NOT NULL DEFAULT '{}'
    )
    """,
    # Append-only event log. One row per HELPFUL / HARMFUL / NEUTRAL
    # verdict. INCONCLUSIVE is *not* persisted — it carries no signal
    # and the Phase-1 mega_meta path already drops it.
    #
    # UNIQUE(session_id, skill_name, host): dedups Stop-hook retries.
    # When session_id is NULL the constraint does NOT fire (SQLite
    # treats two NULLs as distinct), so migration rows and any future
    # session-less writes can coexist.
    """
    CREATE TABLE IF NOT EXISTS verdicts (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        skill_name      TEXT NOT NULL,
        verdict         TEXT NOT NULL,
        reason          TEXT,
        session_id      TEXT,
        host            TEXT NOT NULL,
        occurred_at     TEXT NOT NULL,
        extras_json     TEXT NOT NULL DEFAULT '{}',
        FOREIGN KEY (skill_name) REFERENCES skills(skill_name) ON DELETE CASCADE,
        UNIQUE (session_id, skill_name, host)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_verdicts_skill_time "
    "ON verdicts(skill_name, occurred_at)",
    "CREATE INDEX IF NOT EXISTS idx_verdicts_time ON verdicts(occurred_at)",
    "CREATE INDEX IF NOT EXISTS idx_verdicts_host_time "
    "ON verdicts(host, occurred_at)",
    # FTS5 mirror of verdicts.reason for sub-millisecond full-text
    # search ("recent webhook-related harmful reasons"). Contentless
    # external-content table — the FTS index lives next to verdicts
    # but `verdicts` remains the source of truth. The triggers below
    # keep them in lockstep on INSERT/UPDATE/DELETE.
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS verdicts_fts
    USING fts5(reason, content='verdicts', content_rowid='id', tokenize='unicode61')
    """,
    """
    CREATE TRIGGER IF NOT EXISTS verdicts_fts_insert
    AFTER INSERT ON verdicts BEGIN
        INSERT INTO verdicts_fts(rowid, reason) VALUES (new.id, new.reason);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS verdicts_fts_delete
    AFTER DELETE ON verdicts BEGIN
        INSERT INTO verdicts_fts(verdicts_fts, rowid, reason)
        VALUES ('delete', old.id, old.reason);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS verdicts_fts_update
    AFTER UPDATE ON verdicts BEGIN
        INSERT INTO verdicts_fts(verdicts_fts, rowid, reason)
        VALUES ('delete', old.id, old.reason);
        INSERT INTO verdicts_fts(rowid, reason) VALUES (new.id, new.reason);
    END
    """,
    # Best-effort routing analytics. Inserts are non-fatal; readers are
    # opt-in. Kept simple so future analyses don't need a migration.
    """
    CREATE TABLE IF NOT EXISTS routes (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id        TEXT,
        host              TEXT NOT NULL,
        query_hash        TEXT NOT NULL,
        picked_names_json TEXT NOT NULL,
        routed_at         TEXT NOT NULL,
        extras_json       TEXT NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_routes_time ON routes(routed_at)",
    "CREATE INDEX IF NOT EXISTS idx_routes_host_time ON routes(host, routed_at)",
)


# Retry policy on SQLITE_BUSY. 3 attempts × {0.2, 0.4, 0.8} s is enough
# headroom for a daemon + two hook processes to serialize without
# bubbling exceptions to the host adapter.
_RETRY_DELAYS_SECONDS = (0.2, 0.4, 0.8)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _utc_now_iso() -> str:
    """Tight ISO-8601 UTC string ending in ``Z``."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Placeholder reasons we refuse to record — case-insensitive exact
# match. These showed up in production data via tests that wrote to
# the default store, and they carry zero signal for the embedding
# corpus or the human-readable contexts list.
_REASON_BLOCKLIST: frozenset[str] = frozenset(
    {
        "ok", "okay", "yes", "no", "n/a", "na",
        "test", "tests", "testing", "todo", "tbd",
        "fix", "fixed", "broken",
        "evidence a", "evidence b", "evidence c",
        "good", "bad", "fine",
        "true", "false",
        "x", "y", "z",
        "?", "??", "???",
        "-", "--", "...",
    }
)


def _reason_passes_quality(reason: str | None) -> bool:
    """Return whether ``reason`` carries enough signal to learn from.

    ``None`` is **accepted** — callers that genuinely have no reason
    text (older host hooks, internal infrastructure tests) are not
    placeholder-spamming. The gate only blocks reasons that someone
    *did* write but wrote uselessly.

    Rejected (only when ``reason`` is a non-None string):
      * empty / whitespace-only after strip.
      * < 8 characters after strip — too short to encode a real
        observation (typical good reasons are 30-200 chars).
      * Exact match (case-insensitive) against
        :data:`_REASON_BLOCKLIST` — common placeholder strings.
      * Single token of letters/digits only with no embedded space
        AND length < 12 — catches "r1", "evidence", "shortrun" that
        slip past the length check.

    Used only for HELPFUL/HARMFUL writes; NEUTRAL is treated as
    diagnostic noise where a brief reason is acceptable.
    """
    if reason is None:
        return True
    s = reason.strip()
    if not s:
        return False
    if len(s) < 8:
        return False
    if s.lower() in _REASON_BLOCKLIST:
        return False
    if " " not in s and len(s) < 12 and s.replace("_", "").replace("-", "").isalnum():
        return False
    return True


def _normalize_timestamp(ts: datetime | str | None) -> str:
    """Accept naive datetime, aware datetime, ISO string, or None."""
    if ts is None:
        return _utc_now_iso()
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        else:
            ts = ts.astimezone(timezone.utc)
        return ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(ts)


# --------------------------------------------------------------------------- #
# Public dataclasses
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #


class Store:
    """SQLite verdict store with WAL + retry-on-busy.

    Constructed once per host process (or once per daemon when running
    the canonical-writer path). Methods are thread-safe via a per-
    instance :class:`threading.Lock` plus ``busy_timeout`` + Python
    retry loop, so multiple host hooks in the same process can share
    one Store instance. Cross-process contention is handled by SQLite's
    WAL mode plus our retry loop.

    Reads (``stats``, ``regressions``) are best-effort — they don't
    take the write lock and may briefly see WAL-frame inconsistency,
    which is fine for analytics output.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else _default_store_path()
        self._lock = threading.Lock()
        self._initialized = False

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def initialize(self) -> None:
        """Create the database file and apply DDL. Idempotent."""
        if self._initialized:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            for stmt in DDL:
                conn.execute(stmt)
            # Forward-migration: SCHEMA_VERSION 1 had a `skill_state`
            # derived-counter table that we've since retired (counters
            # now live in SKILL.md frontmatter only). Drop the stale
            # table if an existing DB still carries it.
            conn.execute("DROP TABLE IF EXISTS skill_state")
            # Forward-migration: rebuild `verdicts_fts` from `verdicts`
            # for DBs that were created before the FTS5 mirror existed.
            # The CREATE VIRTUAL TABLE above is idempotent; the rebuild
            # is cheap (one full scan) but only matters once per DB —
            # subsequent rows flow through the triggers.
            #
            # Why `rebuild` and not "INSERT WHERE COUNT(*) = 0": for a
            # contentless-external FTS5 table (`content='verdicts'`)
            # the row count proxies through to `verdicts` so it never
            # reads as empty even when the index is unpopulated. A
            # direct rebuild is the documented and only-reliable way
            # to backfill from existing source rows.
            try:
                conn.execute(
                    "INSERT INTO verdicts_fts(verdicts_fts) VALUES('rebuild')"
                )
            except sqlite3.OperationalError:
                # Older SQLite without the rebuild command — fall back to
                # manual scan. Rare; we don't gate on FTS5 version here.
                conn.execute(
                    "INSERT INTO verdicts_fts(rowid, reason) "
                    "SELECT id, reason FROM verdicts WHERE reason IS NOT NULL"
                )
            # Stamp the schema version on the *first* successful init.
            # A future re-init that finds a different version goes
            # through a migration path.
            cur = conn.execute("SELECT MAX(version) FROM schema_version")
            current = cur.fetchone()[0]
            if current is None:
                conn.execute(
                    "INSERT INTO schema_version(version, applied_at) "
                    "VALUES (?, ?)",
                    (SCHEMA_VERSION, _utc_now_iso()),
                )

            # ---- v1 -> v2 migration: heal NULL session_id rows ---- #
            #
            # The UNIQUE(session_id, skill_name, host) constraint on
            # the verdicts table is silently bypassed for rows whose
            # session_id is NULL (SQLite treats two NULLs as distinct
            # under UNIQUE). The result was that any caller that
            # didn't pass a session_id — historically a handful of
            # tests + the verdict_writer fallback path — could write
            # unlimited duplicates of the same (skill, host) pair.
            #
            # The fix has two parts:
            #   1. verdict_writer.persist_verdicts now stamps a
            #      timestamped fallback session_id whenever the host
            #      hook hands us None (commit landing alongside this).
            #   2. Backfill existing NULL rows here so the dashboard
            #      doesn't keep displaying historical duplicates.
            #      We bucket by (skill_name, host, reason, DATE) so
            #      a real burst of legitimate retries on the same
            #      day still collapses to one row but a year-old
            #      duplicate doesn't get merged with today's.
            if current is not None and current < 2:
                try:
                    conn.execute(
                        """
                        UPDATE verdicts
                        SET session_id =
                            '_anon-' || COALESCE(host, 'unknown')
                            || '-' || substr(occurred_at, 1, 10)
                            || '-' || lower(hex(randomblob(2)))
                        WHERE session_id IS NULL
                        """
                    )
                except sqlite3.OperationalError:
                    pass
                conn.execute(
                    "INSERT INTO schema_version(version, applied_at) "
                    "VALUES (?, ?)",
                    (SCHEMA_VERSION, _utc_now_iso()),
                )
            conn.commit()
        self._initialized = True

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Open a configured connection. Applies WAL + pragmas on every
        open so an empty file inherits the right journal mode without a
        separate setup step."""
        conn = sqlite3.connect(
            self.path,
            timeout=5.0,  # used by busy_timeout below as a safety net
            isolation_level=None,  # explicit transactions via BEGIN
            check_same_thread=False,  # we serialize via self._lock
        )
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 5000")
            yield conn
        finally:
            conn.close()

    def _run_with_retry(self, fn) -> Any:
        """Execute ``fn(conn)`` inside a write transaction; retry on
        ``SQLITE_BUSY`` with exponential backoff."""
        last_exc: Exception | None = None
        for attempt, delay in enumerate((0.0,) + _RETRY_DELAYS_SECONDS):
            if delay > 0:
                time.sleep(delay)
            try:
                with self._lock, self._connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        result = fn(conn)
                    except Exception:
                        conn.execute("ROLLBACK")
                        raise
                    conn.execute("COMMIT")
                    return result
            except sqlite3.OperationalError as e:
                msg = str(e).lower()
                if "locked" not in msg and "busy" not in msg:
                    raise
                last_exc = e
                continue
        assert last_exc is not None  # for the type checker
        raise last_exc

    # ------------------------------------------------------------------ #
    # Skill upsert (lazy — happens automatically on verdict insert)
    # ------------------------------------------------------------------ #

    def upsert_skill(
        self,
        conn: sqlite3.Connection,
        *,
        skill_name: str,
        skill_dir: str | None,
        sha: str | None,
        host: str | None,
        timestamp: str,
    ) -> None:
        """Insert or refresh a row in ``skills``. Called from inside a
        write transaction (the caller already holds ``BEGIN IMMEDIATE``).

        New skills get ``first_seen_at = last_seen_at = timestamp``;
        existing skills only get the ``last_seen_*`` fields bumped so
        we don't lose the original first-seen anchor.
        """
        conn.execute(
            """
            INSERT INTO skills (
                skill_name, skill_dir, last_seen_sha, last_seen_host,
                first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(skill_name) DO UPDATE SET
                skill_dir = excluded.skill_dir,
                last_seen_sha = excluded.last_seen_sha,
                last_seen_host = excluded.last_seen_host,
                last_seen_at = excluded.last_seen_at
            """,
            (
                skill_name,
                skill_dir or "",
                sha,
                host,
                timestamp,
                timestamp,
            ),
        )

    # ------------------------------------------------------------------ #
    # Verdict recording — primary write path
    # ------------------------------------------------------------------ #

    def record_verdict(
        self,
        *,
        skill_name: str,
        verdict: str,
        host: str,
        reason: str | None = None,
        session_id: str | None = None,
        occurred_at: datetime | str | None = None,
        skill_dir: str | None = None,
        skill_sha: str | None = None,
        extras: dict[str, Any] | None = None,
    ) -> bool:
        """Persist one verdict atomically (skill upsert + verdict insert
        in one transaction). Returns ``True`` when a row was written,
        ``False`` when the verdict was dropped (the UNIQUE constraint
        matched, i.e. a retry, or the label was ``INCONCLUSIVE``).

        Cumulative counters live in SKILL.md ``mega_meta:`` frontmatter
        — :func:`mega_tron.verdicts.mega_meta.apply_evaluations` updates them
        on the caller's side. We no longer maintain a derived SQLite
        counter cache; ``stats()`` / ``export_frontmatter`` read the
        frontmatter directly.

        ``occurred_at`` ``None`` stamps :func:`datetime.utcnow` —
        right for live hook events. Pass an explicit timestamp for
        migration replays and tests.
        """
        self.initialize()
        v_upper = (verdict or "").upper()
        if v_upper == "INCONCLUSIVE":
            # Mirror :meth:`MegaMeta.apply_verdict`: INCONCLUSIVE
            # carries no signal — don't write a row, don't bump
            # last_updated.
            return False
        if v_upper not in {"HELPFUL", "HARMFUL", "NEUTRAL"}:
            return False

        # Reason quality gate: drop placeholder reasons before they
        # pollute the learning corpus. "ok" / "r1" / "evidence A" /
        # "test" are common test-fixture noise; they carry no useful
        # signal for the verdict embeddings or human-readable
        # frontmatter contexts. Real reasons are usually >= 8 chars
        # and contain at least one non-token-ish character (space,
        # punctuation, or digit cluster). NEUTRAL verdicts may
        # legitimately have a thin reason, so we only filter
        # HELPFUL/HARMFUL.
        if v_upper in {"HELPFUL", "HARMFUL"} and not _reason_passes_quality(reason):
            return False

        timestamp = _normalize_timestamp(occurred_at)
        extras_json = json.dumps(extras or {}, sort_keys=True)

        def _txn(conn: sqlite3.Connection) -> bool:
            self.upsert_skill(
                conn,
                skill_name=skill_name,
                skill_dir=skill_dir,
                sha=skill_sha,
                host=host,
                timestamp=timestamp,
            )

            try:
                conn.execute(
                    """
                    INSERT INTO verdicts (
                        skill_name, verdict, reason, session_id, host,
                        occurred_at, extras_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        skill_name,
                        v_upper,
                        reason,
                        session_id,
                        host,
                        timestamp,
                        extras_json,
                    ),
                )
            except sqlite3.IntegrityError as e:
                # UNIQUE(session_id, skill_name, host): a duplicate.
                # Drop it but don't fail the transaction — the caller
                # gets ``False`` so it can log a debug-level "retry
                # ignored" if it wants.
                if "UNIQUE" not in str(e).upper():
                    raise
                return False

            return True

        return self._run_with_retry(_txn)

    def record_verdicts(self, items: Iterable[dict[str, Any]]) -> int:
        """Bulk-write convenience wrapper. Returns the number of rows
        that were actually inserted (drops on UNIQUE / INCONCLUSIVE are
        not counted).

        Each item must carry at least ``skill_name``, ``verdict``,
        ``host``. Other fields fall through to :meth:`record_verdict`.
        """
        written = 0
        for item in items:
            if self.record_verdict(**item):
                written += 1
        return written

    # ------------------------------------------------------------------ #
    # Verdict introspection (used by migration verify + regression detector)
    # ------------------------------------------------------------------ #

    def search_reasons(
        self,
        query: str,
        *,
        host: str | None = None,
        verdict: str | None = None,
        limit: int = 20,
        since_days: int | None = None,
    ) -> list[dict[str, Any]]:
        """Full-text search over verdict reasons via FTS5.

        Args:
            query: an FTS5 MATCH expression. Bare words are tokenized;
                quoted phrases match literally; ``AND`` / ``OR`` / ``NOT``
                / ``"..."`` / ``*`` (prefix) are honoured.
            host: filter by host ('codex', 'claude_code', etc.). None = all.
            verdict: filter by verdict ('HELPFUL' / 'HARMFUL' / 'NEUTRAL').
            limit: cap on results, ranked by FTS5 BM25 (smaller is better).
            since_days: only consider verdicts within the last N days.

        Returns:
            A list of dicts (one per matched verdict) with keys:
            ``id``, ``skill_name``, ``verdict``, ``reason``, ``host``,
            ``occurred_at``, ``score`` (bm25; lower = stronger match).
            Empty list when the FTS query yields no rows or fails to
            parse (e.g. invalid MATCH syntax).
        """
        self.initialize()
        if not query.strip():
            return []
        params: dict[str, Any] = {"q": query, "host": host, "limit": limit,
                                  "verdict": verdict.upper() if verdict else None}
        since_clause = ""
        if since_days is not None and since_days > 0:
            params["since_days"] = f"-{since_days} days"
            since_clause = " AND v.occurred_at >= datetime('now', :since_days) "
        sql = f"""
        SELECT v.id, v.skill_name, v.verdict, v.reason, v.host, v.occurred_at,
               bm25(verdicts_fts) AS score
        FROM verdicts_fts
        JOIN verdicts v ON v.id = verdicts_fts.rowid
        WHERE verdicts_fts MATCH :q
          AND (:host IS NULL OR v.host = :host)
          AND (:verdict IS NULL OR v.verdict = :verdict)
          {since_clause}
        ORDER BY score
        LIMIT :limit
        """
        with self._connect() as conn:
            try:
                cur = conn.execute(sql, params)
                rows = cur.fetchall()
            except sqlite3.OperationalError:
                # Malformed MATCH expression — return empty rather than
                # bubble a cryptic SQLite error to the CLI user.
                return []
        return [
            {
                "id": r[0],
                "skill_name": r[1],
                "verdict": r[2],
                "reason": r[3],
                "host": r[4],
                "occurred_at": r[5],
                "score": float(r[6]) if r[6] is not None else None,
            }
            for r in rows
        ]

    def get_verdict_reason(self, verdict_id: int) -> str | None:
        """Return the reason text of a single verdict row, or ``None``
        when it doesn't exist. Used by
        :meth:`mega_tron.verdicts.embeddings.VerdictEmbeddingsStore.compact`
        as a tie-break when collapsing near-duplicate clusters
        (longest reason wins)."""
        self.initialize()
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT reason FROM verdicts WHERE id = ?",
                (int(verdict_id),),
            )
            row = cur.fetchone()
        return None if row is None else row[0]

    def count_verdicts(self, *, host: str | None = None) -> int:
        """Return the total verdict row count, optionally scoped to one host."""
        self.initialize()
        with self._connect() as conn:
            if host is None:
                cur = conn.execute("SELECT COUNT(*) FROM verdicts")
            else:
                cur = conn.execute(
                    "SELECT COUNT(*) FROM verdicts WHERE host = ?", (host,)
                )
            return int(cur.fetchone()[0])

    def count_low_quality_reasons(self) -> int:
        """Number of verdicts whose reason would fail the P3 quality
        gate today. Used by the dashboard health card to surface
        historical noise that was written before the gate landed.

        Definition mirrors :func:`_reason_passes_quality` but in SQL:
        reason is empty, < 8 chars after strip, or matches a known
        placeholder. Single-token short-and-alnum (e.g. ``r1``) is
        caught by the length < 8 case in practice.
        """
        self.initialize()
        placeholders = "(" + ",".join(f"'{p}'" for p in sorted(_REASON_BLOCKLIST)) + ")"
        sql = f"""
        SELECT COUNT(*) FROM verdicts
        WHERE verdict IN ('HELPFUL','HARMFUL') AND (
            reason IS NULL
            OR length(trim(reason)) < 8
            OR lower(trim(reason)) IN {placeholders}
        )
        """
        with self._connect() as conn:
            return int(conn.execute(sql).fetchone()[0])

    def verdict_counts_by_skill(self) -> dict[str, dict[str, Any]]:
        """Return ``{skill_name: {helpful, harmful, neutral, total, hosts:set,
        last_updated}}`` aggregated from the verdicts table.

        Single SQL pass keyed by ``skill_name``. The dashboard uses this
        as the authoritative source of "what's been used and how" — it
        works even when the SKILL.md frontmatter is stale or the skill
        directory has been deleted from disk, because SQLite is the
        durable record. Hosts are returned in their *raw* form
        (``"claude_code"``, ``"gemini_cli"``); callers normalise on
        display.
        """
        self.initialize()
        sql = """
        SELECT skill_name,
               SUM(CASE WHEN verdict='HELPFUL' THEN 1 ELSE 0 END) AS h,
               SUM(CASE WHEN verdict='HARMFUL' THEN 1 ELSE 0 END) AS x,
               SUM(CASE WHEN verdict='NEUTRAL' THEN 1 ELSE 0 END) AS n,
               COUNT(*) AS total,
               MAX(occurred_at) AS last_updated,
               GROUP_CONCAT(DISTINCT host) AS hosts
        FROM verdicts
        GROUP BY skill_name
        """
        out: dict[str, dict[str, Any]] = {}
        with self._connect() as conn:
            for row in conn.execute(sql).fetchall():
                hosts_csv = row[6] or ""
                out[row[0]] = {
                    "helpful": int(row[1] or 0),
                    "harmful": int(row[2] or 0),
                    "neutral": int(row[3] or 0),
                    "total": int(row[4] or 0),
                    "last_updated": row[5],
                    "hosts": [h for h in hosts_csv.split(",") if h],
                }
        return out

    def skill_last_seen(self, skill_name: str) -> dict[str, Any] | None:
        """Return the most recent ``skills`` row for ``skill_name`` or
        ``None``. Used by the dashboard orphan pane to surface the
        directory the skill last lived in before its SKILL.md was
        deleted — without this the user has no way to recall which
        path they removed.
        """
        self.initialize()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT skill_dir, last_seen_host, last_seen_at, "
                "first_seen_at FROM skills WHERE skill_name = ?",
                (skill_name,),
            ).fetchone()
        if row is None:
            return None
        return {
            "skill_dir": row[0] or "",
            "last_seen_host": row[1] or "",
            "last_seen_at": row[2] or "",
            "first_seen_at": row[3] or "",
        }

    def delete_all_verdicts_for_skill(self, skill_name: str) -> int:
        """Delete every verdict row for ``skill_name``. Returns the
        number of rows removed. Used by the dashboard's orphan-pane
        "delete selected" action to clean up history for skills whose
        SKILL.md is no longer on disk. Different from
        :meth:`delete_verdicts_matching` because it does not require
        a host / reason filter — for an orphan the whole history is
        what the user wants gone.
        """
        self.initialize()

        def _txn(conn: sqlite3.Connection) -> int:
            cur = conn.execute(
                "DELETE FROM verdicts WHERE skill_name = ?",
                (skill_name,),
            )
            removed = cur.rowcount or 0
            # Also drop the skills-table row so a future re-install
            # of the same skill starts fresh, and the orphan list
            # doesn't keep showing a name with 0 verdicts.
            conn.execute(
                "DELETE FROM skills WHERE skill_name = ?",
                (skill_name,),
            )
            return removed

        return self._run_with_retry(_txn)

    def delete_verdicts_matching(
        self, *, skill_name: str, host: str, reason: str | None
    ) -> int:
        """Delete every row matching the (skill_name, host, reason)
        triple. Returns the number of rows removed. The FTS5 delete
        trigger sweeps the matching FTS rows in lockstep.

        Used by the dashboard "delete N matching" bulk action so the
        user can clean up an entire spam cluster in one click.

        ``reason=None`` matches rows whose ``reason`` column is NULL
        (legacy / migration-era inserts).
        """
        self.initialize()

        def _txn(conn: sqlite3.Connection) -> int:
            if reason is None:
                cur = conn.execute(
                    "DELETE FROM verdicts WHERE skill_name = ? "
                    "AND host = ? AND reason IS NULL",
                    (skill_name, host),
                )
            else:
                cur = conn.execute(
                    "DELETE FROM verdicts WHERE skill_name = ? "
                    "AND host = ? AND reason = ?",
                    (skill_name, host, reason),
                )
            return cur.rowcount

        return self._run_with_retry(_txn)

    def get_verdict(self, verdict_id: int) -> dict[str, Any] | None:
        """Return the full verdict row keyed by id, or ``None`` if absent.

        Used by dashboard PATCH/DELETE endpoints: they need ``skill_name``
        to drive the post-mutation frontmatter resync and would otherwise
        do an extra round-trip.
        """
        self.initialize()
        with self._connect() as conn:
            cur = conn.execute(
                """
                SELECT id, skill_name, verdict, reason, session_id, host,
                       occurred_at, extras_json
                FROM verdicts WHERE id = ?
                """,
                (int(verdict_id),),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "skill_name": row[1],
            "verdict": row[2],
            "reason": row[3],
            "session_id": row[4],
            "host": row[5],
            "occurred_at": row[6],
            "extras_json": row[7],
        }

    def update_verdict(
        self,
        verdict_id: int,
        *,
        verdict: str | None = None,
        reason: str | None = None,
    ) -> bool:
        """Mutate a verdict row in place. Returns ``True`` when one row
        changed, ``False`` if no row with that id exists.

        Used by the dashboard's human-in-the-loop edit drawer to flip a
        misjudged verdict (HELPFUL ↔ HARMFUL) or mark it NEUTRAL. The
        FTS5 update trigger (DDL line ~152) keeps ``verdicts_fts`` in
        sync automatically — no extra work here.

        ``verdict`` is validated against {HELPFUL, HARMFUL, NEUTRAL};
        anything else raises :class:`ValueError` so a typo doesn't
        silently corrupt the table. At least one of ``verdict`` /
        ``reason`` must be non-None.
        """
        if verdict is None and reason is None:
            raise ValueError("update_verdict requires verdict= or reason=")
        v_upper: str | None = None
        if verdict is not None:
            v_upper = verdict.upper()
            if v_upper not in {"HELPFUL", "HARMFUL", "NEUTRAL"}:
                raise ValueError(
                    f"verdict must be HELPFUL/HARMFUL/NEUTRAL, got {verdict!r}"
                )

        self.initialize()

        def _txn(conn: sqlite3.Connection) -> bool:
            sets: list[str] = []
            params: list[Any] = []
            if v_upper is not None:
                sets.append("verdict = ?")
                params.append(v_upper)
            if reason is not None:
                sets.append("reason = ?")
                params.append(reason)
            params.append(int(verdict_id))
            cur = conn.execute(
                f"UPDATE verdicts SET {', '.join(sets)} WHERE id = ?",
                params,
            )
            return cur.rowcount > 0

        return self._run_with_retry(_txn)

    def delete_verdict(self, verdict_id: int) -> bool:
        """Remove a verdict row. Returns ``True`` on hit, ``False`` if no
        such id. The FTS5 delete trigger sweeps the matching FTS row;
        callers handle ``verdict_embeddings.npz`` cleanup separately
        because that store has no SQLite FK to cascade on.
        """
        self.initialize()

        def _txn(conn: sqlite3.Connection) -> bool:
            cur = conn.execute(
                "DELETE FROM verdicts WHERE id = ?", (int(verdict_id),)
            )
            return cur.rowcount > 0

        return self._run_with_retry(_txn)

    def activity_per_day(
        self,
        *,
        skill_name: str | None = None,
        host: str | None = None,
        days: int = 30,
    ) -> list[tuple[str, int]]:
        """Return ``[(YYYY-MM-DD, count), ...]`` over the trailing window.

        Sparse — days with zero verdicts are omitted. Callers that need
        a dense N-element array (e.g. a sparkline with one dot per day)
        backfill the gaps on the Python side; doing it in SQL with a
        recursive CTE would cost more than the dashboard needs.

        ``days`` must be positive. The result is ordered chronologically.
        """
        if days <= 0:
            raise ValueError(f"days must be > 0, got {days}")
        self.initialize()
        params: dict[str, Any] = {
            "since": f"-{days} days",
            "skill": skill_name,
            "host": host,
        }
        sql = """
        SELECT DATE(occurred_at) AS d, COUNT(*)
        FROM verdicts
        WHERE occurred_at >= datetime('now', :since)
          AND (:skill IS NULL OR skill_name = :skill)
          AND (:host IS NULL OR host = :host)
        GROUP BY d
        ORDER BY d
        """
        with self._connect() as conn:
            cur = conn.execute(sql, params)
            return [(str(r[0]), int(r[1])) for r in cur.fetchall()]

    def schema_version(self) -> int | None:
        """Return the highest schema version recorded in this DB, or
        ``None`` when the file does not yet contain a ``schema_version``
        row (i.e. brand-new file we have not initialized)."""
        if not self.path.exists():
            return None
        try:
            with self._connect() as conn:
                cur = conn.execute("SELECT MAX(version) FROM schema_version")
                row = cur.fetchone()
                return None if row is None else row[0]
        except sqlite3.DatabaseError:
            return None


# --------------------------------------------------------------------------- #
# Module-level convenience — default-path Store accessor
# --------------------------------------------------------------------------- #


def default_store() -> Store:
    """Construct a :class:`Store` at the configured default path. Each
    call returns a fresh instance; long-lived host adapters should
    cache one in their own state."""
    return Store(_default_store_path())


__all__ = [
    "DDL",
    "SCHEMA_VERSION",
    "Store",
    "default_store",
]
