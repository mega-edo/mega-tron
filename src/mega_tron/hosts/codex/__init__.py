"""Codex CLI host adapter.

Wires mega-tron into OpenAI Codex CLI via its ``UserPromptSubmit`` and
``Stop`` hooks. The Codex install path also patches ``~/.codex/config.toml``
to disable the built-in skill catalog so the router's top-K injection is
the sole skill-visibility surface.

Public submodules:
  - :mod:`.hook`              — ``UserPromptSubmit`` entry point
  - :mod:`.stop_hook`         — ``Stop`` 2-phase verdict capture
  - :mod:`.install`           — install / uninstall logic
  - :mod:`.hook_trust`        — Codex SHA-trust handshake (0.130+)
  - :mod:`.prepender`         — Codex-specific ``build_hook_context``
  - :mod:`.tracker`           — Codex JSONL transcript scan
  - :mod:`.compat`            — Codex version detection / compat shims
"""
