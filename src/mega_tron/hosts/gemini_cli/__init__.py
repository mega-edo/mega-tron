"""Gemini CLI host adapter.

Wires mega-tron into Gemini CLI via its ``BeforeAgent`` and
``AfterAgent`` lifecycle hooks (see [Gemini CLI hooks reference](
https://geminicli.com/docs/hooks/reference/)). Differs from the Codex
and Claude Code adapters in three ways:

1. No shell wrapper and no SHA trust stamping — Gemini reads hook
   configuration from ``~/.gemini/settings.json`` and runs the
   configured commands without a separate trust handshake.
2. The Stop-hook equivalent is ``AfterAgent``. The 2-phase grading
   flow uses ``decision: "deny"`` (not Codex's ``"block"``) to force
   one eval retry turn, and the ``stop_hook_active`` flag distinguishes
   Phase 1 from Phase 2. The Phase-2 assistant text arrives on stdin
   as ``prompt_response`` — no transcript tailing needed, in contrast
   to Claude Code.
3. Mode-A "active downgrade" leverages Gemini's official
   ``skills.disabled: [...]`` array (settings.json reference) instead
   of Claude's per-skill ``skillOverrides``. The full original
   ``skills.disabled`` is snapshotted at install time and restored on
   uninstall.

Public submodules:
  - :mod:`.hook`              — ``BeforeAgent`` entry point
  - :mod:`.stop_hook`         — ``AfterAgent`` 2-phase verdict capture
  - :mod:`.install`           — install / uninstall logic
  - :mod:`.skill_overrides`   — Mode-A ``skills.disabled`` writer
  - :mod:`.prepender`         — Gemini-specific ``build_gemini_hook_context``
  - :mod:`.compat`            — Gemini CLI compat shims (version detect)
"""
