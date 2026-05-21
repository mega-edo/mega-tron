"""`mega-tron` cache lifecycle commands.

Three subcommands that maintain the verdict / cache stores:

- ``migrate-to-sqlite`` — one-shot frontmatter → SQLite migration with
  rollback support.
- ``export-frontmatter`` — deprecated no-op stub (frontmatter is now
  the single source of truth; no SQLite cache to re-emit from).
- ``compact-embeddings`` — collapse near-duplicate verdict embeddings
  within each (skill, label) group to bound store growth.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from mega_tron.cli._common import _resolve_skills_dirs


def cmd_migrate_to_sqlite(args: argparse.Namespace) -> int:
    """One-shot frontmatter → SQLite migration. See ``migration.py``."""
    from mega_tron.verdicts.migration import migrate_to_sqlite, rollback

    if args.rollback:
        backup_dir = Path(args.rollback).expanduser()
        try:
            n = rollback(backup_dir)
        except FileNotFoundError as e:
            print(f"[migrate-to-sqlite] {e}", file=sys.stderr)
            return 1
        print(
            f"[migrate-to-sqlite] restored {n} files from {backup_dir}. "
            f"Store renamed to *.rolled-back-<ts>.",
            file=sys.stderr,
        )
        return 0

    skills_dirs = _resolve_skills_dirs(args) or None
    backup_dir = Path(args.backup_dir).expanduser() if args.backup_dir else None
    try:
        stats = migrate_to_sqlite(
            skills_dirs=list(skills_dirs) if skills_dirs else None,
            dry_run=args.dry_run,
            backup_dir=backup_dir,
            force=args.force,
        )
    except RuntimeError as e:
        print(f"[migrate-to-sqlite] {e}", file=sys.stderr)
        return 1

    prefix = "[dry-run] " if args.dry_run else ""
    print(
        f"{prefix}scanned={stats.skills_scanned}  "
        f"migrated={stats.skills_migrated}  "
        f"skipped={stats.skills_skipped}  "
        f"verdicts={stats.verdicts_synthesized}",
        file=sys.stderr,
    )
    if stats.backup_dir:
        print(
            f"[migrate-to-sqlite] backups at {stats.backup_dir} — "
            f"rollback with `mega-tron migrate-to-sqlite --rollback {stats.backup_dir}`",
            file=sys.stderr,
        )
    if stats.invalid:
        print(
            f"[migrate-to-sqlite] {len(stats.invalid)} invalid SKILL.md "
            f"skipped:",
            file=sys.stderr,
        )
        for line in stats.invalid:
            print(f"  - {line}", file=sys.stderr)
    return 0


def cmd_export_frontmatter(args: argparse.Namespace) -> int:
    """Deprecated. SKILL.md ``mega_meta:`` frontmatter is now the single
    source of truth for cumulative counters — host Stop hooks write to it
    directly via :func:`mega_tron.verdicts.mega_meta.apply_evaluations`. There
    is no longer a SQLite derived cache to "export" from.

    Kept as a CLI-surface stub so scripts don't crash; logs a deprecation
    notice and exits 0.
    """
    del args
    print(
        "[export-frontmatter] no-op: SKILL.md frontmatter is the canonical "
        "source for cumulative counters (helpful/harmful/status/etc.). "
        "Host Stop hooks write to it directly; there is no SQLite cache "
        "to re-emit from.",
        file=sys.stderr,
    )
    return 0


def cmd_compact_embeddings(args: argparse.Namespace) -> int:
    """Collapse near-duplicate verdict embeddings within each
    (skill, label) group.

    Active users accumulate semantically-equivalent verdicts
    ("validated webhook HMAC", "verified webhook signature", ...).
    At cos > ``--threshold`` those carry no additional routing signal
    so we keep one representative per cluster and drop the rest from
    the embedding store. The SQLite ``verdicts`` time-series is
    untouched — regression analysis remains time-series-true.

    Use ``--dry-run`` to preview impact without mutating disk.
    """
    from mega_tron.config import store_path
    from mega_tron.verdicts.store import Store
    from mega_tron.verdicts.embeddings import (
        VerdictEmbeddingsStore,
        default_path,
    )

    ves_path = default_path()
    if not ves_path.exists():
        print(
            f"[compact-embeddings] no embedding store at {ves_path}. "
            "Run a session with the Stop hook active to populate it first.",
            file=sys.stderr,
        )
        return 1

    # Use the embedder fingerprint stored on disk by reading the file
    # header — VerdictEmbeddingsStore's fingerprint guard would
    # silently rebuild if we passed a mismatch, which is the wrong
    # behaviour for the compact CLI. Read once for inspection.
    import numpy as np

    try:
        data = np.load(ves_path, allow_pickle=True)
        on_disk_fp = str(data["fingerprint"]) if "fingerprint" in data.files else ""
    except Exception as e:  # noqa: BLE001
        print(
            f"[compact-embeddings] could not read {ves_path}: {e}",
            file=sys.stderr,
        )
        return 1

    ves = VerdictEmbeddingsStore(fingerprint=on_disk_fp)
    if len(ves) == 0:
        print("[compact-embeddings] embedding store is empty — nothing to do.")
        return 0

    # Plumb the SQLite reason lookup in if the store exists, so the
    # longest-reason tie-break activates. Falls back to newest-id-wins
    # when the SQLite store isn't there.
    get_reason = None
    sql_path = store_path()
    if sql_path.exists():
        sql_store = Store(sql_path)
        get_reason = sql_store.get_verdict_reason

    report = ves.compact(
        threshold=args.threshold,
        get_reason=get_reason,
        dry_run=args.dry_run,
    )

    if args.json:
        json.dump(report, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        action = "would remove" if args.dry_run else "removed"
        print(
            f"[compact-embeddings] {action} {report['removed']} of "
            f"{report['before']} rows "
            f"(collapsed {report['clusters_collapsed']} near-duplicate "
            f"clusters across {report['groups_scanned']} (skill,label) groups, "
            f"threshold=cos>{args.threshold})"
        )

    if not args.dry_run and report["removed"] > 0:
        ves.save()

    return 0
