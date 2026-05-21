"""On-disk verdict embeddings for self-improving routing.

Every time the host Stop hook persists a verdict, we embed the
reason text and append the vector here. At routing time the
Router queries this store with the user's prompt embedding and
finds the K most semantically-similar past verdicts — HELPFUL
matches push the involved skill up the ranking, HARMFUL matches
push it down ("I tried that for a similar task last week and it
broke").

Storage: a single ``.npz`` file at
``~/.local/share/mega-tron/verdict_embeddings.npz``. Why a flat
file rather than SQLite BLOB or a vector DB:

- Scale: realistic load is < 100K verdicts over 5 years. At 1024
  dim float32 that's ~400 MB worst-case — single-mmap territory.
- Speed: top-K against 100K vectors via ``np.matmul`` is ~5 ms;
  FAISS would shave maybe 2 ms but adds a heavy dep with known
  conda-cohabitation issues. Re-evaluate at 1M+.
- Operability: ``ls -lh`` shows you the corpus size; ``np.load``
  inspects it; no extension needed.

Schema bump (or fingerprint mismatch) → silent rebuild on the next
write. Verdict embeddings are derived signal — losing them only
costs the next few routing turns until the corpus repopulates.
Cumulative counters live in SKILL.md frontmatter and are untouched.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from mega_tron.config import data_dir

if TYPE_CHECKING:
    from mega_tron.embedder import Embedder


SCHEMA_VERSION = 1
"""Bump on any change to the npz field set or vector encoding."""


AUTO_COMPACT_THRESHOLD = 10_000
"""Verdict-count high-water mark above which the writer fires a
background compact() pass. Sized so that even a 5-years-of-active-use
corpus (~91K verdicts) goes through ~9 compact() calls — each one
cheap because the group-wise matmuls stay small."""


AUTO_COMPACT_COSINE = 0.95
"""Default cosine threshold for near-duplicate clustering. Conservative
on purpose: sentence-transformer space puts true paraphrases above
0.95 and merely-similar-topic verdicts in the 0.85-0.93 range, so
this collapses duplicates without losing diversity."""


def default_path() -> Path:
    """Resolve the default store path. Mirrors :func:`config.store_path`
    style — env override first, then ``data_dir()``."""
    override = os.environ.get("MEGA_TRON_VERDICT_EMBEDDINGS")
    if override:
        return Path(override).expanduser()
    return data_dir() / "verdict_embeddings.npz"


class VerdictEmbeddingsStore:
    """Append-only on-disk store of (verdict_id, skill_name, label,
    embedding) tuples.

    Thread-safe via a per-instance lock. Embedding vectors are
    expected to be L2-normalised on input so cosine similarity
    reduces to a dot product.

    The store reloads itself from disk on construction; subsequent
    ``append()`` calls mutate the in-memory arrays, and ``save()``
    flushes atomically (tmp + ``os.replace``).
    """

    def __init__(
        self,
        path: Path | None = None,
        *,
        fingerprint: str,
    ) -> None:
        self.path = Path(path) if path is not None else default_path()
        self.fingerprint = fingerprint
        self._lock = threading.Lock()
        self._verdict_ids: list[int] = []
        self._skills: list[str] = []
        self._labels: list[str] = []
        self._vecs: np.ndarray | None = None  # shape (N, dim)
        self._load_or_init()

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #

    def _load_or_init(self) -> None:
        if not self.path.exists():
            return
        try:
            data = np.load(self.path, allow_pickle=True)
        except Exception:  # noqa: BLE001
            # Corrupt npz file (truncated, wrong magic, pickle decode
            # failure, etc.) — silently rebuild on next save. The
            # blanket except is intentional: numpy raises a zoo of
            # exception types here depending on how the file is broken.
            return
        try:
            loaded_schema = int(data["schema_version"])
        except (KeyError, ValueError):
            loaded_schema = 0
        loaded_fp = str(data["fingerprint"]) if "fingerprint" in data.files else ""
        if loaded_schema != SCHEMA_VERSION or loaded_fp != self.fingerprint:
            # Schema or embedder model changed → start over rather than
            # mix incompatible vectors. Derived signal; safe to drop.
            return
        try:
            ids = data["verdict_ids"]
            skills = data["skills"]
            labels = data["labels"]
            vecs = data["embeddings"]
        except KeyError:
            return
        # The arrays come back as numpy types; we keep skills/labels as
        # plain Python strings for cheap equality checks downstream.
        self._verdict_ids = [int(v) for v in ids.tolist()]
        self._skills = [str(s) for s in skills.tolist()]
        self._labels = [str(s) for s in labels.tolist()]
        self._vecs = np.asarray(vecs, dtype=np.float32)

    def save(self) -> None:
        """Atomic write of the in-memory state."""
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            vecs = self._vecs if self._vecs is not None else np.empty(
                (0, 0), dtype=np.float32
            )
            with open(tmp, "wb") as fh:
                np.savez(
                    fh,
                    schema_version=np.int32(SCHEMA_VERSION),
                    fingerprint=self.fingerprint,
                    verdict_ids=np.array(self._verdict_ids, dtype=np.int64),
                    skills=np.array(self._skills, dtype=object),
                    labels=np.array(self._labels, dtype=object),
                    embeddings=vecs,
                )
            tmp.replace(self.path)

    # ------------------------------------------------------------------ #
    # Mutation
    # ------------------------------------------------------------------ #

    def append(
        self,
        *,
        verdict_id: int,
        skill_name: str,
        label: str,
        embedding: np.ndarray,
    ) -> None:
        """Append one (verdict, skill, label, vector) tuple. Caller is
        responsible for flushing via :meth:`save`.

        ``embedding`` must be L2-normalised. We don't re-normalise here
        because the embedder protocol guarantees normalised output and
        re-normalising on every append wastes cycles.
        """
        emb = np.asarray(embedding, dtype=np.float32).reshape(-1)
        with self._lock:
            self._verdict_ids.append(int(verdict_id))
            self._skills.append(str(skill_name))
            self._labels.append(str(label).upper())
            if self._vecs is None or self._vecs.size == 0:
                self._vecs = emb[None, :].copy()
            else:
                if emb.shape[0] != self._vecs.shape[1]:
                    # Dimension changed mid-stream (probably from a model
                    # swap we missed). Refuse silently rather than crash.
                    self._verdict_ids.pop()
                    self._skills.pop()
                    self._labels.pop()
                    return
                self._vecs = np.vstack([self._vecs, emb[None, :]])

    def remove_by_verdict_id(self, verdict_id: int) -> bool:
        """Drop the row(s) matching ``verdict_id`` from all parallel
        arrays. Returns ``True`` on hit, ``False`` if no such id is
        currently stored. Persists via :meth:`save` so the on-disk
        ``.npz`` reflects the removal.

        Used by the dashboard's verdict-edit drawer: after a flip the
        old reason's embedding no longer represents the row's label,
        and after a delete the row is gone entirely. Either way the
        embedding is invalid; cheaper to drop than to recompute.
        """
        with self._lock:
            try:
                idx = self._verdict_ids.index(int(verdict_id))
            except ValueError:
                return False
            self._verdict_ids.pop(idx)
            self._skills.pop(idx)
            self._labels.pop(idx)
            if self._vecs is not None and self._vecs.size > 0:
                self._vecs = np.delete(self._vecs, idx, axis=0)
        self.save()
        return True

    # ------------------------------------------------------------------ #
    # Query
    # ------------------------------------------------------------------ #

    def __len__(self) -> int:
        return len(self._verdict_ids)

    def query(
        self,
        q_embedding: np.ndarray,
        *,
        top_k: int = 20,
    ) -> list[tuple[int, str, str, float]]:
        """Return the top-K most-similar verdicts to ``q_embedding``.

        Returns a list of ``(verdict_id, skill_name, label, cosine)``
        tuples, ranked by cosine descending. Empty list when the store
        is empty.
        """
        with self._lock:
            if self._vecs is None or self._vecs.shape[0] == 0:
                return []
            q = np.asarray(q_embedding, dtype=np.float32).reshape(-1)
            if q.shape[0] != self._vecs.shape[1]:
                return []
            sims = self._vecs @ q  # both L2-norm → dot == cosine
            n = sims.shape[0]
            k = min(top_k, n)
            if k <= 0:
                return []
            # Partial sort: argpartition for the top-k, then sort that slice.
            idx = np.argpartition(-sims, k - 1)[:k]
            idx = idx[np.argsort(-sims[idx])]
            return [
                (
                    int(self._verdict_ids[i]),
                    str(self._skills[i]),
                    str(self._labels[i]),
                    float(sims[i]),
                )
                for i in idx
            ]

    # ------------------------------------------------------------------ #
    # Compaction — near-duplicate cluster collapse
    # ------------------------------------------------------------------ #

    def compact(
        self,
        *,
        threshold: float = 0.95,
        get_reason: "callable[[int], str | None] | None" = None,
        dry_run: bool = False,
    ) -> dict:
        """Collapse near-duplicate verdict embeddings within each
        (skill_name, label) group.

        Active users accumulate many semantically-equivalent verdicts
        ("validated webhook HMAC", "verified webhook signature", ...).
        At cos > ``threshold`` they carry no additional routing signal
        — the cluster representative is enough. This method keeps **one
        representative per cluster** and drops the rest from the
        embedding store. The SQLite ``verdicts`` time-series is
        untouched so regression analysis stays time-series-true.

        Cluster representative selection (highest priority first):

        1. **Longest reason text** — most informative for `mega-tron
           why` and future re-embedding. Requires ``get_reason``; when
           it isn't provided (or returns ``None``) we fall through.
        2. **Newest verdict_id** — the time-series uses an
           AUTOINCREMENT id, so a higher id ≈ more recent. Reflects
           the model's most-recent calibration of the situation.

        Args:
            threshold: cosine cutoff. Two verdicts are "near-duplicates"
                when ``embedding_a @ embedding_b > threshold``. Default
                0.95 — sentence-transformer space puts truly-equivalent
                paraphrases here without sweeping in different-task
                verdicts (which sit around 0.85).
            get_reason: optional ``verdict_id -> reason | None``
                lookup. Plumb in
                :func:`mega_tron.verdicts.store.Store.get_verdict_reason` (or
                an equivalent) so the longest-reason tie-break activates.
                When omitted, falls back to newest-verdict-id.
            dry_run: when ``True``, returns the report without mutating
                the in-memory or on-disk state. Use to preview impact
                before applying.

        Returns:
            A report dict:

              - ``before``: row count pre-compaction
              - ``after``: row count post-compaction (== before on dry-run)
              - ``removed``: rows that would be / were dropped
              - ``clusters_collapsed``: number of (skill, label)
                clusters with ≥ 2 members
              - ``groups_scanned``: total (skill, label) groups
              - ``dry_run``: whether the operation mutated anything
        """
        with self._lock:
            n_before = len(self._verdict_ids)
            if n_before <= 1 or self._vecs is None:
                return {
                    "before": n_before, "after": n_before, "removed": 0,
                    "clusters_collapsed": 0, "groups_scanned": 0,
                    "dry_run": dry_run,
                }
            # Group row indices by (skill, label).
            groups: dict[tuple[str, str], list[int]] = {}
            for i, (s, l) in enumerate(zip(self._skills, self._labels)):
                groups.setdefault((s, l), []).append(i)

            keep_mask = np.ones(n_before, dtype=bool)
            clusters_collapsed = 0

            for (_s, _l), idxs in groups.items():
                if len(idxs) < 2:
                    continue
                # Per-group similarity matrix is small (typical group
                # has 1-50 entries), so a plain matmul is fine.
                sub = self._vecs[idxs]  # (m, dim) — all L2-normalised
                sims = sub @ sub.T      # (m, m)
                # Greedy single-pass clustering: walk rows in
                # "kept-so-far" order. A row joins the first earlier-
                # surviving row whose cosine > threshold; otherwise it
                # becomes a new cluster representative.
                #
                # We bias representative selection to *newest* by
                # walking idxs in DESCENDING verdict_id order — the
                # first survivor wins, so it's the most recent member.
                # That makes the "newest" tiebreak automatic.
                order = sorted(
                    range(len(idxs)),
                    key=lambda r: -int(self._verdict_ids[idxs[r]]),
                )
                representatives: list[int] = []  # row indices within sub
                for rank in order:
                    absorbed = False
                    for rep in representatives:
                        if sims[rank, rep] > threshold:
                            # rank gets dropped; rep stays.
                            keep_mask[idxs[rank]] = False
                            absorbed = True
                            break
                    if not absorbed:
                        representatives.append(rank)

                if len(representatives) < len(idxs):
                    clusters_collapsed += 1
                    # Longest-reason refinement: within each absorbed
                    # cluster, if the caller plumbed get_reason in,
                    # promote the longest-reason member over the
                    # newest-id member.
                    if get_reason is not None:
                        # Re-build the cluster assignment so we can
                        # compare members per representative.
                        cluster_of: dict[int, int] = {}  # rank -> rep rank
                        for rank in order:
                            absorbed_to = None
                            for rep in representatives:
                                if rep == rank or sims[rank, rep] > threshold:
                                    absorbed_to = rep
                                    break
                            if absorbed_to is not None:
                                cluster_of[rank] = absorbed_to
                        # Group cluster members by representative.
                        members: dict[int, list[int]] = {}
                        for rank, rep in cluster_of.items():
                            members.setdefault(rep, []).append(rank)
                        for rep, mem_ranks in members.items():
                            if len(mem_ranks) < 2:
                                continue
                            # Fetch reason lengths; missing → 0.
                            lens = {}
                            for r in mem_ranks:
                                vid = int(self._verdict_ids[idxs[r]])
                                try:
                                    text = get_reason(vid)
                                except Exception:
                                    text = None
                                lens[r] = len(text) if isinstance(text, str) else 0
                            best = max(mem_ranks, key=lambda r: lens[r])
                            if best != rep:
                                # Swap survivor: rep dies, best lives.
                                keep_mask[idxs[rep]] = False
                                keep_mask[idxs[best]] = True

            n_after = int(keep_mask.sum())
            report = {
                "before": n_before,
                "after": n_after,
                "removed": n_before - n_after,
                "clusters_collapsed": clusters_collapsed,
                "groups_scanned": len(groups),
                "dry_run": dry_run,
            }
            if dry_run:
                return report

            # Apply mask in-memory; caller flushes via save().
            keep_idx = np.flatnonzero(keep_mask)
            self._verdict_ids = [self._verdict_ids[i] for i in keep_idx]
            self._skills = [self._skills[i] for i in keep_idx]
            self._labels = [self._labels[i] for i in keep_idx]
            self._vecs = self._vecs[keep_idx]
            return report


def make_store(embedder: "Embedder | None" = None) -> "VerdictEmbeddingsStore | None":
    """Convenience constructor.

    Returns ``None`` when no embedder is available (the caller can run
    without it — verdict embeddings are an optional improvement signal).
    """
    if embedder is None:
        try:
            from mega_tron.embedder import make_embedder

            embedder = make_embedder()
        except Exception:
            return None
    from mega_tron.embedder import fingerprint_of

    return VerdictEmbeddingsStore(fingerprint=fingerprint_of(embedder))


__all__ = [
    "SCHEMA_VERSION",
    "VerdictEmbeddingsStore",
    "default_path",
    "make_store",
]
