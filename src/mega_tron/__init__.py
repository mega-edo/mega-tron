"""mega-tron — host-agnostic skill routing + behaviour-learning infrastructure.

This top-level namespace exposes a curated public API. Everything else
lives in submodules — import directly from there (``from
mega_tron.cache import Cache``) rather than reaching through this
file.

Public API (stable across minor versions):

- :class:`Router` — semantic top-K skill ranker.
- :class:`MegaCore` — host-agnostic facade combining router + verdict
  pipeline as a plain Python library.
- :class:`Embedder` + :func:`make_embedder` — embedding backend protocol.
- :class:`Config` — user-facing configuration (config.toml + env vars).
- :func:`build_prefix` — generic candidate-skills prompt prefix builder.
- :func:`scan_transcript` — host-agnostic skill-invocation detector.
- :func:`make_llm_backend` — LLM backend factory (claude / codex / litellm).
- :data:`Verdict`, :data:`Regression`, :data:`StatRow` — evaluation
  result types surfaced by :class:`MegaCore`.
- :data:`__version__` — package version.
"""
from __future__ import annotations

from mega_tron.config import Config
from mega_tron.core import MegaCore, Regression, StatRow, Verdict
from mega_tron.embedder import Embedder, make_embedder
from mega_tron.llm_backends import make_llm_backend
from mega_tron.prepender import build_prefix
from mega_tron.router import Router
from mega_tron.tracker import scan_transcript

__version__ = "1.0.0"

__all__ = [
    "Config",
    "Embedder",
    "MegaCore",
    "Regression",
    "Router",
    "StatRow",
    "Verdict",
    "__version__",
    "build_prefix",
    "make_embedder",
    "make_llm_backend",
    "scan_transcript",
]
