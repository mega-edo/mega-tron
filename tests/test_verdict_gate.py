"""Pin the routes-membership verdict admission gate.

This is the gate that fixes the silent verdict-drop bug: previously the
Stop hook required a `scripts/<name>/` echo in the transcript text to
admit a verdict tag, which dropped almost every legitimate verdict
because most skills don't ship a scripts/ directory. The new gate uses
the routes table (the precise per-turn record of what the router
surfaced) as the catalog.

Tests cover:
  1. With routes data → admit any tagged name in the catalog, even when
     the tracker labeled it ``claimed_use``.
  2. With routes data → reject names not in the catalog
     (hallucinated names from the model).
  3. Without routes data → fall back to the legacy ``claimed_use``
     rejection rule (so fresh installs / dev environments don't
     silently accept every hallucinated tag).
  4. Tag without a verdict attribute → always dropped, regardless of
     gate mode.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from mega_tron.hosts._verdict_gate import filter_invocations


@dataclass
class _FakeInvocation:
    """Mimics tracker.SelfReport just enough for the gate."""
    label: str = "claimed_use"
    verdicts: list = field(default_factory=list)
    reasons: list = field(default_factory=list)


@pytest.fixture
def routes_store(tmp_path: Path, monkeypatch):
    """Point ``store_path()`` at a fresh DB so the gate's routes lookup
    hits a controlled table instead of the user's real store."""
    from mega_tron.verdicts.store import Store
    import mega_tron.config as cfg
    import mega_tron.hosts._verdict_gate as gate_mod

    db = tmp_path / "store.db"
    monkeypatch.setattr(cfg, "store_path", lambda: db)
    # The gate imports store_path inside its function body, so swapping
    # the attribute on `cfg` is enough.
    store = Store(db)
    store.initialize()
    return store


def test_routes_gate_admits_claimed_use_when_in_catalog(routes_store):
    """The whole point of the new gate: a tag with
    ``label=claimed_use`` (no scripts/ echo) is admitted as long as
    the router actually surfaced that skill this session."""
    routes_store.record_route(
        session_id="s1",
        host="codex",
        query_hash="q",
        picked_names=["alpha"],
        total_tok=100,
        k=1,
        k_reason="gap-cut@1",
    )
    inv = _FakeInvocation(
        label="claimed_use",
        verdicts=["HELPFUL"],
        reasons=["Used alpha to do X"],
    )
    outcome = filter_invocations(
        invocations={"alpha": inv}, session_id="s1", host="codex"
    )
    assert outcome.via == "routes"
    assert outcome.admitted == [
        {"skill": "alpha", "verdict": "HELPFUL", "reason": "Used alpha to do X"}
    ]
    assert outcome.skipped_not_in_catalog == []


def test_routes_gate_rejects_hallucinated_name(routes_store):
    """Model emitted a tag for a skill the router never surfaced.
    The gate must reject — this is the hallucinated-name guard."""
    routes_store.record_route(
        session_id="s2",
        host="codex",
        query_hash="q",
        picked_names=["real-skill"],
        total_tok=100,
        k=1,
        k_reason="gap-cut@1",
    )
    inv = _FakeInvocation(
        label="claimed_use",
        verdicts=["HELPFUL"],
        reasons=["Invented out of thin air"],
    )
    outcome = filter_invocations(
        invocations={"fake-skill": inv}, session_id="s2", host="codex"
    )
    assert outcome.via == "routes"
    assert outcome.admitted == []
    assert outcome.skipped_not_in_catalog == ["fake-skill"]


def test_routes_gate_handles_mixed_admit_and_reject(routes_store):
    routes_store.record_route(
        session_id="s3",
        host="claude_code",
        query_hash="q",
        picked_names=["real-a", "real-b"],
        total_tok=200,
        k=2,
        k_reason="gap-cut@2",
    )
    invs = {
        "real-a": _FakeInvocation(
            label="claimed_use", verdicts=["HELPFUL"], reasons=["yes"]
        ),
        "real-b": _FakeInvocation(
            label="informed_use", verdicts=["NEUTRAL"], reasons=["maybe"]
        ),
        "made-up": _FakeInvocation(
            label="claimed_use", verdicts=["HARMFUL"], reasons=["lies"]
        ),
    }
    outcome = filter_invocations(
        invocations=invs, session_id="s3", host="claude_code"
    )
    assert outcome.via == "routes"
    admitted_names = sorted(v["skill"] for v in outcome.admitted)
    assert admitted_names == ["real-a", "real-b"]
    assert outcome.skipped_not_in_catalog == ["made-up"]


def test_legacy_gate_when_no_routes_data(routes_store):
    """Empty routes table → ``via='legacy'``. In that mode the gate
    falls back to the old ``claimed_use`` rejection so a fresh install
    with no routes history doesn't suddenly admit every tag."""
    inv = _FakeInvocation(
        label="claimed_use",
        verdicts=["HELPFUL"],
        reasons=["claimed, no operational trace"],
    )
    outcome = filter_invocations(
        invocations={"x": inv}, session_id="never-routed", host="codex"
    )
    assert outcome.via == "legacy"
    assert outcome.admitted == []
    assert outcome.skipped_not_in_catalog == ["x"]


def test_legacy_gate_admits_informed_use(routes_store):
    """Legacy fallback path still admits non-claimed_use invocations."""
    inv = _FakeInvocation(
        label="informed_use",
        verdicts=["HELPFUL"],
        reasons=["tagged AND ran scripts/"],
    )
    outcome = filter_invocations(
        invocations={"y": inv}, session_id="never-routed", host="codex"
    )
    assert outcome.via == "legacy"
    assert [v["skill"] for v in outcome.admitted] == ["y"]


def test_tag_without_verdict_attribute_is_dropped(routes_store):
    """A `<skill-used name="..."/>` tag with no verdict attribute is no
    signal — drop regardless of gate mode."""
    routes_store.record_route(
        session_id="s4",
        host="codex",
        query_hash="q",
        picked_names=["alpha"],
        total_tok=10,
        k=1,
        k_reason="gap-cut@1",
    )
    inv = _FakeInvocation(label="claimed_use", verdicts=[], reasons=[])
    outcome = filter_invocations(
        invocations={"alpha": inv}, session_id="s4", host="codex"
    )
    assert outcome.skipped_no_verdict == ["alpha"]
    assert outcome.admitted == []


def test_missing_session_id_falls_back_to_legacy(routes_store):
    """No session_id from the host hook payload (rare but possible) →
    legacy gate. Don't crash, don't open the floodgates."""
    inv = _FakeInvocation(
        label="claimed_use", verdicts=["HELPFUL"], reasons=["x"]
    )
    outcome = filter_invocations(
        invocations={"alpha": inv}, session_id=None, host="codex"
    )
    assert outcome.via == "legacy"
    assert outcome.admitted == []
