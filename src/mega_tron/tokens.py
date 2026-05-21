"""Token counting with tiktoken when available, char-heuristic otherwise.

Codex's 2% skill-budget cap is enforced against true tokens, not
characters. The ``chars // 4`` heuristic is within ~10% on English
ASCII text but overshoots on Asian scripts and undershoots on
punctuation-heavy YAML. We prefer ``tiktoken`` with the ``o200k_base``
encoder (used by GPT-4o / Codex 0.130.x) and fall back transparently
when tiktoken is not installed.

Install the precise counter with: ``uv add mega-tron[tokens]``.
"""
from __future__ import annotations

import warnings
from functools import lru_cache

# Conservative 4 chars/token approximation when tiktoken is unavailable.
CHARS_PER_TOKEN = 4


@lru_cache(maxsize=1)
def _encoder():
    try:
        import tiktoken
    except ImportError:
        warnings.warn(
            "tiktoken not installed; falling back to chars//4 token estimate. "
            "Install with `pip install mega-tron[tokens]` for precise counts.",
            stacklevel=3,
        )
        return None
    try:
        return tiktoken.get_encoding("o200k_base")
    except Exception as e:  # network/registry issue at first call
        warnings.warn(f"tiktoken o200k_base unavailable ({e}); using chars//4.", stacklevel=3)
        return None


def count_tokens(text: str) -> int:
    """Token count under Codex's encoder, or a char-based estimate."""
    if not text:
        return 0
    enc = _encoder()
    if enc is None:
        return max(1, len(text) // CHARS_PER_TOKEN)
    return len(enc.encode(text))


def count_skill_tokens(name: str, description: str) -> int:
    """Per-skill desc_tok used by Stager's budget arithmetic."""
    return count_tokens(name) + count_tokens(description)
