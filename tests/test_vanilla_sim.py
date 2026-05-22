"""Pin that the production ``mega_tron.vanilla_sim`` module is wired up.

The Context Savings dashboard tab calls these simulators on every
poll, so they live under ``src/mega_tron/vanilla_sim.py`` as a
production module. The benchmark folder under ``benchmarks/routing/``
has its own independent copy — same algorithms, but the two trees
never import from each other. This test guards the production module
in isolation.
"""
from __future__ import annotations


def test_production_module_imports():
    """The production module must import every public name."""
    from mega_tron.vanilla_sim import (
        CatalogBlock,
        PoolSkill,
        simulate_codex_catalog,
        simulate_claude_catalog,
        simulate_claude_active_downgrade,
        simulate_gemini_catalog,
        DEFAULT_CTX_WINDOW,
        CODEX_CHAR_BUDGET_CAP,
        CODEX_BUDGET_PCT,
        CLAUDE_BUDGET_FRAC,
        CLAUDE_MAX_DESC_CHARS,
    )
    # Touch every imported name so static analysers don't flag the
    # import as unused — the import surface IS the contract this test
    # pins.
    assert DEFAULT_CTX_WINDOW > 0
    assert CODEX_CHAR_BUDGET_CAP > 0
    assert 0 < CODEX_BUDGET_PCT < 1
    assert 0 < CLAUDE_BUDGET_FRAC < 1
    assert CLAUDE_MAX_DESC_CHARS > 0
    assert simulate_claude_catalog is not None
    assert simulate_gemini_catalog is not None

    skills = [PoolSkill(name="foo", description="A test skill description.")]
    block = simulate_codex_catalog(skills)
    assert isinstance(block, CatalogBlock)
    assert block.tokens > 0
    assert "foo" in block.all_emitted_names

    active = simulate_claude_active_downgrade(skills)
    assert active.tokens > 0
    assert "foo" in active.all_emitted_names


def test_active_downgrade_strictly_smaller_than_passive():
    """Below the saturation point active mode emits strictly fewer
    tokens than passive — descriptions get dropped while passive
    would have kept them. At very large pool sizes the two
    coincide (passive's budget runs out filling names alone), and
    the simulator's "take the smaller of the two" guard makes the
    inequality non-strict in that regime; this test stays under the
    saturation threshold (40 skills) so the strict inequality holds.
    """
    from mega_tron.vanilla_sim import (
        PoolSkill,
        simulate_claude_catalog,
        simulate_claude_active_downgrade,
    )

    skills = [
        PoolSkill(
            name=f"skill-{i:03d}",
            description=f"A long enough description for skill {i} to exercise the budget logic.",
        )
        for i in range(40)
    ]
    passive = simulate_claude_catalog(skills)
    active = simulate_claude_active_downgrade(skills)
    assert active.tokens < passive.tokens
    # Active mode emits every name but no descriptions are "predicted."
    assert active.all_emitted_names == frozenset(s.name for s in skills)
    assert active.predicted_skills == frozenset()


def test_strict_suppression_zeros_native_catalog():
    """Strict mode removes the catalog block entirely — every
    measurement is empty, regardless of input size."""
    from mega_tron.vanilla_sim import (
        PoolSkill,
        simulate_claude_strict_suppression,
    )

    skills = [
        PoolSkill(name=f"skill-{i:03d}", description=f"Desc {i}.")
        for i in range(50)
    ]
    strict = simulate_claude_strict_suppression(skills)
    assert strict.tokens == 0
    assert strict.text == ""
    assert strict.all_emitted_names == frozenset()
    assert strict.predicted_skills == frozenset()


def test_active_downgrade_no_op_at_large_pool():
    """At pool sizes above the passive-budget saturation point the
    active downgrade emits the SAME token count as passive — both
    end up at name-only because passive's budget is consumed by
    name lines alone. The simulator's "min(passive, name-only)" rule
    must surface that honestly instead of pretending active still
    saves something."""
    from mega_tron.vanilla_sim import (
        PoolSkill,
        simulate_claude_catalog,
        simulate_claude_active_downgrade,
    )

    skills = [
        PoolSkill(name=f"skill-{i:04d}", description=f"Description {i}.")
        for i in range(500)
    ]
    passive = simulate_claude_catalog(skills)
    active = simulate_claude_active_downgrade(skills)
    # Allow exact-equal (most common case) or active being a tad smaller
    # if the name-only listing happens to encode shorter.
    assert active.tokens <= passive.tokens
    # The meaningful claim: descriptions are gone in both.
    assert len(passive.predicted_skills) == 0
