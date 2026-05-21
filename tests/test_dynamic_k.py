"""Hybrid dynamic-K policy unit tests.

Scenarios use score distributions matching what we observed empirically
on SkillRet + BGE (see docs/dynamic_k.md for thresholds rationale).
Each test asserts the *branch* the policy lands in, not specific K
values when those values are sensitive to threshold tuning.
"""
from __future__ import annotations

from mega_tron.dynamic_k import (
    EMBEDDER_PROFILES,
    TIERS,
    DynamicKConfig,
    EmbedderTier,
    _zscores,
    dynamic_k,
    profile_for,
    softmax_entropy,
)


# --- Edge cases -------------------------------------------------------------


def test_empty_scores_returns_zero_empty():
    assert dynamic_k([]) == (0, "empty")


def test_single_score_floors_to_k_min():
    k, reason = dynamic_k([0.65])
    assert k >= 2
    assert reason.startswith("gap-cut")


# --- Helper sanity ----------------------------------------------------------


def test_zscores_have_zero_mean_unit_std():
    z = _zscores([0.78, 0.62, 0.58, 0.41, 0.38])
    assert abs(sum(z) / len(z)) < 1e-6
    var = sum(x * x for x in z) / len(z)
    assert abs(var - 1.0) < 1e-6


def test_softmax_entropy_higher_for_flat_input():
    flat = [0.0] * 10
    peaky = [3.0] + [0.0] * 9
    assert softmax_entropy(flat) > softmax_entropy(peaky)


def test_softmax_entropy_empty_is_zero():
    assert softmax_entropy([]) == 0.0


# --- Abstain branch (uniform-null) -----------------------------------------


def test_uniform_null_fires_on_truly_flat_distribution():
    """A perfectly uniform top-10: z_top1=0, z_ent ≈ ln(10) ≈ 2.30
    → both abstain gates trip."""
    scores = [0.30] * 10
    k, reason = dynamic_k(scores)
    assert k == 0
    assert reason == "uniform-null"


def test_confident_top1_blocks_abstain():
    """Strong peak (z_top1 well above 1.8) must NOT abstain even when
    the tail is uniform."""
    scores = [0.85] + [0.30] * 9
    k, reason = dynamic_k(scores)
    assert k > 0
    assert reason != "uniform-null"


# --- Ambiguous branches ----------------------------------------------------


def test_ambiguous_branch_via_config_override():
    """Use a custom config to land squarely in the ambiguous branch
    regardless of the natural z_ent of the scores."""
    scores = [0.55, 0.53, 0.51, 0.50, 0.49, 0.49, 0.48, 0.48, 0.47, 0.47]
    cfg = DynamicKConfig(
        ambig_z_ent=1.0,        # force ambig to trip
        very_ambig_z_ent=10.0,  # but not very-ambig
        abstain_z_top1=-100.0,  # disable abstain
    )
    k, reason = dynamic_k(scores, cfg=cfg)
    assert reason == "ambiguous"
    assert k == cfg.k_ambig


def test_very_ambiguous_branch_via_config_override():
    """Same scores but very-ambig gate dropped → very-ambig fires."""
    scores = [0.55, 0.53, 0.51, 0.50, 0.49, 0.49, 0.48, 0.48, 0.47, 0.47]
    cfg = DynamicKConfig(
        very_ambig_z_ent=1.0,   # force very-ambig
        abstain_z_top1=-100.0,
    )
    k, reason = dynamic_k(scores, cfg=cfg)
    assert reason == "very-ambiguous"
    assert k == cfg.k_very_ambig


# --- Confident branch (raw-gap elbow) --------------------------------------


def test_confident_gap_cut_with_clean_peak():
    """Three clear winners then a flat near-zero tail. With ambig
    gates disabled, the elbow lands at index 2."""
    scores = [0.85, 0.80, 0.75, 0.05, 0.04, 0.03, 0.03, 0.02, 0.02, 0.01]
    cfg = DynamicKConfig(
        ambig_z_ent=10.0,
        very_ambig_z_ent=10.0,
        abstain_z_top1=-100.0,
    )
    k, reason = dynamic_k(scores, cfg=cfg)
    assert reason == "gap-cut@2"
    assert k == 3


def test_overwhelming_top1_clamps_to_k_min():
    """Cliff at index 0 → would be K=1; clamp to k_min=2."""
    scores = [0.90, 0.05, 0.05, 0.04, 0.04, 0.03, 0.03, 0.02, 0.02, 0.01]
    cfg = DynamicKConfig(
        ambig_z_ent=10.0, very_ambig_z_ent=10.0, abstain_z_top1=-100.0,
    )
    k, reason = dynamic_k(scores, cfg=cfg)
    assert reason == "gap-cut@0"
    assert k == 2  # k_min default


def test_far_elbow_clamps_to_k_max():
    """Elbow at index 8 (past the k_max=8 ceiling) → clamp to 8."""
    scores = [0.95, 0.94, 0.93, 0.92, 0.91, 0.90, 0.89, 0.88, 0.87, 0.10]
    cfg = DynamicKConfig(
        ambig_z_ent=10.0, very_ambig_z_ent=10.0, abstain_z_top1=-100.0,
    )
    k, reason = dynamic_k(scores, cfg=cfg)
    assert reason == "gap-cut@8"
    assert k == 8


# --- Abs floor (embedder-specific hard nonsense filter) --------------------


def test_abs_floor_abstains_below_threshold():
    """When top1 is below abs_floor, abstain immediately regardless of
    z signals."""
    cfg = DynamicKConfig(abs_floor=0.35)
    # Top1 below floor; z_top1 is high because the gap to the rest is
    # large in relative terms, but the absolute score is junk.
    scores = [0.20, 0.10, 0.09, 0.09, 0.08, 0.08, 0.07, 0.07, 0.06, 0.06]
    k, reason = dynamic_k(scores, cfg=cfg)
    assert k == 0
    assert reason == "abs-floor"


def test_abs_floor_does_not_fire_when_top1_passes():
    """A query with top1 well above abs_floor should not abstain via
    the absolute path (z gates still apply)."""
    cfg = DynamicKConfig(abs_floor=0.30, ambig_z_ent=10.0,
                         very_ambig_z_ent=10.0, abstain_z_top1=-100.0)
    scores = [0.85, 0.80, 0.75, 0.05, 0.04, 0.03, 0.03, 0.02, 0.02, 0.01]
    k, reason = dynamic_k(scores, cfg=cfg)
    assert k > 0
    assert reason != "abs-floor"


def test_abs_floor_disabled_by_default():
    """The bare DynamicKConfig has no absolute floor — only the
    profile_for() factory adds one for known embedders."""
    cfg = DynamicKConfig()
    assert cfg.abs_floor is None


def test_profile_for_returns_floor_when_embedder_known():
    cfg = profile_for("BAAI/bge-small-en-v1.5")
    _, expected_floor = EMBEDDER_PROFILES["BAAI/bge-small-en-v1.5"]
    assert cfg.abs_floor == expected_floor


def test_profile_for_returns_default_when_embedder_unknown():
    cfg = profile_for("some-experimental/embedder-v0")
    assert cfg.abs_floor is None
    # Unknown embedders inherit DynamicKConfig defaults (strong-tier K
    # bounds — see EmbedderTier 'strong' in TIERS).
    assert cfg.k_min == DynamicKConfig().k_min
    assert cfg.k_max == DynamicKConfig().k_max


def test_profile_for_handles_none_model_id():
    cfg = profile_for(None)
    assert cfg.abs_floor is None


# --- Tiering (strong / medium / weak) --------------------------------------


def test_strong_tier_keeps_k_max_small():
    """Strong embedders (BGE-M3, SkillRet) have recall@10 plateaus
    around 90%, so wider K wastes tokens. k_max should stay at 10."""
    cfg = profile_for("BAAI/bge-m3")
    assert cfg.k_min == 2
    assert cfg.k_max == 10


def test_weak_tier_widens_k_max():
    """Weak embedders (BGE-small, e5-small) have lower recall@K, so
    we need a wider window to keep the gold in the staged set."""
    cfg = profile_for("BAAI/bge-small-en-v1.5")
    assert cfg.k_min == 5
    assert cfg.k_max == 20
    assert cfg.k_ambig == 10
    assert cfg.k_very_ambig == 15


def test_medium_tier_in_the_middle():
    cfg = profile_for("BAAI/bge-large-en-v1.5")
    assert cfg.k_min == 3
    assert cfg.k_max == 15


def test_weak_tier_with_weak_query_widens_dynamic_k():
    """End-to-end: a peaky distribution under the weak tier should
    return K≥k_min=5 even when the elbow lands at 0."""
    scores = [0.90, 0.05, 0.05, 0.04, 0.04, 0.03, 0.03, 0.02, 0.02, 0.01]
    cfg = profile_for("BAAI/bge-small-en-v1.5")
    # Override ambig gates to keep us in Confident branch for this test.
    cfg = DynamicKConfig(
        abs_floor=None,  # disable floor so 0.90 > floor doesn't matter
        k_min=cfg.k_min,
        k_max=cfg.k_max,
        k_ambig=cfg.k_ambig,
        k_very_ambig=cfg.k_very_ambig,
        ambig_z_ent=10.0,
        very_ambig_z_ent=10.0,
        abstain_z_top1=-100.0,
    )
    k, reason = dynamic_k(scores, cfg=cfg)
    assert reason == "gap-cut@0"
    assert k == 5  # weak tier k_min


def test_tier_dict_has_all_three_tiers():
    assert set(TIERS.keys()) == {"strong", "medium", "weak"}


def test_each_tier_has_widening_k_max():
    """Sanity: weak tier must have k_max ≥ medium ≥ strong."""
    assert TIERS["strong"].k_max <= TIERS["medium"].k_max
    assert TIERS["medium"].k_max <= TIERS["weak"].k_max


# --- Custom config plumbing ------------------------------------------------


def test_custom_k_min_floor_respected():
    """If user raises k_min, even a singleton score returns that floor."""
    cfg = DynamicKConfig(
        k_min=4, ambig_z_ent=10.0, very_ambig_z_ent=10.0,
        abstain_z_top1=-100.0,
    )
    scores = [0.90, 0.05, 0.05, 0.04, 0.04, 0.03, 0.03, 0.02, 0.02, 0.01]
    k, _ = dynamic_k(scores, cfg=cfg)
    assert k == 4
