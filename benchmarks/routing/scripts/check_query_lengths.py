"""Verify queries.yaml length policy (DESIGN.md §2.3).

Rules:
- short  : ≤ 40 chars       (target 30% ±5pp)
- medium : 41 - 140 chars   (target 50% ±5pp)
- long   : 141 - 400 chars  (target 20% ±5pp)
- hard ceiling: 400 chars on every query

Returns 0 if all rules pass; non-zero with violations printed to stderr otherwise.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

SHORT_MAX = 40
MEDIUM_MAX = 140
LONG_MAX = 400

TARGET_SHORT_PCT = 30
TARGET_MEDIUM_PCT = 50
TARGET_LONG_PCT = 20
TOLERANCE_PP = 5


def _bucket_for(n: int) -> str:
    if n <= SHORT_MAX:
        return "short"
    if n <= MEDIUM_MAX:
        return "medium"
    return "long"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    routing_dir = Path(__file__).resolve().parent.parent
    p.add_argument(
        "--queries",
        type=Path,
        default=routing_dir / "200bench" / "queries.yaml",
    )
    args = p.parse_args(argv)

    data = yaml.safe_load(args.queries.read_text())
    queries = data["queries"]

    violations: list[str] = []
    # only in-distribution queries count toward the length distribution
    in_dist = [q for q in queries if q.get("null_kind") in (None, "null", "")]
    in_dist = [q for q in queries if not q.get("null_kind") or q.get("null_kind") == "null"]
    # Actually: per the schema, null_kind is None for in-dist
    in_dist = [q for q in queries if q.get("null_kind") in (None, "null")]
    # In YAML our null_kind: null becomes Python None
    in_dist = [q for q in queries if q.get("null_kind") is None]
    null_q = [q for q in queries if q.get("null_kind") is not None]

    # Ceiling check on all queries
    for q in queries:
        if len(q["query"]) > LONG_MAX:
            violations.append(
                f"  {q['id']}: query exceeds {LONG_MAX}-char ceiling "
                f"({len(q['query'])} chars)"
            )

    # Length-bucket tag consistency
    for q in queries:
        actual = _bucket_for(len(q["query"]))
        declared = q.get("length_bucket")
        if actual != declared:
            violations.append(
                f"  {q['id']}: length_bucket={declared} but query is "
                f"{len(q['query'])} chars (actual: {actual})"
            )

    # Distribution check (in-distribution queries only)
    if in_dist:
        counts = {"short": 0, "medium": 0, "long": 0}
        for q in in_dist:
            counts[_bucket_for(len(q["query"]))] += 1
        total = sum(counts.values())
        actual_pct = {k: 100.0 * v / total for k, v in counts.items()}
        targets = {"short": TARGET_SHORT_PCT, "medium": TARGET_MEDIUM_PCT, "long": TARGET_LONG_PCT}
        print(
            f"[check_query_lengths] in-distribution distribution:",
            file=sys.stderr,
        )
        for bucket in ("short", "medium", "long"):
            diff = actual_pct[bucket] - targets[bucket]
            print(
                f"  {bucket:6}: {counts[bucket]:3d} ({actual_pct[bucket]:5.1f}%) "
                f"target {targets[bucket]:2d}% diff {diff:+.1f}pp",
                file=sys.stderr,
            )
            if abs(diff) > TOLERANCE_PP:
                violations.append(
                    f"  in-dist {bucket} share {actual_pct[bucket]:.1f}% "
                    f"deviates from target {targets[bucket]}% by "
                    f"{abs(diff):.1f}pp (tolerance ±{TOLERANCE_PP}pp)"
                )

    print(
        f"[check_query_lengths] total {len(queries)} queries: "
        f"{len(in_dist)} in-dist + {len(null_q)} null",
        file=sys.stderr,
    )

    if violations:
        print(f"[check_query_lengths] FAIL: {len(violations)} violation(s):", file=sys.stderr)
        for v in violations:
            print(v, file=sys.stderr)
        return 1
    print("[check_query_lengths] OK", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
