"""MegaCore — host-agnostic facade contract.

The facade is the public library API host adapters build against. The
test surface mirrors the v1.1 promise:

- :meth:`MegaCore.route` returns *exactly* what :meth:`Router.rank`
  would have returned for the same inputs (so swapping a host adapter
  from a direct Router call to MegaCore does not perturb ranking).
- :meth:`MegaCore.record_verdict` and :meth:`record_verdicts` mutate
  SKILL.md ``mega_meta:`` frontmatter through the same code path as the
  legacy CLI / Stop hook (delegates to ``apply_evaluations``).
- The Phase 1 stubs (:meth:`regressions`, :meth:`export_frontmatter`,
  ``by_host`` rows in :meth:`stats`) return safe defaults so host
  adapters can call them today without crashing or producing wrong
  data — they upgrade to real implementations in Phase 2/3 without a
  signature change.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from mega_tron import (
    MegaCore,
    Regression,
    StatRow,
    Verdict,
)
from mega_tron.cache import Cache
from mega_tron.verdicts.mega_meta import read_meta
from mega_tron.router import Router


# --------------------------------------------------------------------------- #
# Routing parity
# --------------------------------------------------------------------------- #


def test_core_route_matches_router_rank(
    fake_embedder, fixtures_dir, tmp_cache_path
):
    """MegaCore.route must return identical results to Router.rank.

    Same skills_dir, same embedder, same cache backend → identical
    ranking order and identical scores. Without this property the
    facade would be a second, subtly-different routing path; the
    point of v1.1 is that it is *the same* routing path.
    """
    skills_dir = fixtures_dir / "skills"
    # Direct router (the legacy call shape that Codex/Claude hooks use).
    router = Router(
        skills_dir=skills_dir,
        embedder=fake_embedder,
        cache=Cache(path=tmp_cache_path),
    )
    router_ranked = router.rank("webhook signature with hmac", top_k=5)

    # Facade with a fresh cache to keep the test deterministic.
    fresh_cache = Cache(path=tmp_cache_path.with_suffix(".facade.npz"))
    core = MegaCore(
        skills_dirs=[skills_dir],
        embedder=fake_embedder,
        cache=fresh_cache,
    )
    core_ranked = core.route("webhook signature with hmac", top_k=5)

    assert [r.skill.name for r in core_ranked] == [
        r.skill.name for r in router_ranked
    ]
    assert [round(r.score, 6) for r in core_ranked] == [
        round(r.score, 6) for r in router_ranked
    ]


def test_core_warmup_returns_tuple(fake_embedder, fixtures_dir, tmp_cache_path):
    """warmup() and warmup_if_stale() are pass-through to Router."""
    core = MegaCore(
        skills_dirs=[fixtures_dir / "skills"],
        embedder=fake_embedder,
        cache=Cache(path=tmp_cache_path),
    )
    n_new, n_reused, invalid = core.warmup()
    assert n_new + n_reused == 50  # 50 fixture skills
    assert invalid == []


def test_core_route_top_k_respected(fake_embedder, fixtures_dir, tmp_cache_path):
    core = MegaCore(
        skills_dirs=[fixtures_dir / "skills"],
        embedder=fake_embedder,
        cache=Cache(path=tmp_cache_path),
    )
    assert len(core.route("webhook hmac", top_k=3)) == 3
    assert len(core.route("webhook hmac", top_k=10)) == 10


# --------------------------------------------------------------------------- #
# Verdict pipeline
# --------------------------------------------------------------------------- #


def _make_writable_skills_dir(fixtures_dir: Path, tmp_path: Path) -> Path:
    """Copy a small slice of fixture skills into tmp so tests can mutate
    SKILL.md frontmatter without polluting the shared fixture set."""
    dst = tmp_path / "skills"
    dst.mkdir()
    # Copy the first 3 fixture skills.
    src_root = fixtures_dir / "skills"
    for skill_dir in sorted(src_root.iterdir())[:3]:
        if skill_dir.is_dir():
            shutil.copytree(skill_dir, dst / skill_dir.name)
    return dst


def test_record_verdict_writes_frontmatter(
    fake_embedder, fixtures_dir, tmp_path, tmp_cache_path
):
    """A single HELPFUL verdict bumps helpful_count by 1.

    This is the v1.1 promise: record_verdict delegates to
    apply_evaluations so the on-disk effect is identical to what the
    Stop hook would produce. Phase 2 will add a SQLite write
    alongside; the frontmatter mutation observed here keeps working.
    """
    skills_dir = _make_writable_skills_dir(fixtures_dir, tmp_path)
    target_name = sorted(p.name for p in skills_dir.iterdir())[0]

    core = MegaCore(
        skills_dirs=[skills_dir],
        embedder=fake_embedder,
        cache=Cache(path=tmp_cache_path),
    )
    before = read_meta(skills_dir / target_name / "SKILL.md")

    core.record_verdict(
        Verdict(
            skill_name=target_name,
            verdict="HELPFUL",
            host="hermes",
            reason="diff +12 -3 in src/auth/, integration test passed",
            session_id="test-session-1",
        )
    )

    after = read_meta(skills_dir / target_name / "SKILL.md")
    assert after.helpful_count == before.helpful_count + 1
    assert after.harmful_count == before.harmful_count
    # The reason was captured in helpful_contexts (capped at 3).
    assert any(
        "integration test passed" in ctx for ctx in after.helpful_contexts
    )


def test_record_verdicts_batch(fake_embedder, fixtures_dir, tmp_path, tmp_cache_path):
    """A batch of mixed verdicts updates each skill's frontmatter once.

    Mirrors the typical Stop hook call: one batch per session covering
    every invoked skill.
    """
    skills_dir = _make_writable_skills_dir(fixtures_dir, tmp_path)
    names = sorted(p.name for p in skills_dir.iterdir())
    assert len(names) >= 3

    core = MegaCore(
        skills_dirs=[skills_dir],
        embedder=fake_embedder,
        cache=Cache(path=tmp_cache_path),
    )
    outcome = core.record_verdicts(
        [
            Verdict(skill_name=names[0], verdict="HELPFUL", host="codex",
                    reason="evidence A", session_id="sess-1"),
            Verdict(skill_name=names[1], verdict="HARMFUL", host="codex",
                    reason="evidence B", session_id="sess-1"),
            Verdict(skill_name=names[2], verdict="INCONCLUSIVE", host="codex",
                    reason=None, session_id="sess-1"),
        ]
    )
    assert outcome.updated == 2  # INCONCLUSIVE is a no-op
    assert outcome.skipped_inconclusive == 1
    assert outcome.skipped_missing == 0
    assert sorted(outcome.applied) == sorted([names[0], names[1]])

    # Verify the on-disk mutations.
    m0 = read_meta(skills_dir / names[0] / "SKILL.md")
    m1 = read_meta(skills_dir / names[1] / "SKILL.md")
    m2 = read_meta(skills_dir / names[2] / "SKILL.md")
    assert m0.helpful_count >= 1 and m0.last_session_id == "sess-1"
    assert m1.harmful_count >= 1 and m1.last_session_id == "sess-1"
    # INCONCLUSIVE is fully no-op: counters unchanged, last_session_id
    # NOT bumped (no signal carried).
    assert m2.helpful_count == 0 and m2.harmful_count == 0


def test_record_verdicts_empty_iterable(
    fake_embedder, fixtures_dir, tmp_cache_path
):
    core = MegaCore(
        skills_dirs=[fixtures_dir / "skills"],
        embedder=fake_embedder,
        cache=Cache(path=tmp_cache_path),
    )
    outcome = core.record_verdicts([])
    assert outcome.updated == 0
    assert outcome.applied == []


def test_verdict_is_frozen_dataclass():
    """Verdict must be immutable so host adapters cannot accidentally
    mutate a wire-format object after it's been queued for write."""
    v = Verdict(
        skill_name="x",
        verdict="HELPFUL",
        host="hermes",
        reason="r",
    )
    with pytest.raises(Exception):
        v.verdict = "HARMFUL"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Analytics stubs (Phase 1 — final shape, empty data)
# --------------------------------------------------------------------------- #


def test_regressions_returns_empty_list_in_phase1(
    fake_embedder, fixtures_dir, tmp_cache_path
):
    """Phase 1: regressions() always returns []. The signature is final
    so host adapters can call it today; Phase 3 wires the SQLite-backed
    classifier without changing the call site."""
    core = MegaCore(
        skills_dirs=[fixtures_dir / "skills"],
        embedder=fake_embedder,
        cache=Cache(path=tmp_cache_path),
    )
    assert core.regressions() == []
    assert core.regressions(window_days=7, min_invocations=3) == []
    assert core.regressions(host="hermes") == []


def test_regression_dataclass_shape():
    """Regression dataclass is constructible with the planned schema."""
    r = Regression(
        skill_name="webhook-signer",
        classification="regressed",
        helpful_recent=0,
        helpful_baseline=12,
        harmful_recent=3,
        harmful_baseline=0,
        window_days=30,
        last_helpful_at=None,
        last_harmful_at=None,
        detail="helpful 12 → 0 over 30d (3 harmful events recent)",
    )
    assert r.classification == "regressed"
    assert r.helpful_baseline > r.helpful_recent


def test_stats_reads_frontmatter(fake_embedder, fixtures_dir, tmp_cache_path):
    """In Phase 1 stats() reads SKILL.md frontmatter directly. All rows
    carry host=None because per-host tagging needs the Phase 2 store."""
    core = MegaCore(
        skills_dirs=[fixtures_dir / "skills"],
        embedder=fake_embedder,
        cache=Cache(path=tmp_cache_path),
    )
    rows = core.stats()
    assert len(rows) == 50
    assert all(isinstance(r, StatRow) for r in rows)
    assert all(r.host is None for r in rows)  # by_host stub


def test_stats_top_filter(fake_embedder, fixtures_dir, tmp_cache_path):
    """top=N caps the row count after sorting by (helpful - harmful)."""
    core = MegaCore(
        skills_dirs=[fixtures_dir / "skills"],
        embedder=fake_embedder,
        cache=Cache(path=tmp_cache_path),
    )
    rows = core.stats(top=5)
    assert len(rows) == 5


def test_export_frontmatter_is_noop_in_phase1(
    fake_embedder, fixtures_dir, tmp_cache_path
):
    core = MegaCore(
        skills_dirs=[fixtures_dir / "skills"],
        embedder=fake_embedder,
        cache=Cache(path=tmp_cache_path),
    )
    assert core.export_frontmatter() == 0


# --------------------------------------------------------------------------- #
# Skills-dirs introspection / construction
# --------------------------------------------------------------------------- #


def test_skills_dirs_property_exposes_construction_arg(
    fake_embedder, fixtures_dir, tmp_cache_path
):
    skills_dir = fixtures_dir / "skills"
    core = MegaCore(
        skills_dirs=[skills_dir],
        embedder=fake_embedder,
        cache=Cache(path=tmp_cache_path),
    )
    assert core.skills_dirs == [skills_dir]
