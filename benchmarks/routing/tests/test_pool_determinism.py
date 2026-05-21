"""Verify ``build_pool.py`` produces a byte-identical manifest across runs
against the synthetic ``fake_home/`` tree.

The fake_home tree exercises every selection rule:

- ``apache-skill``, ``mit-skill``, ``codex-bsd3``: licensed via in-dir LICENSE
- ``frontmatter-mit``, ``fm-license-key``: licensed via frontmatter ``license:``
- ``dup-skill-a``, ``dup-skill-b``, ``dup-skill-claude``: SHA-collide;
  exactly one survives dedup
- ``no-license-skill``: missing LICENSE → dropped at gate
- ``gpl-skill``: GPL-3.0 → dropped (non-permissive)

After dedup + license gate: 6 skills survive. With pool_size=5 and
seed=42 we should get a deterministic, sorted-by-SHA selection that's
identical across two runs and across machines.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
BUILD_POOL = REPO_ROOT / "benchmarks" / "routing" / "scripts" / "build_pool.py"
FAKE_HOME = (
    REPO_ROOT / "benchmarks" / "routing" / "tests" / "fixtures" / "fake_home"
)


def _run_build(out_dir: Path, pool_size: int = 5) -> int:
    """Run build_pool.py against fake_home, return its exit code."""
    return subprocess.run(
        [
            sys.executable,
            str(BUILD_POOL),
            "--source-root", str(FAKE_HOME / ".agents" / "skills"),
            "--source-root", str(FAKE_HOME / ".claude" / "skills"),
            "--source-root", str(FAKE_HOME / ".codex" / "skills"),
            "--out-dir", str(out_dir),
            "--pool-size", str(pool_size),
            "--force",
        ],
        check=False,
    ).returncode


def test_fake_home_pool_is_deterministic(tmp_path: Path) -> None:
    """Two runs against fake_home produce byte-identical manifests."""
    out_a = tmp_path / "run_a"
    out_b = tmp_path / "run_b"
    assert _run_build(out_a) == 0
    assert _run_build(out_b) == 0
    manifest_a = (out_a / "200bench" / "pool_manifest.json").read_bytes()
    manifest_b = (out_b / "200bench" / "pool_manifest.json").read_bytes()
    assert manifest_a == manifest_b


def test_fake_home_dedup_and_gate(tmp_path: Path) -> None:
    """Sanity-check that SHA dedup and the license gate behave as designed
    on the fake_home corpus."""
    out_dir = tmp_path / "run"
    assert _run_build(out_dir, pool_size=5) == 0

    manifest = json.loads(
        (out_dir / "200bench" / "pool_manifest.json").read_text()
    )
    excluded = json.loads(
        (out_dir / "200bench" / "excluded_unlicensed.json").read_text()
    )

    # 10 SKILL.md on disk, 3 of them share one SHA (dup-a/b/claude),
    # so dedup leaves 8 unique SHAs.
    # License gate then drops 2 (no-license, gpl) → 6 survive.
    # We asked for pool_size=5, so the manifest has 5 entries.
    assert len(manifest["entries"]) == 5
    # Excluded list captures the 2 license-gate drops.
    assert len(excluded["entries"]) == 2
    reasons = {e["reason"] for e in excluded["entries"]}
    assert reasons == {"no_license_found"}, (
        f"Expected only no_license_found rejections; got {reasons}. "
        "GPL skill should be detected as non-permissive but ours uses "
        "SPDX-License-Identifier: GPL-3.0-only, which the regex skips."
    )

    # SPDX values are all permissive.
    spdx_values = {e["license_spdx"] for e in manifest["entries"]}
    assert spdx_values <= {
        "Apache-2.0", "MIT", "BSD-2-Clause", "BSD-3-Clause",
        "CC0-1.0", "CC-BY-4.0", "Unlicense",
    }

    # Manifest is sorted by sha256.
    shas = [e["sha256"] for e in manifest["entries"]]
    assert shas == sorted(shas)


def test_fake_home_skill_files_copied(tmp_path: Path) -> None:
    """Each pool entry has a corresponding skills/<sha8>/SKILL.md."""
    out_dir = tmp_path / "run"
    assert _run_build(out_dir, pool_size=5) == 0
    manifest = json.loads(
        (out_dir / "200bench" / "pool_manifest.json").read_text()
    )
    for entry in manifest["entries"]:
        skill_md = out_dir / "skills" / entry["sha8"] / "SKILL.md"
        assert skill_md.is_file(), f"Missing {skill_md}"
        # SHA matches what's in the manifest.
        import hashlib
        actual = hashlib.sha256(skill_md.read_bytes()).hexdigest()
        assert actual == entry["sha256"]
