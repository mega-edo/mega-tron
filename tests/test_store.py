"""SQLite Store — DDL, pragmas, retry loop, UNIQUE dedup, FK cascade.

The store is the time-series spine. These tests pin its essential
invariants:

- Schema initialises idempotently and stamps a version row.
- WAL mode and ``foreign_keys=ON`` are active on every connection.
- ``record_verdict`` writes one ``verdicts`` row atomically.
- The ``UNIQUE(session_id, skill_name, host)`` constraint dedups
  retried Stop hook events (and a ``NULL`` session_id bypasses it).
- Labels outside HELPFUL/HARMFUL/NEUTRAL are never persisted.
- ``ON DELETE CASCADE`` removes child rows when a skill is deleted.
- A stale ``skill_state`` table from an older schema_version=1 install
  is dropped on initialize (forward migration).

Cumulative counters (helpful_count, harmful_count, status, contexts)
no longer live in SQLite — they live in SKILL.md ``mega_meta:``
frontmatter as the single source of truth. The store is purely the
per-event time-series.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mega_tron.verdicts.store import SCHEMA_VERSION, Store


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def store(tmp_path: Path) -> Store:
    """Fresh isolated store per test — no XDG / env pollution."""
    s = Store(path=tmp_path / "test_store.db")
    s.initialize()
    return s


# --------------------------------------------------------------------------- #
# DDL / pragmas / schema version
# --------------------------------------------------------------------------- #


def test_initialize_creates_file_and_tables(tmp_path: Path):
    s = Store(path=tmp_path / "store.db")
    assert s.schema_version() is None  # no file yet
    s.initialize()
    assert s.path.exists()
    assert s.schema_version() == SCHEMA_VERSION


def test_initialize_is_idempotent(store: Store):
    # Second initialise is a no-op — no exception, no duplicate version row.
    store.initialize()
    with store._connect() as conn:
        cur = conn.execute("SELECT COUNT(*) FROM schema_version")
        assert cur.fetchone()[0] == 1


def test_wal_and_foreign_keys_pragmas_active(store: Store):
    with store._connect() as conn:
        cur = conn.execute("PRAGMA journal_mode")
        assert cur.fetchone()[0].lower() == "wal"
        cur = conn.execute("PRAGMA foreign_keys")
        assert cur.fetchone()[0] == 1


def test_skill_state_table_is_not_present(store: Store):
    """The derived `skill_state` counter cache was retired — verify
    it's not in the DDL output."""
    with store._connect() as conn:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        names = {r[0] for r in cur.fetchall()}
    assert "skill_state" not in names
    # The remaining real tables are still there.
    assert {"skills", "verdicts", "routes", "schema_version"}.issubset(names)


def test_initialize_drops_stale_skill_state_table(tmp_path: Path):
    """Forward-migrate an old DB that still carries the retired
    `skill_state` table. The next `initialize()` call must drop it."""
    db = tmp_path / "stale.db"
    # Pre-create with the retired schema.
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            """CREATE TABLE skills (
                skill_name TEXT PRIMARY KEY,
                skill_dir TEXT NOT NULL,
                last_seen_sha TEXT,
                last_seen_host TEXT,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                extras_json TEXT NOT NULL DEFAULT '{}'
            )"""
        )
        conn.execute(
            """CREATE TABLE skill_state (
                skill_name TEXT PRIMARY KEY,
                helpful_count INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (skill_name) REFERENCES skills(skill_name)
                    ON DELETE CASCADE
            )"""
        )
        conn.commit()

    Store(path=db).initialize()
    with sqlite3.connect(db) as conn:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        names = {r[0] for r in cur.fetchall()}
    assert "skill_state" not in names


# --------------------------------------------------------------------------- #
# Verdict recording
# --------------------------------------------------------------------------- #


def test_record_verdict_writes_row(store: Store):
    written = store.record_verdict(
        skill_name="webhook-signer",
        verdict="HELPFUL",
        host="codex",
        reason="diff +12 -3, integration test passed",
        session_id="sess-1",
        skill_dir="/tmp/skills/webhook-signer",
    )
    assert written is True
    assert store.count_verdicts() == 1
    # Sanity-check the row content.
    with store._connect() as conn:
        row = conn.execute(
            "SELECT skill_name, verdict, host, session_id, reason "
            "FROM verdicts"
        ).fetchone()
    assert row == (
        "webhook-signer",
        "HELPFUL",
        "codex",
        "sess-1",
        "diff +12 -3, integration test passed",
    )


def test_record_verdict_dedups_on_retry(store: Store):
    """UNIQUE(session_id, skill_name, host) blocks retried inserts."""
    assert (
        store.record_verdict(
            skill_name="x",
            verdict="HELPFUL",
            host="codex",
            session_id="s1",
            skill_dir="/x",
        )
        is True
    )
    # Same (session_id, skill, host) — retry.
    assert (
        store.record_verdict(
            skill_name="x",
            verdict="HELPFUL",
            host="codex",
            session_id="s1",
            skill_dir="/x",
        )
        is False
    )
    assert store.count_verdicts() == 1


def test_null_session_id_bypasses_unique(store: Store):
    """Migration-synthesised rows pass session_id=None and need to be
    allowed even when there are many of them."""
    for _ in range(5):
        assert (
            store.record_verdict(
                skill_name="x",
                verdict="HELPFUL",
                host="other",
                session_id=None,
                skill_dir="/x",
            )
            is True
        )
    assert store.count_verdicts() == 5


def test_unknown_verdict_label_is_dropped(store: Store):
    assert (
        store.record_verdict(
            skill_name="x",
            verdict="MAYBE",  # not in HELPFUL/HARMFUL/NEUTRAL
            host="codex",
            skill_dir="/x",
        )
        is False
    )
    assert store.count_verdicts() == 0


# --------------------------------------------------------------------------- #
# Skill lifecycle (upsert + FK cascade)
# --------------------------------------------------------------------------- #


def test_skill_row_upserts_first_seen_preserved(store: Store):
    """``first_seen_at`` stays put across multiple verdicts."""
    store.record_verdict(
        skill_name="x", verdict="HELPFUL", host="codex",
        session_id="s1", skill_dir="/v1",
    )
    with store._connect() as conn:
        first = conn.execute(
            "SELECT first_seen_at FROM skills WHERE skill_name='x'"
        ).fetchone()[0]
    store.record_verdict(
        skill_name="x", verdict="HARMFUL", host="codex",
        session_id="s2", skill_dir="/v2",
    )
    with store._connect() as conn:
        again = conn.execute(
            "SELECT first_seen_at, skill_dir FROM skills WHERE skill_name='x'"
        ).fetchone()
    assert again[0] == first  # unchanged
    assert again[1] == "/v2"  # last_seen path updated


def test_fk_cascade_drops_verdicts(store: Store):
    """Deleting a skill row cascades to verdicts. (No more skill_state
    to worry about — only verdicts.)"""
    store.record_verdict(
        skill_name="x", verdict="HELPFUL", host="codex",
        session_id="s1", skill_dir="/x",
    )
    with store._connect() as conn:
        conn.execute("BEGIN")
        conn.execute("DELETE FROM skills WHERE skill_name='x'")
        conn.execute("COMMIT")
    assert store.count_verdicts() == 0


# --------------------------------------------------------------------------- #
# Bulk path + introspection
# --------------------------------------------------------------------------- #


def test_record_verdicts_bulk(store: Store):
    """record_verdicts returns count of *written* rows (unknown labels
    and dupes are excluded)."""
    items = [
        {"skill_name": "a", "verdict": "HELPFUL", "host": "codex",
         "session_id": "s1", "skill_dir": "/a"},
        {"skill_name": "b", "verdict": "HARMFUL", "host": "codex",
         "session_id": "s1", "skill_dir": "/b"},
        {"skill_name": "c", "verdict": "MAYBE", "host": "codex",
         "session_id": "s1", "skill_dir": "/c"},
    ]
    n = store.record_verdicts(items)
    assert n == 2
    assert store.count_verdicts() == 2


def test_count_verdicts_filtered_by_host(store: Store):
    store.record_verdict(
        skill_name="x", verdict="HELPFUL", host="codex",
        session_id="s1", skill_dir="/x",
    )
    store.record_verdict(
        skill_name="x", verdict="HELPFUL", host="hermes",
        session_id="s2", skill_dir="/x",
    )
    assert store.count_verdicts() == 2
    assert store.count_verdicts(host="codex") == 1
    assert store.count_verdicts(host="hermes") == 1
    assert store.count_verdicts(host="missing") == 0


# --------------------------------------------------------------------------- #
# FTS5 full-text search on verdicts.reason
# --------------------------------------------------------------------------- #


def _seed_verdicts(store: Store) -> None:
    fixtures = [
        ("webhook-signer", "HELPFUL", "codex",  "fixed webhook signature validation"),
        ("webhook-signer", "HARMFUL", "codex",  "broke HMAC SHA-256 computation"),
        ("session-cookie", "HELPFUL", "claude_code", "added secure cookie flag"),
        ("csrf-token",     "NEUTRAL", "hermes", "looked at csrf double-submit but didn't apply"),
    ]
    for i, (name, label, host, reason) in enumerate(fixtures):
        store.record_verdict(
            skill_name=name, verdict=label, host=host, reason=reason,
            session_id=f"s{i}", skill_dir=f"/{name}",
        )


def test_fts_search_finds_matching_rows(store: Store):
    _seed_verdicts(store)
    rows = store.search_reasons("webhook OR HMAC")
    names = {r["skill_name"] for r in rows}
    assert names == {"webhook-signer"}
    assert len(rows) == 2  # one HELPFUL + one HARMFUL for webhook-signer
    assert all(r["score"] is not None for r in rows)


def test_fts_search_filters_by_host(store: Store):
    _seed_verdicts(store)
    rows = store.search_reasons("cookie OR csrf", host="hermes")
    assert {r["host"] for r in rows} == {"hermes"}


def test_fts_search_filters_by_verdict(store: Store):
    _seed_verdicts(store)
    rows = store.search_reasons("webhook OR HMAC", verdict="HARMFUL")
    assert all(r["verdict"] == "HARMFUL" for r in rows)
    assert len(rows) == 1


def test_fts_search_returns_empty_on_no_match(store: Store):
    _seed_verdicts(store)
    rows = store.search_reasons("postgres replication shard tuning")
    assert rows == []


def test_fts_search_empty_query_returns_empty(store: Store):
    _seed_verdicts(store)
    assert store.search_reasons("") == []
    assert store.search_reasons("   ") == []


def test_fts_search_handles_malformed_match_gracefully(store: Store):
    """FTS5 raises OperationalError on bad MATCH syntax; we swallow it
    and return [] rather than bubble a cryptic SQL error to the CLI."""
    _seed_verdicts(store)
    # Unbalanced quotes / parentheses → SQLite throws.
    assert store.search_reasons('NOT a valid "expr') == []


def test_fts_search_reflects_updates(store: Store):
    """The verdicts_fts_update trigger keeps the FTS row in sync when
    we rewrite verdicts.reason directly."""
    _seed_verdicts(store)
    # Baseline: the HELPFUL row mentions "webhook"; the HARMFUL one says "HMAC".
    assert len(store.search_reasons("webhook")) == 1
    assert len(store.search_reasons("HMAC")) == 1
    # Rewrite the HELPFUL reason to remove the "webhook" word.
    with store._connect() as conn:
        conn.execute("BEGIN")
        conn.execute(
            "UPDATE verdicts SET reason='switched to grpc timeout' "
            "WHERE skill_name='webhook-signer' AND verdict='HELPFUL'"
        )
        conn.execute("COMMIT")
    assert store.search_reasons("webhook") == []
    rows = store.search_reasons("grpc")
    assert len(rows) == 1
    assert rows[0]["reason"] == "switched to grpc timeout"
    # The HARMFUL row's HMAC mention is unaffected.
    assert len(store.search_reasons("HMAC")) == 1


def test_fts_search_reflects_deletes(store: Store):
    """The verdicts_fts_delete trigger removes the index entry."""
    _seed_verdicts(store)
    assert len(store.search_reasons("HMAC")) == 1
    with store._connect() as conn:
        conn.execute("BEGIN")
        conn.execute(
            "DELETE FROM verdicts WHERE reason LIKE '%HMAC%'"
        )
        conn.execute("COMMIT")
    assert store.search_reasons("HMAC") == []


def test_fts_search_since_days_filter(store: Store):
    """``since_days`` filters out older rows even when the FTS index
    matches them."""
    import datetime

    old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=10)
    store.record_verdict(
        skill_name="old-skill", verdict="HELPFUL", host="codex",
        reason="ancient webhook fix", session_id="old1", skill_dir="/x",
        occurred_at=old,
    )
    store.record_verdict(
        skill_name="new-skill", verdict="HELPFUL", host="codex",
        reason="fresh webhook fix", session_id="new1", skill_dir="/x",
    )
    rows = store.search_reasons("webhook", since_days=3)
    assert {r["skill_name"] for r in rows} == {"new-skill"}


def test_fts_backfills_existing_verdicts_on_initialize(tmp_path: Path):
    """A DB created before the FTS table existed gets backfilled on the
    next initialize() so historical reasons become searchable without
    a re-write."""
    db = tmp_path / "legacy.db"
    # Seed an old-style DB with verdicts but NO FTS table.
    with sqlite3.connect(db) as conn:
        conn.executescript(
            """
            CREATE TABLE schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            INSERT INTO schema_version VALUES (1, '2026-01-01T00:00:00Z');
            CREATE TABLE skills (
                skill_name TEXT PRIMARY KEY,
                skill_dir TEXT NOT NULL,
                last_seen_sha TEXT,
                last_seen_host TEXT,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                extras_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE verdicts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                skill_name TEXT NOT NULL,
                verdict TEXT NOT NULL,
                reason TEXT,
                session_id TEXT,
                host TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                extras_json TEXT NOT NULL DEFAULT '{}',
                UNIQUE (session_id, skill_name, host)
            );
            INSERT INTO skills VALUES (
                'webhook-signer','/x',NULL,NULL,
                '2026-01-01T00:00:00Z','2026-01-01T00:00:00Z','{}'
            );
            INSERT INTO verdicts
                (skill_name, verdict, reason, host, occurred_at)
                VALUES
                ('webhook-signer','HELPFUL','legacy webhook fix','codex',
                 '2026-01-01T00:00:00Z');
            """
        )
    s = Store(db)
    rows = s.search_reasons("webhook")
    assert len(rows) == 1
    assert rows[0]["reason"] == "legacy webhook fix"


# --------------------------------------------------------------------------- #
# Dashboard helpers — get/update/delete verdict + activity_per_day
# --------------------------------------------------------------------------- #


def _seed_one(
    store: Store,
    *,
    skill: str = "webhook-signer",
    verdict: str = "HELPFUL",
    reason: str = "signed HMAC correctly",
    host: str = "codex",
    session_id: str | None = None,
) -> int:
    """Insert one verdict and return its row id."""
    store.record_verdict(
        skill_name=skill,
        verdict=verdict,
        reason=reason,
        host=host,
        session_id=session_id,
    )
    with store._connect() as conn:
        cur = conn.execute(
            "SELECT id FROM verdicts ORDER BY id DESC LIMIT 1"
        )
        return int(cur.fetchone()[0])


def test_get_verdict_returns_full_row(store: Store):
    vid = _seed_one(store, reason="ratelimit token bucket math")
    row = store.get_verdict(vid)
    assert row is not None
    assert row["skill_name"] == "webhook-signer"
    assert row["verdict"] == "HELPFUL"
    assert row["reason"] == "ratelimit token bucket math"
    assert row["host"] == "codex"
    assert row["occurred_at"]  # ISO timestamp


def test_get_verdict_unknown_id_returns_none(store: Store):
    assert store.get_verdict(99_999) is None


def test_update_verdict_flips_helpful_to_harmful_and_fts(store: Store):
    vid = _seed_one(store, reason="webhook signature mismatch")
    # Confirm FTS sees the row pre-update.
    pre = store.search_reasons("webhook")
    assert any(r["id"] == vid for r in pre)

    changed = store.update_verdict(vid, verdict="HARMFUL")
    assert changed is True
    row = store.get_verdict(vid)
    assert row["verdict"] == "HARMFUL"

    # FTS still finds it — update trigger preserves the row.
    post = store.search_reasons("webhook")
    assert any(r["id"] == vid and r["verdict"] == "HARMFUL" for r in post)


def test_update_verdict_changes_reason_text_and_fts(store: Store):
    vid = _seed_one(store, reason="webhook signature mismatch")
    store.update_verdict(vid, reason="HMAC tolerance was wrong")
    # Old query loses the row, new query finds it — proves the FTS
    # update trigger re-indexed the reason.
    assert all(r["id"] != vid for r in store.search_reasons("mismatch"))
    assert any(r["id"] == vid for r in store.search_reasons("tolerance"))


def test_update_verdict_invalid_verdict_raises(store: Store):
    vid = _seed_one(store)
    with pytest.raises(ValueError):
        store.update_verdict(vid, verdict="BANANA")


def test_update_verdict_no_fields_raises(store: Store):
    vid = _seed_one(store)
    with pytest.raises(ValueError):
        store.update_verdict(vid)


def test_update_verdict_missing_id_returns_false(store: Store):
    assert store.update_verdict(99_999, verdict="HARMFUL") is False


def test_delete_verdict_removes_row_and_fts(store: Store):
    vid_keep = _seed_one(
        store, reason="ratelimit token bucket", session_id="s1"
    )
    vid_drop = _seed_one(
        store,
        skill="rust-clap-parser",
        reason="webhook payload double-encoded",
        session_id="s2",
    )

    assert store.delete_verdict(vid_drop) is True
    assert store.get_verdict(vid_drop) is None
    # Surviving row still indexed.
    survivors = [r["id"] for r in store.search_reasons("ratelimit")]
    assert vid_keep in survivors
    # Dropped row's text gone from FTS.
    assert all(r["id"] != vid_drop for r in store.search_reasons("payload"))


def test_delete_verdict_missing_id_returns_false(store: Store):
    assert store.delete_verdict(99_999) is False


def test_activity_per_day_groups_and_filters(store: Store):
    # Three rows on two distinct days — group should collapse to 2 entries.
    _seed_one(store, host="codex", session_id="s1")
    _seed_one(
        store, host="claude_code", reason="claude case", session_id="s2"
    )
    _seed_one(
        store, host="codex", reason="another codex case", session_id="s3"
    )

    rows = store.activity_per_day(days=30)
    # At minimum one entry whose count equals the rows we inserted today.
    assert sum(c for _, c in rows) == 3

    by_host = store.activity_per_day(days=30, host="codex")
    assert sum(c for _, c in by_host) == 2

    by_skill = store.activity_per_day(days=30, skill_name="webhook-signer")
    assert sum(c for _, c in by_skill) == 3  # default skill on every seed


def test_activity_per_day_zero_days_raises(store: Store):
    with pytest.raises(ValueError):
        store.activity_per_day(days=0)


# --------------------------------------------------------------------------- #
# Reason quality gate (P3)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "reason",
    ["ok", "OK", "evidence A", "yes", "test", "todo", "r1", "x", "?"],
)
def test_record_verdict_drops_placeholder_reason(store: Store, reason: str):
    """Placeholder reasons must not pollute the corpus. HELPFUL/HARMFUL
    writes silently fail when the reason fails the quality gate so
    misconfigured callers (tests writing to the default DB, hooks
    with stub fixtures) can't accumulate noise."""
    ok = store.record_verdict(
        skill_name="x", verdict="HELPFUL", reason=reason,
        host="codex", session_id="s",
    )
    assert ok is False
    assert store.count_verdicts() == 0


def test_record_verdict_keeps_real_reason(store: Store):
    """A real, descriptive reason still flows through."""
    ok = store.record_verdict(
        skill_name="x", verdict="HELPFUL",
        reason="signed HMAC correctly with constant-time comparison",
        host="codex", session_id="s",
    )
    assert ok is True
    assert store.count_verdicts() == 1


def test_record_verdict_accepts_thin_neutral(store: Store):
    """NEUTRAL is diagnostic; the gate does not apply. A user marking
    a verdict NEUTRAL with a short reason via the dashboard drawer
    should not be blocked."""
    ok = store.record_verdict(
        skill_name="x", verdict="NEUTRAL", reason="ok",
        host="codex", session_id="s",
    )
    assert ok is True


# --------------------------------------------------------------------------- #
# v1 -> v2 migration: NULL session_id backfill
# --------------------------------------------------------------------------- #


def test_migration_v1_to_v2_heals_null_session_id(tmp_path: Path):
    """Open a v1-shaped DB with NULL session_id rows; initialize a
    fresh Store on the same path; the migration must stamp a
    fallback session_id on every previously-NULL row."""
    db = tmp_path / "v1.db"
    import sqlite3
    with sqlite3.connect(db) as conn:
        conn.executescript(
            """
            CREATE TABLE schema_version (
                version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
            );
            INSERT INTO schema_version VALUES (1, '2026-01-01T00:00:00Z');
            CREATE TABLE skills (
                skill_name TEXT PRIMARY KEY,
                skill_dir TEXT,
                sha TEXT,
                last_host TEXT,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                extras_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE verdicts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                skill_name TEXT NOT NULL,
                verdict TEXT NOT NULL,
                reason TEXT,
                session_id TEXT,
                host TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                extras_json TEXT NOT NULL DEFAULT '{}',
                UNIQUE (session_id, skill_name, host)
            );
            INSERT INTO skills VALUES (
                'webhook-signer','/x',NULL,NULL,
                '2026-01-01T00:00:00Z','2026-01-01T00:00:00Z','{}'
            );
            INSERT INTO verdicts
                (skill_name, verdict, reason, host, occurred_at)
                VALUES
                ('webhook-signer','HELPFUL','signature ok','codex',
                 '2026-01-01T00:00:00Z'),
                ('webhook-signer','HELPFUL','signature ok','codex',
                 '2026-01-01T00:00:01Z');
            """
        )

    s = Store(db)
    s.initialize()

    with s._connect() as conn:
        rows = conn.execute(
            "SELECT session_id FROM verdicts WHERE session_id IS NULL"
        ).fetchall()
        assert rows == []
        rows = conn.execute(
            "SELECT session_id FROM verdicts"
        ).fetchall()
        # Every row now carries a non-NULL fallback id.
        assert all(r[0] and r[0].startswith("_anon-") for r in rows)
        # Schema version bumped.
        v = conn.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()[0]
        assert v == 2
