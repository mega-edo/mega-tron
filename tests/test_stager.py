"""Stager symlink farm + budget cap + safe_clear_dir."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from mega_tron.router import RankedSkill, Skill
from mega_tron.stager import Stager, safe_clear_dir


def _ranked(name: str, desc_tok: int, score: float = 1.0, skill_dir: Path | None = None) -> RankedSkill:
    return RankedSkill(
        skill=Skill(
            name=name,
            skill_dir=skill_dir or Path("/tmp/fake") / name,
            description=f"desc for {name}",
            desc_tok=desc_tok,
            sha="x" * 16,
        ),
        score=score,
    )


def test_safe_clear_dir_removes_contents_only(tmp_path):
    (tmp_path / "a").write_text("x")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "y").write_text("y")
    safe_clear_dir(tmp_path)
    # Parent dir survives.
    assert tmp_path.exists()
    # Contents gone.
    assert list(tmp_path.iterdir()) == []


def test_safe_clear_dir_refuses_file(tmp_path):
    f = tmp_path / "file"
    f.write_text("x")
    with pytest.raises(RuntimeError):
        safe_clear_dir(f)


def test_safe_clear_dir_silent_when_missing(tmp_path):
    # Does not raise.
    safe_clear_dir(tmp_path / "does-not-exist")


def test_stager_creates_symlink_farm(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "skill-a").mkdir()
    target = tmp_path / "codex_home"
    stager = Stager(target=target, budget_tok=10_000)
    ranked = [_ranked("skill-a", desc_tok=10, skill_dir=src / "skill-a")]
    manifest = stager.stage(ranked)
    farm = target / "skills"
    assert farm.exists()
    assert (farm / "skill-a").is_symlink()
    assert (farm / "skill-a").resolve() == (src / "skill-a").resolve()
    assert manifest.total_staged_tok == 10
    assert len(manifest.staged) == 1
    assert len(manifest.dropped) == 0


def test_stager_budget_cap_drops_overflow(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    for name in ["a", "b", "c"]:
        (src / name).mkdir()
    target = tmp_path / "codex_home"
    stager = Stager(target=target, budget_tok=15)
    ranked = [
        _ranked("a", desc_tok=10, score=0.9, skill_dir=src / "a"),
        _ranked("b", desc_tok=10, score=0.8, skill_dir=src / "b"),  # would overflow → dropped
        _ranked("c", desc_tok=3, score=0.7, skill_dir=src / "c"),  # fits in remaining 5
    ]
    manifest = stager.stage(ranked)
    staged_names = [s.name for s in manifest.staged]
    dropped_names = [d.name for d in manifest.dropped]
    assert "a" in staged_names
    assert "c" in staged_names
    assert "b" in dropped_names
    assert manifest.total_staged_tok == 13  # 10 + 3


def test_stager_writes_manifest_json(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a").mkdir()
    target = tmp_path / "codex_home"
    stager = Stager(target=target, budget_tok=100)
    stager.stage([_ranked("a", desc_tok=10, skill_dir=src / "a")])
    manifest_file = target / "codex_home_staged.json"
    assert manifest_file.exists()
    data = json.loads(manifest_file.read_text())
    assert data["staged"][0]["name"] == "a"


def test_stager_idempotent_re_stage(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a").mkdir()
    (src / "b").mkdir()
    target = tmp_path / "codex_home"
    stager = Stager(target=target, budget_tok=100)
    stager.stage([_ranked("a", desc_tok=5, skill_dir=src / "a")])
    # Second stage with a different skill must clear "a" and stage "b".
    stager.stage([_ranked("b", desc_tok=5, skill_dir=src / "b")])
    farm = target / "skills"
    assert not (farm / "a").exists()
    assert (farm / "b").exists()


def test_stager_symlink_collision_drops_gracefully(tmp_path):
    """Two ranked entries with the same name must not raise FileExistsError."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a").mkdir()
    target = tmp_path / "codex_home"
    stager = Stager(target=target, budget_tok=100)
    ranked = [
        _ranked("a", desc_tok=5, score=0.9, skill_dir=src / "a"),
        _ranked("a", desc_tok=5, score=0.5, skill_dir=src / "a"),  # collision
    ]
    manifest = stager.stage(ranked)
    assert len(manifest.staged) == 1
    assert len(manifest.dropped) == 1
    assert manifest.dropped[0].reason == "symlink_collision"


def test_stager_on_stage_callback_fires(tmp_path):
    """Stager(on_stage=cb) lets integrators tap into per-call manifests."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a").mkdir()
    target = tmp_path / "codex_home"
    seen: list = []
    stager = Stager(target=target, budget_tok=100, on_stage=lambda m: seen.append(m))
    stager.stage([_ranked("a", desc_tok=5, skill_dir=src / "a")])
    assert len(seen) == 1
    assert seen[0].staged[0].name == "a"
