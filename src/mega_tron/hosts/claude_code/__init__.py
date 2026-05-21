"""Claude Code host adapter.

Wires mega-tron into Claude Code via its ``UserPromptSubmit`` and
``Stop`` hooks. Differs from the Codex adapter in three ways:

1. No shell wrapper — Claude Code's hook fires before the model sees the
   prompt, so ``additionalContext`` injection is enough.
2. Mode-A "active downgrade" optionally rewrites
   ``~/.claude/settings.local.json`` ``skillOverrides`` each turn so the
   native catalog hides non-routed skills.
3. ``last_assistant_message`` is missing from Claude Code's Stop hook
   payload, so Phase-2 verdict capture tails ``transcript_path``
   instead.

Public submodules:
  - :mod:`.hook`              — ``UserPromptSubmit`` entry point
  - :mod:`.stop_hook`         — ``Stop`` verdict capture (transcript-tail)
  - :mod:`.install`           — install / uninstall logic
  - :mod:`.skill_overrides`   — Mode-A ``skillOverrides`` writer
  - :mod:`.prepender`         — Claude-specific ``build_claude_hook_context``
  - :mod:`.compat`            — Claude Code compat shims
"""
