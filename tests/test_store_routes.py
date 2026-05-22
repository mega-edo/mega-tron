"""Pin the routes-table writer + aggregator on Store.

The Context Savings dashboard tab depends on these for its measured
median display. We test them in isolation so a regression here is
caught long before it shows up as a wrong number in the dashboard.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mega_tron.verdicts.store import Store


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(path=tmp_path / "routes.db")
    s.initialize()
    return s


def test_record_route_round_trip(store):
    ok = store.record_route(
        session_id="sess-1",
        host="codex",
        query_hash="abc123",
        picked_names=["webhook-signer", "hmac-validator"],
        total_tok=420,
        k=2,
        k_reason="gap-cut@2",
    )
    assert ok is True
    stats = store.route_stats(days=30)
    assert stats["turn_count"] == 1
    assert stats["median_tok"] == 420
    assert stats["p50_tok"] == 420
    assert stats["p90_tok"] == 420


def test_route_stats_empty_window(store):
    """No routes recorded yet — every percentile must be None and
    turn_count zero so the dashboard can fall back to its benchmark
    constant."""
    stats = store.route_stats(days=30)
    assert stats["turn_count"] == 0
    assert stats["median_tok"] is None
    assert stats["p50_tok"] is None
    assert stats["p90_tok"] is None


def test_route_stats_percentiles_on_known_distribution(store):
    """With samples 120, 125, 130, …, 240 (25 values, step 5):
        median  = 180  (index 12)
        p90     = 230  (ceil(0.9 * 25) = 23 → index 22)
    """
    for i in range(25):
        ok = store.record_route(
            session_id=f"sess-{i}",
            host="codex",
            query_hash=f"q-{i}",
            picked_names=["x", "y"],
            total_tok=120 + i * 5,
            k=2,
            k_reason="gap-cut@2",
        )
        assert ok
    stats = store.route_stats(days=30)
    assert stats["turn_count"] == 25
    assert stats["median_tok"] == 180
    assert stats["p50_tok"] == 180
    assert stats["p90_tok"] == 230


def test_route_stats_only_counts_window(store):
    """Routes recorded more than `days` ago must NOT contribute. We
    can't easily backdate via the public API, so we use the
    occurred_at override to plant an "old" row directly."""
    # 5 fresh rows
    for i in range(5):
        store.record_route(
            session_id=f"new-{i}", host="codex",
            query_hash=f"new-{i}", picked_names=["x"],
            total_tok=200, k=1, k_reason="gap-cut@1",
        )
    # 5 stale rows backdated 60 days
    for i in range(5):
        store.record_route(
            session_id=f"old-{i}", host="codex",
            query_hash=f"old-{i}", picked_names=["x"],
            total_tok=9999,    # would obviously skew median if included
            k=1, k_reason="gap-cut@1",
            occurred_at="2020-01-01T00:00:00Z",
        )
    stats = store.route_stats(days=30)
    # Only the 5 fresh ones contribute.
    assert stats["turn_count"] == 5
    assert stats["median_tok"] == 200
    # And 90-day window should pick them all up.
    wide = store.route_stats(days=10_000)
    assert wide["turn_count"] == 10


def test_record_route_never_raises_on_bad_input(store):
    """Analytics path is best-effort. Even garbage input should return
    False without taking down the host hook that called us."""
    # None for session_id is fine (CLI path uses this).
    ok = store.record_route(
        session_id=None,
        host="cli",
        query_hash="x",
        picked_names=[],
        total_tok=0,
        k=0,
        k_reason="empty",
    )
    assert ok is True  # legit empty turn — gets recorded


def test_record_route_extras_carry_k_and_reason(store, tmp_path):
    """The k / k_reason fields are stored in extras_json. Read raw
    SQLite to confirm they're persisted (the aggregator only reads
    total_tok, so this test pins the audit trail directly)."""
    import json
    import sqlite3

    store.record_route(
        session_id="s",
        host="claude_code",
        query_hash="q",
        picked_names=["foo"],
        total_tok=147,
        k=5,
        k_reason="entropy-wide",
    )
    conn = sqlite3.connect(store.path)
    row = conn.execute(
        "SELECT extras_json FROM routes LIMIT 1"
    ).fetchone()
    conn.close()
    payload = json.loads(row[0])
    assert payload == {"total_tok": 147, "k": 5, "k_reason": "entropy-wide"}


def test_session_picked_names_empty_for_missing_session(store):
    assert store.session_picked_names(session_id="nope") == set()


def test_session_picked_names_union_across_turns(store):
    """The same session can route multiple turns. session_picked_names
    must return the union so a Stop hook firing at end-of-session sees
    every skill that was surfaced anywhere in the conversation."""
    store.record_route(
        session_id="sess-X",
        host="codex",
        query_hash="q1",
        picked_names=["alpha", "beta"],
        total_tok=100,
        k=2,
        k_reason="gap-cut@2",
    )
    store.record_route(
        session_id="sess-X",
        host="codex",
        query_hash="q2",
        picked_names=["beta", "gamma"],
        total_tok=200,
        k=2,
        k_reason="gap-cut@2",
    )
    names = store.session_picked_names(session_id="sess-X")
    assert names == {"alpha", "beta", "gamma"}


def test_session_picked_names_filters_by_host(store):
    """A session_id that somehow appears under two hosts should still
    be filterable. We don't expect this in practice, but the column
    exists so we honor it."""
    store.record_route(
        session_id="sess-Y",
        host="codex",
        query_hash="q",
        picked_names=["only-codex"],
        total_tok=50,
        k=1,
        k_reason="gap-cut@1",
    )
    store.record_route(
        session_id="sess-Y",
        host="gemini_cli",
        query_hash="q",
        picked_names=["only-gemini"],
        total_tok=50,
        k=1,
        k_reason="gap-cut@1",
    )
    assert store.session_picked_names(
        session_id="sess-Y", host="codex"
    ) == {"only-codex"}
    assert store.session_picked_names(
        session_id="sess-Y", host="gemini_cli"
    ) == {"only-gemini"}
    assert store.session_picked_names(session_id="sess-Y") == {
        "only-codex",
        "only-gemini",
    }


def test_session_picked_names_empty_session_id_returns_empty(store):
    """Defensive: empty string session_id must not match every row in
    the table just because SQL would happily accept it. The Stop hook
    falls back to legacy gating in this case."""
    store.record_route(
        session_id="real-session",
        host="codex",
        query_hash="q",
        picked_names=["x"],
        total_tok=10,
        k=1,
        k_reason="gap-cut@1",
    )
    assert store.session_picked_names(session_id="") == set()

