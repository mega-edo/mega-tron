"""SkillRet-Embedding adapter — Qwen3-Embedding-0.6B fine-tuned on the
SkillRet train split (127K contrastive pairs) for skill retrieval.

Reference: Cho, Kang & Kim, "SkillRet: A Large-Scale Benchmark for Skill
Retrieval in LLM Agents", arXiv:2605.05726 (2026).
https://arxiv.org/abs/2605.05726

Published baseline: NDCG@10 = 0.7803 on SkillRet test (vs BGE-large 0.5582,
Qwen3-Embedding-8B 0.5998). The model is a *task-specific* fine-tune — it
gives mega-tron a much stronger prefilter than BGE-small.

Why this matters for us: the failure-mode analysis on the 50-query dry run
showed 22 of 89 gold skills were missing from BGE-small's top-200 (25%
prefilter miss). Replacing the prefilter with a model trained specifically
for skill retrieval is the highest-leverage single change available.

Prefix asymmetry (per the model card):
    queries → must be prefixed with the instruction template below
    docs    → no prefix, just the skill ``name\\ndescription`` text

The :class:`Embedder` Protocol stays single-method (``embed(texts)``); the
asymmetry lives in the optional ``query_prefix`` attribute that
:meth:`Router.rank` prepends before calling ``embed`` on query text.
Cache.sync() never touches ``query_prefix`` so skill embeddings remain
prefix-free, exactly as the model expects.

Source: https://huggingface.co/ThakiCloud/SKILLRET-Embedding-0.6B (Apache-2.0).
"""
from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np


# Instruction prefix from the model card — exact string matters because the
# model was trained with this template; deviating drops retrieval quality.
SKILLRET_QUERY_PREFIX = (
    "Instruct: Given a skill search query, retrieve relevant skills "
    "that match the query\nQuery: "
)


class SkillRetEmbedder:
    """Qwen3-Embedding-0.6B fine-tuned for SkillRet (sentence-transformers).

    Auto-picks the best device on the host (CUDA → Apple MPS → CPU). Pass
    ``device="cpu"`` to force CPU for reproducibility benchmarks.
    """

    model_id = "ThakiCloud/SKILLRET-Embedding-0.6B"
    query_prefix = SKILLRET_QUERY_PREFIX

    def __init__(self, device: str = "auto") -> None:
        from sentence_transformers import SentenceTransformer

        resolved = self._pick_device() if device == "auto" else device
        self.device = resolved
        self._model = SentenceTransformer(
            self.model_id,
            device=resolved,
            trust_remote_code=True,
        )
        # Qwen3-Embedding-0.6B is 1024-dim; read it from the model to stay
        # honest if the upstream ever ships a variant.
        self.dim = int(self._model.get_sentence_embedding_dimension())
        self._fingerprint: str | None = None

    @staticmethod
    def _pick_device() -> str:
        try:
            import torch

            if torch.cuda.is_available():
                return "cuda"
            if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                return "mps"
        except ImportError:
            pass
        return "cpu"

    @property
    def fingerprint(self) -> str:
        """``model_id@<sha256-of-first-layer-weights[:16]>`` — same
        scheme as the generic :class:`SentenceTransformerEmbedder` so
        :class:`Cache` invalidates correctly when the model file changes."""
        if self._fingerprint is None:
            try:
                first = next(self._model.parameters())
                h = hashlib.sha256(
                    first.detach().cpu().numpy().tobytes()
                ).hexdigest()[:16]
            except Exception:
                h = "unknown"
            self._fingerprint = f"{self.model_id}@{h}"
        return self._fingerprint

    def embed(self, texts: list[str]) -> "np.ndarray":
        """Doc-side encode (no prefix). Returns ``(len(texts), dim)`` float32,
        L2-normalized so the router's dot-product cosine works unchanged.
        """
        import numpy as np

        embs = self._model.encode(
            texts,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return embs.astype(np.float32, copy=False)


__all__ = ["SkillRetEmbedder", "SKILLRET_QUERY_PREFIX"]
