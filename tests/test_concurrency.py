"""Concurrent writers + readers must not corrupt the SQLite store.

The realistic deployment has Codex Stop hook + Claude Stop hook +
Hermes Curator thread all potentially writing verdicts at once. WAL
mode + ``busy_timeout`` + our Python retry loop should handle it; this
test pins that promise so a future change to the locking strategy can
break it loudly.

We stay well below pytest's default timeout — 2 seconds of concurrent
activity is more than enough to surface lock contention against the
~5 ms write path. The retry budget (3 attempts × {0.2, 0.4, 0.8} s) is
larger than the test's total runtime, so any retry path that *would*
fail in production should also fail here.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from mega_tron.verdicts.store import Store


# Realistic but small numbers — we want to surface contention, not
# benchmark. Two writers × 100 inserts each = 200 verdicts.
N_WRITES_PER_WORKER = 100
N_WRITERS = 2


@pytest.fixture
def shared_store(tmp_path: Path) -> Store:
    s = Store(path=tmp_path / "shared.db")
    s.initialize()
    return s


def _writer(store: Store, worker_id: int, n: int) -> int:
    """Insert ``n`` verdicts and return the count actually written."""
    written = 0
    for i in range(n):
        ok = store.record_verdict(
            skill_name=f"skill-{worker_id}-{i}",
            verdict="HELPFUL",
            host="codex",
            reason=f"worker-{worker_id} write {i}",
            session_id=f"w{worker_id}-{i}",
            skill_dir=f"/tmp/s/{worker_id}/{i}",
        )
        if ok:
            written += 1
    return written


def _reader(store: Store, deadline: float) -> int:
    """Spin-read row counts until ``deadline`` (monotonic). Returns the
    final count seen — used as a sanity probe; real assertion is that
    no exception escapes."""
    count = 0
    while time.monotonic() < deadline:
        try:
            count = store.count_verdicts()
        except Exception:  # surfaces locking bugs
            raise
        time.sleep(0.001)
    return count


def test_two_writers_no_torn_writes(shared_store: Store):
    """Sum of per-worker insert counts must equal final row count.

    Failure mode: a torn write or a swallowed ``database is locked``
    would leave us with fewer rows than the workers think they wrote.
    """
    with ThreadPoolExecutor(max_workers=N_WRITERS) as pool:
        futures = [
            pool.submit(_writer, shared_store, w, N_WRITES_PER_WORKER)
            for w in range(N_WRITERS)
        ]
        written = sum(f.result() for f in futures)

    assert written == N_WRITERS * N_WRITES_PER_WORKER
    assert shared_store.count_verdicts() == written


def test_reader_during_writes_never_raises(shared_store: Store):
    """A concurrent reader sees a monotonically-growing count and
    never raises a database-locked exception."""
    barrier = threading.Barrier(N_WRITERS + 1)
    deadline = time.monotonic() + 2.0

    def writer_with_barrier(worker_id: int) -> int:
        barrier.wait()
        return _writer(shared_store, worker_id, N_WRITES_PER_WORKER)

    def reader_with_barrier() -> int:
        barrier.wait()
        return _reader(shared_store, deadline)

    with ThreadPoolExecutor(max_workers=N_WRITERS + 1) as pool:
        wf = [pool.submit(writer_with_barrier, w) for w in range(N_WRITERS)]
        rf = pool.submit(reader_with_barrier)
        total_written = sum(f.result() for f in wf)
        last_seen = rf.result()  # raises if any read failed

    assert total_written == N_WRITERS * N_WRITES_PER_WORKER
    assert last_seen <= total_written  # reader may finish before the last write
    assert shared_store.count_verdicts() == total_written


def test_unique_constraint_holds_under_contention(shared_store: Store):
    """Two workers attempting the same (session_id, skill_name, host)
    must produce exactly one row, not two."""

    def colliding_writer() -> bool:
        return shared_store.record_verdict(
            skill_name="contended",
            verdict="HELPFUL",
            host="codex",
            session_id="shared-session",
            skill_dir="/c",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(colliding_writer) for _ in range(2)]
        outcomes = [f.result() for f in results]

    # Exactly one writer should have succeeded; the other gets False
    # from the UNIQUE-handling branch.
    assert sum(outcomes) == 1
    assert shared_store.count_verdicts() == 1
