"""`mega-tron stats` — per-skill helpful/harmful counter snapshot.

Reads cumulative counts from SKILL.md ``mega_meta:`` frontmatter (the
single source of truth) and optionally pivots SQLite ``verdicts`` rows
to surface per-host activity. ``--source auto/sqlite/frontmatter``
selects the read path; ``--by-host`` requires the SQLite store.
"""
from __future__ import annotations

import argparse
import json
import sys

from mega_tron.cli._common import _resolve_skills_dirs


def cmd_stats(args: argparse.Namespace) -> int:
    """Per-skill helpful/harmful counter snapshot.

    Source resolution:
      - ``--source sqlite``  → reads from :class:`Store` (requires a
        migrated install; errors out clearly otherwise).
      - ``--source frontmatter`` → reads SKILL.md ``mega_meta:`` blocks.
      - ``--source auto`` (default) → SQLite when a store exists at the
        configured path, frontmatter otherwise.

    ``--by-host`` pivots over ``verdicts.host`` and is only meaningful
    when reading from SQLite.
    """
    from mega_tron.config import store_path

    source = getattr(args, "source", "auto") or "auto"
    if source == "auto":
        source = "sqlite" if store_path().exists() else "frontmatter"

    by_host = getattr(args, "by_host", False)
    top = getattr(args, "top", None)
    all_flag = getattr(args, "all", False)

    if source == "sqlite":
        return _stats_from_sqlite(by_host=by_host, top=top, all_flag=all_flag, json_out=args.json)
    if source == "frontmatter":
        if by_host:
            print(
                "[stats] --by-host requires --source sqlite (frontmatter has no per-host data)",
                file=sys.stderr,
            )
            return 2
        return _stats_from_frontmatter(args, top=top, all_flag=all_flag)
    print(f"[stats] unknown --source {source!r}", file=sys.stderr)
    return 2


def _stats_from_frontmatter(args, *, top: int | None, all_flag: bool) -> int:
    from mega_tron.verdicts.mega_meta import read_meta

    skills_dirs = _resolve_skills_dirs(args)
    if not skills_dirs:
        print(
            "[stats] no skill directories found — "
            "register one with `mega-tron dirs add <path>`",
            file=sys.stderr,
        )
        return 1
    rows: list[tuple[str, int, int, str]] = []
    seen_names: set[str] = set()
    for skills_dir in skills_dirs:
        if not skills_dir.exists():
            continue
        for entry in sorted(skills_dir.iterdir()):
            skill_md = entry / "SKILL.md"
            if not skill_md.exists():
                continue
            if entry.name in seen_names:
                continue
            seen_names.add(entry.name)
            try:
                meta = read_meta(skill_md)
            except Exception:
                continue
            if meta.helpful_count == 0 and meta.harmful_count == 0 and not all_flag:
                continue
            rows.append((entry.name, meta.helpful_count, meta.harmful_count, meta.status))

    if top is not None:
        rows.sort(key=lambda r: (r[1] - r[2]), reverse=True)
        rows = rows[:top]

    if args.json:
        out = [
            {"name": n, "helpful": h, "harmful": ha, "status": s}
            for n, h, ha, s in rows
        ]
        json.dump(out, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    if not rows:
        print("(no tracked skills yet — run a codex session with the Stop hook active)")
        return 0
    width = max(len(n) for n, *_ in rows)
    print(f"  {'skill'.ljust(width)}  helpful  harmful  status")
    print(f"  {'-' * width}  -------  -------  ------")
    for n, h, ha, s in sorted(rows, key=lambda r: (-r[1], r[2])):
        print(f"  {n.ljust(width)}  {h:>7}  {ha:>7}  {s}")
    return 0


def _stats_from_sqlite(
    *, by_host: bool, top: int | None, all_flag: bool, json_out: bool
) -> int:
    """Stats reader for the ``--source sqlite`` path.

    Cumulative counts now live in SKILL.md ``mega_meta:`` frontmatter,
    so per-skill totals come from there. ``--by-host`` additionally
    pivots the SQLite ``verdicts`` table to surface per-runtime activity.
    Status / last_updated remain frontmatter-derived (single source of
    truth) so the host rows of one skill share the same status.
    """
    from mega_tron.config import discover_skill_dirs, store_path
    from mega_tron.verdicts.mega_meta import read_meta
    from mega_tron.verdicts.store import Store

    skills_dirs = discover_skill_dirs()
    metas: dict[str, "object"] = {}
    for root in skills_dirs:
        if not root.exists():
            continue
        for d in sorted(root.iterdir()):
            if not d.is_dir():
                continue
            sm = d / "SKILL.md"
            if not sm.exists():
                continue
            if d.name in metas:
                continue
            try:
                metas[d.name] = read_meta(sm)
            except Exception:
                continue

    if not by_host:
        rows = [
            (name, m.helpful_count, m.harmful_count, m.status, None)
            for name, m in metas.items()
            if all_flag or m.helpful_count > 0 or m.harmful_count > 0
        ]
    else:
        path = store_path()
        if not path.exists():
            print(
                f"[stats] no SQLite store at {path}; --by-host needs the "
                "store to pivot per (skill, host). Run a session first or "
                "drop --by-host to fall back to frontmatter-only totals.",
                file=sys.stderr,
            )
            return 1
        store = Store(path)
        with store._connect() as conn:
            cur = conn.execute(
                """
                SELECT skill_name, host,
                       SUM(CASE WHEN verdict='HELPFUL' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN verdict='HARMFUL' THEN 1 ELSE 0 END)
                FROM verdicts
                GROUP BY skill_name, host
                ORDER BY skill_name, host
                """
            )
            host_rows = cur.fetchall()
        rows = [
            (
                name,
                int(h or 0),
                int(x or 0),
                metas[name].status if name in metas else "active",
                host,
            )
            for name, host, h, x in host_rows
            if all_flag or (h or 0) > 0 or (x or 0) > 0
        ]

    if top is not None:
        rows.sort(key=lambda r: (r[1] - r[2]), reverse=True)
        rows = rows[:top]

    if json_out:
        out = [
            {
                "name": n,
                "helpful": h,
                "harmful": ha,
                "status": s,
                **({"host": host} if host is not None else {}),
            }
            for n, h, ha, s, host in rows
        ]
        json.dump(out, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    if not rows:
        print(
            "(no verdicts in store yet — run a session with the Stop hook active)"
        )
        return 0
    if by_host:
        width = max(len(n) for n, *_ in rows)
        host_w = max(len(h or "") for *_, h in rows)
        print(
            f"  {'skill'.ljust(width)}  {'host'.ljust(host_w)}  helpful  harmful  status"
        )
        print(
            f"  {'-' * width}  {'-' * host_w}  -------  -------  ------"
        )
        for n, h, ha, s, host in sorted(rows, key=lambda r: (-r[1], r[2])):
            print(
                f"  {n.ljust(width)}  {(host or '').ljust(host_w)}  "
                f"{h:>7}  {ha:>7}  {s}"
            )
    else:
        width = max(len(n) for n, *_ in rows)
        print(f"  {'skill'.ljust(width)}  helpful  harmful  status")
        print(f"  {'-' * width}  -------  -------  ------")
        for n, h, ha, s, _ in sorted(rows, key=lambda r: (-r[1], r[2])):
            print(f"  {n.ljust(width)}  {h:>7}  {ha:>7}  {s}")
    return 0
