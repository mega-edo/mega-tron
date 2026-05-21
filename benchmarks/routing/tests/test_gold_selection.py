"""Verify ``select_golds.py`` is a deterministic pure function of
``pool_manifest.json`` + ``seed=42``."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SELECT_GOLDS = REPO_ROOT / "benchmarks" / "routing" / "scripts" / "select_golds.py"
ROUTING_DIR = REPO_ROOT / "benchmarks" / "routing"


def test_select_golds_is_deterministic(tmp_path: Path) -> None:
    """Two runs against the committed manifest produce byte-identical golds.json."""
    out_a = tmp_path / "golds_a.json"
    out_b = tmp_path / "golds_b.json"
    for out in (out_a, out_b):
        rc = subprocess.run(
            [
                sys.executable, str(SELECT_GOLDS),
                "--manifest", str(ROUTING_DIR / "200bench" / "pool_manifest.json"),
                "--skills-dir", str(ROUTING_DIR / "skills"),
                "--out", str(out),
            ],
            check=False,
        ).returncode
        assert rc == 0
    assert out_a.read_bytes() == out_b.read_bytes()


def test_select_golds_matches_committed() -> None:
    """The committed golds.json matches what select_golds.py would produce now.
    Detects accidental drift (e.g. someone hand-edits golds.json without re-running)."""
    committed = (ROUTING_DIR / "200bench" / "golds.json").read_text()
    # Re-run into a temp file and compare
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w+", suffix=".json", delete=False) as tf:
        tmp_out = Path(tf.name)
    try:
        rc = subprocess.run(
            [
                sys.executable, str(SELECT_GOLDS),
                "--manifest", str(ROUTING_DIR / "200bench" / "pool_manifest.json"),
                "--skills-dir", str(ROUTING_DIR / "skills"),
                "--out", str(tmp_out),
            ],
            check=False,
        ).returncode
        assert rc == 0
        regenerated = tmp_out.read_text()
        assert committed == regenerated, (
            "Committed golds.json differs from regenerated output. "
            "Was it hand-edited? Re-run scripts/select_golds.py."
        )
    finally:
        tmp_out.unlink(missing_ok=True)


def test_golds_have_59_entries() -> None:
    data = json.loads((ROUTING_DIR / "200bench" / "golds.json").read_text())
    assert data["_meta"]["seed"] == 42
    assert data["_meta"]["gold_count"] == 59
    assert len(data["golds"]) == 59
    # gold_ids are G01..G59 in order
    expected_ids = [f"G{i:02d}" for i in range(1, 60)]
    actual_ids = [g["gold_id"] for g in data["golds"]]
    assert actual_ids == expected_ids
    # Sorted by sha256
    shas = [g["sha256"] for g in data["golds"]]
    assert shas == sorted(shas)
