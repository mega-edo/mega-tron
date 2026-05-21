"""Task-less skill packing for interactive `codex` REPL.

When a user runs plain `codex` (no `exec --prompt`) we don't yet know the
task — we can't do semantic ranking. But we still need to keep the inlined
SKILL descriptions under Codex's 2% silent-failure threshold.

`pack()` greedily fills a CODEX_HOME symlink farm under a token budget,
ordering candidates by a simple priority (mtime by default — recently-edited
skills are the user's current focus). It's idempotent via an mtime marker
so re-invoking is a ~1ms no-op when no SKILL.md has changed.

Compare to `Stager`:
- Stager: task-aware, embeds + ranks, takes a RankedSkill list, drops
  budget-overflow into a manifest.
- Packer: task-less, no embedding, drops silently — used at codex startup
  before any prompt exists.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from mega_tron.router import load_skills
from mega_tron.stager import safe_clear_dir


@dataclass
class PackResult:
    packed: int
    total: int
    total_tok: int
    budget_tok: int
    skipped_invalid: int


def _priority_mtime(skills_dir: Path) -> dict[str, float]:
    """Return {skill_name: SKILL.md mtime} — newer = higher priority."""
    out: dict[str, float] = {}
    for entry in skills_dir.iterdir():
        skill_md = entry / "SKILL.md"
        if skill_md.exists():
            out[entry.name] = skill_md.stat().st_mtime
    return out


def _read_marker(marker: Path) -> float | None:
    try:
        return float(marker.read_text())
    except (FileNotFoundError, ValueError, OSError):
        return None


def _max_skill_mtime(skills_dir: Path) -> float:
    mtimes = [
        (entry / "SKILL.md").stat().st_mtime
        for entry in skills_dir.iterdir()
        if (entry / "SKILL.md").exists()
    ]
    return max(mtimes) if mtimes else 0.0


def pack(
    *,
    skills_dir: Path,
    target: Path,
    budget_tok: int,
    order: str = "mtime",
    quiet: bool = False,
) -> PackResult | None:
    """Pack as many skills as fit under budget into `target/skills/`.

    Returns None on cache hit (no rebuild needed). Returns PackResult on rebuild.
    """
    skills_dir = Path(skills_dir)
    target = Path(target).resolve()
    farm = target / "skills"

    if not skills_dir.exists():
        if not quiet:
            print(
                f"[pack] skills-dir {skills_dir} not found — passthrough mode.",
                file=sys.stderr,
            )
        return None

    skill_mtime = _max_skill_mtime(skills_dir)
    marker = target / ".pack-mtime"
    cached = _read_marker(marker)
    # Fast path: cache is fresh.
    if cached is not None and cached >= skill_mtime and farm.exists():
        return None

    invalid: list = []
    skills = load_skills(skills_dir, invalid=invalid)

    # Order candidates.
    if order == "mtime":
        priority = _priority_mtime(skills_dir)
        skills.sort(key=lambda s: priority.get(s.skill_dir.name, 0.0), reverse=True)
    elif order == "name":
        skills.sort(key=lambda s: s.name)
    else:
        raise ValueError(f"unknown order {order!r}")

    target.mkdir(parents=True, exist_ok=True)
    farm.mkdir(exist_ok=True)
    safe_clear_dir(farm)

    total_tok = 0
    packed = 0
    for s in skills:
        if total_tok + s.desc_tok > budget_tok:
            continue
        link = farm / s.name
        try:
            link.symlink_to(s.skill_dir)
        except FileExistsError:
            continue  # dedup already done by load_skills; defensive only
        total_tok += s.desc_tok
        packed += 1

    marker.write_text(str(skill_mtime))
    result = PackResult(
        packed=packed,
        total=len(skills),
        total_tok=total_tok,
        budget_tok=budget_tok,
        skipped_invalid=len(invalid),
    )
    if not quiet:
        print(
            f"[pack] packed={result.packed}/{result.total} tok={result.total_tok}/{result.budget_tok} "
            f"order={order} target={target}",
            file=sys.stderr,
        )
    return result
