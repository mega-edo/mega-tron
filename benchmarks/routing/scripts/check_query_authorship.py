"""Reject queries that copy ≥12-char substrings from any pool SKILL.md *body*.

The body is everything after the closing ``---`` of the frontmatter. We
exempt the frontmatter (name + description) because queries are supposed
to paraphrase descriptions — that's the whole point of a routing query.

Rule (DESIGN.md §2.3):
- Pre-commit hook rejects any query containing a ≥12-character substring
  from any SKILL.md body.

This catches lifted phrases, copied trigger lists, and ad-hoc quotes
from skill bodies without false-positiving on legitimate intent
paraphrases (which only match descriptions).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

MIN_SUBSTRING = 25

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _body_of(skill_md: Path) -> str:
    try:
        content = skill_md.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    m = _FRONTMATTER_RE.match(content)
    if not m:
        return content
    return content[m.end():]


def _build_body_substrings(skills_dir: Path, min_len: int) -> set[str]:
    """Collect every distinct min_len-char substring from every body."""
    substrings: set[str] = set()
    for skill_md in skills_dir.glob("*/SKILL.md"):
        body = _body_of(skill_md).lower()
        # Normalize whitespace so query-side variations don't dodge the check
        body = re.sub(r"\s+", " ", body)
        for i in range(len(body) - min_len + 1):
            substrings.add(body[i : i + min_len])
    return substrings


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    routing_dir = Path(__file__).resolve().parent.parent
    p.add_argument("--queries", type=Path, default=routing_dir / "200bench" / "queries.yaml")
    p.add_argument("--skills-dir", type=Path, default=routing_dir / "skills")
    p.add_argument(
        "--min-substring",
        type=int,
        default=MIN_SUBSTRING,
        help=f"Minimum substring length to flag (default {MIN_SUBSTRING}).",
    )
    args = p.parse_args(argv)

    print(
        f"[check_query_authorship] indexing body substrings (min_len={args.min_substring})...",
        file=sys.stderr,
    )
    body_substrings = _build_body_substrings(args.skills_dir, args.min_substring)
    print(
        f"[check_query_authorship] indexed {len(body_substrings):,} distinct "
        f"{args.min_substring}-char body substrings",
        file=sys.stderr,
    )

    queries = yaml.safe_load(args.queries.read_text())["queries"]
    violations: list[str] = []

    for q in queries:
        text = re.sub(r"\s+", " ", q["query"].lower())
        for i in range(len(text) - args.min_substring + 1):
            chunk = text[i : i + args.min_substring]
            if chunk in body_substrings:
                violations.append(
                    f"  {q['id']}: contains body substring {chunk!r}"
                )
                break  # one violation per query is enough

    if violations:
        print(
            f"[check_query_authorship] FAIL: {len(violations)} query(s) "
            f"contain ≥{args.min_substring}-char SKILL.md body substrings:",
            file=sys.stderr,
        )
        for v in violations:
            print(v, file=sys.stderr)
        return 1
    print(
        f"[check_query_authorship] OK ({len(queries)} queries, no body-substring overlap)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
