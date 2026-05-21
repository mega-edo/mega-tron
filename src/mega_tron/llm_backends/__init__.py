"""LLM backends for agentic search (v0.5+).

Two implementations:
- :class:`CodexBackend` — wraps ``codex exec``, free for ChatGPT subscribers.
- :class:`LiteLLMBackend` — wraps :mod:`litellm` for any model / provider.

Both are lazy-imported: the codex backend only needs the ``codex`` binary at
runtime; the litellm backend triggers an actual Python import of ``litellm``.

v1.0: backend + model selection migrated from ``MEGA_AGENTIC_BACKEND`` /
``MEGA_AGENTIC_MODEL`` to plain ``MEGA_BACKEND`` / ``MEGA_MODEL``. The
legacy names still resolve via :mod:`mega_tron._env` with a
deprecation warning.
"""
from __future__ import annotations

from typing import Protocol

from mega_tron._env import read_env


class LLMBackendError(RuntimeError):
    """Raised when an LLM backend call fails (subprocess, network, parse, etc)."""


class LLMBackend(Protocol):
    """One-shot chat protocol. Returns the assistant's reply as text."""

    name: str

    def chat(
        self, system: str, user: str, *, timeout_s: int = 30
    ) -> str:  # pragma: no cover - protocol
        ...


def make_llm_backend() -> "LLMBackend":
    """Select a backend based on ``MEGA_BACKEND`` env var.

    - ``codex`` (default): :class:`CodexBackend` with model
      ``MEGA_MODEL`` (default ``gpt-5.4-mini``).
    - ``litellm``: :class:`LiteLLMBackend` with model ``MEGA_MODEL``
      (default ``openai/gpt-5.4-mini``). Requires the ``agentic-litellm`` extra.

    The legacy ``MEGA_AGENTIC_BACKEND`` / ``MEGA_AGENTIC_MODEL`` names
    still work (with a one-shot deprecation warning).

    Raises :class:`LLMBackendError` for an unknown backend name.
    """
    raw_kind = read_env("MEGA_BACKEND")
    kind = (raw_kind or "codex").strip().lower()
    model_env = read_env("MEGA_MODEL")
    if kind == "codex":
        from mega_tron.llm_backends.codex import CodexBackend

        return CodexBackend(model=model_env or "gpt-5.4-mini")
    if kind == "litellm":
        from mega_tron.llm_backends.litellm_backend import LiteLLMBackend

        return LiteLLMBackend(model=model_env or "openai/gpt-5.4-mini")
    raise LLMBackendError(
        f"unknown MEGA_BACKEND={kind!r} (expected 'codex' or 'litellm')"
    )


__all__ = ["LLMBackend", "LLMBackendError", "make_llm_backend"]
