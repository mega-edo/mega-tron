"""Self-evaluation contract — anti-hallucination clauses.

The Stop hook can already drop verdict tags whose name isn't in the
catalog (see ``test_verdict_writer_catalog_filter.py``), but the
cleaner outcome is for the model to never emit such tags in the
first place. The contract text rendered by
``self_eval_contract.py`` is responsible for that. These tests pin
three clauses we found to be load-bearing — if a future edit drops
any of them, the model regresses to coining plausible-sounding
skill names from generic best-practices ("check-existing-first",
"prefer-stdlib") and the dashboard fills up with hallucination
artefacts the user has to clean.

Pinned clauses:
  1. Catalog is *exactly* the Skills block this turn's hook injects.
     Anchors the term so the model can't redefine it as "any skill
     name that sounds reasonable to me."
  2. Do-not-invent-names — explicit ban with the failure pattern
     spelled out (coining names from general principles).
  3. Verdicts are routing signal, not self-evaluation — the
     self-narration urge is the real source of the failure, so it
     gets called out by name.

We test the *spirit* of each clause with multiple key phrases so a
copy-edit that changes wording but keeps meaning still passes; a
deletion that removes the clause entirely will fail every related
phrase and surface the regression.
"""
from __future__ import annotations

import pytest

from mega_tron.self_eval_contract import (
    render_inline_self_eval_contract,
    render_install_tagging_guide,
)


# ---------- Inline form (injected per-turn by the prepender) -------- #


def test_inline_contract_anchors_catalog_to_the_skills_block():
    text = render_inline_self_eval_contract()
    # Must explicitly tie "catalog" to the Skills block the hook
    # just injected, not to any imagined registry.
    assert "Skills" in text
    assert "catalog" in text.lower()
    assert "this turn" in text.lower()


def test_inline_contract_bans_inventing_names():
    text = render_inline_self_eval_contract()
    assert "Do not invent names" in text
    # Should give a concrete example of the failure pattern so the
    # model can pattern-match before falling into it.
    assert "general principle" in text.lower()


def test_inline_contract_calls_out_self_narration():
    text = render_inline_self_eval_contract()
    # The phrase the model should recognise in its own internal urge.
    assert "self-evaluation" in text.lower()
    assert "routing signal" in text.lower()
    # And the operational consequence.
    assert "next" in text.lower()  # "next turn's router..."


# ---------- Install form (planted into AGENTS.md / CLAUDE.md / GEMINI.md) ---- #


@pytest.mark.parametrize("hook_name", ["Stop", "AfterAgent"])
def test_install_guide_anchors_catalog_to_runtime_skills_block(hook_name):
    text = render_install_tagging_guide(hook_name=hook_name)
    assert "Skills block" in text
    assert "this turn" in text.lower()
    # Hook name appears in the closing sentence so the doc names the
    # right event for the host it was installed on.
    assert hook_name in text


@pytest.mark.parametrize("hook_name", ["Stop", "AfterAgent"])
def test_install_guide_bans_inventing_names(hook_name):
    text = render_install_tagging_guide(hook_name=hook_name)
    assert "Do not invent names" in text
    assert "general principle" in text.lower()


@pytest.mark.parametrize("hook_name", ["Stop", "AfterAgent"])
def test_install_guide_calls_out_self_narration(hook_name):
    text = render_install_tagging_guide(hook_name=hook_name)
    assert "routing signal" in text.lower()
    assert "self-evaluation" in text.lower()


# ---------- Format invariants we still rely on -------- #


def test_inline_contract_has_section_heading_and_tag_form():
    """Existing prepender + tracker tests assume the inline contract
    starts with the ``### Self-evaluation`` heading and includes the
    tag form. Pin them here so a future edit to those two anchors
    can't slip past the focused contract tests."""
    text = render_inline_self_eval_contract()
    assert text.startswith("### Self-evaluation")
    assert "<skill-used" in text


@pytest.mark.parametrize("hook_name", ["Stop", "AfterAgent"])
def test_install_guide_has_tag_form_and_evidence_clause(hook_name):
    text = render_install_tagging_guide(hook_name=hook_name)
    assert "<skill-used" in text
    # The "silence > speculative verdict" rule is the existing
    # baseline rule the three new clauses build on; if it goes
    # missing, the new clauses lose half their teeth.
    assert "silence" in text.lower()
