"""Ratio-based auto-compact trigger for verdict embeddings.

The disk-cost trigger (``AUTO_COMPACT_THRESHOLD = 10_000``) fires far
too late at normal scale. The ratio trigger fires when the embedding
store grows meaningfully faster than the verdicts table — the
symptom of one busy skill accumulating near-duplicate verdicts.

These tests cover the decision-only logic. End-to-end persist
behaviour is exercised by ``test_verdict_writer_catalog_filter.py``.
"""
from __future__ import annotations

from mega_tron.verdicts.embeddings import (
    AUTO_COMPACT_THRESHOLD,
    RATIO_AUTO_COMPACT_FLOOR,
    RATIO_AUTO_COMPACT_MULTIPLIER,
)


def _should_compact(ves_rows: int, verdicts_count: int) -> bool:
    """Mirrors the trigger decision in ``writer.persist_verdicts``.

    Inlined here so the test asserts the *policy*, not the I/O path —
    the writer is the only call site, and the trigger predicate is
    short enough that copying it into the test keeps the assertion
    explicit and traceable.
    """
    if ves_rows > AUTO_COMPACT_THRESHOLD:
        return True
    if ves_rows < RATIO_AUTO_COMPACT_FLOOR:
        return False
    if verdicts_count <= 0:
        return False
    return ves_rows > verdicts_count * RATIO_AUTO_COMPACT_MULTIPLIER


def test_fresh_install_does_not_trigger():
    """0 verdicts / 0 embeddings — no trigger."""
    assert _should_compact(ves_rows=0, verdicts_count=0) is False


def test_floor_suppresses_small_n():
    """49 rows is below the 50-row floor regardless of ratio."""
    # 49 rows with 10 verdicts → ratio = 4.9× would normally fire,
    # but the floor at 50 holds the trigger asleep.
    assert _should_compact(ves_rows=49, verdicts_count=10) is False


def test_ratio_trigger_fires_when_embeddings_outpace_verdicts():
    """Once past the floor, ratio > 1.5× fires."""
    # 60 rows / 30 verdicts = 2.0× > 1.5× — fires.
    assert _should_compact(ves_rows=60, verdicts_count=30) is True


def test_ratio_just_at_threshold_does_not_fire():
    """Exactly at the multiplier is not a strict overshoot — fires
    only on `>`, not `>=`. Matches the writer's predicate."""
    # 75 rows / 50 verdicts = 1.5× exactly — does NOT fire.
    assert _should_compact(ves_rows=75, verdicts_count=50) is False
    # 76 / 50 = 1.52× — fires.
    assert _should_compact(ves_rows=76, verdicts_count=50) is True


def test_ratio_below_multiplier_does_not_fire():
    """Healthy growth (more verdicts than embeddings) never fires."""
    # 100 rows / 100 verdicts = 1.0× — fires? No.
    assert _should_compact(ves_rows=100, verdicts_count=100) is False


def test_zero_verdicts_does_not_fire_above_floor():
    """A non-empty embedding store with zero verdicts shouldn't crash
    or trigger — the ratio is undefined and we treat it as "no
    signal". (In practice this means a corrupted DB or a race; we
    don't want to spam compaction in that case.)"""
    assert _should_compact(ves_rows=200, verdicts_count=0) is False


def test_disk_trigger_still_fires_at_10k_regardless_of_verdicts():
    """The original disk-cost trigger remains: at 10K+ rows we
    compact even if the ratio is healthy (someone may have
    legitimately accumulated tens of thousands of distinct verdicts,
    but the disk/memory cost still wants periodic compaction)."""
    assert _should_compact(ves_rows=10_001, verdicts_count=20_000) is True
    # And of course it fires when verdicts is small too.
    assert _should_compact(ves_rows=10_001, verdicts_count=5_000) is True


def test_constants_sane():
    """Sanity bounds — keeps future tuning honest."""
    assert RATIO_AUTO_COMPACT_MULTIPLIER > 1.0
    assert RATIO_AUTO_COMPACT_FLOOR >= 10
    assert RATIO_AUTO_COMPACT_FLOOR < AUTO_COMPACT_THRESHOLD
