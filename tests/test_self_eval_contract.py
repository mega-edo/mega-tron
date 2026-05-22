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


# ---------- "No match" escape clause ----------------------- #
#
# When a turn calls `mega-tron search` and decides nothing applies,
# we DON'T want the model to emit a defensive NEUTRAL on every
# unmatched skill. That would flood the verdict-embedding store
# with noise and degrade routing on later turns. Both forms of the
# contract have to spell out "no tag is the right tag" for that
# case, or else we re-introduce the silence-vs-force-tag confusion
# the inline contract's "silence > speculative" rule was trying to
# fix in the first place.


def test_inline_contract_permits_no_tag_on_no_match():
    text = render_inline_self_eval_contract()
    # The case is called out by name.
    assert "no match" in text.lower() or "no surfaced skill" in text.lower()
    # And the operational guidance is "don't tag" rather than
    # "emit NEUTRAL anyway".
    assert "do NOT need to emit" in text or "do not need to emit" in text


@pytest.mark.parametrize("hook_name", ["Stop", "AfterAgent"])
def test_install_guide_permits_no_tag_on_no_match(hook_name):
    text = render_install_tagging_guide(hook_name=hook_name)
    assert "no match" in text.lower() or "no surfaced skill" in text.lower()
    assert "do NOT need to emit" in text or "do not need to emit" in text
    # Critically, the rationale connects the silence back to the
    # routes table — so the reader knows the absence-of-tag is
    # itself the signal, not a missing one.
    assert "routes" in text.lower() or "logged" in text.lower()


# ---------- Memory blocks: default-call mandate --------------- #
#
# The three host memory blocks (CLAUDE.md / AGENTS.md / GEMINI.md)
# must instruct the model to call `mega-tron search` BY DEFAULT on
# every turn that needs a skill — earlier wording ("any time you
# need a skill") was empirically being skipped by the model on
# turns where a skill plainly applied. The new wording inverts the
# default: call unless the prompt is purely conversational.


def _claude_block_body():
    from mega_tron.hosts.claude_code.install import CLAUDE_BLOCK_BODY
    return CLAUDE_BLOCK_BODY


def _agents_block_body():
    from mega_tron.hosts.codex.install import AGENTS_BLOCK_BODY
    return AGENTS_BLOCK_BODY


def _gemini_block_body():
    from mega_tron.hosts.gemini_cli.install import GEMINI_BLOCK_BODY
    return GEMINI_BLOCK_BODY


@pytest.mark.parametrize(
    "block_body",
    [_claude_block_body, _agents_block_body, _gemini_block_body],
    ids=["claude", "codex", "gemini"],
)
def test_memory_block_mandates_default_call(block_body):
    text = block_body()
    # The default-call wording — the load-bearing word is MUST.
    assert "MUST" in text
    assert "mega-tron search" in text
    # The escape is *only* purely-conversational turns. Spelling out
    # the four "DO call" categories was rejected (false-positive risk
    # — see chat history). The skip condition is what we test.
    assert "conversational" in text.lower()
    # If unsure, call. This protects against the model interpreting
    # "needs a skill" as "I know this off the top of my head".
    assert "if in doubt" in text.lower() or "if unsure" in text.lower()


@pytest.mark.parametrize(
    "block_body",
    [_claude_block_body, _agents_block_body, _gemini_block_body],
    ids=["claude", "codex", "gemini"],
)
def test_memory_block_legitimises_no_match_outcome(block_body):
    """The reason mandating default-call is safe: 'no surfaced skill
    applies' has to be a first-class outcome, otherwise the model
    will force-fit an unrelated skill just because search surfaced
    something."""
    text = block_body()
    assert "no surfaced skill applies" in text.lower()
    # The user-visible fallback line is the same on all three hosts.
    assert "general knowledge" in text.lower()
    assert "force-fit" in text.lower()


@pytest.mark.parametrize(
    "block_body",
    [_claude_block_body, _agents_block_body, _gemini_block_body],
    ids=["claude", "codex", "gemini"],
)
def test_memory_block_does_not_enumerate_trigger_categories(block_body):
    """Empirically, an enumerated trigger list ("library / framework /
    API / tool / file layout / config / code-writing task") collapsed
    to false-positive on conversational follow-ups. The new wording
    defines only the SKIP condition (purely conversational) and lets
    the model decide what counts as a skill turn within that gate.

    This is a negative assertion: we pin the *absence* of the old
    enum so a future copy-edit doesn't re-introduce it.
    """
    text = block_body()
    # Each of these on its own is fine in passing prose. The failure
    # mode is the *enumeration* — three or more of these in series in
    # a list/clause that defines when to call search. We catch that
    # cheaply by counting how many of them appear in the block.
    enum_terms = [
        "library/framework",
        "framework/API",
        "tool invocation, a file/system",  # the old list signature
        "CLI flag/option",
    ]
    hits = sum(1 for t in enum_terms if t in text)
    assert hits == 0, (
        f"memory block looks like it re-introduced the trigger "
        f"enum (matched: {[t for t in enum_terms if t in text]})"
    )
