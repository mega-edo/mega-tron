"""VerdictEmbeddingsStore — atomic write, schema/fingerprint guard,
append/query round-trip, malformed-load tolerance."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from mega_tron.verdicts.embeddings import (
    VerdictEmbeddingsStore,
    default_path,
)


def _norm(v: list[float]) -> np.ndarray:
    a = np.array(v, dtype=np.float32)
    n = np.linalg.norm(a)
    return a / n if n > 0 else a


@pytest.fixture
def store_path(tmp_path: Path, monkeypatch) -> Path:
    """Isolated store path via env override."""
    p = tmp_path / "ve.npz"
    monkeypatch.setenv("MEGA_TRON_VERDICT_EMBEDDINGS", str(p))
    return p


def test_empty_store_has_zero_len(store_path: Path):
    s = VerdictEmbeddingsStore(fingerprint="fp-A")
    assert len(s) == 0
    assert s.query(_norm([1, 0, 0]), top_k=5) == []


def test_default_path_honours_env_override(store_path: Path):
    assert default_path() == store_path


def test_append_then_query(store_path: Path):
    s = VerdictEmbeddingsStore(fingerprint="fp-A")
    s.append(verdict_id=1, skill_name="a", label="HELPFUL",
             embedding=_norm([1, 0, 0]))
    s.append(verdict_id=2, skill_name="b", label="HARMFUL",
             embedding=_norm([0, 1, 0]))
    s.append(verdict_id=3, skill_name="a", label="HELPFUL",
             embedding=_norm([0.9, 0.1, 0]))
    res = s.query(_norm([1, 0, 0]), top_k=2)
    assert len(res) == 2
    assert res[0][0] == 1  # verdict_id of the exact match
    assert res[0][2] == "HELPFUL"
    assert res[0][3] == pytest.approx(1.0, abs=1e-6)
    # Second-best is the near-aligned vector, also HELPFUL.
    assert res[1][0] == 3


def test_label_is_uppercased(store_path: Path):
    s = VerdictEmbeddingsStore(fingerprint="fp-A")
    s.append(verdict_id=1, skill_name="a", label="helpful",
             embedding=_norm([1, 0, 0]))
    res = s.query(_norm([1, 0, 0]), top_k=1)
    assert res[0][2] == "HELPFUL"


def test_save_and_reload_round_trip(store_path: Path):
    s1 = VerdictEmbeddingsStore(fingerprint="fp-A")
    s1.append(verdict_id=10, skill_name="webhook", label="HELPFUL",
              embedding=_norm([1, 0, 0]))
    s1.append(verdict_id=11, skill_name="webhook", label="HARMFUL",
              embedding=_norm([0, 1, 0]))
    s1.save()
    assert store_path.exists()
    s2 = VerdictEmbeddingsStore(fingerprint="fp-A")
    assert len(s2) == 2
    res = s2.query(_norm([1, 0, 0]), top_k=2)
    assert res[0][0] == 10
    assert res[1][0] == 11


def test_fingerprint_mismatch_silently_rebuilds(store_path: Path):
    """A different embedder fingerprint → ignore the existing file
    and start over (derived signal; safe to drop)."""
    s1 = VerdictEmbeddingsStore(fingerprint="fp-A")
    s1.append(verdict_id=1, skill_name="x", label="HELPFUL",
              embedding=_norm([1, 0, 0]))
    s1.save()
    # New process with a different embedder.
    s2 = VerdictEmbeddingsStore(fingerprint="fp-B")
    assert len(s2) == 0


def test_corrupt_npz_falls_back_to_empty(store_path: Path):
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text("not a valid npz file")
    s = VerdictEmbeddingsStore(fingerprint="fp-A")
    assert len(s) == 0
    # A save() recovers cleanly.
    s.append(verdict_id=1, skill_name="x", label="HELPFUL",
             embedding=_norm([1, 0, 0]))
    s.save()
    s2 = VerdictEmbeddingsStore(fingerprint="fp-A")
    assert len(s2) == 1


def test_query_with_mismatched_dim_returns_empty(store_path: Path):
    s = VerdictEmbeddingsStore(fingerprint="fp-A")
    s.append(verdict_id=1, skill_name="x", label="HELPFUL",
             embedding=_norm([1, 0, 0]))  # dim 3
    # Query with dim 5 → silently returns [] rather than crashing.
    assert s.query(_norm([1, 0, 0, 0, 0]), top_k=5) == []


def test_append_with_mismatched_dim_drops_silently(store_path: Path):
    s = VerdictEmbeddingsStore(fingerprint="fp-A")
    s.append(verdict_id=1, skill_name="x", label="HELPFUL",
             embedding=_norm([1, 0, 0]))
    # Wrong dim — refused.
    s.append(verdict_id=2, skill_name="x", label="HARMFUL",
             embedding=_norm([1, 0, 0, 0]))
    assert len(s) == 1


def test_top_k_clamps_to_corpus_size(store_path: Path):
    s = VerdictEmbeddingsStore(fingerprint="fp-A")
    s.append(verdict_id=1, skill_name="x", label="HELPFUL",
             embedding=_norm([1, 0, 0]))
    res = s.query(_norm([1, 0, 0]), top_k=20)
    assert len(res) == 1


def test_atomic_write_does_not_leave_temp(store_path: Path):
    s = VerdictEmbeddingsStore(fingerprint="fp-A")
    s.append(verdict_id=1, skill_name="x", label="HELPFUL",
             embedding=_norm([1, 0, 0]))
    s.save()
    tmp = store_path.with_suffix(store_path.suffix + ".tmp")
    assert not tmp.exists()  # replaced into place


# --------------------------------------------------------------------------- #
# compact() — near-duplicate cluster collapse
# --------------------------------------------------------------------------- #


def test_compact_noop_on_empty_store(store_path: Path):
    s = VerdictEmbeddingsStore(fingerprint="fp-A")
    report = s.compact()
    assert report["before"] == 0
    assert report["after"] == 0
    assert report["removed"] == 0


def test_compact_noop_on_singleton(store_path: Path):
    s = VerdictEmbeddingsStore(fingerprint="fp-A")
    s.append(verdict_id=1, skill_name="a", label="HELPFUL",
             embedding=_norm([1, 0, 0]))
    report = s.compact()
    assert report["removed"] == 0
    assert len(s) == 1


def test_compact_collapses_near_duplicates_within_group(store_path: Path):
    """Three near-identical embeddings in one (skill, label) group →
    two collapse into the newest survivor."""
    s = VerdictEmbeddingsStore(fingerprint="fp-A")
    s.append(verdict_id=1, skill_name="x", label="HELPFUL",
             embedding=_norm([1, 0.1, 0]))
    s.append(verdict_id=2, skill_name="x", label="HELPFUL",
             embedding=_norm([1, 0.12, 0]))
    s.append(verdict_id=3, skill_name="x", label="HELPFUL",
             embedding=_norm([1, 0.05, 0]))
    report = s.compact(threshold=0.95)
    assert report["before"] == 3
    assert report["after"] == 1
    assert report["clusters_collapsed"] == 1
    # Newest survives by default (highest verdict_id).
    assert s._verdict_ids == [3]


def test_compact_preserves_outliers(store_path: Path):
    """An outlier embedding within the same group must survive even
    when other members near-duplicate each other."""
    s = VerdictEmbeddingsStore(fingerprint="fp-A")
    s.append(verdict_id=1, skill_name="x", label="HELPFUL",
             embedding=_norm([1, 0, 0]))
    s.append(verdict_id=2, skill_name="x", label="HELPFUL",
             embedding=_norm([1, 0.05, 0]))  # near-dup of #1
    s.append(verdict_id=3, skill_name="x", label="HELPFUL",
             embedding=_norm([0, 1, 0]))     # outlier
    s.compact(threshold=0.95)
    surviving = set(s._verdict_ids)
    assert 3 in surviving  # outlier kept
    assert len(surviving) == 2


def test_compact_does_not_cross_label_boundary(store_path: Path):
    """HELPFUL and HARMFUL verdicts must not be merged even when their
    embeddings happen to be near-identical — they convey opposite
    routing signals."""
    s = VerdictEmbeddingsStore(fingerprint="fp-A")
    s.append(verdict_id=1, skill_name="x", label="HELPFUL",
             embedding=_norm([1, 0, 0]))
    s.append(verdict_id=2, skill_name="x", label="HARMFUL",
             embedding=_norm([1, 0, 0]))  # same vec, opposite label
    s.compact(threshold=0.5)  # aggressive
    assert len(s) == 2


def test_compact_does_not_cross_skill_boundary(store_path: Path):
    """Two different skills with similar-looking reasons must not
    collapse — they're meaningful evidence about different skills."""
    s = VerdictEmbeddingsStore(fingerprint="fp-A")
    s.append(verdict_id=1, skill_name="webhook", label="HELPFUL",
             embedding=_norm([1, 0, 0]))
    s.append(verdict_id=2, skill_name="jwt", label="HELPFUL",
             embedding=_norm([1, 0, 0]))
    s.compact(threshold=0.5)
    assert len(s) == 2


def test_compact_threshold_below_cluster_keeps_everything(store_path: Path):
    """When the cluster's actual cosine is below the threshold,
    nothing is collapsed."""
    s = VerdictEmbeddingsStore(fingerprint="fp-A")
    s.append(verdict_id=1, skill_name="x", label="HELPFUL",
             embedding=_norm([1, 0, 0]))
    s.append(verdict_id=2, skill_name="x", label="HELPFUL",
             embedding=_norm([0.6, 0.8, 0]))  # cos ≈ 0.6 with #1
    report = s.compact(threshold=0.95)
    assert report["removed"] == 0
    assert len(s) == 2


def test_compact_tie_breaks_on_longest_reason(store_path: Path):
    """When a get_reason lookup is provided, the longest-reason
    cluster member wins over the newest-id member."""
    s = VerdictEmbeddingsStore(fingerprint="fp-A")
    s.append(verdict_id=1, skill_name="x", label="HELPFUL",
             embedding=_norm([1, 0.1, 0]))
    s.append(verdict_id=2, skill_name="x", label="HELPFUL",
             embedding=_norm([1, 0.12, 0]))
    s.append(verdict_id=3, skill_name="x", label="HELPFUL",
             embedding=_norm([1, 0.05, 0]))  # newest

    reasons = {
        1: "this is a very long and informative evidence citation",
        2: "medium length explanation here",
        3: "short",  # newest but least informative
    }
    s.compact(threshold=0.95, get_reason=reasons.get)
    # Longest-reason wins → id=1.
    assert s._verdict_ids == [1]


def test_compact_dry_run_does_not_mutate(store_path: Path):
    s = VerdictEmbeddingsStore(fingerprint="fp-A")
    s.append(verdict_id=1, skill_name="x", label="HELPFUL",
             embedding=_norm([1, 0.1, 0]))
    s.append(verdict_id=2, skill_name="x", label="HELPFUL",
             embedding=_norm([1, 0.12, 0]))
    report = s.compact(threshold=0.95, dry_run=True)
    assert report["dry_run"] is True
    assert report["removed"] == 1
    assert len(s) == 2  # unchanged on disk-in-memory state


def test_compact_round_trips_through_save(store_path: Path):
    """A compacted store survives save/reload with the survivor set
    intact."""
    s = VerdictEmbeddingsStore(fingerprint="fp-A")
    s.append(verdict_id=1, skill_name="x", label="HELPFUL",
             embedding=_norm([1, 0.1, 0]))
    s.append(verdict_id=2, skill_name="x", label="HELPFUL",
             embedding=_norm([1, 0.12, 0]))
    s.append(verdict_id=3, skill_name="y", label="HARMFUL",
             embedding=_norm([0, 0, 1]))
    s.compact(threshold=0.95)
    s.save()
    s2 = VerdictEmbeddingsStore(fingerprint="fp-A")
    assert set(s2._verdict_ids) == {2, 3}  # 1 absorbed into 2 (newer)
    # Survivor's query alignment still works after the round-trip.
    res = s2.query(_norm([1, 0.1, 0]), top_k=1)
    assert res[0][0] == 2


def test_compact_handles_get_reason_exceptions(store_path: Path):
    """A get_reason callback that raises should not break compact —
    the row falls back to length 0 and the newer survivor wins."""
    s = VerdictEmbeddingsStore(fingerprint="fp-A")
    s.append(verdict_id=1, skill_name="x", label="HELPFUL",
             embedding=_norm([1, 0.1, 0]))
    s.append(verdict_id=2, skill_name="x", label="HELPFUL",
             embedding=_norm([1, 0.12, 0]))

    def get_reason(_):
        raise RuntimeError("db down")

    s.compact(threshold=0.95, get_reason=get_reason)
    # Both reason-lengths read as 0; newest (id=2) wins on tie via
    # newest-first traversal.
    assert s._verdict_ids == [2]


# --------------------------------------------------------------------------- #
# remove_by_verdict_id — dashboard verdict-edit drawer hook
# --------------------------------------------------------------------------- #


def test_remove_by_verdict_id_drops_row_and_persists(store_path: Path):
    s = VerdictEmbeddingsStore(fingerprint="fp-A")
    s.append(verdict_id=1, skill_name="a", label="HELPFUL",
             embedding=_norm([1, 0, 0]))
    s.append(verdict_id=2, skill_name="a", label="HELPFUL",
             embedding=_norm([0, 1, 0]))
    s.append(verdict_id=3, skill_name="b", label="HARMFUL",
             embedding=_norm([0, 0, 1]))

    assert s.remove_by_verdict_id(2) is True
    assert len(s) == 2
    assert s._verdict_ids == [1, 3]
    assert s._skills == ["a", "b"]
    # Query no longer returns the removed row.
    res = s.query(_norm([0, 1, 0]), top_k=5)
    assert all(vid != 2 for vid, *_ in res)

    # Reload from disk — save() ran inside remove_by_verdict_id.
    s2 = VerdictEmbeddingsStore(fingerprint="fp-A")
    assert s2._verdict_ids == [1, 3]


def test_remove_by_verdict_id_missing_returns_false(store_path: Path):
    s = VerdictEmbeddingsStore(fingerprint="fp-A")
    s.append(verdict_id=1, skill_name="a", label="HELPFUL",
             embedding=_norm([1, 0, 0]))
    assert s.remove_by_verdict_id(99) is False
    assert len(s) == 1


def test_remove_by_verdict_id_on_empty_store(store_path: Path):
    s = VerdictEmbeddingsStore(fingerprint="fp-A")
    assert s.remove_by_verdict_id(1) is False
    assert len(s) == 0

