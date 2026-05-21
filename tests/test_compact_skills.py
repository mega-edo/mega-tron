"""Skill-level near-duplicate dedup (compact_skills).

The clustering walks cached entries best-winner-first, absorbs each
sibling above the cosine threshold into the highest-similarity existing
cluster, and persists the loser names in a sidecar JSON next to the
.npz so suppression survives warmup. These tests pin each branch of
that contract.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from mega_tron.cache import (
    Cache,
    CacheEntry,
    _suppressed_path_for,
    make_sync_row,
)


def _entry(
    name: str,
    *,
    vec: np.ndarray,
    skill_dir: Path,
    status: str = "active",
    helpful: int = 0,
    harmful: int = 0,
    sha: str = "deadbeefdeadbeef",
) -> CacheEntry:
    """Build a CacheEntry with hand-crafted unit vectors so we can pin
    cosine relationships exactly. Mirrors the production embedding shape
    (full + name vectors L2-normalised)."""
    v = vec.astype(np.float32)
    v /= np.linalg.norm(v) or 1.0
    return CacheEntry(
        name=name,
        embedding=v,
        embedding_name=v,
        sha=sha,
        skill_dir=skill_dir,
        description=f"description-of-{name}",
        desc_tok=4,
        helpful_count=helpful,
        harmful_count=harmful,
        status=status,
        consecutive_harmful=0,
    )


def _write_skill_md(parent: Path, name: str, mtime: float | None = None) -> Path:
    """Materialise a SKILL.md so the mtime tiebreak has something real
    to read. Returns the skill_dir path that goes onto the CacheEntry."""
    d = parent / name
    d.mkdir(parents=True, exist_ok=True)
    md = d / "SKILL.md"
    md.write_text(f"---\nname: {name}\ndescription: x\n---\n\nbody\n")
    if mtime is not None:
        import os
        os.utime(md, (mtime, mtime))
    return d


def test_compact_skills_empty_cache(tmp_cache_path):
    """No entries → no-op report; never crashes on empty matmul."""
    cache = Cache(path=tmp_cache_path)
    report = cache.compact_skills(threshold=0.95)
    assert report["before"] == 0
    assert report["after"] == 0
    assert report["removed"] == 0
    assert report["clusters_collapsed"] == 0
    assert report["clusters"] == []


def test_compact_skills_no_duplicates(tmp_path, tmp_cache_path):
    """Orthogonal vectors → no cluster collapses, all entries survive."""
    cache = Cache(path=tmp_cache_path)
    sd_a = _write_skill_md(tmp_path, "a")
    sd_b = _write_skill_md(tmp_path, "b")
    cache.upsert(_entry("a", vec=np.array([1.0, 0.0, 0.0]), skill_dir=sd_a))
    cache.upsert(_entry("b", vec=np.array([0.0, 1.0, 0.0]), skill_dir=sd_b))

    report = cache.compact_skills(threshold=0.95)
    assert report["removed"] == 0
    assert report["clusters_collapsed"] == 0
    assert {e.name for e in cache.entries()} == {"a", "b"}


def test_compact_skills_collapses_near_duplicates(tmp_path, tmp_cache_path):
    """Two vectors at cos ≈ 0.999 → one survivor, the other is suppressed."""
    cache = Cache(path=tmp_cache_path)
    sd_winner = _write_skill_md(tmp_path, "winner")
    sd_loser = _write_skill_md(tmp_path, "loser")
    # Both essentially point the same direction.
    base = np.array([1.0, 0.01, 0.0])
    nearby = np.array([1.0, 0.02, 0.0])
    cache.upsert(
        _entry("winner", vec=base, skill_dir=sd_winner, helpful=10, harmful=0)
    )
    cache.upsert(
        _entry("loser", vec=nearby, skill_dir=sd_loser, helpful=0, harmful=0)
    )

    report = cache.compact_skills(threshold=0.95)
    assert report["removed"] == 1
    assert report["clusters_collapsed"] == 1
    cluster = report["clusters"][0]
    assert cluster["winner"] == "winner"
    assert cluster["losers"] == ["loser"]
    assert cluster["max_sim"] > 0.95
    assert cache.suppressed_names() == {"loser"}
    assert "loser" not in {e.name for e in cache.entries()}


def test_compact_skills_winner_priority_status_first(tmp_path, tmp_cache_path):
    """Status (active > suspect > archived) outranks verdict score and mtime."""
    cache = Cache(path=tmp_cache_path)
    sd_active = _write_skill_md(tmp_path, "active_skill", mtime=1000.0)
    sd_suspect = _write_skill_md(tmp_path, "suspect_skill", mtime=9999.0)
    cache.upsert(
        _entry(
            "active_skill",
            vec=np.array([1.0, 0.0, 0.001]),
            skill_dir=sd_active,
            status="active",
            helpful=0,
            harmful=0,
        )
    )
    cache.upsert(
        _entry(
            "suspect_skill",
            vec=np.array([1.0, 0.0, 0.002]),
            skill_dir=sd_suspect,
            status="suspect",
            helpful=100,
            harmful=0,
        )
    )

    report = cache.compact_skills(threshold=0.95)
    assert report["clusters_collapsed"] == 1
    assert report["clusters"][0]["winner"] == "active_skill"
    assert "suspect_skill" in cache.suppressed_names()


def test_compact_skills_winner_priority_verdict_then_mtime(tmp_path, tmp_cache_path):
    """When statuses tie, net verdict score (helpful − harmful) wins.
    mtime breaks score ties (newer = better)."""
    cache = Cache(path=tmp_cache_path)
    sd_old = _write_skill_md(tmp_path, "old_winner", mtime=100.0)
    sd_new_loser = _write_skill_md(tmp_path, "new_loser", mtime=99999.0)
    cache.upsert(
        _entry(
            "old_winner",
            vec=np.array([1.0, 0.0, 0.001]),
            skill_dir=sd_old,
            helpful=5,
            harmful=0,
        )
    )
    cache.upsert(
        _entry(
            "new_loser",
            vec=np.array([1.0, 0.0, 0.002]),
            skill_dir=sd_new_loser,
            helpful=0,
            harmful=0,
        )
    )

    report = cache.compact_skills(threshold=0.95)
    assert report["clusters"][0]["winner"] == "old_winner"
    assert cache.suppressed_names() == {"new_loser"}

    # Now equal verdict scores → mtime wins.
    cache2 = Cache(path=tmp_cache_path.parent / "second.npz")
    cache2.upsert(
        _entry(
            "stale",
            vec=np.array([1.0, 0.0, 0.001]),
            skill_dir=_write_skill_md(tmp_path, "stale", mtime=100.0),
        )
    )
    cache2.upsert(
        _entry(
            "fresh",
            vec=np.array([1.0, 0.0, 0.002]),
            skill_dir=_write_skill_md(tmp_path, "fresh", mtime=99999.0),
        )
    )
    r2 = cache2.compact_skills(threshold=0.95)
    assert r2["clusters"][0]["winner"] == "fresh"


def test_compact_skills_dry_run_no_mutation(tmp_path, tmp_cache_path):
    """dry_run=True must not touch _entries, the suppression set, or
    the sidecar file."""
    cache = Cache(path=tmp_cache_path)
    sd_a = _write_skill_md(tmp_path, "a")
    sd_b = _write_skill_md(tmp_path, "b")
    cache.upsert(
        _entry("a", vec=np.array([1.0, 0.0, 0.0]), skill_dir=sd_a, helpful=5)
    )
    cache.upsert(
        _entry("b", vec=np.array([1.0, 0.001, 0.0]), skill_dir=sd_b)
    )

    report = cache.compact_skills(threshold=0.95, dry_run=True)
    assert report["removed"] == 1  # report still shows what *would* happen
    assert cache.suppressed_names() == set()
    assert {e.name for e in cache.entries()} == {"a", "b"}
    assert not _suppressed_path_for(tmp_cache_path).exists()


def test_compact_skills_sidecar_persists_across_load(fake_embedder, tmp_path, tmp_cache_path):
    """Suppression survives save() → new Cache instance → sync() — the
    whole point of the sidecar."""
    cache = Cache(path=tmp_cache_path)
    # First seed with sync so the .npz path is properly populated.
    cache.sync(
        [
            make_sync_row("winner", tmp_path / "winner", "d", 1, "sha-w"),
            make_sync_row("loser", tmp_path / "loser", "d", 1, "sha-l"),
        ],
        fake_embedder,
    )
    # Force the cluster: overwrite the embeddings to be near-identical.
    base = np.array([1.0, 0.0, 0.001], dtype=np.float32)
    base /= np.linalg.norm(base)
    near = np.array([1.0, 0.0, 0.002], dtype=np.float32)
    near /= np.linalg.norm(near)
    cache._entries["winner"].embedding = base
    cache._entries["loser"].embedding = near
    cache._entries["winner"].helpful_count = 10  # winner outranks on verdict score

    report = cache.compact_skills(threshold=0.95)
    assert report["removed"] == 1
    cache.save()
    assert _suppressed_path_for(tmp_cache_path).exists()

    # Re-open fresh Cache; suppression set should reload.
    cache2 = Cache(path=tmp_cache_path)
    assert cache2.suppressed_names() == {"loser"}
    cache2.load()
    # sync() with both names present must NOT re-add the loser.
    cache2.sync(
        [
            make_sync_row("winner", tmp_path / "winner", "d", 1, "sha-w"),
            make_sync_row("loser", tmp_path / "loser", "d", 1, "sha-l"),
        ],
        fake_embedder,
    )
    assert "loser" not in {e.name for e in cache2.entries()}
    assert "winner" in {e.name for e in cache2.entries()}


def test_compact_skills_unsuppress_all_restores(fake_embedder, tmp_path, tmp_cache_path):
    """unsuppress_all() clears the set; the next sync() repopulates the
    cleared skill from the host's rows."""
    cache = Cache(path=tmp_cache_path)
    cache.sync(
        [
            make_sync_row("winner", tmp_path / "winner", "d", 1, "sha-w"),
            make_sync_row("loser", tmp_path / "loser", "d", 1, "sha-l"),
        ],
        fake_embedder,
    )
    base = np.array([1.0, 0.0, 0.001], dtype=np.float32)
    base /= np.linalg.norm(base)
    near = np.array([1.0, 0.0, 0.002], dtype=np.float32)
    near /= np.linalg.norm(near)
    cache._entries["winner"].embedding = base
    cache._entries["loser"].embedding = near
    cache._entries["winner"].helpful_count = 10
    cache.compact_skills(threshold=0.95)
    assert cache.suppressed_names() == {"loser"}

    n = cache.unsuppress_all()
    assert n == 1
    assert cache.suppressed_names() == set()

    cache.sync(
        [
            make_sync_row("winner", tmp_path / "winner", "d", 1, "sha-w"),
            make_sync_row("loser", tmp_path / "loser", "d", 1, "sha-l"),
        ],
        fake_embedder,
    )
    assert {e.name for e in cache.entries()} == {"winner", "loser"}


def test_compact_skills_fingerprint_change_clears_suppression(fake_embedder, tmp_path, tmp_cache_path):
    """A different embedder fingerprint wipes _entries AND _suppressed —
    clusters computed in one geometry don't survive a model swap."""
    cache = Cache(path=tmp_cache_path)
    cache.sync(
        [make_sync_row("a", tmp_path / "a", "d", 1, "sha-a")],
        fake_embedder,
    )
    cache._suppressed.add("phantom")  # simulate a prior compaction loser
    assert cache.suppressed_names() == {"phantom"}

    class OtherEmbedder:
        model_id = "another-model"
        dim = fake_embedder.dim
        fingerprint = "different-fp"

        def embed(self, texts):
            return np.zeros((len(texts), self.dim), dtype=np.float32)

    cache.sync(
        [make_sync_row("a", tmp_path / "a", "d", 1, "sha-a")],
        OtherEmbedder(),
    )
    assert cache.suppressed_names() == set()


def test_compact_skills_sidecar_file_format(tmp_path, tmp_cache_path):
    """The sidecar JSON layout is part of the on-disk contract — pin it."""
    cache = Cache(path=tmp_cache_path)
    sd_w = _write_skill_md(tmp_path, "w")
    sd_l = _write_skill_md(tmp_path, "l")
    cache.upsert(_entry("w", vec=np.array([1.0, 0.0, 0.0]), skill_dir=sd_w, helpful=5))
    cache.upsert(_entry("l", vec=np.array([1.0, 0.001, 0.0]), skill_dir=sd_l))
    cache.compact_skills(threshold=0.95)
    cache._save_suppressed()

    sp = _suppressed_path_for(tmp_cache_path)
    assert sp.exists()
    payload = json.loads(sp.read_text())
    assert payload["version"] == 1
    assert payload["suppressed"] == ["l"]


def test_compact_skills_unsuppress_removes_sidecar(tmp_path, tmp_cache_path):
    """Empty suppression set deletes the sidecar — absence is the default."""
    cache = Cache(path=tmp_cache_path)
    sd_w = _write_skill_md(tmp_path, "w")
    sd_l = _write_skill_md(tmp_path, "l")
    cache.upsert(_entry("w", vec=np.array([1.0, 0.0, 0.0]), skill_dir=sd_w, helpful=5))
    cache.upsert(_entry("l", vec=np.array([1.0, 0.001, 0.0]), skill_dir=sd_l))
    cache.compact_skills(threshold=0.95)
    cache._save_suppressed()
    sp = _suppressed_path_for(tmp_cache_path)
    assert sp.exists()

    cache.unsuppress_all()
    cache._save_suppressed()
    assert not sp.exists()


def test_compact_skills_threshold_respected(tmp_path, tmp_cache_path):
    """Cos ~0.97 pair → collapses at 0.95 but not at 0.99."""
    cache = Cache(path=tmp_cache_path)
    sd_a = _write_skill_md(tmp_path, "a")
    sd_b = _write_skill_md(tmp_path, "b")
    # Construct a pair with cosine in the (0.95, 0.99) window.
    v1 = np.array([1.0, 0.0, 0.0])
    v2 = np.array([1.0, 0.20, 0.0])  # cos ≈ 0.9806
    cache.upsert(_entry("a", vec=v1, skill_dir=sd_a, helpful=5))
    cache.upsert(_entry("b", vec=v2, skill_dir=sd_b))

    r_loose = cache.compact_skills(threshold=0.95, dry_run=True)
    assert r_loose["clusters_collapsed"] == 1

    r_strict = cache.compact_skills(threshold=0.99, dry_run=True)
    assert r_strict["clusters_collapsed"] == 0


def test_compact_skills_three_in_one_cluster(tmp_path, tmp_cache_path):
    """A 3-member cluster collapses to one winner + two losers; report
    lists both losers in a single cluster entry."""
    cache = Cache(path=tmp_cache_path)
    sd_a = _write_skill_md(tmp_path, "a")
    sd_b = _write_skill_md(tmp_path, "b")
    sd_c = _write_skill_md(tmp_path, "c")
    cache.upsert(
        _entry("a", vec=np.array([1.0, 0.0, 0.0]), skill_dir=sd_a, helpful=10)
    )
    cache.upsert(
        _entry("b", vec=np.array([1.0, 0.001, 0.0]), skill_dir=sd_b)
    )
    cache.upsert(
        _entry("c", vec=np.array([1.0, 0.002, 0.0]), skill_dir=sd_c)
    )

    report = cache.compact_skills(threshold=0.95)
    assert report["clusters_collapsed"] == 1
    assert report["removed"] == 2
    cluster = report["clusters"][0]
    assert cluster["winner"] == "a"
    assert set(cluster["losers"]) == {"b", "c"}
