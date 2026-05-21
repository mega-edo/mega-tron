"""Codex subprocess backend — calls ``codex exec`` and returns the final
assistant message text.

Lifted from the pre-v0.5 ``hyde.py`` invocation pattern, which proved out
the ``--json --sandbox read-only --skip-git-repo-check`` invariants and the
JSONL event-stream parser. v0.5 generalizes that parser to return the full
assistant message instead of pulling a single field.

Models verified against ChatGPT-subscription auth (April 2026 binary):
- ``gpt-5.4`` and ``gpt-5.4-mini`` work
- ``gpt-5``, ``gpt-5-codex``, ``gpt-5.4-codex``, ``o3-mini``,
  ``codex-mini-latest`` all return 400 "model not supported"
So our agentic default is ``gpt-5.4-mini`` — the cheap tier subscribers can use.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

from mega_tron.llm_backends import LLMBackendError


def _is_quiet() -> bool:
    return os.environ.get("MEGA_QUIET", "").strip() not in ("", "0")


class CodexBackend:
    """Run a one-shot chat through ``codex exec``."""

    name = "codex"

    def __init__(
        self,
        *,
        model: str = "gpt-5.4-mini",
        codex_cmd: list[str] | None = None,
    ) -> None:
        self.model = model
        # Caller can override for tests; default is the same invocation the
        # v0.3/v0.4 HyDE pipeline shipped with.
        self.codex_cmd = codex_cmd or [
            "codex",
            "exec",
            "-m",
            model,
            "--json",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
        ]

    def chat(self, system: str, user: str, *, timeout_s: int = 30) -> str:
        """Return the final assistant text. Raises :class:`LLMBackendError`."""
        prompt = system.strip() + "\n\n" + user.strip() if system else user.strip()
        if not _is_quiet():
            print(
                f"[agentic codex] model={self.model} timeout={timeout_s}s",
                file=sys.stderr,
                flush=True,
            )
        t0 = time.monotonic()
        try:
            result = subprocess.run(
                [*self.codex_cmd, prompt],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            raise LLMBackendError(f"codex exec timed out after {timeout_s}s") from e
        except FileNotFoundError as e:
            raise LLMBackendError(
                f"codex CLI not found: {self.codex_cmd[0]!r}. "
                "Install from https://github.com/openai/codex and authenticate."
            ) from e

        if result.returncode != 0:
            raise LLMBackendError(
                f"codex exec returned rc={result.returncode}: "
                f"{result.stderr.strip()[:500]}"
            )
        out = extract_message_text(result.stdout)
        if not _is_quiet():
            dt = time.monotonic() - t0
            print(
                f"[agentic codex] returned {len(out)} chars in {dt:.1f}s",
                file=sys.stderr,
                flush=True,
            )
        return out


def extract_message_text(stdout: str) -> str:
    """Pull the final assistant text out of a ``codex exec --json`` stream.

    Codex emits one JSON event per line. The final response is the last
    text-bearing item event. Falls back to plain stdout if no JSONL parses.
    Also strips ``` fences the model might wrap its output in.
    """
    last_text = ""
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        item = ev.get("item") or ev
        if isinstance(item, dict):
            text = item.get("text") or item.get("output_text") or item.get("content")
            if isinstance(text, str) and text.strip():
                last_text = text
    body = (last_text or stdout).strip()
    body = re.sub(r"^```(?:[a-zA-Z]+)?\s*", "", body)
    body = re.sub(r"\s*```$", "", body)
    return body.strip()


__all__ = ["CodexBackend", "extract_message_text"]
