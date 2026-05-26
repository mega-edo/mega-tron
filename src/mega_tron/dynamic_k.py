"""Static dynamic top-K policy (hybrid: z-space abstain + raw-cosine elbow).

Picks how many ranked skills to stage for a given query, based on the
shape of the top-20 score distribution. Deterministic — calibration
lives in a separate (future) module.

Why hybrid:

- **Z-space signals** (top1 in σ units, softmax entropy of z-scores)
  are *embedder-invariant*. We use them to decide whether to surface
  any skill at all and to detect ambiguous queries. A 0.30 raw cosine
  on SkillRet might mean "no match" but the same 0.30 on BGE-small
  means almost nothing — z-units strip out that scale mismatch.
- **Raw cosine gaps** stay inside one query's score list, so they're
  unaffected by cross-embedder scale either way. Cutting at the elbow
  of the *raw* gap series preserves the strong separation that good
  embedders (e.g. BGE) build in.

Branching (first match wins):

- **K=0 (uniform-null)** — z_top1 weak AND z entropy high → no skill
  is meaningfully relevant. Send nothing.
- **K=10 (very-ambiguous)** — z entropy very high → flatten case;
  surface a wide window so the model can disambiguate.
- **K=5 (ambiguous)** — z entropy moderately high → mild flatten.
- **K∈[k_min, k_max] (gap-cut)** — peaky distribution; cut at the
  argmax of consecutive raw-cosine gaps, clamped to [k_min, k_max].

K=1 is deliberately absent: a misranked top-1 leaves the host with no
fallback inside the staged window. ``k_min=2`` is the safety floor.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Sequence


# --- Silence penalty (continuous guard against silence-anchored loops) ---
#
# A skill that gets surfaced repeatedly without ever earning a verdict
# is the symptom of cosine-only anchoring: the router never sees a
# signal back from `<skill-used>` tags, so the same skill keeps
# winning the top slot turn after turn. The silence penalty is a small
# smooth down-shift applied to each candidate's score *only at K-
# selection time* — it does not change the displayed score, only
# whether the candidate makes it into K. As soon as any verdict
# (HELPFUL, HARMFUL, or NEUTRAL) lands, the penalty fades smoothly
# toward zero. Discrete cliff alternatives were rejected because they
# create a 19-vs-20 boundary problem.
#
# Formula:  α × log1p(surfaced) × max(0, 1 − verdict_density)
# where verdict_density = (helpful + harmful) / max(1, surfaced)
#
# Penalty scale at α=0.02 (default):
#   surfaced=10,  no verdicts  → 0.02 × 2.40 × 1.0 ≈ 0.048
#   surfaced=50,  no verdicts  → 0.02 × 3.93 × 1.0 ≈ 0.079
#   surfaced=50,  1 verdict    → 0.02 × 3.93 × 0.98 ≈ 0.077
#   surfaced=50,  5 verdicts   → 0.02 × 3.93 × 0.90 ≈ 0.071
#   surfaced=50, 25 verdicts   → 0.02 × 3.93 × 0.50 ≈ 0.039
#   surfaced=50, 50 verdicts   → 0 (fully disarmed)
#
# Same scale as `count_bonus_contribution` in ranker.py (W_COUNT=0.10
# × bonus∈[-0.5,+0.5] ≈ ±0.05) so a long-silent skill loses roughly
# what a consistently-HARMFUL skill would lose — strong enough to
# break top-1 ties, weak enough not to bury a genuinely-best-cosine
# match.
_SILENCE_PENALTY_ALPHA_DEFAULT = 0.02


def _silence_penalty_alpha() -> float:
    """Read α from ``MEGA_SILENCE_PENALTY_ALPHA``. Defaults to 0.02.
    Setting it to 0 disables the penalty entirely (parity with the
    pre-penalty K policy)."""
    raw = os.environ.get("MEGA_SILENCE_PENALTY_ALPHA")
    if raw is None or raw == "":
        return _SILENCE_PENALTY_ALPHA_DEFAULT
    try:
        return float(raw)
    except ValueError:
        return _SILENCE_PENALTY_ALPHA_DEFAULT


def silence_penalty(
    surfaced: int, helpful: int, harmful: int, *, alpha: float | None = None
) -> float:
    """Compute the silence penalty for one candidate. See module-level
    notes for the formula and scale rationale.

    Always returns a non-negative float. Cold-start candidates
    (``surfaced=0``) return 0 because ``log1p(0) = 0``."""
    if surfaced <= 0:
        return 0.0
    a = _silence_penalty_alpha() if alpha is None else alpha
    if a == 0.0:
        return 0.0
    evaluated = max(0, int(helpful)) + max(0, int(harmful))
    verdict_density = evaluated / max(1, int(surfaced))
    silence_factor = max(0.0, 1.0 - verdict_density)
    return a * math.log1p(int(surfaced)) * silence_factor


@dataclass(frozen=True)
class DynamicKConfig:
    """Threshold knobs for :func:`dynamic_k`.

    Defaults derive from a SkillRet + BGE-small cross-validation on
    100 in-distribution + 50 null queries each. Z-space thresholds are
    embedder-invariant and chosen for ~2-3% false-abstain. The optional
    ``abs_floor`` is embedder-specific (see :data:`EMBEDDER_PROFILES`)
    and gives the policy a hard nonsense filter when the embedder is
    known.
    """

    # --- Absolute raw cosine floor (embedder-specific) ---
    # If top1 falls below this, abstain immediately regardless of z
    # signals. None disables the floor (z-only abstain). The right
    # value depends on the embedder's scoring range — see
    # :data:`EMBEDDER_PROFILES`.
    abs_floor: float | None = None

    # Abstain branch — both z gates must trigger. Tuned for ~2-3%
    # false-abstain on in-distribution queries.
    abstain_z_top1: float = 1.8       # top1 must clear ~1.8σ to NOT abstain
    abstain_z_ent: float = 1.85       # entropy must stay below this to NOT abstain

    # Ambiguous branches — entropy-only.
    very_ambig_z_ent: float = 2.1     # above this → K=k_very_ambig
    ambig_z_ent: float = 1.7          # above this → K=k_ambig

    # K targets per branch.
    k_ambig: int = 5
    k_very_ambig: int = 10
    k_min: int = 2                    # safety floor (no K=1)
    k_max: int = 8                    # ceiling for gap-cut branch

    # Mechanics.
    elbow_window: int = 9             # consider gaps[0..elbow_window-1]
    entropy_window: int = 10          # entropy uses z[:entropy_window]


# Strength tiers for embedder-aware K bounds.
#
# The intuition: a weaker embedder ranks the gold skill lower on
# average (recall@1 ≈ 45% for BGE-small on SkillRet vs ~70% for
# SkillRet-Embedding). To compensate, weaker embedders need to surface
# a wider K so the gold is still inside the staged window. Stronger
# embedders can stage fewer skills without losing recall.
#
# Defaults derived from measured recall@K plateaus:
#   recall@10 ≈ 90% for strong embedders → k_max=10 suffices
#   recall@15 ≈ 79% for weak embedders   → k_max=20 needed for safety
#
# The threshold gates (z_top1, z_ent) come from DynamicKConfig defaults
# and are embedder-invariant — only the K bounds change per tier.

@dataclass(frozen=True)
class EmbedderTier:
    """K-bound profile for one strength tier."""

    k_min: int
    k_max: int
    k_ambig: int
    k_very_ambig: int


TIERS: dict[str, EmbedderTier] = {
    # MTEB ~63+ multilingual or domain fine-tuned. Recall@10 plateau,
    # so K=10 covers nearly every gold hit. Keep K small to save tokens.
    "strong": EmbedderTier(k_min=2, k_max=10, k_ambig=5, k_very_ambig=10),
    # MTEB ~60-62 general-purpose multilingual. Slightly more spread
    # needed since recall@10 is lower.
    "medium": EmbedderTier(k_min=3, k_max=15, k_ambig=7, k_very_ambig=12),
    # MTEB <60 or English-only small models. Recall@K climbs slowly,
    # so we have to surface more candidates to keep gold in the window.
    "weak": EmbedderTier(k_min=5, k_max=20, k_ambig=10, k_very_ambig=15),
}


# Embedder profile = (strength tier, raw cosine abs_floor).
# The tier picks K bounds; the floor catches nonsense queries before
# the z-only abstain branch. Floors are calibrated from the embedder's
# observed top1 distribution on in-dist vs null prompts
# (see docs/dynamic_k.md).
EMBEDDER_PROFILES: dict[str, tuple[str, float | None]] = {
    # Tuned 2026-05-21 against the 200-query routing benchmark:
    # abs_floor 0.60 maximises mean score (0.840 vs 0.772 at 0.45) by
    # dropping null FP from 0.66 to 0.08 while only nudging in-dist
    # down from 0.916 to 0.813 — the abstain-when-uncertain branch
    # outweighs the lost in-dist tail.
    "BAAI/bge-m3": ("strong", 0.60),
    # Tuned 2026-05-21: abs_floor 0.40 (was 0.35) lifts mean score to
    # 0.892 from 0.823 — null score 0.52→0.82 while in-dist holds at
    # 0.916. The 0.35 floor was too lenient against natural-language
    # null queries that happen to land in the 0.35-0.40 cosine band.
    "ThakiCloud/SKILLRET-Embedding-0.6B": ("strong", 0.40),
    # English-only small model (MTEB English 62.17). IN p10=0.73,
    # NULL p90=0.68 — clean separation; floor 0.70 catches ~96% of nulls
    # at ~2% false-abstain cost. Tier = weak because recall@K is low
    # (recall@5 ~66%, recall@10 ~75%) — needs wider K to compensate.
    "BAAI/bge-small-en-v1.5": ("weak", 0.70),
    # Unmeasured — estimates based on model family. Replace with
    # measured values when null benchmarks are run.
    #
    # BGE-large: same English-only family as bge-small but richer 1024d
    # embedding. IN p10 likely ~0.78 (vs small's 0.73), NULL p90 likely
    # ~0.70 (vs small's 0.68). Floor 0.65 catches most nulls while
    # keeping false-abstain ≤ ~5%.
    "BAAI/bge-large-en-v1.5": ("medium", 0.65),
    # Qwen3-Embedding-0.6B and SkillRet-0.6B share the same backbone.
    "Qwen/Qwen3-Embedding-0.6B": ("strong", 0.35),
    # Multilingual models tend to score lower than English-only models
    # because the embedding space is spread across 100+ languages.
    # e5-small: estimated IN median ~0.55, NULL median ~0.40.
    # e5-base : estimated IN median ~0.60, NULL median ~0.42.
    "intfloat/multilingual-e5-small": ("weak", 0.40),
    "intfloat/multilingual-e5-base": ("medium", 0.45),
}


def profile_for(embedder_model_id: str | None) -> DynamicKConfig:
    """Return a :class:`DynamicKConfig` tuned for the named embedder.

    Looks up the tier (K bounds) and absolute floor (nonsense filter)
    from :data:`EMBEDDER_PROFILES`. For unknown embedders, returns the
    bare default — z-only abstain, strong-tier K bounds.
    """
    profile = EMBEDDER_PROFILES.get(embedder_model_id or "")
    if profile is None:
        return DynamicKConfig()
    tier_name, abs_floor = profile
    tier = TIERS[tier_name]
    return DynamicKConfig(
        abs_floor=abs_floor,
        k_min=tier.k_min,
        k_max=tier.k_max,
        k_ambig=tier.k_ambig,
        k_very_ambig=tier.k_very_ambig,
    )


def softmax_entropy(zs: Sequence[float]) -> float:
    """Shannon entropy of softmax(zs), in nats.

    ``zs`` is expected to be z-scores (mean 0, std 1), so we apply
    softmax at temperature 1 — no extra sharpening needed. Returns 0
    on empty input.
    """
    if not zs:
        return 0.0
    m = max(zs)
    exps = [math.exp(z - m) for z in zs]
    z_sum = sum(exps)
    if z_sum <= 0.0:
        return 0.0
    h = 0.0
    for e in exps:
        p = e / z_sum
        if p > 0.0:
            h -= p * math.log(p)
    return h


def _clamp(x: int, lo: int, hi: int) -> int:
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x


def _zscores(scores: Sequence[float]) -> list[float]:
    """Z-score normalization with a small-sd guard."""
    n = len(scores)
    if n == 0:
        return []
    mu = sum(scores) / n
    var = sum((s - mu) ** 2 for s in scores) / n
    sd = max(math.sqrt(var), 1e-9)
    return [(s - mu) / sd for s in scores]


def dynamic_k(
    scores: Sequence[float],
    cfg: DynamicKConfig | None = None,
    *,
    candidate_stats: Sequence[tuple[int, int, int]] | None = None,
) -> tuple[int, str]:
    """Pick a top-K and a telemetry reason from a sorted score list.

    ``scores`` must be sorted descending (the router's natural output).
    Returns ``(k, reason)`` where ``reason`` is a stable branch
    identifier (e.g. ``"uniform-null"``, ``"gap-cut@2"``) used by
    telemetry and tests.

    ``candidate_stats``, when provided, must be order-aligned with
    ``scores``. Each tuple is ``(surfaced, helpful, harmful)`` for the
    candidate at that rank. A per-candidate silence penalty
    (see :func:`silence_penalty`) is subtracted from a *copy* of
    ``scores`` before the K-selection branches run — sparse-evaluated
    candidates may get edged out of K by a fresher peer. The returned
    score values surfaced to callers (via the router's natural rank()
    return) stay unchanged; only the K boundary is affected.

    Passing ``candidate_stats=None`` (the default) preserves the
    pre-penalty K policy exactly — used by tests and by the agentic
    rerank path where the LLM does its own selection.
    """
    if cfg is None:
        cfg = DynamicKConfig()
    if not scores:
        return 0, "empty"

    # Apply silence penalty to a private copy of scores. Sparse-
    # evaluated candidates lose a small smooth amount; verdict-positive
    # candidates lose ~0. The penalty is monotone-decreasing in
    # rank-order *only if* candidates with lower scores also have less
    # verdict signal — there's no guarantee, so after penalty the
    # score list may no longer be perfectly sorted. We re-sort the
    # working copy to keep the z-space and elbow analysis honest;
    # the K returned is still a top-K count from the caller's
    # already-ordered list.
    if candidate_stats is not None:
        adjusted: list[float] = []
        for s, stats in zip(scores, candidate_stats):
            surfaced, helpful, harmful = stats
            adjusted.append(float(s) - silence_penalty(surfaced, helpful, harmful))
        # Re-sort descending so the score-shape analysis (z, entropy,
        # elbow) sees a monotone sequence. We do NOT propagate the new
        # order back to the caller — the caller's `order` is the
        # authoritative ranking; we're only deciding how many to keep.
        scores = sorted(adjusted, reverse=True)

    # --- Step 1: compute z-space signals (embedder-invariant) ---
    zs = _zscores(scores)
    z_top1 = zs[0]
    z_ent = softmax_entropy(zs[: cfg.entropy_window])

    # --- Step 2: branch ---

    # A0. Hard absolute floor — embedder-specific, catches nonsense
    # queries before the z-only abstain. Disabled when abs_floor=None.
    if cfg.abs_floor is not None and scores[0] < cfg.abs_floor:
        return 0, "abs-floor"

    # A. Z-only abstain. Both conditions must hold — z_top1 alone
    # overlaps heavily between in-dist and null distributions.
    if z_top1 < cfg.abstain_z_top1 and z_ent > cfg.abstain_z_ent:
        return 0, "uniform-null"

    # B. Very ambiguous (distribution nearly uniform).
    if z_ent > cfg.very_ambig_z_ent:
        return _clamp(cfg.k_very_ambig, cfg.k_min, cfg.k_very_ambig), \
            "very-ambiguous"

    # C. Ambiguous (distribution flat).
    if z_ent > cfg.ambig_z_ent:
        return _clamp(cfg.k_ambig, cfg.k_min, cfg.k_very_ambig), "ambiguous"

    # D. Confident — elbow cut on RAW cosine gaps. Raw gaps preserve
    # the absolute separation that strong embedders build in (which z
    # normalization flattens away).
    window = min(cfg.elbow_window, len(scores) - 1)
    if window <= 0:
        return _clamp(1, cfg.k_min, cfg.k_max), "gap-cut@0"
    best_elbow = 0
    best_gap = scores[0] - scores[1]
    for i in range(1, window):
        gap = scores[i] - scores[i + 1]
        if gap > best_gap:
            best_gap = gap
            best_elbow = i
    return _clamp(best_elbow + 1, cfg.k_min, cfg.k_max), \
        f"gap-cut@{best_elbow}"


__all__ = [
    "DynamicKConfig",
    "EMBEDDER_PROFILES",
    "EmbedderTier",
    "TIERS",
    "dynamic_k",
    "profile_for",
    "silence_penalty",
    "softmax_entropy",
]
