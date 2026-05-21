"""Optional Embedder adapters.

Each module here imports its third-party SDK lazily so the dependency only
activates when the adapter is actually constructed. Install with extras:

- `pip install mega-tron[openai]`
- `pip install mega-tron[voyage]`
- `pip install mega-tron[multilingual]`  # for BGE-m3
"""
from __future__ import annotations

__all__ = ["OpenAIEmbedder", "VoyageEmbedder", "BGEm3Embedder"]


def __getattr__(name):
    # Lazy import so listing the module doesn't pull every SDK.
    if name == "OpenAIEmbedder":
        from mega_tron.embedders.openai import OpenAIEmbedder

        return OpenAIEmbedder
    if name == "VoyageEmbedder":
        from mega_tron.embedders.voyage import VoyageEmbedder

        return VoyageEmbedder
    if name == "BGEm3Embedder":
        from mega_tron.embedders.bge_m3 import BGEm3Embedder

        return BGEm3Embedder
    raise AttributeError(name)
