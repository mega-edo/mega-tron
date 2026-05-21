"""Unit tests for the vanilla catalog simulators (DESIGN.md §4.2)."""
from __future__ import annotations

import pytest

from benchmarks.routing.vanilla_sim import (
    PoolSkill,
    simulate_claude_catalog,
    simulate_codex_catalog,
    simulate_gemini_catalog,
)


# Tiny fixture: 4 skills with known properties.
_FIXTURE = [
    PoolSkill("alpha-skill", "short description for alpha"),
    PoolSkill("beta-skill", "another short description"),
    PoolSkill("gamma-skill", "third short description here"),
    PoolSkill("delta-skill", "fourth one for sorting tests"),
]


# --- Codex --------------------------------------------------------------

def test_codex_fits_intact_under_budget():
    """4 short descriptions fit comfortably; every description survives."""
    cb = simulate_codex_catalog(_FIXTURE)
    assert cb.predicted_skills == {"alpha-skill", "beta-skill", "gamma-skill", "delta-skill"}
    # Order: alphabetical
    assert cb.text.index("alpha-skill") < cb.text.index("beta-skill")
    assert cb.text.index("beta-skill") < cb.text.index("delta-skill")
    assert cb.text.index("delta-skill") < cb.text.index("gamma-skill")
    # Wire format: every line has the (file: path) suffix
    assert "(file: skills/alpha-skill/SKILL.md)" in cb.text


def test_codex_wire_format_has_file_suffix():
    """Codex's render_minimum form is `- name: (file: path)` — every
    skill line must include the path suffix, even for skills whose
    description survives intact."""
    cb = simulate_codex_catalog(_FIXTURE)
    for s in _FIXTURE:
        # The path suffix must appear after the name line
        assert f"(file: skills/{s.name}/SKILL.md)" in cb.text


def test_codex_phase3_drops_overflow_skills_entirely():
    """Phase 3: if minimum-form lines themselves exceed budget, surplus
    skills are dropped from the catalog entirely — neither name, nor
    description, nor path."""
    # 200 skills × minimum line ~75 chars = 15K chars >> 4K budget. Forces Phase 3.
    skills = [PoolSkill(f"skill-{i:03d}", "x" * 50) for i in range(200)]
    cb = simulate_codex_catalog(skills)
    # Far fewer than 200 skills survive
    assert len(cb.all_emitted_names) < 100
    # No skill has its description intact (Phase 3 emits minimum form only)
    assert cb.predicted_skills == frozenset()


def test_codex_phase2_round_robin_descriptions():
    """Phase 2: when minimum-form lines fit the budget, the remainder
    is distributed across descriptions in round-robin order. Short
    descriptions finish early and unused share flows to longer ones."""
    # 10 skills × minimum line ~75 chars = 750 chars << 4K budget. Phase 2.
    skills = [PoolSkill(f"sk-{i:02d}", "short desc") for i in range(10)]
    cb = simulate_codex_catalog(skills)
    # All 10 skills' names appear
    assert len(cb.all_emitted_names) == 10
    # All descriptions are short and fit → all predicted
    assert len(cb.predicted_skills) == 10


def test_codex_budget_is_min_of_2pct_and_8000():
    """At 200K ctx_window, 2% = 4000 < 8000 cap, so 4000-char budget binds."""
    # Many skills → minimum form overflows 4K budget at ~55 skills.
    skills = [PoolSkill(f"skill-{i:03d}", "x" * 95) for i in range(100)]
    cb = simulate_codex_catalog(skills, ctx_window=200_000)
    # Phase 3 binds; only a fraction of skills survive
    assert len(cb.all_emitted_names) < 100


def test_codex_budget_caps_at_8000_for_huge_ctx():
    """At 2M ctx_window, 2% = 40000 but cap is 8000."""
    # 300 skills with minimum line ~75 chars each → ~22500 chars total,
    # which overflows the 8000-char cap → Phase 3 binds.
    skills = [PoolSkill(f"skill-name-{i:03d}", "x" * 100) for i in range(300)]
    cb = simulate_codex_catalog(skills, ctx_window=2_000_000)
    # Budget = min(2_000_000 * 0.02, 8_000) = 8_000 (cap binds, not the 2%).
    assert len(cb.all_emitted_names) < 300


def test_codex_alphabetical_order():
    skills = [PoolSkill(name, "desc") for name in ["zebra", "alpha", "mango", "banana"]]
    cb = simulate_codex_catalog(skills)
    positions = [cb.text.index(n) for n in ["alpha", "banana", "mango", "zebra"]]
    assert positions == sorted(positions)


# --- Claude Code --------------------------------------------------------

def test_claude_all_names_always_emitted():
    """Claude never drops a skill entirely — even at extreme overflow."""
    long_desc = "x" * 9000
    skills = [PoolSkill(f"s{i:03d}", long_desc) for i in range(50)]
    cb = simulate_claude_catalog(skills)
    for s in skills:
        assert s.name in cb.text
    assert cb.all_emitted_names == {s.name for s in skills}


def test_claude_descriptions_appended_until_budget():
    """In a fresh-user / tie-broken-alphabetically scenario, descriptions
    fill in alphabetical order until the token budget runs out."""
    cb = simulate_claude_catalog(_FIXTURE)
    assert cb.predicted_skills == {"alpha-skill", "beta-skill", "gamma-skill", "delta-skill"}


def test_claude_drops_descriptions_under_pressure():
    """With many fat descriptions, some skills end up name-only."""
    long_desc = "x" * 3000
    skills = [PoolSkill(f"s{i:03d}", long_desc) for i in range(20)]
    cb = simulate_claude_catalog(skills)
    assert len(cb.predicted_skills) < len(skills)
    for s in skills:
        assert s.name in cb.text


def test_claude_per_entry_description_cap():
    """maxSkillDescriptionChars=1536: descriptions longer than that are
    truncated at the source before the budget step."""
    # 10K-char description — should be capped to 1536 at the source.
    skills = [PoolSkill("solo", "x" * 10_000)]
    cb = simulate_claude_catalog(skills)
    # The catalog text should not contain a 10K-char run of 'x'
    assert "x" * 2000 not in cb.text
    # But it should contain at least the 1536-char cap
    assert "x" * 1500 in cb.text


def test_claude_token_budget_uses_tiktoken():
    """The budget is in tokens, not chars."""
    skills = [PoolSkill(f"sk{i}", f"desc {i}") for i in range(8)]
    cb = simulate_claude_catalog(skills)
    assert cb.tokens < 200  # well under 2000-token budget


# --- Gemini -------------------------------------------------------------

def test_gemini_emits_everything_no_cap():
    """No budget; every skill's full description ships."""
    long_desc = "x" * 5000
    skills = [PoolSkill(f"sk{i:03d}", long_desc) for i in range(50)]
    cb = simulate_gemini_catalog(skills)
    assert cb.predicted_skills == {s.name for s in skills}
    # Token count grows linearly with pool — 50 skills with 5K-char descs.
    assert cb.tokens > 10_000


def test_gemini_xml_wire_format():
    """Gemini's renderAgentSkills emits an XML <skill> block per entry."""
    cb = simulate_gemini_catalog(_FIXTURE)
    # Header
    assert "# Available Agent Skills" in cb.text
    # XML structure
    assert "<available_skills>" in cb.text
    assert "</available_skills>" in cb.text
    # Each skill becomes one <skill> element with three children
    for s in _FIXTURE:
        assert f"<name>{s.name}</name>" in cb.text
        assert f"<description>{s.description}</description>" in cb.text
        assert f"<location>.gemini/skills/{s.name}/SKILL.md</location>" in cb.text


def test_gemini_preserves_input_order():
    """Filesystem-iteration order = the order skills are passed in."""
    skills = [PoolSkill(name, "d") for name in ["zebra", "alpha", "mango"]]
    cb = simulate_gemini_catalog(skills)
    positions = [cb.text.index(f"<name>{n}</name>") for n in ["zebra", "alpha", "mango"]]
    assert positions == sorted(positions)


def test_gemini_empty_pool():
    cb = simulate_gemini_catalog([])
    # Empty pool still emits header + empty <available_skills> block
    # (the model is told the section exists; it's just empty)
    assert cb.predicted_skills == frozenset()
    assert cb.all_emitted_names == frozenset()


# --- Cross-simulator sanity ---------------------------------------------

def test_all_simulators_return_catalog_block():
    """Type-shape contract: all three simulators return a CatalogBlock."""
    from benchmarks.routing.vanilla_sim import CatalogBlock
    for fn in (simulate_codex_catalog, simulate_claude_catalog, simulate_gemini_catalog):
        result = fn(_FIXTURE)
        assert isinstance(result, CatalogBlock)
        assert isinstance(result.tokens, int)
        assert isinstance(result.predicted_skills, frozenset)
        assert isinstance(result.all_emitted_names, frozenset)


@pytest.mark.parametrize("simulator", [
    simulate_codex_catalog,
    simulate_claude_catalog,
    simulate_gemini_catalog,
])
def test_all_simulators_handle_empty_pool(simulator):
    cb = simulator([])
    assert cb.predicted_skills == frozenset()
    assert cb.all_emitted_names == frozenset()
