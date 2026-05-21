"""Voyage AI Embedder adapter — `voyage-3` by default."""
from __future__ import annotations

from typing import TYPE_CHECKING

from mega_tron.embedders._retry import retry

if TYPE_CHECKING:
    import numpy as np


class VoyageEmbedder:
    """Voyage AI embeddings. Lazy SDK import.

    Args:
        model: voyage model id (default `voyage-3`).
        dim: output dimension. `voyage-3` is 1024.
        api_key: forwarded to the SDK; defaults to `VOYAGE_API_KEY` env var.
        batch_size: max texts per request. Voyage accepts up to 128.
        input_type: "document" or "query" — Voyage tunes embeddings per role.
            We use "document" for skill descriptions; integrators can pass
            "query" for the query side if they care about the asymmetry.
    """

    def __init__(
        self,
        model: str = "voyage-3",
        dim: int = 1024,
        api_key: str | None = None,
        batch_size: int = 128,
        input_type: str = "document",
    ) -> None:
        try:
            import voyageai
        except ImportError as e:  # pragma: no cover
            raise ImportError(
                "VoyageEmbedder requires `voyageai` — install with "
                "`pip install mega-tron[voyage]`."
            ) from e

        self.model_id = model
        self.dim = dim
        self.batch_size = batch_size
        self.input_type = input_type
        self._client = voyageai.Client(api_key=api_key) if api_key else voyageai.Client()

    @property
    def fingerprint(self) -> str:
        return f"voyage:{self.model_id}:{self.dim}:{self.input_type}"

    def embed(self, texts: list[str]) -> "np.ndarray":
        import numpy as np

        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            chunk = texts[i : i + self.batch_size]

            def _call(chunk=chunk):
                return self._client.embed(chunk, model=self.model_id, input_type=self.input_type)

            resp = retry(_call)
            out.extend(resp.embeddings)
        arr = np.asarray(out, dtype=np.float32)
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return arr / norms
