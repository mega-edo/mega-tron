"""Verify queries.yaml gold-coverage invariants (DESIGN.md §2.3).

Rules:
- Every gold_id (G01..G59) appears in 1-5 in-distribution queries.
- Null queries reference zero golds.
- No query references a gold_id that doesn't exist in golds.json.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import yaml

MIN_COVERAGE = 1
MAX_COVERAGE = 5


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    routing_dir = Path(__file__).resolve().parent.parent
    p.add_argument("--queries", type=Path, default=routing_dir / "200bench" / "queries.yaml")
    p.add_argument("--golds", type=Path, default=routing_dir / "200bench" / "golds.json")
    args = p.parse_args(argv)

    queries = yaml.safe_load(args.queries.read_text())["queries"]
    golds_data = json.loads(args.golds.read_text())
    valid_gold_ids = {g["gold_id"] for g in golds_data["golds"]}

    violations: list[str] = []
    counts: Counter[str] = Counter()

    for q in queries:
        is_null = q.get("null_kind") is not None
        golds = q.get("golds") or []

        # Null queries must have no golds
        if is_null and golds:
            violations.append(
                f"  {q['id']}: null_kind={q['null_kind']!r} but has golds {golds}"
            )

        # In-dist queries must have at least one gold
        if not is_null and not golds:
            violations.append(
                f"  {q['id']}: in-dist query has no golds"
            )

        # All gold_ids must be valid
        for g in golds:
            if g not in valid_gold_ids:
                violations.append(
                    f"  {q['id']}: references unknown gold_id {g!r}"
                )
            counts[g] += 1

    # Coverage: every gold appears MIN..MAX times in in-dist
    for gid in sorted(valid_gold_ids):
        c = counts[gid]
        if c < MIN_COVERAGE:
            violations.append(
                f"  {gid}: appears {c} times, need ≥{MIN_COVERAGE}"
            )
        elif c > MAX_COVERAGE:
            violations.append(
                f"  {gid}: appears {c} times, max is {MAX_COVERAGE}"
            )

    print(
        f"[check_gold_coverage] coverage: min={min(counts.values()) if counts else 0}, "
        f"max={max(counts.values()) if counts else 0}, "
        f"mean={sum(counts.values())/len(valid_gold_ids):.2f}",
        file=sys.stderr,
    )

    if violations:
        print(f"[check_gold_coverage] FAIL: {len(violations)} violation(s):", file=sys.stderr)
        for v in violations:
            print(v, file=sys.stderr)
        return 1
    print(f"[check_gold_coverage] OK ({len(valid_gold_ids)} golds, all 1-5 occurrences)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
