"""Print frontmatter-only briefs for the 59 gold skills.

Used during query authoring (DESIGN.md §2.3). The SKILL.md *body*
is deliberately not shown — authors should write queries against
the trigger intent in the description, not against worked
examples or implementation notes in the body. The pre-commit
``check_query_authorship.py`` enforces the no-body-substring rule.

Output is plain text on stdout, one block per gold:

    ==== G01 | 009ee35a | engineering-features-for-machine-learning (len=267)
    name: engineering-features-for-machine-learning
    description: ...

Pipe to a file or a pager:

    uv run python benchmarks/routing/scripts/show_gold_briefs.py > /tmp/golds.txt
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _load_frontmatter(skill_md: Path) -> dict:
    content = skill_md.read_text(encoding="utf-8", errors="ignore")
    m = _FRONTMATTER_RE.match(content)
    if not m:
        return {}
    try:
        return yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return {}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    routing_dir = Path(__file__).resolve().parent.parent
    p.add_argument(
        "--golds",
        type=Path,
        default=routing_dir / "200bench" / "golds.json",
    )
    p.add_argument(
        "--skills-dir",
        type=Path,
        default=routing_dir / "skills",
    )
    p.add_argument(
        "--ids",
        nargs="*",
        help="Subset of gold_ids to print (e.g. G01 G07 G42). Default: all.",
    )
    args = p.parse_args(argv)

    if not args.golds.is_file():
        print(f"[show_gold_briefs] FAIL: golds.json not found at {args.golds}", file=sys.stderr)
        return 1

    data = json.loads(args.golds.read_text())
    wanted = set(args.ids) if args.ids else None

    printed = 0
    for entry in data["golds"]:
        if wanted is not None and entry["gold_id"] not in wanted:
            continue
        skill_md = args.skills_dir / entry["sha8"] / "SKILL.md"
        fm = _load_frontmatter(skill_md)
        name = fm.get("name", "?")
        desc = str(fm.get("description", "")).strip()
        unqueriable = " [UNQUERIABLE]" if entry.get("unqueriable") else ""
        print(
            f"==== {entry['gold_id']} | {entry['sha8']} | "
            f"{entry['skill_name']} (len={entry['description_len']}){unqueriable}"
        )
        print(f"name: {name}")
        print(f"description: {desc}")
        print()
        printed += 1

    print(f"---- printed {printed} gold brief(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
