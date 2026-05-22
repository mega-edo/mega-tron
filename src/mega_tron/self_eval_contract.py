"""Single source of truth for the self-evaluation tagging contract.

Two renderers, both producing the same verdict semantics in different
contexts:

- :func:`render_inline_self_eval_contract` — short form appended to
  every UserPromptSubmit / BeforeAgent injection. Read once per turn.
- :func:`render_install_tagging_guide` — longer form planted into the
  host's persistent memory file (AGENTS.md / CLAUDE.md / GEMINI.md) at
  install time. Read once and referred back to across many turns.

Both spell out the same two-axis verdict rule:

  HELPFUL  = (used content) AND (skill's recommended approach is correct)
  HARMFUL  = skill recommends an incorrect / outdated / anti-pattern
             approach — regardless of whether you used it
  NEUTRAL  = loaded but did not influence the answer, or correctness
             unassessable

The "counter-example trap" — where a model marks an anti-pattern skill
HELPFUL because the bad content served as a useful counter-example — is
explicitly named and forbidden. This is what feedback_loop smoke runs
exposed as the dominant verdict-noise mode.
"""
from __future__ import annotations


def render_inline_self_eval_contract() -> str:
    """Self-evaluation block injected with every routed user prompt.

    Used by:
      - :func:`mega_tron.prepender.build_hook_context` (codex)
      - :func:`mega_tron.prepender.build_claude_hook_context`
      - :func:`mega_tron.prepender.build_gemini_hook_context`
      - ``benchmarks/feedback_loop/run.py`` (experiment runner)

    Returned text starts with the ``### Self-evaluation`` heading and ends
    with a trailing newline.
    """
    return (
        "### Self-evaluation\n"
        "Tag only the skills listed above (the **Skills (selected for "
        "this turn)** block this hook just injected). That block IS the "
        "catalog for this turn — names that don't appear there are not "
        "in the catalog. For each skill from that list that influenced "
        "your reply, append one tag at the end of your message. Before "
        "writing each verdict, answer two questions about the skill "
        "(think silently, do not narrate):\n"
        "  (1) Did your final answer use content from this skill?\n"
        "  (2) Is the approach this skill RECOMMENDS correct for the "
        "user's task?\n"
        "\n"
        "Verdict mapping:\n"
        "  - HELPFUL  = (1) yes AND (2) correct\n"
        "  - HARMFUL  = (2) incorrect / outdated / anti-pattern — "
        "regardless of whether you used it. The verdict protects future "
        "users from being routed to a misleading skill.\n"
        "  - NEUTRAL  = loaded but did not influence the answer, OR you "
        "cannot assess correctness.\n"
        "\n"
        "Critical anti-pattern (the counter-example trap): if a skill's "
        "content ITSELF contains the bug, outdated API, or anti-pattern "
        "the user is asking about, that is HARMFUL — even if it served "
        "you as a useful counter-example. The verdict is about whether "
        "the skill steers the next user correctly; your own knowledge "
        "rescuing the answer this turn does not redeem the skill.\n"
        "\n"
        "**Do not invent names.** If a name is not in the Skills block "
        "above, do NOT emit a `<skill-used>` tag for it — even if your "
        "reply happened to follow a general principle that sounds like "
        "it could be a skill. Coining names from a generic best-"
        "practice you already knew (\"check-existing-first\", "
        "\"prefer-stdlib\", etc.) is the most common failure mode of "
        "this contract; mega-tron's Stop hook drops such tags silently "
        "but the cleaner outcome is that you don't emit them.\n"
        "\n"
        "**Verdicts are routing signal, not self-evaluation.** If your "
        "urge is \"I want to record that I did something good this "
        "turn,\" that is NOT a verdict — drop it. A verdict exists only "
        "to tell the NEXT turn's router whether a real surfaced skill "
        "helped or hurt; it is never a place to narrate your own "
        "reasoning quality.\n"
        "\n"
        "**\"No match\" turns.** If none of the surfaced skills applied "
        "to the user's prompt and you proceeded with general knowledge, "
        "you do NOT need to emit a tag. Silence is the correct signal "
        "for that case — it tells the router that the top-K missed, "
        "without injecting a speculative NEUTRAL into the verdict "
        "store.\n"
        "\n"
        "Tag form: "
        "`<skill-used name=\"...\" verdict=\"HELPFUL|HARMFUL|NEUTRAL\" "
        "reason=\"<one short sentence of concrete evidence>\"/>`\n"
        "\n"
        "The `reason` attribute **must be written in English** even when "
        "the rest of your reply is in another language — verdict reasons "
        "are persisted to SKILL.md and the verdict store as long-lived "
        "training signal, and the router's semantic verdict-embedding "
        "lookup is tuned on English text, so non-English reasons silently "
        "degrade future routing quality. Cite a file path / test name / "
        "command output when possible. If you can't cite concrete "
        "evidence, omit the tag entirely — silence is treated as 'no "
        "signal' and is preferred over a speculative verdict. mega-tron "
        "reads these tags directly from the transcript and updates "
        "routing weights silently; there is no follow-up evaluation turn."
    )


def render_install_tagging_guide(
    *, hook_name: str, mega_tron_bin: str = "mega-tron"
) -> str:
    """Persistent tagging guide planted into the host memory file at install.

    Used by:
      - :mod:`mega_tron.hosts.codex.install`           (hook_name="Stop")
      - :mod:`mega_tron.hosts.claude_code.install`     (hook_name="Stop")
      - :mod:`mega_tron.hosts.gemini_cli.install`      (hook_name="AfterAgent")

    Args:
        hook_name: Which hook reads the tags on this host — used only in
            the closing sentence so the doc names the correct event.
        mega_tron_bin: Absolute path to the mega-tron binary, as it
            should appear in the rendered guide. Stamped at install
            time via :func:`mega_tron.cli.path_setup.resolve_bin_path`
            so the model invokes mega-tron via its absolute path —
            host-spawned subshells routinely run with a minimal PATH
            that doesn't include ``~/.local/bin``, which would
            otherwise make ``mega-tron search ...`` fail with
            ``command not found``. Default ``mega-tron`` preserved for
            tests and ad-hoc callers; the install path always passes
            the resolved absolute path.

    Returned text is the body of the "Tagging contract" paragraph; the
    caller chooses how to embed it (markdown section, AGENTS.md prose,
    etc.).
    """
    return (
        "Tagging contract: for each skill that influenced your reply, "
        "append one tag at the end of your final message. The **catalog "
        "for any given turn is exactly the Skills block that mega-tron's "
        "user-prompt hook injects at the top of that turn** — names "
        "outside that block are not in the catalog, regardless of "
        "whether they sound plausible. Before assigning a verdict, "
        "answer two questions about the skill (silently):\n"
        "  (1) Did your final answer use content from this skill?\n"
        "  (2) Is the approach this skill RECOMMENDS correct for the "
        "user's task?\n"
        "\n"
        "Verdict mapping:\n"
        "  - HELPFUL  = (1) yes AND (2) correct.\n"
        "  - HARMFUL  = (2) incorrect / outdated / anti-pattern, "
        "regardless of whether you used it. The verdict is a signal to "
        "future routing, not a grade on this turn.\n"
        "  - NEUTRAL  = loaded but did not influence the answer, or "
        "correctness unassessable.\n"
        "\n"
        "Critical anti-pattern (the counter-example trap): if a skill's "
        "content itself contains the bug or anti-pattern the user is "
        "asking about, that is HARMFUL — even if it served you as a "
        "useful counter-example. The verdict is about whether the skill "
        "steers the next user correctly.\n"
        "\n"
        "**Do not invent names.** If a name is not in this turn's "
        "Skills block, do NOT emit a `<skill-used>` tag for it — even "
        "if your reply happened to follow a general principle that "
        "sounds like it could be a skill. Coining names from a generic "
        "best-practice you already knew (\"check-existing-first\", "
        "\"prefer-stdlib\", etc.) is the most common failure mode of "
        f"this contract; the {hook_name} hook drops such tags silently "
        "but the cleaner outcome is that you don't emit them.\n"
        "\n"
        "**Verdicts are routing signal, not self-evaluation.** If your "
        "urge is \"I want to record that I did something good this "
        "turn,\" that is NOT a verdict — drop it. A verdict exists only "
        "to tell the next turn's router whether a real surfaced skill "
        "helped or hurt; it is never a place to narrate your own "
        "reasoning quality.\n"
        "\n"
        f"**\"No match\" turns.** When you called `{mega_tron_bin} "
        "search` and decided no surfaced skill applies, you do NOT need to emit a "
        "tag. The call itself is already logged to the routes table; "
        "the absence of a tag is itself signal that the top-K did not "
        "help, and the router learns from that. Force-tagging a NEUTRAL "
        "on every unmatched turn would only inject noise into the "
        "verdict-embedding store.\n"
        "\n"
        "Tag form: "
        "`<skill-used name=\"<name>\" verdict=\"HELPFUL|HARMFUL|NEUTRAL\" "
        "reason=\"<file/test/command evidence>\"/>`. Cite concrete "
        "evidence (file path, test name, command output). If you can't "
        "cite evidence, omit the tag — silence is treated as 'no signal' "
        "and is preferred over a speculative verdict. The `reason` "
        "attribute must be written in English even when the rest of your "
        "reply is in another language — verdict reasons are persisted as "
        "long-lived training signal and the router's semantic verdict-"
        "embedding lookup is tuned on English text, so non-English "
        "reasons silently degrade future routing quality. "
        f"The {hook_name} hook reads these tags directly from the "
        "transcript and updates routing weights silently; there is no "
        "follow-up evaluation turn."
    )


__all__ = [
    "render_inline_self_eval_contract",
    "render_install_tagging_guide",
]
