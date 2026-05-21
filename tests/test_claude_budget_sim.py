"""Tests for the Claude Code 1% description-budget simulator.

Source contract: https://code.claude.com/docs/en/skills#skill-descriptions-are-cut-short

We exercise the two stacked caps:
  - per-skill: description + when_to_use truncated at max_skill_chars
  - total: descriptions dropped to "name-only" when summed tokens > budget,
    least-invoked first.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make sibling module importable (benchmarks/hosts/claude/ is not a package).
sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "benchmarks" / "hosts" / "claude"),
)

from claude_budget_sim import (  # noqa: E402
    DEFAULT_BUDGET_FRACTION,
    DEFAULT_MAX_SKILL_DESCRIPTION_CHARS,
    Skill,
    render_catalog,
)


# --- Helpers ----------------------------------------------------------------


def _make_skill(name: str, desc_chars: int, invocations: int = 0) -> Skill:
    return Skill(
        name=name,
        description="x" * desc_chars,
        when_to_use="",
        invocation_count=invocations,
    )


# --- Per-skill cap ----------------------------------------------------------


def test_per_skill_cap_truncates_long_description():
    s = _make_skill("big", desc_chars=10_000)
    result = render_catalog([s], context_window=1_000_000)  # huge budget, no overflow
    render = result.get("big")
    assert render.final_state == "on"
    # Description visible but truncated to the 1,536 char cap.
    assert len(render.description_text) == DEFAULT_MAX_SKILL_DESCRIPTION_CHARS


def test_per_skill_cap_keeps_short_description():
    s = _make_skill("small", desc_chars=200)
    result = render_catalog([s], context_window=1_000_000)
    assert result.get("small").final_state == "on"
    assert len(result.get("small").description_text) == 200


# --- Total budget overflow → LRU drop --------------------------------------


def test_50_skills_at_1pct_200k_fit_with_room():
    """50 skills × ~150-char descriptions fit easily inside 1% of 200K."""
    skills = [_make_skill(f"sk_{i:02d}", desc_chars=150) for i in range(50)]
    result = render_catalog(skills, context_window=200_000)
    assert result.skills_on == 50
    assert result.skills_name_only == 0
    # 2000-token budget, ~30-50 tokens per skill (name+150-char desc) → well under.
    assert result.catalog_tokens < 2_000


def test_500_skills_overflow_drops_least_invoked_first():
    """500 skills × heavy descriptions exceeds 1% × 200K (=2,000 tok). The
    least-invoked are dropped to name-only first while names always stay.

    Note: at 128K the *names alone* (500 × ~3 tokens = 1,500) already
    exceed the 1,280-token budget, so the simulator drops every
    description — which is itself a valid signal but doesn't exercise
    the LRU ordering. We use 200K context (matches Sonnet 4.6 / Opus 4.7
    defaults) so a partial set survives and we can verify ordering.
    """
    skills = []
    for i in range(500):
        # i==0 is invoked the LEAST (count=0), i==499 the MOST.
        skills.append(_make_skill(f"sk_{i:03d}", desc_chars=200, invocations=i))
    result = render_catalog(skills, context_window=200_000)
    # Overflow → not everyone keeps description (but >0 survive at 200K).
    assert 0 < result.skills_on < 500
    assert result.skills_name_only > 0
    # The most-invoked ones should keep theirs.
    assert result.get("sk_499").final_state == "on"
    # The least-invoked one should be downgraded.
    assert result.get("sk_000").final_state == "name-only"
    budget = int(200_000 * DEFAULT_BUDGET_FRACTION)
    # Cost is at or just under budget (the drop loop stops once we fit).
    assert result.catalog_tokens <= budget + 200


def test_name_only_skill_has_zero_description_tokens():
    """When dropped to name-only, description_tokens MUST be 0 (not the
    pre-drop value)."""
    skills = []
    # 1 huge skill (invocation=0 → drops first) + 1 small skill that stays.
    skills.append(_make_skill("big", desc_chars=10_000, invocations=0))
    skills.append(_make_skill("small", desc_chars=50, invocations=10))
    # Tiny budget so big must drop.
    result = render_catalog(skills, context_window=10_000)  # 100-token budget
    big = result.get("big")
    small = result.get("small")
    assert big.final_state == "name-only"
    assert big.description_tokens == 0
    assert small.final_state == "on"
    assert small.description_tokens > 0


def test_tie_break_alphabetical_for_equal_invocations():
    """When two skills have the same invocation count (default 0), the
    drop order should be alphabetical for determinism.

    Budget chosen so exactly one description fits: a single 2000-char
    description is ~250 tokens; budget = 300 (= 30_000 × 0.01) accepts
    one description plus both names.
    """
    skills = [
        _make_skill("zebra", desc_chars=2_000, invocations=0),
        _make_skill("apple", desc_chars=2_000, invocations=0),
    ]
    result = render_catalog(skills, context_window=30_000)
    # `apple` comes first alphabetically (with equal invocation counts)
    # → drops first.
    assert result.get("apple").final_state == "name-only"
    assert result.get("zebra").final_state == "on"


# --- Expected_skill state tracking -----------------------------------------


def test_expected_skill_rank_uses_alphabetical_listing_order():
    skills = [
        _make_skill("zeta", desc_chars=50),
        _make_skill("alpha", desc_chars=50),
        _make_skill("middle", desc_chars=50),
    ]
    result = render_catalog(skills, context_window=200_000, expected_skill="middle")
    # Listing is alphabetical: alpha (rank 1), middle (rank 2), zeta (rank 3).
    assert result.expected_skill_rank == 2
    assert result.expected_skill_state == "on"


def test_expected_skill_missing_rank_is_none():
    skills = [_make_skill("alpha", desc_chars=50)]
    result = render_catalog(skills, context_window=200_000, expected_skill="not-installed")
    assert result.expected_skill_rank is None
    assert result.expected_skill_state is None


def test_expected_skill_dropped_state_is_name_only():
    """When the expected skill drops to name-only, that's the model
    losing the keywords it needs to pick it — the failure mode the
    router fixes."""
    # 1 small + 1 huge expected skill. expected drops first (invocations=0 vs 100).
    skills = [
        _make_skill("expected", desc_chars=2_000, invocations=0),
        _make_skill("other", desc_chars=50, invocations=100),
    ]
    result = render_catalog(
        skills, context_window=10_000, expected_skill="expected"
    )
    assert result.expected_skill_state == "name-only"
