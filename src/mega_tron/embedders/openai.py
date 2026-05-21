"""OpenAI Embedder adapter — `text-embedding-3-small` by default."""
from __future__ import annotations

from typing import TYPE_CHECKING

from mega_tron.embedders._retry import retry

if TYPE_CHECKING:
    import numpy as np


class OpenAIEmbedder:
    """OpenAI embeddings. Lazy SDK import; never opens a client at import time.

    Args:
        model: any OpenAI embedding model id (default `text-embedding-3-small`).
        dim: output dimension; must match the model. `text-embedding-3-small`
            is 1536, `text-embedding-3-large` is 3072.
        api_key: forwarded to the SDK; defaults to `OPENAI_API_KEY` env var.
        batch_size: max texts per request. OpenAI accepts up to ~2048.
    """

    def __init__(
        self,
        model: str = "text-embedding-3-small",
        dim: int = 1536,
        api_key: str | None = None,
        batch_size: int = 1024,
    ) -> None:
        try:
            from openai import OpenAI
        except ImportError as e:  # pragma: no cover — install-time path
            raise ImportError(
                "OpenAIEmbedder requires `openai` — install with "
                "`pip install mega-tron[openai]`."
            ) from e

        self.model_id = model
        self.dim = dim
        self.batch_size = batch_size
        self._client = OpenAI(api_key=api_key)

    @property
    def fingerprint(self) -> str:
        # OpenAI pins model versions internally; trust model id alone.
        return f"openai:{self.model_id}:{self.dim}"

    def embed(self, texts: list[str]) -> "np.ndarray":
        import numpy as np

        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            chunk = texts[i : i + self.batch_size]

            def _call(chunk=chunk):
                return self._client.embeddings.create(model=self.model_id, input=chunk)

            resp = retry(_call)
            out.extend(item.embedding for item in resp.data)
        arr = np.asarray(out, dtype=np.float32)
        # OpenAI vectors are already L2-normalized, but renormalize defensively.
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return arr / norms
