"""Table-driven tests for partial-credit recall@K scoring (DESIGN.md §3.2).

The rule: score = |G ∩ P| / |G| with partial credit on multi-gold queries.
Null queries: 1.0 if predicted is empty, else 0.0.
"""
from __future__ import annotations

import math

import pytest

from benchmarks.routing.scoring import (
    mean_score,
    per_stratum_mean_score,
    score_query,
)


def _approx(a: float, b: float, tol: float = 1e-6) -> bool:
    return math.isclose(a, b, abs_tol=tol)


@pytest.mark.parametrize(
    "name, gold, pred, expected_hit_count, expected_score",
    [
        # === k=1 cases — single gold ===
        ("k1_hit",            ["G07"], ["G07"],             1, 1.0),
        ("k1_hit_with_extra", ["G07"], ["G07", "G2", "G9"], 1, 1.0),
        ("k1_miss",           ["G07"], ["G08"],             0, 0.0),
        ("k1_empty_pred",     ["G07"], [],                  0, 0.0),

        # === k=2 cases — partial credit kicks in ===
        ("k2_full",          ["G07", "G14"], ["G07", "G14"],        2, 1.0),
        ("k2_full_w_extra",  ["G07", "G14"], ["G07", "G14", "G99"], 2, 1.0),
        ("k2_half",          ["G07", "G14"], ["G07"],               1, 0.5),
        ("k2_half_w_extra",  ["G07", "G14"], ["G07", "G99"],        1, 0.5),
        ("k2_zero",          ["G07", "G14"], ["G88", "G99"],        0, 0.0),
        ("k2_empty",         ["G07", "G14"], [],                    0, 0.0),

        # === k=3 cases — partial credit at 1/3 and 2/3 ===
        ("k3_full",     ["G07", "G14", "G22"], ["G07", "G14", "G22"],         3, 1.0),
        ("k3_two_of_3", ["G07", "G14", "G22"], ["G07", "G14"],                2, 2/3),
        ("k3_two_w_fp", ["G07", "G14", "G22"], ["G07", "G14", "G99", "G88"],  2, 2/3),
        ("k3_one_of_3", ["G07", "G14", "G22"], ["G07"],                       1, 1/3),
        ("k3_zero",     ["G07", "G14", "G22"], ["G88", "G99"],                0, 0.0),

        # === null cases ===
        ("null_correct_abstain", [], [],            0, 1.0),
        ("null_one_fp",          [], ["G07"],       0, 0.0),
        ("null_many_fp",         [], ["G07", "G14"],0, 0.0),
    ],
)
def test_score_query_partial_credit(name, gold, pred, expected_hit_count, expected_score):
    s = score_query("q_test", gold, pred)
    assert s.hit_count == expected_hit_count, f"{name}: hit_count={s.hit_count} != {expected_hit_count}"
    assert _approx(s.score, expected_score), f"{name}: score={s.score} != {expected_score}"


def test_mean_score_includes_all_strata():
    scores = [
        score_query("q1", ["G1"], ["G1"]),                 # 1.0
        score_query("q2", ["G1", "G2"], ["G1"]),           # 0.5
        score_query("q3", ["G1", "G2", "G3"], ["G1"]),     # 1/3
        score_query("q4", [], []),                          # 1.0 (null OK)
        score_query("q5", [], ["G1"]),                      # 0.0 (null FP)
    ]
    # (1.0 + 0.5 + 0.333... + 1.0 + 0.0) / 5 = 2.833.../5 = 0.5667
    assert _approx(mean_score(scores), 17/30)


def test_mean_score_empty():
    assert mean_score([]) == 0.0


def test_per_stratum_mean_score():
    scores = [
        score_query("q1", ["G1"], ["G1"]),               # k1 stratum, 1.0
        score_query("q2", ["G1", "G2"], ["G1"]),         # k2 stratum, 0.5
        score_query("q3", ["G1", "G2", "G3"], ["G2"]),   # k3 stratum, 1/3
        score_query("q4", [], []),                        # null stratum, 1.0
    ]
    strat = {"q1": "k1", "q2": "k2", "q3": "k3", "q4": "null"}
    result = per_stratum_mean_score(scores, strat)
    assert _approx(result["k1"], 1.0)
    assert _approx(result["k2"], 0.5)
    assert _approx(result["k3"], 1/3)
    assert _approx(result["null"], 1.0)


def test_score_query_returns_frozensets():
    """Gold and predicted sets are frozenset for hashability."""
    s = score_query("q", ["G1", "G2"], ["G2", "G3"])
    assert isinstance(s.gold_set, frozenset)
    assert isinstance(s.predicted_set, frozenset)
    assert s.gold_set == {"G1", "G2"}
    assert s.predicted_set == {"G2", "G3"}


def test_extra_predictions_no_penalty():
    """The rule: predictions BEYOND the golds incur no penalty."""
    # k=1 query, top-K=10 with the gold included → still 1.0
    s = score_query("q", ["G7"], ["G7"] + [f"G{i}" for i in range(20, 29)])
    assert s.score == 1.0


def test_duplicate_predictions_collapse():
    """Predicted list with dupes collapses via frozenset semantics."""
    s = score_query("q", ["G1"], ["G1", "G1", "G2", "G2"])
    assert s.hit_count == 1
    assert s.score == 1.0
