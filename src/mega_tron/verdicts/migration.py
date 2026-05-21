"""Frontmatter → SQLite one-shot migration.

Walks every registered skill root, reads each ``SKILL.md``'s
``mega_meta:`` block, and synthesises verdict rows into the SQLite
:class:`Store` so Phase 3's regression detector has a baseline to
compare recent activity against.

Migration is a *user-triggered* event (``mega-tron migrate-to-sqlite``)
that runs once per install. The migration is reversible:

- Every mutated ``SKILL.md`` is copied byte-for-byte to a backup
  directory (``~/.local/share/mega-tron/backup/<UTC-timestamp>/``).
- A manifest records each file's pre-migration SHA so ``--rollback``
  can restore them later even if the user has since edited them.
- ``--dry-run`` reports what *would* happen without writing anything.

Synthetic verdict timestamps. We don't have per-verdict history in the
old frontmatter — only cumulative counts plus a single ``last_updated``.
To keep regression detection sound, all synthesised rows land at
``min(last_updated, now - (window_days + 1) days)`` so they fall
*before* any regression window. This means migration data only
contributes to the *baseline* counts, never the *recent* counts —
exactly the right semantic for "before we knew anything, this skill
had N helpful events."
"""
from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from mega_tron.config import Config, data_dir, discover_skill_dirs, store_path
from mega_tron.verdicts.mega_meta import read_meta
from mega_tron.pre_flight import ValidationError, parse_frontmatter, validate
from mega_tron.verdicts.store import Store


# Synthetic timestamps are clamped this many days before "now" so they
# fall well outside any regression window the Phase 3 classifier might
# use. Conservative default (any reasonable window is <= 60 days).
SYNTHETIC_BASELINE_OFFSET_DAYS = 90

# Frontmatter schema marker. Migrated skills get
# ``mega_meta.schema: 2`` added so the Stop hook can distinguish
# migrated-vs-unmigrated installs and avoid double-counting.
FRONTMATTER_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class MigrationStats:
    """Counters returned by :func:`migrate_to_sqlite`."""

    skills_scanned: int = 0
    skills_migrated: int = 0
    skills_skipped: int = 0
    verdicts_synthesized: int = 0
    invalid: list[str] = field(default_factory=list)
    backup_dir: Path | None = None


@dataclass(frozen=True)
class FileManifest:
    """One entry in the migration backup manifest."""

    skill_name: str
    src_path: str
    backup_path: str
    sha_before: str


def _file_sha(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _baseline_timestamp(meta_last_updated, now: datetime) -> str:
    """Compute the synthetic verdict timestamp for a migration row.

    Clamps to ``now - SYNTHETIC_BASELINE_OFFSET_DAYS`` so the rows fall
    *before* any reasonable regression window. If ``last_updated`` is
    even older than the clamp, use it verbatim — preserves real history
    when available.

    ``meta_last_updated`` can be a string (the canonical ISO-8601
    representation we emit) or a :class:`datetime` (PyYAML auto-parses
    timestamp-shaped scalars into datetime objects).
    """
    clamp = now - timedelta(days=SYNTHETIC_BASELINE_OFFSET_DAYS)
    if meta_last_updated is None:
        return clamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    ts: datetime | None = None
    if isinstance(meta_last_updated, datetime):
        ts = meta_last_updated
    elif isinstance(meta_last_updated, str):
        try:
            ts = datetime.fromisoformat(meta_last_updated.rstrip("Z"))
        except (TypeError, ValueError):
            ts = None
    if ts is None:
        return clamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    if ts < clamp:
        return ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    return clamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def _iter_migration_candidates(
    skills_dirs: Iterable[Path],
) -> Iterable[tuple[Path, str]]:
    """Yield ``(skill_md_path, skill_name)`` for every valid ``SKILL.md``
    under the registered roots that carries a ``mega_meta:`` block worth
    migrating. First-dir-wins on ``name:`` collisions.
    """
    seen: set[str] = set()
    for root in skills_dirs:
        if not root.exists():
            continue
        for entry in sorted(root.iterdir()):
            if not entry.is_dir():
                continue
            skill_md = entry / "SKILL.md"
            if not skill_md.exists():
                continue
            err = validate(skill_md)
            if err is not None:
                continue
            try:
                fm, _ = parse_frontmatter(skill_md)
            except Exception:
                continue
            name = str(fm.get("name") or entry.name).strip()
            if name in seen:
                continue
            seen.add(name)
            yield skill_md, name


def _new_backup_dir() -> Path:
    """``<data_dir>/backup/<UTC-timestamp>/`` — created on demand."""
    stamp = _utc_now().strftime("%Y%m%dT%H%M%SZ")
    return data_dir() / "backup" / stamp


def _write_schema_marker(skill_md: Path) -> None:
    """Add ``mega_meta.schema: <N>`` to the SKILL.md frontmatter.

    This is the post-migration version pin: the Stop hook checks it to
    avoid double-counting on a half-migrated install (frontmatter has
    counters but SQLite hasn't been seeded for this skill yet).
    """
    from mega_tron.verdicts.mega_meta import _dump_frontmatter, _parse_frontmatter

    text = skill_md.read_text(encoding="utf-8")
    fm, _, body = _parse_frontmatter(text)
    mega = dict(fm.get("mega_meta") or {})
    mega["schema"] = FRONTMATTER_SCHEMA_VERSION
    fm["mega_meta"] = mega
    new_text = "---\n" + _dump_frontmatter(fm) + "\n---\n" + body
    tmp = skill_md.with_suffix(skill_md.suffix + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    tmp.replace(skill_md)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def migrate_to_sqlite(
    *,
    skills_dirs: list[Path] | None = None,
    store: Store | None = None,
    dry_run: bool = False,
    backup_dir: Path | None = None,
    force: bool = False,
    config: Config | None = None,
) -> MigrationStats:
    """Read every SKILL.md ``mega_meta:`` block and seed the SQLite store.

    Args:
        skills_dirs: explicit roots to scan. ``None`` uses
            :func:`discover_skill_dirs`.
        store: target store. ``None`` constructs a fresh :class:`Store`
            at :func:`store_path`.
        dry_run: when ``True``, no files are mutated and the store is
            not written. The returned :class:`MigrationStats` still
            reports what *would* happen.
        backup_dir: backup destination. ``None`` allocates a fresh
            timestamped dir under :func:`data_dir`/backup.
        force: bypass the "store already has verdicts" pre-flight check.
            Use with care — re-running migration on a populated store
            will double-count.
        config: optional config override.

    Returns:
        :class:`MigrationStats` summarising the run.
    """
    cfg = config or Config.load()
    roots: list[Path] = (
        [Path(p) for p in skills_dirs]
        if skills_dirs is not None
        else discover_skill_dirs(config=cfg)
    )
    target_store = store or Store(store_path())

    # Pre-flight: refuse if the store already holds verdicts (re-running
    # migration would double-count). Force overrides for tests.
    target_store.initialize()
    if not force and target_store.count_verdicts() > 0:
        raise RuntimeError(
            f"Store at {target_store.path} already contains verdicts. "
            "Pass force=True to override (may cause double-counting)."
        )

    backup_root = backup_dir or _new_backup_dir()
    manifest: list[FileManifest] = []
    invalid: list[str] = []
    skills_scanned = 0
    skills_migrated = 0
    skills_skipped = 0
    verdicts_synthesized = 0
    now = _utc_now()

    for skill_md, skill_name in _iter_migration_candidates(roots):
        skills_scanned += 1
        try:
            meta = read_meta(skill_md)
        except (ValidationError, ValueError) as e:
            invalid.append(f"{skill_name}: {e}")
            skills_skipped += 1
            continue

        if meta.helpful_count == 0 and meta.harmful_count == 0:
            # Nothing to migrate from this skill — no synthetic rows
            # would be created. We still emit a schema marker so the
            # frontmatter is consistently tagged.
            if not dry_run:
                _write_schema_marker(skill_md)
            skills_skipped += 1
            continue

        timestamp = _baseline_timestamp(meta.last_updated, now)

        if dry_run:
            verdicts_synthesized += meta.helpful_count + meta.harmful_count
            skills_migrated += 1
            continue

        # Backup before mutating.
        backup_root.mkdir(parents=True, exist_ok=True)
        sha = _file_sha(skill_md)
        backup_file = backup_root / skill_name / "SKILL.md"
        backup_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(skill_md, backup_file)
        manifest.append(
            FileManifest(
                skill_name=skill_name,
                src_path=str(skill_md),
                backup_path=str(backup_file),
                sha_before=sha,
            )
        )

        # Synthesize verdict rows. session_id=NULL bypasses the UNIQUE
        # constraint so multiple synthetics for one skill are allowed.
        n_h = meta.helpful_count
        n_x = meta.harmful_count
        help_ctxs = list(meta.helpful_contexts)
        harm_ctxs = list(meta.harmful_contexts)

        for i in range(n_h):
            # Attach the last helpful_context as the reason on the
            # final synthesised HELPFUL row so the context is preserved
            # in the time series.
            reason = help_ctxs[-1] if i == n_h - 1 and help_ctxs else None
            target_store.record_verdict(
                skill_name=skill_name,
                verdict="HELPFUL",
                host="other",
                reason=reason,
                session_id=None,
                occurred_at=timestamp,
                skill_dir=str(skill_md.parent),
                extras={"synthetic": True, "source": "frontmatter_migration"},
            )
            verdicts_synthesized += 1

        for i in range(n_x):
            reason = harm_ctxs[-1] if i == n_x - 1 and harm_ctxs else None
            target_store.record_verdict(
                skill_name=skill_name,
                verdict="HARMFUL",
                host="other",
                reason=reason,
                session_id=None,
                occurred_at=timestamp,
                skill_dir=str(skill_md.parent),
                extras={"synthetic": True, "source": "frontmatter_migration"},
            )
            verdicts_synthesized += 1

        # Stamp the frontmatter with the schema marker so the Stop hook
        # knows this skill has been migrated.
        _write_schema_marker(skill_md)
        skills_migrated += 1

    # Write the manifest so --rollback can find this run later.
    if not dry_run and manifest:
        manifest_path = backup_root / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "store_path": str(target_store.path),
                    "skills_dirs": [str(p) for p in roots],
                    "entries": [
                        {
                            "skill_name": m.skill_name,
                            "src_path": m.src_path,
                            "backup_path": m.backup_path,
                            "sha_before": m.sha_before,
                        }
                        for m in manifest
                    ],
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    return MigrationStats(
        skills_scanned=skills_scanned,
        skills_migrated=skills_migrated,
        skills_skipped=skills_skipped,
        verdicts_synthesized=verdicts_synthesized,
        invalid=invalid,
        backup_dir=backup_root if not dry_run and manifest else None,
    )


def rollback(backup_dir: Path, *, store: Store | None = None) -> int:
    """Restore SKILL.md files from a previous migration backup.

    Reads ``backup_dir/manifest.json`` and copies each backup back over
    its original path. The destination store (defaults to the current
    :func:`store_path`) is *renamed* to a sibling file with suffix
    ``.rolled-back-<UTC-timestamp>`` so the user can re-attempt the
    migration without losing the SQLite data.

    Returns the number of files restored.
    """
    manifest_path = Path(backup_dir) / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"No manifest.json under {backup_dir}; cannot roll back."
        )
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = data.get("entries") or []
    restored = 0
    for e in entries:
        backup_path = Path(e["backup_path"])
        src_path = Path(e["src_path"])
        if not backup_path.exists():
            continue
        src_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup_path, src_path)
        restored += 1

    # Rename the store so a re-attempt starts fresh. We rename rather
    # than delete so the user can post-mortem the failed run.
    target_store = store or Store(store_path())
    if target_store.path.exists():
        stamp = _utc_now().strftime("%Y%m%dT%H%M%SZ")
        renamed = target_store.path.with_name(
            target_store.path.name + f".rolled-back-{stamp}"
        )
        target_store.path.rename(renamed)
    return restored


__all__ = [
    "FRONTMATTER_SCHEMA_VERSION",
    "FileManifest",
    "MigrationStats",
    "SYNTHETIC_BASELINE_OFFSET_DAYS",
    "migrate_to_sqlite",
    "rollback",
]
