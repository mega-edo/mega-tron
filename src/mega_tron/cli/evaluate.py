"""`mega-tron evaluate` — apply a JSON verdict batch to SKILL.md ``mega_meta`` blocks.

The mutation half of the Stop hook exposed as a standalone CLI so CI
evaluators, batch scripts, and the Stop hook itself all go through
the same code path.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def cmd_evaluate(args: argparse.Namespace) -> int:
    """Apply a JSON verdict batch to SKILL.md ``mega_meta`` blocks.

    The mutation half of the Stop hook is exposed here as a standalone
    CLI so CI evaluators, batch scripts, and the Stop hook itself all
    go through the same code path. Reads ``{"evaluations": [...]}``
    JSON from stdin (default) or ``--file``.
    """
    from mega_tron.verdicts.mega_meta import apply_evaluations

    raw: str
    if args.file:
        try:
            raw = Path(args.file).read_text(encoding="utf-8")
        except OSError as e:
            print(f"[evaluate] cannot read {args.file}: {e}", file=sys.stderr)
            return 2
    else:
        raw = sys.stdin.read()

    if not raw.strip():
        print("[evaluate] empty input — nothing to do", file=sys.stderr)
        return 2

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"[evaluate] invalid JSON: {e}", file=sys.stderr)
        return 2

    evaluations = payload.get("evaluations") if isinstance(payload, dict) else None
    if not isinstance(evaluations, list):
        print(
            "[evaluate] payload must be an object with `evaluations: [...]`",
            file=sys.stderr,
        )
        return 2

    outcome = apply_evaluations(
        skills_dir=Path(args.skills_dir),
        evaluations=evaluations,
        session_id=args.session_id,
        dry_run=args.dry_run,
    )

    if args.json:
        out = {
            "updated": outcome.updated,
            "skipped_missing": outcome.skipped_missing,
            "skipped_invalid": outcome.skipped_invalid,
            "errors": [{"skill": n, "error": m} for n, m in outcome.errors],
            "applied": outcome.applied,
            "dry_run": args.dry_run,
        }
        json.dump(out, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        verb = "would update" if args.dry_run else "updated"
        print(
            f"[evaluate] {verb} {outcome.updated}/{len(evaluations)} skills "
            f"({outcome.skipped_missing} missing, "
            f"{outcome.skipped_invalid} invalid)",
            file=sys.stderr,
        )
        for skill_name, err in outcome.errors:
            print(f"  error: {skill_name}: {err}", file=sys.stderr)
        for skill_name in outcome.applied:
            print(skill_name)

    return 0 if not outcome.errors else 1
