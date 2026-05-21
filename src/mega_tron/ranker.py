"""Evaluation-aware score blending — pure functions, isolated from Cache+Router.

Decorates a base semantic cosine score with three additive terms drawn from
per-skill `mega_meta:` evidence accumulated by the Stop-hook self-evaluation:

    adjusted = (semantic
                + W_COUNT   * beta_smoothed_rate_bonus(meta)
                + W_CONTEXT * (helpful_match - W_HARM * harm_match)
               ) * status_multiplier(status)

Cold start (h=ha=0, no contexts): adjusted == semantic, so unevaluated skills
are never penalized vs evaluated peers.

Weights are read from `MEGA_EVAL_*` env vars at module-import time; tests
call :func:`reload_weights` to override.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

    from mega_tron.verdicts.mega_meta import MegaMeta


# Defaults: conservative — semantic stays the dominant signal.
_DEFAULT_W_COUNT = 0.10
_DEFAULT_W_CONTEXT = 0.15
_DEFAULT_W_HARM = 1.5
_DEFAULT_W_RELATED = 0.10
"""Weight on the per-verdict embedding signal. The contribution is
``W_RELATED × (max_helpful_cos − max_harmful_cos)``, computed at the
:class:`Router` level by looking up the top-K most semantically similar
past verdicts via :class:`VerdictEmbeddingsStore`. Zero when no related
verdicts exist — backward-compatible with installs that have no
verdict-embedding store yet.

Why 0.10: same scale as ``W_COUNT`` so a single high-similarity HELPFUL
verdict ≈ a fully-warm helpful_count signal, and a single
high-similarity HARMFUL verdict ≈ a partial penalty. Tunable via
``MEGA_EVAL_RELATED_W``."""
_CONFIDENCE_RAMP = 10.0  # invocations beyond which count_bonus is at full strength
_ARCHIVED_SENTINEL = -1.0  # ranks below any real score, effectively excludes
_SUSPECT_MULT = 0.5


@dataclass
class Weights:
    count: float
    context: float
    harm: float
    related: float = _DEFAULT_W_RELATED


def _read_float_env(key: str, default: float) -> float:
    raw = os.environ.get(key, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _read_bool_env(key: str, default: bool) -> bool:
    raw = os.environ.get(key, "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


def load_weights() -> Weights:
    """Read W_COUNT / W_CONTEXT / W_HARM / W_RELATED from env, falling
    back to defaults."""
    return Weights(
        count=_read_float_env("MEGA_EVAL_COUNT_W", _DEFAULT_W_COUNT),
        context=_read_float_env("MEGA_EVAL_CONTEXT_W", _DEFAULT_W_CONTEXT),
        harm=_read_float_env("MEGA_EVAL_HARM_W", _DEFAULT_W_HARM),
        related=_read_float_env("MEGA_EVAL_RELATED_W", _DEFAULT_W_RELATED),
    )


_WEIGHTS = load_weights()


def reload_weights() -> Weights:
    """Re-read env (used by tests that mutate `os.environ`)."""
    global _WEIGHTS
    _WEIGHTS = load_weights()
    return _WEIGHTS


def current_weights() -> Weights:
    return _WEIGHTS


def eval_blend_enabled() -> bool:
    """Master switch — `MEGA_EVAL_BLEND=0` reverts to pure semantic."""
    return _read_bool_env("MEGA_EVAL_BLEND", True)


def count_bonus(helpful: int, harmful: int) -> float:
    """Beta-Bernoulli smoothed rate bonus.

    Cold start (n=0): 0. Otherwise: (smoothed_rate - 0.5) * min(1, n/10), so
    a fresh evaluation moves the needle a little and many evaluations move
    it more, but never out of [-0.5, +0.5].
    """
    n = helpful + harmful
    if n <= 0:
        return 0.0
    rate = (helpful + 1.0) / (n + 2.0)  # Beta(1,1) prior
    confidence = min(1.0, n / _CONFIDENCE_RAMP)
    return (rate - 0.5) * confidence


def context_term(
    query_vec: "np.ndarray",
    helpful_ctx_embs: "np.ndarray | None",
    harmful_ctx_embs: "np.ndarray | None",
    w_harm: float | None = None,
) -> float:
    """Best-case match against past helpful contexts minus asymmetric penalty
    against past harmful contexts.

    Each `*_embs` is shape (k, dim) of L2-normalized vectors (cosine = dot),
    or None when the skill has no contexts of that polarity.
    """
    import numpy as np

    help_match = 0.0
    harm_match = 0.0
    if helpful_ctx_embs is not None and helpful_ctx_embs.size > 0:
        # query_vec shape: (dim,) or (1, dim)
        q = query_vec.reshape(-1)
        sims = helpful_ctx_embs @ q
        help_match = float(np.max(sims))
    if harmful_ctx_embs is not None and harmful_ctx_embs.size > 0:
        q = query_vec.reshape(-1)
        sims = harmful_ctx_embs @ q
        harm_match = float(np.max(sims))
    w = _WEIGHTS.harm if w_harm is None else w_harm
    return help_match - w * harm_match


def status_multiplier(status: str | None) -> float:
    """Final gate: archived→sentinel, suspect→halved, active(or unknown)→1."""
    if status == "archived":
        return _ARCHIVED_SENTINEL
    if status == "suspect":
        return _SUSPECT_MULT
    return 1.0


def adjusted_score(
    semantic: float,
    query_vec: "np.ndarray",
    meta: "MegaMeta",
    helpful_ctx_embs: "np.ndarray | None" = None,
    harmful_ctx_embs: "np.ndarray | None" = None,
    *,
    related_helpful_max: float = 0.0,
    related_harmful_max: float = 0.0,
) -> float:
    """Compose all four layers. Pure function — no I/O, no globals besides weights."""
    return adjusted_score_breakdown(
        semantic, query_vec, meta, helpful_ctx_embs, harmful_ctx_embs,
        related_helpful_max=related_helpful_max,
        related_harmful_max=related_harmful_max,
    )["final"]


def adjusted_score_breakdown(
    semantic: float,
    query_vec: "np.ndarray",
    meta: "MegaMeta",
    helpful_ctx_embs: "np.ndarray | None" = None,
    harmful_ctx_embs: "np.ndarray | None" = None,
    *,
    related_helpful_max: float = 0.0,
    related_harmful_max: float = 0.0,
) -> dict:
    """Same composition as :func:`adjusted_score`, but returns each contribution.

    Used by the `mega-tron why` CLI to surface how a skill earned its
    final rank. Output keys (all floats unless noted):

      - ``semantic``: input cosine (passed through verbatim)
      - ``count_bonus_raw``: unweighted Beta-smoothed rate bonus
      - ``count_bonus_contribution``: ``W_COUNT × count_bonus_raw``
      - ``helpful_match`` / ``harmful_match``: best cos(query, ctx) per polarity
      - ``context_term_raw``: ``helpful_match − W_HARM × harmful_match``
      - ``context_term_contribution``: ``W_CONTEXT × context_term_raw``
      - ``related_helpful_max`` / ``related_harmful_max``: best cos(query,
        past verdict reason) the Router looked up for this skill —
        positive for HELPFUL verdicts, positive for HARMFUL verdicts
        (each is the max cosine; we subtract harm at compose time).
      - ``related_term_raw``: ``related_helpful_max − related_harmful_max``
      - ``related_term_contribution``: ``W_RELATED × related_term_raw``
      - ``status``: "active" | "suspect" | "archived"
      - ``status_multiplier``: 1.0 / 0.5 / -1.0 sentinel
      - ``final``: the adjusted_score the router would use

    For archived skills the breakdown short-circuits with the sentinel; the
    additive terms are reported as 0 so the table reads cleanly.
    """
    import numpy as np

    weights = _WEIGHTS
    weights_dict = {
        "count": weights.count,
        "context": weights.context,
        "harm": weights.harm,
        "related": weights.related,
    }
    if meta.status == "archived":
        return {
            "semantic": float(semantic),
            "count_bonus_raw": 0.0,
            "count_bonus_contribution": 0.0,
            "helpful_match": 0.0,
            "harmful_match": 0.0,
            "context_term_raw": 0.0,
            "context_term_contribution": 0.0,
            "related_helpful_max": 0.0,
            "related_harmful_max": 0.0,
            "related_term_raw": 0.0,
            "related_term_contribution": 0.0,
            "status": "archived",
            "status_multiplier": _ARCHIVED_SENTINEL,
            "final": _ARCHIVED_SENTINEL,
            "weights": weights_dict,
            "helpful_count": meta.helpful_count,
            "harmful_count": meta.harmful_count,
        }

    cb_raw = count_bonus(meta.helpful_count, meta.harmful_count)

    help_match = 0.0
    harm_match = 0.0
    q = query_vec.reshape(-1)
    if helpful_ctx_embs is not None and helpful_ctx_embs.size > 0:
        help_match = float(np.max(helpful_ctx_embs @ q))
    if harmful_ctx_embs is not None and harmful_ctx_embs.size > 0:
        harm_match = float(np.max(harmful_ctx_embs @ q))
    ctx_raw = help_match - weights.harm * harm_match

    # Verdict-embedding signal: query semantic similarity to past
    # verdict reasons specifically about this skill, separated by
    # polarity. Net contribution = HELPFUL pull − HARMFUL push.
    related_raw = float(related_helpful_max) - float(related_harmful_max)

    pre_status = (
        float(semantic)
        + weights.count * cb_raw
        + weights.context * ctx_raw
        + weights.related * related_raw
    )
    mult = status_multiplier(meta.status)
    return {
        "semantic": float(semantic),
        "count_bonus_raw": cb_raw,
        "count_bonus_contribution": weights.count * cb_raw,
        "helpful_match": help_match,
        "harmful_match": harm_match,
        "context_term_raw": ctx_raw,
        "context_term_contribution": weights.context * ctx_raw,
        "related_helpful_max": float(related_helpful_max),
        "related_harmful_max": float(related_harmful_max),
        "related_term_raw": related_raw,
        "related_term_contribution": weights.related * related_raw,
        "status": meta.status,
        "status_multiplier": mult,
        "final": pre_status * mult,
        "weights": weights_dict,
        "helpful_count": meta.helpful_count,
        "harmful_count": meta.harmful_count,
    }
