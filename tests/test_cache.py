"""Cache npz save/load + per-skill SHA invalidation."""
from __future__ import annotations

from pathlib import Path

import numpy as np

from mega_tron.cache import Cache, file_sha16, make_sync_row


def _row(name: str, sha: str, dir_: Path = Path("/tmp/dummy"), desc: str = "d") -> tuple:
    return make_sync_row(name, dir_, desc, len(desc) // 4, sha)


def test_file_sha16_stable(tmp_path):
    p = tmp_path / "skill.md"
    p.write_text("hello world")
    a = file_sha16(p)
    b = file_sha16(p)
    assert a == b
    assert len(a) == 16


def test_file_sha16_changes_with_content(tmp_path):
    p = tmp_path / "skill.md"
    p.write_text("v1")
    a = file_sha16(p)
    p.write_text("v2")
    b = file_sha16(p)
    assert a != b


def test_cache_sync_embeds_only_new(fake_embedder, tmp_cache_path):
    cache = Cache(path=tmp_cache_path)
    rows = [_row("a", "sha-a"), _row("b", "sha-b")]
    n_new, n_reused = cache.sync(rows, fake_embedder)
    assert n_new == 2
    assert n_reused == 0


def test_cache_sync_reuses_unchanged(fake_embedder, tmp_cache_path):
    cache = Cache(path=tmp_cache_path)
    cache.sync([_row("a", "sha-a"), _row("b", "sha-b")], fake_embedder)
    # Same SHAs second time → all reused.
    n_new, n_reused = cache.sync(
        [_row("a", "sha-a"), _row("b", "sha-b")],
        fake_embedder,
    )
    assert n_new == 0
    assert n_reused == 2


def test_cache_sync_re_embeds_changed(fake_embedder, tmp_cache_path):
    cache = Cache(path=tmp_cache_path)
    cache.sync([_row("a", "sha-a"), _row("b", "sha-b")], fake_embedder)
    # 'b' changed.
    n_new, n_reused = cache.sync(
        [_row("a", "sha-a"), _row("b", "sha-b-CHANGED")],
        fake_embedder,
    )
    assert n_new == 1
    assert n_reused == 1


def test_cache_sync_drops_removed(fake_embedder, tmp_cache_path):
    cache = Cache(path=tmp_cache_path)
    cache.sync([_row("a", "sha-a"), _row("b", "sha-b")], fake_embedder)
    cache.sync([_row("a", "sha-a")], fake_embedder)
    names = [e.name for e in cache.entries()]
    assert names == ["a"]


def test_cache_sync_invalidates_on_model_change(fake_embedder, tmp_cache_path):
    cache = Cache(path=tmp_cache_path)
    cache.sync([_row("a", "sha-a")], fake_embedder)

    class OtherEmbedder:
        model_id = "another-model"
        dim = 16

        def embed(self, texts):
            return np.zeros((len(texts), self.dim), dtype=np.float32)

    n_new, n_reused = cache.sync([_row("a", "sha-a")], OtherEmbedder())
    assert n_new == 1  # full rebuild
    assert n_reused == 0


def test_cache_round_trip_npz(fake_embedder, tmp_cache_path):
    cache = Cache(path=tmp_cache_path)
    cache.sync(
        [_row("a", "sha-a", dir_=Path("/tmp/a")), _row("b", "sha-b", dir_=Path("/tmp/b"))],
        fake_embedder,
    )
    cache.save()
    assert tmp_cache_path.exists()

    cache2 = Cache(path=tmp_cache_path)
    cache2.load()
    assert {e.name for e in cache2.entries()} == {"a", "b"}
    a = next(e for e in cache2.entries() if e.name == "a")
    assert a.sha == "sha-a"
    assert a.skill_dir == Path("/tmp/a")
    assert a.embedding.shape == (16,)


def test_cache_save_atomic(fake_embedder, tmp_cache_path):
    """No .tmp file should remain after a successful save."""
    cache = Cache(path=tmp_cache_path)
    cache.sync([_row("a", "sha-a")], fake_embedder)
    cache.save()
    tmp_files = list(tmp_cache_path.parent.glob("*.tmp"))
    assert tmp_files == []


# --------------------------------------------------------------------------
# Context embeddings (helpful/harmful_contexts)
# --------------------------------------------------------------------------


def _row_v3(
    name: str,
    sha: str,
    helpful_ctxs=(),
    harmful_ctxs=(),
    dir_: Path = Path("/tmp/dummy"),
    desc: str = "d",
) -> tuple:
    return make_sync_row(
        name, dir_, desc, len(desc) // 4, sha,
        helpful_contexts=list(helpful_ctxs),
        harmful_contexts=list(harmful_ctxs),
    )


def test_cache_sync_embeds_helpful_contexts(fake_embedder, tmp_cache_path):
    cache = Cache(path=tmp_cache_path)
    rows = [_row_v3("a", "sha-a", helpful_ctxs=["catches HMAC mismatch", "handles replay"])]
    n_new, _ = cache.sync(rows, fake_embedder)
    assert n_new == 1
    entry = cache.entries()[0]
    assert entry.helpful_contexts == ["catches HMAC mismatch", "handles replay"]
    assert entry.embedding_helpful_ctxs is not None
    assert entry.embedding_helpful_ctxs.shape == (2, fake_embedder.dim)
    assert entry.embedding_harmful_ctxs is None  # no harmful contexts


def test_cache_sync_embeds_both_polarities(fake_embedder, tmp_cache_path):
    cache = Cache(path=tmp_cache_path)
    rows = [_row_v3("a", "sha-a", helpful_ctxs=["h1"], harmful_ctxs=["bad1", "bad2"])]
    cache.sync(rows, fake_embedder)
    entry = cache.entries()[0]
    assert entry.embedding_helpful_ctxs.shape == (1, fake_embedder.dim)
    assert entry.embedding_harmful_ctxs.shape == (2, fake_embedder.dim)


def test_cache_v3_round_trip(fake_embedder, tmp_cache_path):
    """save → load preserves context strings + embeddings."""
    cache = Cache(path=tmp_cache_path)
    cache.sync(
        [_row_v3("a", "sha-a", helpful_ctxs=["alpha"], harmful_ctxs=["beta", "gamma"])],
        fake_embedder,
    )
    cache.save()

    cache2 = Cache(path=tmp_cache_path)
    cache2.load()
    e = next(e for e in cache2.entries() if e.name == "a")
    assert e.helpful_contexts == ["alpha"]
    assert e.harmful_contexts == ["beta", "gamma"]
    assert e.embedding_helpful_ctxs is not None
    assert e.embedding_helpful_ctxs.shape == (1, fake_embedder.dim)
    assert e.embedding_harmful_ctxs.shape == (2, fake_embedder.dim)


# --------------------------------------------------------------------------
# mega_meta counts/status caching
# --------------------------------------------------------------------------


def _row_v4(
    name: str,
    sha: str,
    *,
    helpful_ctxs=(),
    harmful_ctxs=(),
    helpful_count: int = 0,
    harmful_count: int = 0,
    status: str = "active",
    consecutive_harmful: int = 0,
    dir_: Path = Path("/tmp/dummy"),
    desc: str = "d",
) -> tuple:
    return make_sync_row(
        name,
        dir_,
        desc,
        len(desc) // 4,
        sha,
        helpful_contexts=list(helpful_ctxs),
        harmful_contexts=list(harmful_ctxs),
        helpful_count=helpful_count,
        harmful_count=harmful_count,
        status=status,
        consecutive_harmful=consecutive_harmful,
    )


def test_cache_v4_stores_counts_and_status(fake_embedder, tmp_cache_path):
    cache = Cache(path=tmp_cache_path)
    cache.sync(
        [
            _row_v4(
                "skill-a",
                "sha-a",
                helpful_count=7,
                harmful_count=2,
                status="suspect",
                consecutive_harmful=1,
            )
        ],
        fake_embedder,
    )
    e = cache.entries()[0]
    assert e.helpful_count == 7
    assert e.harmful_count == 2
    assert e.status == "suspect"
    assert e.consecutive_harmful == 1


def test_cache_v4_round_trip_preserves_counts(fake_embedder, tmp_cache_path):
    cache = Cache(path=tmp_cache_path)
    cache.sync(
        [
            _row_v4(
                "skill-a",
                "sha-a",
                helpful_count=12,
                harmful_count=4,
                status="suspect",
                consecutive_harmful=2,
            ),
            _row_v4("skill-b", "sha-b", status="archived", consecutive_harmful=3),
        ],
        fake_embedder,
    )
    cache.save()

    cache2 = Cache(path=tmp_cache_path)
    cache2.load()
    a = next(e for e in cache2.entries() if e.name == "skill-a")
    b = next(e for e in cache2.entries() if e.name == "skill-b")
    assert (a.helpful_count, a.harmful_count, a.status, a.consecutive_harmful) == (12, 4, "suspect", 2)
    assert (b.helpful_count, b.harmful_count, b.status, b.consecutive_harmful) == (0, 0, "archived", 3)


def test_cache_v4_sha_unchanged_keeps_counts_synced(fake_embedder, tmp_cache_path):
    """SHA-stable sync should still refresh counts/status from the row.

    Drives the safety claim in the v0.4 plan: even when the embedding is
    reused, the cached row's mega_meta view tracks the latest SKILL.md state.
    """
    cache = Cache(path=tmp_cache_path)
    cache.sync([_row_v4("a", "sha-a", helpful_count=1)], fake_embedder)
    # Same SHA, different counts — represents what would happen if SHA-keyed
    # invalidation were bypassed (e.g. a downstream tool tweaks counts without
    # rewriting the file). The cache must mirror whatever the row says.
    cache.sync(
        [_row_v4("a", "sha-a", helpful_count=5, harmful_count=1, status="suspect")],
        fake_embedder,
    )
    e = cache.entries()[0]
    assert e.helpful_count == 5
    assert e.harmful_count == 1
    assert e.status == "suspect"


def test_cache_v3_on_disk_triggers_full_rebuild(fake_embedder, tmp_cache_path):
    """Loading a v3-formatted cache (no count fields) rebuilds cleanly at v4."""
    import numpy as np

    dim = fake_embedder.dim
    one = np.empty(1, dtype=object)
    one[0] = []
    with open(tmp_cache_path, "wb") as fh:
        np.savez(
            fh,
            schema_version=np.int32(3),
            names=np.array(["legacy"]),
            embeddings=np.zeros((1, dim), dtype=np.float32),
            embeddings_name=np.zeros((1, dim), dtype=np.float32),
            shas=np.array(["sha-old"]),
            skill_dirs=np.array(["/tmp/x"]),
            descriptions=np.array(["old"]),
            desc_toks=np.array([1]),
            model_id="fake-test-embedder-v1",
            fingerprint="fake-test-embedder-v1@deterministic",
            helpful_contexts=one,
            harmful_contexts=one,
            embeddings_helpful_ctxs=one,
            embeddings_harmful_ctxs=one,
        )

    cache = Cache(path=tmp_cache_path)
    cache.load()
    assert cache.entries() == []  # v3 cache rejected — rebuild required
    cache.sync(
        [_row_v4("legacy", "sha-new", helpful_count=3, status="active")],
        fake_embedder,
    )
    cache.save()

    cache2 = Cache(path=tmp_cache_path)
    cache2.load()
    e = cache2.entries()[0]
    assert e.helpful_count == 3
    assert e.status == "active"
