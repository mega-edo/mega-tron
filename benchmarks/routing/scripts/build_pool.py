"""Deterministically sample 500 SKILL.md files from the user's local install
and freeze them under ``benchmarks/routing/skills/`` for hermetic benchmarking.

The pool's identity is captured in ``200bench/pool_manifest.json``. See
``benchmarks/routing/DESIGN.md`` §2.1 for the full design rationale.

Algorithm (seed=42, deterministic):

1. Walk source roots ``~/.agents/skills``, ``~/.claude/skills``,
   ``~/.codex/skills`` (override with ``--source-root`` for tests).
2. SHA-256 dedup, first-occurrence wins.
3. License gate — keep only skills with a permissive SPDX identifier
   (Apache-2.0, MIT, BSD-2/3-Clause, CC0-1.0, CC-BY-4.0, Unlicense).
   License is detected from frontmatter ``license:`` key or by walking
   up to the source root looking for a ``LICENSE`` / ``LICENSE.md`` /
   ``LICENSE.txt`` / ``COPYING`` file and pattern-matching its body.
4. ``Random(42).sample(licensed_pool, 500)``; sort by SHA-256.
5. Copy ``SKILL.md`` (+ ``references/``, ``scripts/`` siblings, each
   capped at 64 KB) into ``<out-dir>/skills/<sha256[:8]>/``.
6. Write ``<out-dir>/200bench/pool_manifest.json``,
   ``<out-dir>/200bench/LICENSES.md``,
   ``<out-dir>/200bench/excluded_unlicensed.json``,
   ``<out-dir>/200bench/NOTICE-fragments.md``.

If the license gate would reject more than 50% of the discovered
skills the script fails loudly so the user can patch upstream
LICENSE files or relax the gate intentionally.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import shutil
import sys
from collections import Counter
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Iterable

SEED = 42
POOL_SIZE = 500
SIBLING_FILE_CAP_BYTES = 64 * 1024
LICENSE_GATE_MAX_REJECT_FRAC = 0.50

# Permissive SPDX identifiers we accept. Anything else (GPL, AGPL,
# proprietary, unknown) is rejected at the license gate.
PERMISSIVE_SPDX = {
    "Apache-2.0",
    "MIT",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "CC0-1.0",
    "CC-BY-4.0",
    "Unlicense",
}

LICENSE_FILE_CANDIDATES = ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING")

# Source roots walked in priority order. First-occurrence-wins on
# SHA collision means ~/.agents copies are canonical, since that's
# the typical convention (host-neutral pool that mirrors out).
DEFAULT_SOURCE_ROOTS = (
    Path.home() / ".agents" / "skills",
    Path.home() / ".claude" / "skills",
    Path.home() / ".codex" / "skills",
)


# --- License detection ----------------------------------------------------

_SPDX_RE = re.compile(r"SPDX-License-Identifier:\s*([A-Za-z0-9.\-]+)", re.I)
_APACHE_RE = re.compile(r"Apache License,?\s*Version\s*2\.0", re.I)
_MIT_RE = re.compile(r"\bMIT License\b|\blicensed under the MIT\b", re.I)
_BSD2_RE = re.compile(r"BSD[\s-]?2[\s-]?Clause", re.I)
_BSD3_RE = re.compile(r"BSD[\s-]?3[\s-]?Clause", re.I)
_CC0_RE = re.compile(r"\bCC0\b|Creative Commons Zero", re.I)
_CC_BY_RE = re.compile(r"Creative Commons Attribution\s*4\.0|\bCC[\s-]?BY[\s-]?4\.0\b", re.I)
_UNLICENSE_RE = re.compile(r"\bUnlicense\b|unlicense\.org", re.I)


def _detect_spdx_from_text(text: str) -> str | None:
    """Best-effort SPDX detection from a LICENSE file body."""
    if m := _SPDX_RE.search(text):
        spdx = m.group(1)
        return spdx if spdx in PERMISSIVE_SPDX else None
    if _APACHE_RE.search(text):
        return "Apache-2.0"
    if _MIT_RE.search(text):
        return "MIT"
    if _BSD2_RE.search(text):
        return "BSD-2-Clause"
    if _BSD3_RE.search(text):
        return "BSD-3-Clause"
    if _CC0_RE.search(text):
        return "CC0-1.0"
    if _CC_BY_RE.search(text):
        return "CC-BY-4.0"
    if _UNLICENSE_RE.search(text):
        return "Unlicense"
    return None


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_LICENSE_KEY_RE = re.compile(r"^license:\s*['\"]?([^'\"\n]+?)['\"]?\s*$", re.M)


def _detect_license(
    skill_md: Path,
    source_root: Path,
) -> tuple[str | None, str | None]:
    """Return ``(spdx, license_source_path)`` or ``(None, None)``.

    Checks (in order):
      1. SKILL.md frontmatter ``license:`` key.
      2. ``LICENSE`` file in the skill dir.
      3. ``LICENSE`` file in any ancestor up to ``source_root``.
    """
    # 1. Frontmatter
    try:
        content = skill_md.read_text(encoding="utf-8", errors="ignore")
        if m := _FRONTMATTER_RE.match(content):
            fm = m.group(1)
            if km := _LICENSE_KEY_RE.search(fm):
                spdx = km.group(1).strip()
                if spdx in PERMISSIVE_SPDX:
                    return spdx, f"frontmatter:{skill_md.name}"
    except OSError:
        pass

    # 2-3. Walk up looking for LICENSE files
    current = skill_md.parent
    while True:
        for lf_name in LICENSE_FILE_CANDIDATES:
            lf = current / lf_name
            if lf.is_file():
                try:
                    text = lf.read_text(encoding="utf-8", errors="ignore")
                    spdx = _detect_spdx_from_text(text)
                    if spdx:
                        return spdx, str(lf)
                except OSError:
                    continue
        if current == source_root or current.parent == current:
            break
        current = current.parent
    return None, None


# --- Pool building --------------------------------------------------------

@dataclass(frozen=True)
class SkillCandidate:
    """A SKILL.md that survived dedup. License is filled in by the gate."""
    sha256: str
    skill_md: Path
    skill_dir: Path
    source_root: Path
    byte_size: int
    license_spdx: str | None = None
    license_source: str | None = None


@dataclass
class ManifestEntry:
    sha256: str
    sha8: str
    source_root: str
    original_dirname: str
    byte_size: int
    license_spdx: str
    license_source: str


@dataclass
class ExcludedEntry:
    sha256: str
    source_root: str
    original_dirname: str
    reason: str  # "no_license_found" | "non_permissive: <spdx>"


def discover_and_dedup(
    source_roots: Iterable[Path],
) -> tuple[list[SkillCandidate], int]:
    """Walk source roots and SHA-dedup. Returns (candidates, total_seen)."""
    seen: dict[str, SkillCandidate] = {}
    total = 0
    for root in source_roots:
        if not root.is_dir():
            continue
        for skill_md in sorted(root.rglob("SKILL.md")):
            total += 1
            try:
                data = skill_md.read_bytes()
            except OSError:
                continue
            sha = hashlib.sha256(data).hexdigest()
            if sha in seen:
                continue
            seen[sha] = SkillCandidate(
                sha256=sha,
                skill_md=skill_md,
                skill_dir=skill_md.parent,
                source_root=root,
                byte_size=len(data),
            )
    return list(seen.values()), total


def apply_license_gate(
    candidates: list[SkillCandidate],
) -> tuple[list[SkillCandidate], list[ExcludedEntry]]:
    """Return (licensed_candidates, excluded_entries)."""
    licensed: list[SkillCandidate] = []
    excluded: list[ExcludedEntry] = []
    for c in candidates:
        spdx, src = _detect_license(c.skill_md, c.source_root)
        if spdx is None:
            excluded.append(ExcludedEntry(
                sha256=c.sha256,
                source_root=str(c.source_root),
                original_dirname=c.skill_dir.name,
                reason="no_license_found",
            ))
            continue
        licensed.append(SkillCandidate(
            sha256=c.sha256,
            skill_md=c.skill_md,
            skill_dir=c.skill_dir,
            source_root=c.source_root,
            byte_size=c.byte_size,
            license_spdx=spdx,
            license_source=src,
        ))
    return licensed, excluded


def sample_pool(
    licensed: list[SkillCandidate], pool_size: int = POOL_SIZE
) -> list[SkillCandidate]:
    """seed=42 random sample of ``pool_size``; sorted by sha256."""
    # Sort by sha256 first so the sample input is deterministic
    # regardless of OS-walk order.
    sorted_input = sorted(licensed, key=lambda c: c.sha256)
    rng = random.Random(SEED)
    chosen = rng.sample(sorted_input, pool_size)
    return sorted(chosen, key=lambda c: c.sha256)


# --- Output writing -------------------------------------------------------

def _copy_skill(
    cand: SkillCandidate,
    dest_dir: Path,
) -> tuple[int, list[str]]:
    """Copy SKILL.md + bounded siblings. Returns (siblings_copied, skipped_too_large)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(cand.skill_md, dest_dir / "SKILL.md")
    siblings = 0
    skipped: list[str] = []
    for sibling_dir_name in ("references", "scripts"):
        src = cand.skill_dir / sibling_dir_name
        if not src.is_dir():
            continue
        dst = dest_dir / sibling_dir_name
        dst.mkdir(parents=True, exist_ok=True)
        for f in src.rglob("*"):
            if not f.is_file():
                continue
            rel = f.relative_to(src)
            if f.stat().st_size > SIBLING_FILE_CAP_BYTES:
                skipped.append(f"{sibling_dir_name}/{rel}")
                continue
            target = dst / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, target)
            siblings += 1
    return siblings, skipped


def _write_manifest(
    pool: list[SkillCandidate],
    manifest_path: Path,
) -> None:
    entries = [
        ManifestEntry(
            sha256=c.sha256,
            sha8=c.sha256[:8],
            source_root=str(c.source_root),
            original_dirname=c.skill_dir.name,
            byte_size=c.byte_size,
            license_spdx=c.license_spdx or "",
            license_source=c.license_source or "",
        )
        for c in pool
    ]
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {"_meta": {"seed": SEED, "size": POOL_SIZE}, "entries": [asdict(e) for e in entries]},
            indent=2,
            sort_keys=False,
        ) + "\n",
        encoding="utf-8",
    )


def _write_licenses_md(
    pool: list[SkillCandidate],
    out_path: Path,
) -> None:
    lines = [
        "# License audit — 500-skill pool",
        "",
        "Every skill in `benchmarks/routing/skills/` carries a permissive",
        "SPDX identifier. This table is the source of truth for the",
        "benchmark's license posture.",
        "",
        "| sha8 | skill_dirname | SPDX | license_source |",
        "|---|---|---|---|",
    ]
    for c in pool:
        src = (c.license_source or "").replace("|", "\\|")
        lines.append(
            f"| `{c.sha256[:8]}` | `{c.skill_dir.name}` | {c.license_spdx} | `{src}` |"
        )
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_excluded(
    excluded: list[ExcludedEntry],
    out_path: Path,
) -> None:
    out_path.write_text(
        json.dumps(
            {
                "_meta": {"count": len(excluded)},
                "entries": [asdict(e) for e in excluded],
            },
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )


def _write_notice_fragments(
    pool: list[SkillCandidate],
    out_path: Path,
) -> None:
    """Group pool by parent source dir under each source root; emit
    one stanza per distinct upstream pack so the user can copy into
    the top-level NOTICE file."""
    by_root: dict[str, Counter] = {}
    for c in pool:
        root_str = str(c.source_root)
        by_root.setdefault(root_str, Counter())[c.skill_dir.parent.name] += 1

    lines = [
        "# NOTICE fragments (per upstream source)",
        "",
        "Copy the stanzas below into the repo's top-level `NOTICE` file.",
        "Each stanza summarises one source root + parent-pack and the",
        "count of skills sampled from it.",
        "",
    ]
    for root, packs in sorted(by_root.items()):
        lines.append(f"## Source root: `{root}`")
        lines.append("")
        for pack, n in sorted(packs.items(), key=lambda x: (-x[1], x[0])):
            lines.append(f"- `{pack}` — {n} skill(s) sampled")
        lines.append("")
    out_path.write_text("\n".join(lines), encoding="utf-8")


# --- Driver ---------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument(
        "--source-root",
        type=Path,
        action="append",
        help="Override default source roots (repeatable). Used by tests.",
    )
    p.add_argument(
        "--out-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Output dir; pool lands at <out-dir>/skills/, "
             "manifest at <out-dir>/200bench/. "
             "Default: benchmarks/routing/.",
    )
    p.add_argument(
        "--allow-gate-failure",
        action="store_true",
        help="Skip the >50% rejection guard (use only intentionally).",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Wipe <out-dir>/skills/ before writing.",
    )
    p.add_argument(
        "--pool-size",
        type=int,
        default=POOL_SIZE,
        help=f"Override target pool size (default {POOL_SIZE}, used by tests).",
    )
    args = p.parse_args(argv)

    source_roots = args.source_root or list(DEFAULT_SOURCE_ROOTS)
    pool_size = args.pool_size

    print(f"[build_pool] source roots:", file=sys.stderr)
    for r in source_roots:
        marker = "ok" if r.is_dir() else "MISSING"
        print(f"  - {r} ({marker})", file=sys.stderr)

    candidates, total = discover_and_dedup(source_roots)
    print(
        f"[build_pool] discovered {total} SKILL.md, "
        f"deduped to {len(candidates)} unique SHAs",
        file=sys.stderr,
    )

    licensed, excluded = apply_license_gate(candidates)
    reject_frac = len(excluded) / len(candidates) if candidates else 0.0
    print(
        f"[build_pool] license gate: {len(licensed)} kept, "
        f"{len(excluded)} dropped ({reject_frac:.1%})",
        file=sys.stderr,
    )
    if reject_frac > LICENSE_GATE_MAX_REJECT_FRAC and not args.allow_gate_failure:
        print(
            f"[build_pool] FAIL: license gate rejects "
            f"{reject_frac:.1%} > {LICENSE_GATE_MAX_REJECT_FRAC:.0%}. "
            "Patch upstream LICENSE files or pass --allow-gate-failure.",
            file=sys.stderr,
        )
        return 2

    if len(licensed) < pool_size:
        print(
            f"[build_pool] FAIL: only {len(licensed)} licensed skills, "
            f"need {pool_size}.",
            file=sys.stderr,
        )
        return 3

    pool = sample_pool(licensed, pool_size=pool_size)
    print(
        f"[build_pool] sampled {len(pool)} skills (seed={SEED}, "
        f"sha256-sorted)",
        file=sys.stderr,
    )

    skills_dir = args.out_dir / "skills"
    bench_dir = args.out_dir / "200bench"

    if args.force and skills_dir.exists():
        shutil.rmtree(skills_dir)
    skills_dir.mkdir(parents=True, exist_ok=True)
    bench_dir.mkdir(parents=True, exist_ok=True)

    total_siblings = 0
    skipped_oversize: list[tuple[str, str]] = []
    for c in pool:
        dest = skills_dir / c.sha256[:8]
        siblings, skipped = _copy_skill(c, dest)
        total_siblings += siblings
        for s in skipped:
            skipped_oversize.append((c.sha256[:8], s))

    _write_manifest(pool, bench_dir / "pool_manifest.json")
    _write_licenses_md(pool, bench_dir / "LICENSES.md")
    _write_excluded(excluded, bench_dir / "excluded_unlicensed.json")
    _write_notice_fragments(pool, bench_dir / "NOTICE-fragments.md")

    spdx_counts = Counter(c.license_spdx for c in pool)
    print(
        f"[build_pool] wrote {pool_size} skills to {skills_dir} "
        f"({total_siblings} siblings, {len(skipped_oversize)} oversize skipped)",
        file=sys.stderr,
    )
    print(
        f"[build_pool] SPDX distribution: {dict(spdx_counts)}",
        file=sys.stderr,
    )
    print(f"[build_pool] manifest → {bench_dir / 'pool_manifest.json'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
