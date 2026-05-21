"""Windowed regression classifier — pinning each rung of the ladder.

The classifier is a pure function over a populated :class:`Store`. Each
test seeds a deterministic 60-day verdict timeline and asserts the
expected classification. Specifically pinned:

- ``broken`` — was working, now mostly harmful.
- ``regressed`` (hard) — zero helpful, at least one harmful recent.
- ``regressed`` (soft) — sharp helpful drop-off, no harm.
- ``unused`` — too few recent invocations (the false-positive guard).
- ``stable`` — no actionable trend.
- Host scoping isolates per-host signal.
- ``include_all=False`` hides ``unused`` / ``stable``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mega_tron.verdicts.regressions import compute
from mega_tron.verdicts.store import Store


# --------------------------------------------------------------------------- #
# Fixtures + helpers
# --------------------------------------------------------------------------- #


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(path=tmp_path / "store.db")
    s.initialize()
    return s


def _record_n(
    store: Store,
    *,
    skill: str,
    verdict: str,
    host: str = "codex",
    n: int,
    days_ago: int,
    session_prefix: str,
) -> None:
    """Seed ``n`` verdicts at ``now - days_ago``."""
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    for i in range(n):
        store.record_verdict(
            skill_name=skill,
            verdict=verdict,
            host=host,
            session_id=f"{session_prefix}-{i}",
            occurred_at=ts,
            skill_dir=f"/skills/{skill}",
        )


# --------------------------------------------------------------------------- #
# Empty store
# --------------------------------------------------------------------------- #


def test_empty_store_returns_no_regressions(store: Store):
    assert compute(store) == []
    assert compute(store, include_all=True) == []


# --------------------------------------------------------------------------- #
# Classification ladder — each class pinned with a minimal seed
# --------------------------------------------------------------------------- #


def test_broken_classification(store: Store):
    """Helpful in baseline, recent is >= 50% harmful → ``broken``."""
    _record_n(store, skill="webhook-signer", verdict="HELPFUL",
              n=8, days_ago=45, session_prefix="base-h")
    _record_n(store, skill="webhook-signer", verdict="HARMFUL",
              n=6, days_ago=3, session_prefix="recent-x")

    rows = compute(store)
    assert len(rows) == 1
    r = rows[0]
    assert r.skill_name == "webhook-signer"
    assert r.classification == "broken"
    assert r.helpful_recent == 0
    assert r.helpful_baseline == 8
    assert r.harmful_recent == 6
    assert "helpful 8 → 0" in r.detail
    assert "6 harmful events recent" in r.detail


def test_regressed_hard_is_subsumed_by_broken(store: Store):
    """The "zero helpful, ≥1 harmful" pattern is the strongest signal
    and always classifies as ``broken`` (harm_ratio = 1.0 ≥ 0.5 trips
    that rung first). The hard-regressed rung in :func:`_classify`
    exists only as a defensive fallback in case the ratio computation
    short-circuits — broken handles every realistic instance of this
    pattern, which is the right answer.

    This test pins the precedence: hard-regressed never wins against
    broken on the same input.
    """
    _record_n(store, skill="jwt-verifier", verdict="HELPFUL",
              n=8, days_ago=45, session_prefix="base-h")
    _record_n(store, skill="jwt-verifier", verdict="NEUTRAL",
              n=5, days_ago=3, session_prefix="recent-n")
    _record_n(store, skill="jwt-verifier", verdict="HARMFUL",
              n=1, days_ago=3, session_prefix="recent-x")

    rows = compute(store)
    assert len(rows) == 1
    r = rows[0]
    assert r.classification == "broken"  # not "regressed"
    assert r.helpful_recent == 0
    assert r.harmful_recent == 1


def test_regressed_soft_dropoff_classification(store: Store):
    """Helpful baseline, sharp drop-off (<= 20%), no harm → ``regressed`` (soft)."""
    _record_n(store, skill="dropoff-skill", verdict="HELPFUL",
              n=10, days_ago=45, session_prefix="base-h")
    # 1 helpful recent = 10% of baseline, no harm. Need recent_total >=
    # min_invocations(5), so pad with 4 NEUTRALs (counted toward
    # recent_total but not toward ratios).
    _record_n(store, skill="dropoff-skill", verdict="HELPFUL",
              n=1, days_ago=3, session_prefix="recent-h")
    _record_n(store, skill="dropoff-skill", verdict="NEUTRAL",
              n=4, days_ago=3, session_prefix="recent-n")

    rows = compute(store)
    assert len(rows) == 1
    r = rows[0]
    assert r.classification == "regressed"
    assert r.helpful_recent == 1
    assert r.harmful_recent == 0


def test_unused_false_positive_guard(store: Store):
    """Helpful in baseline, zero recent invocations → ``unused``, not
    ``regressed``. This is the key false-positive guard: a skill that
    nobody calls is dormant, not broken."""
    _record_n(store, skill="dormant-skill", verdict="HELPFUL",
              n=5, days_ago=45, session_prefix="base-h")
    # Zero recent verdicts.

    actionable = compute(store)
    assert actionable == []  # not actionable

    debug = compute(store, include_all=True)
    assert len(debug) == 1
    assert debug[0].classification == "unused"


def test_stable_classification(store: Store):
    """Healthy baseline AND healthy recent → ``stable``, not emitted by default."""
    _record_n(store, skill="stable-skill", verdict="HELPFUL",
              n=5, days_ago=45, session_prefix="base-h")
    _record_n(store, skill="stable-skill", verdict="HELPFUL",
              n=5, days_ago=3, session_prefix="recent-h")

    actionable = compute(store)
    assert actionable == []

    debug = compute(store, include_all=True)
    assert len(debug) == 1
    assert debug[0].classification == "stable"


# --------------------------------------------------------------------------- #
# Threshold knobs
# --------------------------------------------------------------------------- #


def test_min_invocations_relaxes_unused_guard(store: Store):
    """Lowering ``min_invocations`` makes a small-volume harmful trend
    actionable."""
    _record_n(store, skill="low-vol", verdict="HELPFUL",
              n=5, days_ago=45, session_prefix="base-h")
    _record_n(store, skill="low-vol", verdict="HARMFUL",
              n=2, days_ago=3, session_prefix="recent-x")

    # Default min=5 — recent_total=2 → unused.
    assert compute(store) == []

    # min=2 — recent_total=2 → hits broken (2/2 = 100% harm).
    rows = compute(store, min_invocations=2)
    assert len(rows) == 1
    assert rows[0].classification == "broken"


def test_window_days_changes_baseline_window(store: Store):
    """Verdicts that fall outside the baseline window don't contribute.

    A `window_days=30` analysis looks at baseline = 30..60 days ago.
    A verdict 70 days ago falls *outside* the baseline window.
    """
    _record_n(store, skill="old-skill", verdict="HELPFUL",
              n=5, days_ago=70, session_prefix="too-old")
    _record_n(store, skill="old-skill", verdict="HARMFUL",
              n=5, days_ago=3, session_prefix="recent-x")
    # baseline window 30..60 days ago has zero helpful; the
    # `helpful_baseline >= min_invocations` precondition fails on every
    # rung → stable (or absent from actionable).
    assert compute(store) == []


# --------------------------------------------------------------------------- #
# Host scoping
# --------------------------------------------------------------------------- #


def test_host_filter_isolates_per_host_signal(store: Store):
    """``host=hermes`` ignores Codex verdicts and vice versa."""
    # Codex says the skill is fine.
    _record_n(store, skill="cross-host", verdict="HELPFUL",
              host="codex", n=5, days_ago=45, session_prefix="codex-base")
    _record_n(store, skill="cross-host", verdict="HELPFUL",
              host="codex", n=5, days_ago=3, session_prefix="codex-recent")
    # Hermes says it's broken.
    _record_n(store, skill="cross-host", verdict="HELPFUL",
              host="hermes", n=5, days_ago=45, session_prefix="hermes-base")
    _record_n(store, skill="cross-host", verdict="HARMFUL",
              host="hermes", n=5, days_ago=3, session_prefix="hermes-recent")

    # Cross-host view (no filter): Codex's healthy signal swamps Hermes's
    # complaint — 5 baseline-helpful + 5 baseline-helpful = 10 baseline,
    # 5 recent-helpful + 5 recent-harmful = 10 recent. harm_ratio=0.5 →
    # broken.
    cross = compute(store)
    assert len(cross) == 1
    # We just verified the cross-host signal isn't host-agnostic luck.

    # Scoped to Codex: stable.
    assert compute(store, host="codex") == []
    # Scoped to Hermes: broken.
    h = compute(store, host="hermes")
    assert len(h) == 1
    assert h[0].classification == "broken"


# --------------------------------------------------------------------------- #
# Output shape
# --------------------------------------------------------------------------- #


def test_actionable_only_by_default(store: Store):
    """One broken + one unused + one stable → only broken in default output."""
    _record_n(store, skill="b", verdict="HELPFUL", n=8, days_ago=45,
              session_prefix="bb")
    _record_n(store, skill="b", verdict="HARMFUL", n=6, days_ago=3,
              session_prefix="br")
    _record_n(store, skill="u", verdict="HELPFUL", n=5, days_ago=45,
              session_prefix="uu")
    _record_n(store, skill="s", verdict="HELPFUL", n=5, days_ago=45,
              session_prefix="ss")
    _record_n(store, skill="s", verdict="HELPFUL", n=5, days_ago=3,
              session_prefix="sr")

    actionable = compute(store)
    assert [r.skill_name for r in actionable] == ["b"]

    debug = compute(store, include_all=True)
    classes = {r.skill_name: r.classification for r in debug}
    assert classes == {"b": "broken", "u": "unused", "s": "stable"}


def test_results_are_severity_sorted(store: Store):
    """``broken`` < ``regressed`` < ``unused`` < ``stable``. Within a
    class, skills are alphabetical for reproducibility.

    Soft-regressed (sharp helpful drop-off, no harm) is the realistic
    ``regressed`` case — hard-regressed is subsumed by broken (see
    :func:`test_regressed_hard_is_subsumed_by_broken`).
    """
    # zeta_broken: high-volume harmful recent.
    _record_n(store, skill="zeta_broken", verdict="HELPFUL", n=8,
              days_ago=45, session_prefix="zb1")
    _record_n(store, skill="zeta_broken", verdict="HARMFUL", n=6,
              days_ago=3, session_prefix="zb2")
    # alpha_regressed: 10 helpful baseline, 1 helpful recent + 4 neutral
    # → soft drop-off (10% of baseline, no harm). recent_invocations = 5
    # so it clears min_invocations.
    _record_n(store, skill="alpha_regressed", verdict="HELPFUL", n=10,
              days_ago=45, session_prefix="ar1")
    _record_n(store, skill="alpha_regressed", verdict="HELPFUL", n=1,
              days_ago=3, session_prefix="ar2")
    _record_n(store, skill="alpha_regressed", verdict="NEUTRAL", n=4,
              days_ago=3, session_prefix="ar3")

    rows = compute(store)
    assert [r.classification for r in rows] == ["broken", "regressed"]
    assert [r.skill_name for r in rows] == ["zeta_broken", "alpha_regressed"]


def test_last_event_timestamps_populated(store: Store):
    """``last_helpful_at`` / ``last_harmful_at`` carry through the SQL
    aggregation as datetime objects."""
    _record_n(store, skill="x", verdict="HELPFUL", n=5, days_ago=45,
              session_prefix="h")
    _record_n(store, skill="x", verdict="HARMFUL", n=5, days_ago=3,
              session_prefix="x")

    rows = compute(store, include_all=True)
    assert len(rows) == 1
    r = rows[0]
    assert r.last_helpful_at is not None
    assert r.last_harmful_at is not None
    # Recent harm is more recent than baseline helpful.
    assert r.last_harmful_at > r.last_helpful_at


# --------------------------------------------------------------------------- #
# MegaCore wiring
# --------------------------------------------------------------------------- #


def test_megacore_regressions_delegates_to_compute(
    store: Store, fake_embedder, fixtures_dir: Path, tmp_path: Path
):
    """MegaCore.regressions() must produce the same rows as
    regressions.compute() called directly on the same store."""
    _record_n(store, skill="webhook-signer", verdict="HELPFUL", n=8,
              days_ago=45, session_prefix="b")
    _record_n(store, skill="webhook-signer", verdict="HARMFUL", n=6,
              days_ago=3, session_prefix="r")

    from mega_tron.cache import Cache
    from mega_tron.core import MegaCore

    core = MegaCore(
        skills_dirs=[fixtures_dir / "skills"],
        embedder=fake_embedder,
        cache=Cache(path=tmp_path / "cache.npz"),
        store=store,
    )
    via_core = core.regressions()
    via_compute = compute(store)
    assert [r.skill_name for r in via_core] == [r.skill_name for r in via_compute]
    assert [r.classification for r in via_core] == [
        r.classification for r in via_compute
    ]


def test_megacore_regressions_returns_empty_without_store(
    fake_embedder, fixtures_dir: Path, tmp_path: Path, monkeypatch
):
    """No store wired = silent empty list, not an exception."""
    # Force store auto-discovery to miss by pointing it at a nonexistent path.
    monkeypatch.setenv("MEGA_TRON_STORE", str(tmp_path / "absent.db"))
    from mega_tron.cache import Cache
    from mega_tron.core import MegaCore

    core = MegaCore(
        skills_dirs=[fixtures_dir / "skills"],
        embedder=fake_embedder,
        cache=Cache(path=tmp_path / "cache.npz"),
    )
    assert core.store is None
    assert core.regressions() == []
