"""`mega-tron skills` — cross-host master skill pool management."""
from __future__ import annotations

import argparse
import json
import sys


def cmd_skills(args: argparse.Namespace) -> int:
    """Manage the cross-host master skill pool."""
    from mega_tron import pool

    op = args.skills_op
    if op == "list":
        records = pool.list_pool()
        if args.json:
            json.dump(
                [
                    {
                        "name": r.name,
                        "promoted_from": r.promoted_from,
                        "promoted_at": r.promoted_at,
                        "content_sha": r.content_sha,
                        "mirrored_to": sorted(r.mirrored_to),
                    }
                    for r in records
                ],
                sys.stdout,
                indent=2,
            )
            sys.stdout.write("\n")
            return 0
        if not records:
            print("(master pool is empty — promote a skill with `mega-tron skills promote <name>`)")
            print(f"  pool root: {pool.pool_root()}")
            return 0
        name_w = max(len(r.name) for r in records)
        print(f"  {'name'.ljust(name_w)}  mirrors  promoted_at")
        print(f"  {'-' * name_w}  -------  --------------------")
        for r in records:
            print(f"  {r.name.ljust(name_w)}  {len(r.mirrored_to):>7}  {r.promoted_at}")
        print()
        print(f"  pool root: {pool.pool_root()}")
        return 0

    if op == "promote":
        try:
            result = pool.promote(
                args.target,
                source_host=args.from_host,
                force=args.force,
            )
        except pool.PoolError as e:
            print(f"[skills] error: {e}", file=sys.stderr)
            return 1
        if result.was_present:
            print(f"[skills] {result.name} already in pool (sha match) — left as-is at {result.target}")
        else:
            print(f"[skills] promoted {result.name}: {result.source} → {result.target}")
        return 0

    if op == "unpromote":
        try:
            restored = pool.unpromote(args.name)
        except pool.PoolError as e:
            print(f"[skills] error: {e}", file=sys.stderr)
            return 1
        print(f"[skills] unpromoted {args.name}: restored to {restored}")
        return 0

    if op == "mirror":
        try:
            changed = pool.mirror(args.target)
        except ValueError as e:
            print(f"[skills] error: {e}", file=sys.stderr)
            return 2
        if not changed:
            print(f"[skills] no new mirror symlinks needed for {args.target}")
            return 0
        print(f"[skills] mirrored {len(changed)} skill(s) to {args.target}:")
        for p in changed:
            print(f"  {p}")
        return 0

    if op == "unmirror":
        try:
            removed = pool.unmirror(args.target)
        except ValueError as e:
            print(f"[skills] error: {e}", file=sys.stderr)
            return 2
        if not removed:
            print(f"[skills] no pool symlinks under {args.target}")
            return 0
        print(f"[skills] removed {len(removed)} symlink(s) from {args.target}:")
        for p in removed:
            print(f"  {p}")
        return 0

    if op == "sync":
        result = pool.sync()
        total = sum(len(v) for v in result.values())
        if not result:
            print("[skills] no host skills dirs present; install with `mega-tron install --target ...`")
            return 0
        for host, paths in result.items():
            print(f"  {host}: {len(paths)} new symlink(s)")
        print(f"[skills] sync complete: {total} new symlink(s) across {len(result)} host(s)")
        return 0

    print(f"[skills] unknown op {op!r}", file=sys.stderr)
    return 2
