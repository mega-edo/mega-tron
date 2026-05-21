"""Claude Code subprocess backend (Stage 0 scaffold).

Calls ``claude -p "<prompt>"`` (the Claude Code headless / "print" mode)
and returns the final assistant message text. Mirrors
:class:`mega_tron.llm_backends.codex.CodexBackend` so agentic rerank /
self-eval flows can target either CLI by swapping ``MEGA_BACKEND``.

Stage 0 status: skeleton with the right shape and an explicit
``NotImplementedError`` in :meth:`chat`. Stage 1+ may fill the body when
agentic rerank needs LLM-driven scoring on the Claude side.
"""
from __future__ import annotations

import sys

from mega_tron.llm_backends import LLMBackendError


class ClaudeBackend:
    """Run a one-shot chat through ``claude -p`` (Claude Code headless mode).

    Models go through Claude Code's own auth + routing; we just shell out.
    """

    name = "claude"

    def __init__(
        self,
        *,
        model: str = "claude-sonnet-4-6",
        claude_cmd: list[str] | None = None,
    ) -> None:
        self.model = model
        # Default invocation: ``claude -p <prompt> --model <model>``.
        # Callers may override (e.g. tests) by passing ``claude_cmd``.
        self.claude_cmd = claude_cmd or ["claude", "-p", "--model", model]

    def chat(self, system: str, user: str, *, timeout_s: int = 30) -> str:
        """Return the final assistant text. Raises :class:`LLMBackendError`.

        Stage 0: not implemented. Stage 1+ should mirror
        :meth:`CodexBackend.chat` — assemble the prompt, run subprocess,
        parse stdout. Until then, raise so callers fall back to
        ``MEGA_BACKEND=codex`` or ``litellm``.
        """
        print(
            "[mega-tron claude backend] not yet implemented; "
            "set MEGA_BACKEND=codex or litellm for agentic rerank.",
            file=sys.stderr,
        )
        raise LLMBackendError(
            "ClaudeBackend.chat is a Stage 0 stub. Implement before using "
            "MEGA_BACKEND=claude."
        )


__all__ = ["ClaudeBackend"]
