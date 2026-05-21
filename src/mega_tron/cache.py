"""On-disk embedding cache (.npz) keyed by SKILL.md SHA.

Layout (npz):
- schema_version: scalar int. Bumping invalidates all existing caches.
- names: (N,) array of skill names.
- embeddings: (N, dim) float32, L2-normalized — encodes
  ``name\\n\\ndescription``.
- embeddings_name: (N, dim) float32 — encodes name alone for the
  max(cos) blend.
- embeddings_helpful_ctxs / embeddings_harmful_ctxs: dtype=object,
  length N, each element either None or a float32 array of shape
  ``(k, dim)`` with up to 3 rows. These are the embeddings of
  natural-language ``mega_meta`` evaluation contexts, used by
  ``ranker.py`` for task-specific quality boosts.
- helpful_contexts / harmful_contexts: dtype=object, length N, each
  element a ``list[str]`` (preserved verbatim for stats/debug; not
  consumed by ranking directly).
- helpful_counts / harmful_counts: (N,) int32 — ``mega_meta`` counters.
- statuses: (N,) object — ``"active" | "suspect" | "archived"``.
- consecutive_harmful_counts: (N,) int32 — current HARMFUL streak.
- shas, skill_dirs, descriptions, desc_toks, model_id, fingerprint: metadata.

Per-skill SHA comparison means only changed SKILL.md files re-embed.
Because the Stop hook's ``update_meta()`` rewrites the frontmatter
(changing the file SHA), context-vector freshness piggybacks on the
same invalidation — no separate plumbing. Counts/status ride the same
SHA wave: every ``update_meta()`` flips the SHA, the cached row is
re-synced from the new YAML, and ``Router.rank()`` reads from
``CacheEntry`` instead of re-parsing YAML.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

    from mega_tron.embedder import Embedder


SCHEMA_VERSION = 1


def file_sha16(path: Path) -> str:
    """Truncated SHA256 of file contents. 16 hex chars (64 bits) — collision-resistant
    enough for ~10^5 skill files."""
    h = hashlib.sha256(path.read_bytes()).hexdigest()
    return h[:16]


# Row format consumed by `Cache.sync`. Kept as a free function so callers
# (Router.warmup, mega-symphony's hybrid hook) can build rows without
# importing CacheEntry.
def make_sync_row(
    name: str,
    skill_dir: Path,
    description: str,
    desc_tok: int,
    sha: str,
    helpful_contexts: list[str] | None = None,
    harmful_contexts: list[str] | None = None,
    *,
    helpful_count: int = 0,
    harmful_count: int = 0,
    status: str = "active",
    consecutive_harmful: int = 0,
) -> tuple:
    """Build an 11-tuple row consumed by :meth:`Cache.sync`.

    Positional args carry name, skill_dir, description, desc_tok, and
    sha; the variable-length context lists and the mega_meta
    counts/status fields ride via keyword args.
    """
    return (
        name,
        skill_dir,
        description,
        desc_tok,
        sha,
        list(helpful_contexts or []),
        list(harmful_contexts or []),
        int(helpful_count),
        int(harmful_count),
        str(status or "active"),
        int(consecutive_harmful),
    )


@dataclass
class CacheEntry:
    name: str
    embedding: "np.ndarray"  # (dim,) full `name\n\ndescription` vector
    sha: str
    skill_dir: Path
    description: str
    desc_tok: int
    # Schema v2+: name-only vector for the dual-score branch.
    embedding_name: "np.ndarray | None" = field(default=None)
    # Schema v3+: context embeddings and the raw NL strings they came from.
    helpful_contexts: list[str] = field(default_factory=list)
    harmful_contexts: list[str] = field(default_factory=list)
    embedding_helpful_ctxs: "np.ndarray | None" = field(default=None)  # (k, dim) or None
    embedding_harmful_ctxs: "np.ndarray | None" = field(default=None)
    # Schema v4: `mega_meta` counters + status. Cached so Router.rank doesn't
    # have to re-parse YAML on every call. SHA-keyed invalidation keeps these
    # in sync with SKILL.md — update_meta() rewrites the file → SHA flips →
    # row re-syncs.
    helpful_count: int = 0
    harmful_count: int = 0
    status: str = "active"
    consecutive_harmful: int = 0


def _normalize_row(row: tuple) -> tuple:
    """Validate the sync-row shape: 11-tuple from :func:`make_sync_row`."""
    if len(row) == 11:
        return row
    raise ValueError(f"sync row must be an 11-tuple, got len={len(row)}")


class Cache:
    """Per-skill embedding cache. Atomic write on save()."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._entries: dict[str, CacheEntry] = {}
        self._model_id: str | None = None
        self._fingerprint: str | None = None
        self._schema_version: int = SCHEMA_VERSION

    def load(self) -> None:
        """Load cache from disk if present. Silent no-op if missing."""
        if not self.path.exists():
            return
        import numpy as np

        data = np.load(self.path, allow_pickle=True)
        loaded_schema = int(data["schema_version"]) if "schema_version" in data.files else 1
        if loaded_schema != SCHEMA_VERSION:
            # Older schema — refuse to load; sync() will rebuild.
            self._schema_version = loaded_schema
            return
        loaded_model = str(data["model_id"])
        names = data["names"]
        embeddings = data["embeddings"]
        embeddings_name = (
            data["embeddings_name"] if "embeddings_name" in data.files else None
        )
        shas = data["shas"]
        skill_dirs = data["skill_dirs"] if "skill_dirs" in data.files else None
        descriptions = data["descriptions"] if "descriptions" in data.files else None
        desc_toks = data["desc_toks"] if "desc_toks" in data.files else None
        fingerprint = str(data["fingerprint"]) if "fingerprint" in data.files else None
        helpful_ctx_strs = (
            data["helpful_contexts"] if "helpful_contexts" in data.files else None
        )
        harmful_ctx_strs = (
            data["harmful_contexts"] if "harmful_contexts" in data.files else None
        )
        helpful_ctx_embs = (
            data["embeddings_helpful_ctxs"]
            if "embeddings_helpful_ctxs" in data.files
            else None
        )
        harmful_ctx_embs = (
            data["embeddings_harmful_ctxs"]
            if "embeddings_harmful_ctxs" in data.files
            else None
        )
        # v4 fields. Optional in cache files so the loader stays tolerant of
        # half-written or mid-migration caches.
        helpful_counts = (
            data["helpful_counts"] if "helpful_counts" in data.files else None
        )
        harmful_counts = (
            data["harmful_counts"] if "harmful_counts" in data.files else None
        )
        statuses = data["statuses"] if "statuses" in data.files else None
        consec_h = (
            data["consecutive_harmful_counts"]
            if "consecutive_harmful_counts" in data.files
            else None
        )
        self._model_id = loaded_model
        self._fingerprint = fingerprint
        self._schema_version = SCHEMA_VERSION
        self._entries = {}
        for i, name in enumerate(names):
            self._entries[str(name)] = CacheEntry(
                name=str(name),
                embedding=embeddings[i],
                embedding_name=embeddings_name[i] if embeddings_name is not None else None,
                sha=str(shas[i]),
                skill_dir=Path(str(skill_dirs[i])) if skill_dirs is not None else Path(),
                description=str(descriptions[i]) if descriptions is not None else "",
                desc_tok=int(desc_toks[i]) if desc_toks is not None else 0,
                helpful_contexts=list(helpful_ctx_strs[i]) if helpful_ctx_strs is not None else [],
                harmful_contexts=list(harmful_ctx_strs[i]) if harmful_ctx_strs is not None else [],
                embedding_helpful_ctxs=(
                    helpful_ctx_embs[i] if helpful_ctx_embs is not None else None
                ),
                embedding_harmful_ctxs=(
                    harmful_ctx_embs[i] if harmful_ctx_embs is not None else None
                ),
                helpful_count=int(helpful_counts[i]) if helpful_counts is not None else 0,
                harmful_count=int(harmful_counts[i]) if harmful_counts is not None else 0,
                status=str(statuses[i]) if statuses is not None else "active",
                consecutive_harmful=int(consec_h[i]) if consec_h is not None else 0,
            )

    def save(self) -> None:
        """Write cache to disk atomically (tmp + rename)."""
        import numpy as np

        if not self._entries:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        entries = list(self._entries.values())
        names = np.array([e.name for e in entries])
        embeddings = np.stack([e.embedding for e in entries])
        dim = embeddings.shape[1]
        embeddings_name = np.stack(
            [
                e.embedding_name if e.embedding_name is not None
                else np.zeros(dim, dtype=np.float32)
                for e in entries
            ]
        )
        shas = np.array([e.sha for e in entries])
        skill_dirs = np.array([str(e.skill_dir) for e in entries])
        descriptions = np.array([e.description for e in entries])
        desc_toks = np.array([e.desc_tok for e in entries])
        # Variable-length per-skill arrays go via dtype=object.
        helpful_ctxs = np.empty(len(entries), dtype=object)
        harmful_ctxs = np.empty(len(entries), dtype=object)
        help_embs = np.empty(len(entries), dtype=object)
        harm_embs = np.empty(len(entries), dtype=object)
        for i, e in enumerate(entries):
            helpful_ctxs[i] = list(e.helpful_contexts)
            harmful_ctxs[i] = list(e.harmful_contexts)
            help_embs[i] = e.embedding_helpful_ctxs
            harm_embs[i] = e.embedding_harmful_ctxs
        helpful_counts = np.array([e.helpful_count for e in entries], dtype=np.int32)
        harmful_counts = np.array([e.harmful_count for e in entries], dtype=np.int32)
        statuses = np.array([e.status for e in entries])
        consec_h = np.array([e.consecutive_harmful for e in entries], dtype=np.int32)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp, "wb") as fh:
            np.savez(
                fh,
                schema_version=np.int32(SCHEMA_VERSION),
                names=names,
                embeddings=embeddings,
                embeddings_name=embeddings_name,
                shas=shas,
                skill_dirs=skill_dirs,
                descriptions=descriptions,
                desc_toks=desc_toks,
                model_id=self._model_id or "",
                fingerprint=self._fingerprint or "",
                helpful_contexts=helpful_ctxs,
                harmful_contexts=harmful_ctxs,
                embeddings_helpful_ctxs=help_embs,
                embeddings_harmful_ctxs=harm_embs,
                helpful_counts=helpful_counts,
                harmful_counts=harmful_counts,
                statuses=statuses,
                consecutive_harmful_counts=consec_h,
            )
        tmp.replace(self.path)

    def sync(
        self,
        skills: list[tuple],
        embedder: "Embedder",
    ) -> tuple[int, int]:
        """Bring the cache up to date for the given skill list.

        Args:
            skills: list of rows from :func:`make_sync_row`.
            embedder: used for any (re-)embedding needed.

        Returns:
            ``(n_added_or_changed, n_reused)``.
        """
        from mega_tron.embedder import fingerprint_of

        new_fp = fingerprint_of(embedder)
        # Full clear if schema drifted or the embedder fingerprint changed.
        if self._schema_version != SCHEMA_VERSION:
            self._entries.clear()
        elif self._fingerprint and self._fingerprint != new_fp:
            self._entries.clear()
        self._model_id = embedder.model_id
        self._fingerprint = new_fp
        self._schema_version = SCHEMA_VERSION

        rows = [_normalize_row(r) for r in skills]
        present_names = {r[0] for r in rows}
        for stale in list(self._entries.keys()):
            if stale not in present_names:
                del self._entries[stale]

        to_embed: list[tuple] = []
        reused = 0
        for row in rows:
            (name, skill_dir, desc, desc_tok, sha, _h_ctxs, _ha_ctxs,
             h_count, ha_count, status, consec_h) = row
            entry = self._entries.get(name)
            # Re-embed if missing, sha mismatch, or entry lacks a name vector.
            if (
                entry is not None
                and entry.sha == sha
                and entry.embedding_name is not None
            ):
                # SHA same → mega_meta block unchanged. Refresh metadata in case
                # skill dir moved; counts/status are guaranteed identical since
                # any update_meta() flips the SHA.
                entry.skill_dir = skill_dir
                entry.description = desc
                entry.desc_tok = desc_tok
                entry.helpful_count = h_count
                entry.harmful_count = ha_count
                entry.status = status
                entry.consecutive_harmful = consec_h
                reused += 1
            else:
                to_embed.append(row)

        if to_embed:
            # Build a single batched text list:  full[N] + name[N] + ctxs...
            # then split the returned matrix back.
            full_texts = [f"{r[0]}\n\n{r[2]}" for r in to_embed]
            name_texts = [r[0] for r in to_embed]
            ctx_texts: list[str] = []
            ctx_layout: list[tuple[int, int]] = []  # per skill (n_helpful, n_harmful)
            for r in to_embed:
                h_ctxs, ha_ctxs = r[5], r[6]
                ctx_layout.append((len(h_ctxs), len(ha_ctxs)))
                ctx_texts.extend(h_ctxs)
                ctx_texts.extend(ha_ctxs)

            n = len(to_embed)
            batch = full_texts + name_texts + ctx_texts
            vectors = embedder.embed(batch)
            full_vecs = vectors[:n]
            name_vecs = vectors[n:2 * n]
            ctx_vecs = vectors[2 * n:]
            cursor = 0
            for i, row in enumerate(to_embed):
                (name, skill_dir, desc, desc_tok, sha, h_ctxs, ha_ctxs,
                 h_count, ha_count, status, consec_h) = row
                n_h, n_ha = ctx_layout[i]
                help_emb = ctx_vecs[cursor:cursor + n_h] if n_h else None
                cursor += n_h
                harm_emb = ctx_vecs[cursor:cursor + n_ha] if n_ha else None
                cursor += n_ha
                self._entries[name] = CacheEntry(
                    name=name,
                    embedding=full_vecs[i],
                    embedding_name=name_vecs[i],
                    sha=sha,
                    skill_dir=skill_dir,
                    description=desc,
                    desc_tok=desc_tok,
                    helpful_contexts=list(h_ctxs),
                    harmful_contexts=list(ha_ctxs),
                    embedding_helpful_ctxs=help_emb if help_emb is not None and len(help_emb) else None,
                    embedding_harmful_ctxs=harm_emb if harm_emb is not None and len(harm_emb) else None,
                    helpful_count=h_count,
                    harmful_count=ha_count,
                    status=status,
                    consecutive_harmful=consec_h,
                )

        return len(to_embed), reused

    def upsert(self, entry: CacheEntry) -> None:
        """Insert or replace a single entry in-memory. Caller drives save()."""
        self._entries[entry.name] = entry

    def entries(self) -> list[CacheEntry]:
        return list(self._entries.values())

    @property
    def model_id(self) -> str | None:
        return self._model_id

    def embeddings_matrix(self) -> "np.ndarray":
        """Stacked (N, dim) matrix of FULL embeddings (name + description)."""
        import numpy as np

        if not self._entries:
            return np.zeros((0, 0), dtype="float32")
        return np.stack([e.embedding for e in self._entries.values()])

    def name_embeddings_matrix(self) -> "np.ndarray":
        """Stacked (N, dim) matrix of NAME-only embeddings (schema v2+).

        Returns a zero-filled fallback for entries without a name vector.
        """
        import numpy as np

        if not self._entries:
            return np.zeros((0, 0), dtype="float32")
        any_full = next(iter(self._entries.values())).embedding
        dim = any_full.shape[0]
        return np.stack(
            [
                e.embedding_name if e.embedding_name is not None
                else np.zeros(dim, dtype=np.float32)
                for e in self._entries.values()
            ]
        )
