"""Per-query Recall@K scoring with partial credit (DESIGN.md §3.2).

The rule: **score = fraction of gold skills present in the top-K**.
Partial credit when only some of a multi-gold query's skills landed
in the top-K. Extra predictions beyond the golds incur no penalty —
the model downstream picks from the top-K, so getting golds in front
of it is the only thing the router is graded on.

    if |G| > 0:
        score = |G ∩ P| / |G|          # recall@K with partial credit
    elif |G| == 0 and |P| == 0:
        score = 1                       # correct null abstain
    else:  # |G| == 0 and |P| > 0
        score = 0                       # false positive on null

Examples:
    k=1: golds={G7}, pred={G7, G2, G9}     → 1/1 = 1.0
    k=1: golds={G7}, pred={G2, G9}         → 0/1 = 0.0
    k=2: golds={G7, G14}, pred={G7, G2}    → 1/2 = 0.5
    k=3: golds={G7, G14, G22}, pred={G7, G14, G99}
                                            → 2/3 ≈ 0.667
    k=3: golds={G7, G14, G22}, pred=[]     → 0/3 = 0.0
    null: golds={}, pred={}                 → 1.0
    null: golds={}, pred={G7}              → 0.0

Mean score is the arithmetic mean across the full query set.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class QueryScore:
    """Per-query scoring result."""
    query_id: str
    gold_set: frozenset[str]
    predicted_set: frozenset[str]
    score: float  # |G ∩ P| / |G| for in-dist; 1.0 / 0.0 for null
    hit_count: int  # |G ∩ P|
    gold_count: int  # |G|


def score_query(
    query_id: str,
    gold_set: Iterable[str],
    predicted_set: Iterable[str],
) -> QueryScore:
    """Score a single query with partial-credit recall@K. Pure function."""
    g = frozenset(gold_set)
    p = frozenset(predicted_set)
    hit_count = len(g & p)

    if not g and not p:
        score = 1.0  # correct null abstain
    elif not g and p:
        score = 0.0  # false positive on null
    elif g and not p:
        score = 0.0  # false negative on in-dist
    else:
        score = hit_count / len(g)  # partial-credit recall

    return QueryScore(
        query_id=query_id,
        gold_set=g,
        predicted_set=p,
        score=score,
        hit_count=hit_count,
        gold_count=len(g),
    )


def mean_score(scores: Iterable[QueryScore]) -> float:
    """Arithmetic mean of per-query partial-credit recall."""
    scores_list = list(scores)
    if not scores_list:
        return 0.0
    return sum(s.score for s in scores_list) / len(scores_list)


def per_stratum_mean_score(
    scores: Iterable[QueryScore],
    stratify: dict[str, str],
) -> dict[str, float]:
    """Mean recall split by stratum (e.g. ``k1`` / ``k2`` / ``k3`` / ``null``)."""
    buckets: dict[str, list[QueryScore]] = {}
    for s in scores:
        st = stratify.get(s.query_id, "unknown")
        buckets.setdefault(st, []).append(s)
    return {st: mean_score(items) for st, items in sorted(buckets.items())}


__all__ = [
    "QueryScore",
    "score_query",
    "mean_score",
    "per_stratum_mean_score",
]
