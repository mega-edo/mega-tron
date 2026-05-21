"""LiteLLM backend — covers any provider supported by the ``litellm`` package
(OpenAI, Anthropic, together.ai, Groq, Bedrock, OpenRouter, Mistral …).

Optional dependency: install with ``uv add 'mega-tron[agentic-litellm]'``.
``litellm`` reads provider keys from environment variables on its own
(``OPENAI_API_KEY``, ``ANTHROPIC_API_KEY``, …) so we don't replicate that.
"""
from __future__ import annotations

import os
import sys
import time

from mega_tron.llm_backends import LLMBackendError


def _is_quiet() -> bool:
    return os.environ.get("MEGA_QUIET", "").strip() not in ("", "0")


class LiteLLMBackend:
    """Run a one-shot chat through :func:`litellm.completion`."""

    name = "litellm"

    def __init__(self, *, model: str = "openai/gpt-5.4-mini") -> None:
        self.model = model
        try:
            import litellm  # noqa: F401
        except ImportError as e:
            raise LLMBackendError(
                "litellm is not installed. Add the optional extra with:\n"
                "    uv add 'mega-tron[agentic-litellm]'\n"
                "or:\n"
                "    pip install 'mega-tron[agentic-litellm]'"
            ) from e

    def chat(self, system: str, user: str, *, timeout_s: int = 30) -> str:
        import litellm  # local import keeps the dep optional.

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})

        if not _is_quiet():
            print(
                f"[agentic litellm] model={self.model} timeout={timeout_s}s",
                file=sys.stderr,
                flush=True,
            )
        t0 = time.monotonic()
        try:
            response = litellm.completion(
                model=self.model,
                messages=messages,
                timeout=timeout_s,
                # `temperature=0` makes routing deterministic across reruns —
                # matters for the test suite and for users tuning prompts.
                temperature=0,
            )
        except Exception as e:  # noqa: BLE001 - litellm raises a zoo
            raise LLMBackendError(f"litellm completion failed: {e}") from e

        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMBackendError(
                f"litellm response missing choices[0].message.content: {response!r}"
            ) from e
        if not isinstance(content, str):
            raise LLMBackendError(
                f"litellm content is not a string: {type(content).__name__}"
            )
        if not _is_quiet():
            dt = time.monotonic() - t0
            print(
                f"[agentic litellm] returned {len(content)} chars in {dt:.1f}s",
                file=sys.stderr,
                flush=True,
            )
        return content.strip()


__all__ = ["LiteLLMBackend"]
