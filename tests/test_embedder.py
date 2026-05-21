"""Embedder Protocol + real BGE smoke test (opt-in `slow`)."""
from __future__ import annotations

import numpy as np
import pytest

from mega_tron.embedder import Embedder


def test_fake_embedder_satisfies_protocol(fake_embedder):
    assert isinstance(fake_embedder, Embedder)
    assert fake_embedder.model_id
    assert fake_embedder.dim > 0


def test_fake_embedder_shape(fake_embedder):
    vecs = fake_embedder.embed(["hello", "world"])
    assert vecs.shape == (2, fake_embedder.dim)
    assert vecs.dtype == np.float32


def test_fake_embedder_normalized(fake_embedder):
    vecs = fake_embedder.embed(["webhook signature"])
    norm = float(np.linalg.norm(vecs[0]))
    assert abs(norm - 1.0) < 1e-5


def test_fake_embedder_keyword_overlap_drives_similarity(fake_embedder):
    """Two webhook-related texts must score higher together than against an unrelated one."""
    a, b, c = fake_embedder.embed(
        [
            "webhook signature hmac",
            "validate webhook hmac",
            "prisma database query",
        ]
    )
    assert float(a @ b) > float(a @ c)


@pytest.mark.slow
def test_real_bge_smoke():
    """Smoke-load a small public model from HuggingFace cache."""
    from mega_tron.embedder import SentenceTransformerEmbedder

    emb = SentenceTransformerEmbedder("BAAI/bge-small-en-v1.5", device="cpu")
    vecs = emb.embed(["hello world"])
    assert vecs.shape == (1, 384)
    assert vecs.dtype == np.float32
    norm = float(np.linalg.norm(vecs[0]))
    assert abs(norm - 1.0) < 1e-3
