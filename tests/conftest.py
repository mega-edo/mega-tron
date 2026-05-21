"""Shared pytest fixtures.

We do NOT load the real BGE model in unit tests — too slow and pulls in PyTorch.
Integration tests that need real embeddings use the `real_embedder` fixture and
are marked `slow` so they can be opted out of with `pytest -m 'not slow'`.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "slow: marks tests that load a real sentence-transformers model (>10s)",
    )


@pytest.fixture(autouse=True)
def _isolate_default_store(tmp_path, monkeypatch):
    """Block every test from touching the production
    ``~/.local/share/mega-tron/store.db`` and
    ``~/.local/share/mega-tron/verdict_embeddings.npz``.

    Background: an unguarded ``MegaCore()`` call defaults its SQLite
    store path to :func:`mega_tron.config.store_path` and writes
    verdicts there. Without this fixture, a test that constructs a
    ``MegaCore`` without an explicit ``store=`` argument silently
    appends rows to the user's real DB — exactly how 24 noise rows
    ("ok"/"wrong audience"/"evidence A"/etc.) accumulated in
    production data over time.

    The override is process-local (monkeypatched env vars) and
    auto-cleaned at test teardown.
    """
    monkeypatch.setenv("MEGA_TRON_STORE", str(tmp_path / "_isolated-store.db"))
    monkeypatch.setenv(
        "MEGA_TRON_VERDICT_EMBEDDINGS",
        str(tmp_path / "_isolated-verdict-embeddings.npz"),
    )
    # XDG_DATA_HOME covers anything else under data_dir() that a
    # forgotten helper might pick up.
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "_isolated-xdg-data"))
    yield


class FakeEmbedder:
    """Deterministic stand-in for the real embedder.

    Produces unit vectors derived from a stable hash of each text. The same text
    always maps to the same vector, and lexically similar texts produce vectors
    with non-trivial cosine similarity (we share a few dimensions across
    keyword presence).
    """

    model_id = "fake-test-embedder-v1"
    dim = 16
    fingerprint = "fake-test-embedder-v1@deterministic"

    KEYWORDS = (
        "webhook",
        "signature",
        "hmac",
        "git",
        "commit",
        "pull",
        "push",
        "branch",
        "prisma",
        "database",
        "test",
        "linear",
        "ticket",
        "url",
        "parse",
        "timing",
    )

    def embed(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, t in enumerate(texts):
            lower = t.lower()
            for j, kw in enumerate(self.KEYWORDS):
                if kw in lower:
                    out[i, j] = 1.0
            # Tiny pseudo-random tail so identical-keyword texts aren't equal.
            seed = sum(ord(c) for c in t) % 997
            out[i, 0] += (seed % 11) * 0.01
            norm = np.linalg.norm(out[i])
            if norm > 0:
                out[i] /= norm
        return out


@pytest.fixture
def fake_embedder() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def tmp_cache_path(tmp_path: Path) -> Path:
    return tmp_path / "test_cache.npz"


@pytest.fixture
def tmp_codex_home(tmp_path: Path) -> Path:
    target = tmp_path / "codex_home"
    target.mkdir()
    return target
