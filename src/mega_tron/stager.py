"""Stager — materialize a ranked skill list into a CODEX_HOME symlink farm.

Two safety properties:
1. SAFE_BUDGET_TOK cap stops at the Codex 2% inline budget (default 1500 tok,
   well under 2% × 128K = 2560).
2. safe_clear_dir refuses to remove the staging target itself — only its
   contents. Misconfigured paths cannot accidentally wipe a parent directory.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from mega_tron.router import RankedSkill


# Default conservative cap. Codex 2% of 128K context ≈ 2560 tok; 200K context
# would give ≈ 4000. We sit comfortably under both.
DEFAULT_BUDGET_TOK = 1500


@dataclass
class StagedItem:
    name: str
    desc_tok: int
    score: float
    sha: str
    symlink_target: str  # original skill_dir as a string


@dataclass
class DroppedItem:
    name: str
    desc_tok: int
    score: float
    reason: str


@dataclass
class StageManifest:
    target: str
    budget_tok: int
    total_staged_tok: int
    staged: list[StagedItem] = field(default_factory=list)
    dropped: list[DroppedItem] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)


def safe_clear_dir(d: Path) -> None:
    """Remove every entry *inside* d. Never removes d itself.

    Guards against the catastrophic case where a misconfigured target points at
    a directory the user did not mean to clear (their home dir, a project root,
    etc.) — those mistakes still lose only the inner symlinks, not the parent.
    """
    if not d.exists():
        return
    if not d.is_dir():
        raise RuntimeError(f"expected directory, got file: {d}")
    for entry in d.iterdir():
        if entry.is_symlink() or entry.is_file():
            entry.unlink()
        elif entry.is_dir():
            shutil.rmtree(entry)


class Stager:
    """Stage a ranked skill list into target/skills/ as a symlink farm."""

    def __init__(
        self,
        target: Path,
        budget_tok: int = DEFAULT_BUDGET_TOK,
        on_stage: Callable[[StageManifest], None] | None = None,
    ) -> None:
        self.target = Path(target).resolve()
        self.budget_tok = budget_tok
        self.on_stage = on_stage

    def stage(
        self,
        ranked: list[RankedSkill],
        write_manifest: bool = True,
    ) -> StageManifest:
        """Materialize the symlink farm and return a manifest.

        Args:
            ranked: descending-score skill list (from Router.rank).
            write_manifest: also write `<target>/codex_home_staged.json`.
        """
        skills_target = self.target / "skills"
        skills_target.mkdir(parents=True, exist_ok=True)
        safe_clear_dir(skills_target)

        manifest = StageManifest(
            target=str(self.target),
            budget_tok=self.budget_tok,
            total_staged_tok=0,
        )

        for rs in ranked:
            if manifest.total_staged_tok + rs.skill.desc_tok > self.budget_tok:
                manifest.dropped.append(
                    DroppedItem(
                        name=rs.skill.name,
                        desc_tok=rs.skill.desc_tok,
                        score=round(rs.score, 4),
                        reason="budget_exceeded",
                    )
                )
                continue
            link_path = skills_target / rs.skill.name
            try:
                link_path.symlink_to(rs.skill.skill_dir)
            except FileExistsError:
                # safe_clear_dir runs first, so this is only reachable if a
                # parallel staging or a name collision sneaks through.
                manifest.dropped.append(
                    DroppedItem(
                        name=rs.skill.name,
                        desc_tok=rs.skill.desc_tok,
                        score=round(rs.score, 4),
                        reason="symlink_collision",
                    )
                )
                continue
            manifest.staged.append(
                StagedItem(
                    name=rs.skill.name,
                    desc_tok=rs.skill.desc_tok,
                    score=round(rs.score, 4),
                    sha=rs.skill.sha,
                    symlink_target=str(rs.skill.skill_dir),
                )
            )
            manifest.total_staged_tok += rs.skill.desc_tok

        if write_manifest:
            (self.target / "codex_home_staged.json").write_text(manifest.to_json())

        if self.on_stage is not None:
            self.on_stage(manifest)

        return manifest
