"""Evaluation-aware score blending — pure-function tests + Router.rank
ordering scenarios from the v0.3 plan's worked example.
"""
from __future__ import annotations

import os
from unittest.mock import patch

import numpy as np
import pytest

from mega_tron.verdicts.mega_meta import MegaMeta
from mega_tron.ranker import (
    Weights,
    adjusted_score,
    context_term,
    count_bonus,
    current_weights,
    eval_blend_enabled,
    reload_weights,
    status_multiplier,
)


@pytest.fixture(autouse=True)
def _reset_weights():
    """Ensure tests start from defaults regardless of caller env."""
    with patch.dict(os.environ, {}, clear=False):
        for k in (
            "MEGA_EVAL_COUNT_W",
            "MEGA_EVAL_CONTEXT_W",
            "MEGA_EVAL_HARM_W",
            "MEGA_EVAL_RELATED_W",
            "MEGA_EVAL_BLEND",
        ):
            os.environ.pop(k, None)
        reload_weights()
        yield


# --------------------------------------------------------------------------
# count_bonus
# --------------------------------------------------------------------------


def test_count_bonus_cold_start():
    assert count_bonus(0, 0) == 0.0


def test_count_bonus_helpful_dominates_positive():
    assert count_bonus(8, 0) > 0


def test_count_bonus_harmful_dominates_negative():
    assert count_bonus(0, 8) < 0


def test_count_bonus_balanced_near_zero():
    # Symmetric counts → rate stays near 0.5 → bonus near 0.
    assert abs(count_bonus(5, 5)) < 0.05


def test_count_bonus_bounded():
    # Even with extreme counts the bonus stays within [-0.5, +0.5].
    assert count_bonus(1000, 0) < 0.5
    assert count_bonus(0, 1000) > -0.5


def test_count_bonus_grows_with_confidence():
    """Same 100% helpful rate; more samples → stronger bonus.

    Two compounding effects: confidence ramps from 0→1 over the first 10
    invocations, AND the Beta(1,1)-smoothed rate creeps toward 1 as the
    prior gets washed out. Both are monotonic.
    """
    assert count_bonus(1, 0) < count_bonus(5, 0) < count_bonus(20, 0) < count_bonus(100, 0)
    # Confidence is saturated past n=10, so the ratio of consecutive bumps
    # comes only from the rate term — small but still positive.
    assert count_bonus(100, 0) - count_bonus(20, 0) < 0.1


# --------------------------------------------------------------------------
# context_term
# --------------------------------------------------------------------------


def _unit(*vals: float) -> np.ndarray:
    """Build a unit-norm vector from explicit components."""
    v = np.array(vals, dtype=np.float32)
    return v / np.linalg.norm(v)


def test_context_term_no_contexts_returns_zero():
    q = _unit(1, 0, 0)
    assert context_term(q, None, None) == 0.0


def test_context_term_helpful_match_boosts():
    q = _unit(1, 0, 0)
    help_embs = np.stack([_unit(1, 0, 0)])  # perfect match
    assert context_term(q, help_embs, None, w_harm=1.5) == pytest.approx(1.0)


def test_context_term_helpful_unrelated_no_boost():
    q = _unit(1, 0, 0)
    help_embs = np.stack([_unit(0, 1, 0)])  # orthogonal
    assert context_term(q, help_embs, None, w_harm=1.5) == pytest.approx(0.0, abs=1e-5)


def test_context_term_harm_is_amplified_asymmetric():
    """Harmful match penalty is 1.5× the equivalent helpful boost."""
    q = _unit(1, 0, 0)
    harm_embs = np.stack([_unit(1, 0, 0)])
    assert context_term(q, None, harm_embs, w_harm=1.5) == pytest.approx(-1.5)


def test_context_term_takes_max_across_multiple_contexts():
    q = _unit(1, 0, 0)
    help_embs = np.stack([_unit(0, 1, 0), _unit(1, 0, 0)])  # max should pick the second
    assert context_term(q, help_embs, None, w_harm=1.5) == pytest.approx(1.0)


# --------------------------------------------------------------------------
# status_multiplier
# --------------------------------------------------------------------------


def test_status_multiplier_values():
    assert status_multiplier("active") == 1.0
    assert status_multiplier(None) == 1.0
    assert status_multiplier("suspect") == 0.5
    assert status_multiplier("archived") == -1.0
    assert status_multiplier("anything-else") == 1.0


# --------------------------------------------------------------------------
# adjusted_score — composite
# --------------------------------------------------------------------------


def test_adjusted_score_cold_start_equals_semantic():
    q = _unit(1, 0, 0)
    meta = MegaMeta()  # zero counts, no contexts, active
    assert adjusted_score(0.70, q, meta) == pytest.approx(0.70)


def test_adjusted_score_archived_returns_sentinel():
    q = _unit(1, 0, 0)
    meta = MegaMeta(helpful_count=100, status="archived")
    assert adjusted_score(0.90, q, meta) == -1.0


def test_adjusted_score_suspect_halves():
    q = _unit(1, 0, 0)
    meta = MegaMeta(helpful_count=1, harmful_count=4, status="suspect")
    out = adjusted_score(0.70, q, meta)
    # Status multiplier applied last; helpful/harmful counts also push it
    # below 0.35. Just assert it dropped under the halving threshold.
    assert out < 0.70 / 2 + 1e-3


def test_adjusted_score_helpful_history_with_matching_task_boosts():
    """The B-scenario from the plan: same semantic, task-relevant help → top."""
    q = _unit(1, 0, 0)
    help_embs = np.stack([_unit(0.85, 0.527, 0)])  # cos ≈ 0.85 with q
    meta = MegaMeta(
        helpful_count=8,
        harmful_count=0,
        helpful_contexts=["catches HMAC mismatch"],
    )
    a = adjusted_score(0.70, q, MegaMeta())               # cold
    b = adjusted_score(0.70, q, meta, helpful_ctx_embs=help_embs)
    assert b > a


def test_adjusted_score_harm_matching_task_drops():
    """The D-scenario: harm history close to query → big penalty."""
    q = _unit(1, 0, 0)
    harm_embs = np.stack([_unit(0.75, 0.661, 0)])  # cos ≈ 0.75
    meta = MegaMeta(helpful_count=1, harmful_count=4)
    a = adjusted_score(0.70, q, MegaMeta())                # cold
    d = adjusted_score(0.70, q, meta, harmful_ctx_embs=harm_embs)
    assert d < a


def test_adjusted_score_unrelated_history_barely_moves():
    """C-scenario: helpful history but task-orthogonal → tiny change."""
    q = _unit(1, 0, 0)
    help_embs = np.stack([_unit(0.2, 0.980, 0)])  # cos ≈ 0.20
    meta = MegaMeta(helpful_count=8, harmful_count=0)
    a = adjusted_score(0.70, q, MegaMeta())
    c = adjusted_score(0.70, q, meta, helpful_ctx_embs=help_embs)
    # Moves up slightly but less than a strongly-matching history would.
    assert c > a
    assert c < a + 0.15  # tighter bound than W_CONTEXT alone


def test_adjusted_score_ordering_matches_plan_table():
    """Reproduce the full worked example: B > C > A > D > E."""
    q = _unit(1, 0, 0)
    help_match = np.stack([_unit(0.85, 0.527, 0)])  # cos ≈ 0.85
    help_off = np.stack([_unit(0.2, 0.980, 0)])
    harm_match = np.stack([_unit(0.75, 0.661, 0)])

    a = adjusted_score(0.70, q, MegaMeta())                                              # cold
    b = adjusted_score(0.70, q, MegaMeta(helpful_count=8), helpful_ctx_embs=help_match)
    c = adjusted_score(0.70, q, MegaMeta(helpful_count=8), helpful_ctx_embs=help_off)
    d = adjusted_score(0.70, q, MegaMeta(helpful_count=1, harmful_count=4),
                       harmful_ctx_embs=harm_match)
    e = adjusted_score(0.70, q, MegaMeta(helpful_count=1, harmful_count=4, status="suspect"))

    assert b > c > a > d > e


# --------------------------------------------------------------------------
# env knobs
# --------------------------------------------------------------------------


def test_eval_blend_enabled_default():
    assert eval_blend_enabled() is True


def test_eval_blend_disabled_via_env():
    with patch.dict(os.environ, {"MEGA_EVAL_BLEND": "0"}):
        assert eval_blend_enabled() is False
    with patch.dict(os.environ, {"MEGA_EVAL_BLEND": "false"}):
        assert eval_blend_enabled() is False


def test_weights_overridden_by_env():
    with patch.dict(os.environ, {
        "MEGA_EVAL_COUNT_W": "0.25",
        "MEGA_EVAL_CONTEXT_W": "0.40",
        "MEGA_EVAL_HARM_W": "2.0",
    }):
        w = reload_weights()
        assert isinstance(w, Weights)
        assert w.count == 0.25
        assert w.context == 0.40
        assert w.harm == 2.0
        assert current_weights().count == 0.25
    # Cleanup: reload back to defaults so other tests aren't affected.
    reload_weights()


def test_weights_invalid_env_falls_back_to_default():
    with patch.dict(os.environ, {"MEGA_EVAL_COUNT_W": "not-a-number"}):
        w = reload_weights()
        assert w.count == 0.10


# --------------------------------------------------------------------------
# related-verdict embedding signal (P2)
# --------------------------------------------------------------------------


def _baseline_score(meta: MegaMeta) -> float:
    """Helper: adjusted_score with no related-verdict input — what we
    get from the original three-layer composition."""
    q = np.zeros(3, dtype=np.float32)
    return adjusted_score(0.70, q, meta)


def test_related_signal_helpful_boosts_score():
    """A similar past HELPFUL verdict should push the final score up."""
    meta = MegaMeta()
    base = _baseline_score(meta)
    q = np.zeros(3, dtype=np.float32)
    boosted = adjusted_score(0.70, q, meta, related_helpful_max=0.9)
    assert boosted > base
    # Default W_RELATED = 0.10, so boost ≈ 0.1 × 0.9 = 0.09.
    assert boosted == pytest.approx(base + 0.09, abs=1e-6)


def test_related_signal_harmful_penalises_score():
    """A similar past HARMFUL verdict should push the final score down."""
    meta = MegaMeta()
    base = _baseline_score(meta)
    q = np.zeros(3, dtype=np.float32)
    penalised = adjusted_score(0.70, q, meta, related_harmful_max=0.9)
    assert penalised < base
    assert penalised == pytest.approx(base - 0.09, abs=1e-6)


def test_related_signal_helpful_and_harmful_cancel_when_equal():
    """If a similar HELPFUL and a similar HARMFUL verdict appear with
    equal cosine, the net contribution should be zero."""
    meta = MegaMeta()
    base = _baseline_score(meta)
    q = np.zeros(3, dtype=np.float32)
    neutral = adjusted_score(
        0.70, q, meta,
        related_helpful_max=0.75, related_harmful_max=0.75,
    )
    assert neutral == pytest.approx(base, abs=1e-6)


def test_related_signal_archived_skill_remains_sentinel():
    """Archived skills short-circuit to the negative sentinel; the
    related-verdict signal must not lift them back above zero."""
    meta = MegaMeta(status="archived")
    q = np.zeros(3, dtype=np.float32)
    s = adjusted_score(0.95, q, meta, related_helpful_max=1.0)
    assert s == -1.0


def test_related_signal_breakdown_keys_present():
    """The breakdown exposes both raw and weighted related-term fields
    so the `mega-tron why` view can show them."""
    from mega_tron.ranker import adjusted_score_breakdown

    meta = MegaMeta()
    q = np.zeros(3, dtype=np.float32)
    out = adjusted_score_breakdown(
        0.70, q, meta,
        related_helpful_max=0.8, related_harmful_max=0.2,
    )
    assert out["related_helpful_max"] == 0.8
    assert out["related_harmful_max"] == 0.2
    assert out["related_term_raw"] == pytest.approx(0.6, abs=1e-6)
    assert out["related_term_contribution"] == pytest.approx(0.06, abs=1e-6)
    assert "related" in out["weights"]


def test_related_signal_weight_env_override():
    """``MEGA_EVAL_RELATED_W`` swaps the related-signal weight."""
    with patch.dict(os.environ, {"MEGA_EVAL_RELATED_W": "0.5"}):
        w = reload_weights()
        assert w.related == 0.5
        meta = MegaMeta()
        q = np.zeros(3, dtype=np.float32)
        s = adjusted_score(0.70, q, meta, related_helpful_max=0.6)
        # 0.5 × 0.6 = 0.3 boost over the 0.70 cold-start.
        assert s == pytest.approx(1.0, abs=1e-6)
    reload_weights()
