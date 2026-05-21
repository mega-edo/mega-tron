"""Shared env-var helpers.

Pipeline knobs are organised by *function* (prefilter / shortlist /
read-max / timeout / backend / model / mode) so the env-var names match
the CLI flag names: ``MEGA_PREFILTER``, ``MEGA_SHORTLIST``,
``MEGA_READ_MAX``, ``MEGA_TIMEOUT_S``, ``MEGA_BACKEND``, ``MEGA_MODEL``,
``MEGA_MODE``.
"""
from __future__ import annotations

import os


def read_env(name: str) -> str | None:
    """Return the env var value (untrimmed) or ``None`` if unset."""
    return os.environ.get(name)


def read_int_env(name: str, default: int) -> int:
    """Read an int env var. Returns ``default`` on missing / non-numeric
    values. Clamped to ``>= 1``."""
    raw = read_env(name)
    if raw is None or not raw.strip():
        return default
    try:
        return max(1, int(raw.strip()))
    except ValueError:
        return default


def resolve_mode(explicit: str | None) -> str:
    """Pick the effective search mode.

    Priority: ``explicit`` (CLI flag) → ``MEGA_MODE`` env → fallback
    ``"semantic"``. The semantic path is hermetic (no LLM, no network);
    ``--mode agentic`` adds 1-2 LLM calls on top of the cosine
    prefilter for deployments that want LLM-driven picks.
    """
    if explicit:
        return explicit
    env_mode = (os.environ.get("MEGA_MODE") or "").strip().lower()
    if env_mode in ("agentic", "semantic"):
        return env_mode
    return "semantic"


__all__ = ["read_env", "read_int_env", "resolve_mode"]
