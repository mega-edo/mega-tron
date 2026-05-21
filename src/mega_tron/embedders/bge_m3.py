"""BGE-m3 multilingual embedder — 110 languages, 1024 dim, MIT.

Use this when ticket strings come in non-English (Korean/Japanese/Chinese)
against English SKILL.md descriptions. ~570MB model — larger than the
default BGE-small (120MB), so it's opt-in via `pip install
mega-tron[multilingual]`.
"""
from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np


class BGEm3Embedder:
    """BAAI/bge-m3 via sentence-transformers."""

    model_id = "BAAI/bge-m3"
    dim = 1024

    def __init__(self, device: str = "cpu") -> None:
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(self.model_id, device=device)
        self._fingerprint: str | None = None

    @property
    def fingerprint(self) -> str:
        if self._fingerprint is None:
            try:

                first = next(self._model.parameters())
                fp = hashlib.sha256(first.detach().cpu().numpy().tobytes()).hexdigest()[:16]
            except Exception:
                fp = "unknown"
            self._fingerprint = f"{self.model_id}@{fp}"
        return self._fingerprint

    def embed(self, texts: list[str]) -> "np.ndarray":
        import numpy as np

        embs = self._model.encode(
            texts,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return embs.astype(np.float32, copy=False)
