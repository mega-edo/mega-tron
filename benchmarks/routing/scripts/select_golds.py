"""Pick 59 gold skills from the 500-skill pool, deterministically.

The pool is fixed by ``200bench/pool_manifest.json``. From it we select
59 skills whose frontmatter ``description`` length sits in the
``[p30, p80]`` percentile range. The percentile filter is the only
non-uniform-random step; it removes one-line stub descriptions (too
terse to author a query against) and multi-paragraph essays (too
verbose, risk of inadvertent query/body overlap) without making
per-skill judgment calls.

See ``benchmarks/routing/DESIGN.md`` §2.2 for the full rationale.

Output: ``200bench/golds.json`` — ordered list of
``{gold_id: G01..G59, sha256, sha8, skill_name, description_len}``.
``Random(42).sample(filtered, 59)`` is the source of randomness;
re-running produces a byte-identical file.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import statistics
import sys
from pathlib import Path

import yaml

SEED = 42
GOLD_COUNT = 59
LOW_PCT = 30
HIGH_PCT = 80

# Same SkillCandidate semantics used in build_pool.py — we don't need
# the dataclass here, just the data dicts straight from the manifest.

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _description_length(skill_md: Path) -> int:
    """Length of the YAML-parsed ``description`` field, in characters.

    Returns 0 on missing / unparsable frontmatter so the skill ranks
    in the bottom and gets filtered out by the percentile gate.
    """
    try:
        content = skill_md.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return 0
    m = _FRONTMATTER_RE.match(content)
    if not m:
        return 0
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return 0
    desc = fm.get("description")
    if desc is None:
        return 0
    return len(str(desc).strip())


def _skill_name(skill_md: Path) -> str:
    """Return the frontmatter ``name`` value, or '' on failure."""
    try:
        content = skill_md.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    m = _FRONTMATTER_RE.match(content)
    if not m:
        return ""
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return ""
    return str(fm.get("name", "")).strip()


def select_golds(
    manifest_path: Path,
    skills_dir: Path,
    *,
    gold_count: int = GOLD_COUNT,
    low_pct: int = LOW_PCT,
    high_pct: int = HIGH_PCT,
    seed: int = SEED,
) -> dict:
    """Pure function: ``manifest + skills_dir → golds dict``.

    The output is a dict (not just a list) so we can attach metadata
    like ``regenerated_at_seed`` later if seed=42 ever has to be
    bumped because of >5 unqueriable golds.
    """
    manifest = json.loads(manifest_path.read_text())
    entries = manifest["entries"]

    # Compute description length for every pool member.
    enriched = []
    for e in entries:
        skill_md = skills_dir / e["sha8"] / "SKILL.md"
        enriched.append({
            "sha256": e["sha256"],
            "sha8": e["sha8"],
            "skill_name": _skill_name(skill_md),
            "description_len": _description_length(skill_md),
        })

    lengths = [e["description_len"] for e in enriched]
    if len(lengths) < gold_count:
        raise ValueError(
            f"Pool has only {len(lengths)} entries, need at least {gold_count}."
        )

    # Percentile thresholds (inclusive both ends).
    p_low = int(statistics.quantiles(lengths, n=100)[low_pct - 1])
    p_high = int(statistics.quantiles(lengths, n=100)[high_pct - 1])

    candidates = [e for e in enriched if p_low <= e["description_len"] <= p_high]
    if len(candidates) < gold_count:
        raise ValueError(
            f"Only {len(candidates)} skills in [P{low_pct}={p_low}, "
            f"P{high_pct}={p_high}], need {gold_count}."
        )

    # Deterministic sample. We sort candidates by sha256 first so the
    # sampling input is stable regardless of OS-walk / dict ordering.
    candidates_sorted = sorted(candidates, key=lambda e: e["sha256"])
    rng = random.Random(seed)
    chosen = rng.sample(candidates_sorted, gold_count)

    # Assign G01..G59 in sha256-sorted order so re-running produces
    # the same gold_id → sha mapping.
    chosen.sort(key=lambda e: e["sha256"])
    for idx, entry in enumerate(chosen, start=1):
        entry["gold_id"] = f"G{idx:02d}"
        entry["unqueriable"] = False  # author may flip this to true

    return {
        "_meta": {
            "seed": seed,
            "gold_count": gold_count,
            "candidate_count": len(candidates),
            "low_pct": low_pct,
            "high_pct": high_pct,
            "p_low_len": p_low,
            "p_high_len": p_high,
        },
        "golds": [
            {
                "gold_id": e["gold_id"],
                "sha256": e["sha256"],
                "sha8": e["sha8"],
                "skill_name": e["skill_name"],
                "description_len": e["description_len"],
                "unqueriable": e["unqueriable"],
            }
            for e in chosen
        ],
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    routing_dir = Path(__file__).resolve().parent.parent
    p.add_argument(
        "--manifest",
        type=Path,
        default=routing_dir / "200bench" / "pool_manifest.json",
    )
    p.add_argument(
        "--skills-dir",
        type=Path,
        default=routing_dir / "skills",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=routing_dir / "200bench" / "golds.json",
    )
    args = p.parse_args(argv)

    if not args.manifest.is_file():
        print(f"[select_golds] FAIL: manifest not found: {args.manifest}", file=sys.stderr)
        return 1
    if not args.skills_dir.is_dir():
        print(f"[select_golds] FAIL: skills_dir not found: {args.skills_dir}", file=sys.stderr)
        return 1

    result = select_golds(args.manifest, args.skills_dir)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(result, indent=2, sort_keys=False, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(
        f"[select_golds] wrote {len(result['golds'])} golds → {args.out}",
        file=sys.stderr,
    )
    print(
        f"[select_golds] candidate range: "
        f"P{result['_meta']['low_pct']}={result['_meta']['p_low_len']}, "
        f"P{result['_meta']['high_pct']}={result['_meta']['p_high_len']} "
        f"({result['_meta']['candidate_count']} candidates)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
