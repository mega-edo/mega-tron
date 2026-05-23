"""Pin :mod:`mega_tron.hosts._route_log` helpers.

The two helpers (``log_route_from_ranked`` / ``log_route_from_daemon``)
are the single chokepoint every host hook uses to push routes-table
rows. Their main job is "always write a row when the hook fires" so
that multi-turn analytics — and the dashboard's measured-median —
can count no-match turns instead of treating them as if the hook
never ran.
"""
from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from mega_tron.hosts import _route_log
from mega_tron.verdicts.store import Store


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> Store:
    """Point the module at a fresh per-test store.db."""
    db = tmp_path / "routes.db"
    s = Store(path=db)
    s.initialize()
    monkeypatch.setattr(_route_log, "store_path", lambda: db)
    return s


def test_log_route_from_daemon_writes_row_even_for_empty_skills(store):
    """No-match turns MUST land in the routes table.

    Without this, a session that fires the hook but surfaces nothing
    is indistinguishable from a session where the hook never ran — and
    multi-turn QA (codex / claude / gemini) can't validate that the
    hook actually executed on every user turn.
    """
    _route_log.log_route_from_daemon(
        prompt="hello there",
        daemon_response={"skills": []},
        skills_dirs=[],
        session_id="sess-empty",
        host="codex",
    )

    stats = store.route_stats(days=30)
    assert stats["turn_count"] == 1, (
        "empty-skills daemon response must still produce a routes row "
        "so no-match turns are visible to analytics"
    )
    assert stats["median_tok"] == 0
    picked = store.session_picked_names(session_id="sess-empty")
    assert picked == set()


def test_log_route_from_daemon_tags_no_match_reason(store, tmp_path):
    """The k_reason column should make no-match turns inspectable.

    Operators reading the routes table need to be able to tell a
    no-match turn from a "dynamic K → 0" miss; we mark the former
    with ``k_reason='no-match'`` when the daemon returned zero
    picks and no extras.
    """
    _route_log.log_route_from_daemon(
        prompt="hi",
        daemon_response={"skills": []},
        skills_dirs=[],
        session_id="sess-tag",
        host="codex",
    )
    # Pull the row directly to inspect extras_json.
    import json
    import sqlite3

    rows = sqlite3.connect(tmp_path / "routes.db").execute(
        "SELECT extras_json FROM routes WHERE session_id='sess-tag'"
    ).fetchall()
    assert len(rows) == 1
    extras = json.loads(rows[0][0])
    assert extras["k"] == 0
    assert extras["k_reason"] == "no-match"
    assert extras["total_tok"] == 0


def test_log_route_from_daemon_uses_extras_when_provided(store):
    """When the daemon ships an extras block, the helper must trust
    it instead of resolving picked names against the on-disk pool —
    daemon-side numbers are authoritative."""
    _route_log.log_route_from_daemon(
        prompt="some prompt",
        daemon_response={
            "skills": ["picked-a", "picked-b"],
            "extras": {"total_tok": 180, "k": 2, "k_reason": "gap-cut@2"},
        },
        skills_dirs=[],
        session_id="sess-extras",
        host="claude_code",
    )
    stats = store.route_stats(days=30)
    assert stats["turn_count"] == 1
    assert stats["median_tok"] == 180


def test_log_route_from_ranked_writes_row_for_empty_ranked(store):
    """Cold-path symmetry: in-process Router returning empty ranked
    list also produces a row. Multi-turn QA depends on both paths
    behaving identically here."""

    class FakeRouter:
        last_dynamic = (0, "uniform-null")

    _route_log.log_route_from_ranked(
        prompt="weather chat",
        ranked=[],
        router=FakeRouter(),
        session_id="sess-cold-empty",
        host="gemini_cli",
    )
    stats = store.route_stats(days=30)
    assert stats["turn_count"] == 1
    assert stats["median_tok"] == 0
    picked = store.session_picked_names(session_id="sess-cold-empty")
    assert picked == set()
