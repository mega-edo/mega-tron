"""Frontmatter → SQLite migration — end-to-end contract.

Pins the v1.2 migration story:

- A fresh install has no store; migration creates one, synthesises
  ``helpful_count + harmful_count`` verdict rows per skill, and stamps
  ``mega_meta.schema: 2`` on every migrated SKILL.md.
- ``--dry-run`` reports the same counts as a real run but writes
  nothing — no store file, no frontmatter edits, no backup.
- A second migration on a populated store refuses to run (would
  double-count); ``force=True`` overrides.
- ``rollback`` restores SKILL.md files byte-for-byte and renames the
  store aside so a re-attempt starts fresh.
- Synthetic verdict timestamps fall outside any reasonable regression
  window — Phase 3's classifier sees them as baseline-only data.
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mega_tron.verdicts.migration import (
    FRONTMATTER_SCHEMA_VERSION,
    SYNTHETIC_BASELINE_OFFSET_DAYS,
    migrate_to_sqlite,
    rollback,
)
from mega_tron.verdicts.store import Store


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate the store path + XDG_DATA_HOME so each test gets a fresh
    backup root and a fresh ``store.db``."""
    store_dir = tmp_path / "data"
    store_dir.mkdir()
    monkeypatch.setenv("MEGA_TRON_STORE", str(store_dir / "store.db"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    return tmp_path


@pytest.fixture
def writable_skills(fixtures_dir: Path, tmp_path: Path) -> Path:
    """Copy 3 fixture skills into a writable tmp dir so migration can
    mutate them. The first skill gets a seeded ``mega_meta:`` block;
    the other two stay clean (will be skipped)."""
    dst = tmp_path / "skills"
    dst.mkdir()
    src = fixtures_dir / "skills"
    chosen = sorted(p for p in src.iterdir() if p.is_dir())[:3]
    for d in chosen:
        shutil.copytree(d, dst / d.name)
    # Seed one with mega_meta so migration has something to do.
    first = dst / chosen[0].name / "SKILL.md"
    body = first.read_text()
    seeded = body.replace(
        "description:",
        (
            "mega_meta:\n"
            "  helpful_count: 5\n"
            "  harmful_count: 1\n"
            "  status: active\n"
            "  last_updated: 2026-05-01T12:00:00Z\n"
            "description:"
        ),
        1,
    )
    first.write_text(seeded)
    return dst


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_migration_happy_path(isolated_env, writable_skills: Path):
    """One mega_meta-bearing skill → 6 synthetic verdicts in the
    time-series table + schema marker. The 2 clean skills are skipped
    but still get the schema marker so frontmatter stays consistently
    tagged. Cumulative counters remain in SKILL.md mega_meta — we no
    longer maintain a derived SQLite state cache."""
    stats = migrate_to_sqlite(skills_dirs=[writable_skills])
    assert stats.skills_scanned == 3
    assert stats.skills_migrated == 1  # only the seeded one had counters
    assert stats.skills_skipped == 2
    assert stats.verdicts_synthesized == 6  # 5 helpful + 1 harmful
    assert stats.backup_dir is not None
    assert stats.backup_dir.exists()

    store = Store()
    assert store.count_verdicts() == 6
    seeded_name = sorted(p.name for p in writable_skills.iterdir())[0]

    # Aggregate verdicts directly — derived `skill_state` was retired.
    with store._connect() as conn:
        cur = conn.execute(
            """
            SELECT
              SUM(CASE WHEN verdict='HELPFUL' THEN 1 ELSE 0 END),
              SUM(CASE WHEN verdict='HARMFUL' THEN 1 ELSE 0 END)
            FROM verdicts WHERE skill_name = ?
            """,
            (seeded_name,),
        )
        h, x = cur.fetchone()
    assert h == 5
    assert x == 1

    # All 3 SKILL.md files now carry the schema marker.
    for d in sorted(writable_skills.iterdir()):
        text = (d / "SKILL.md").read_text()
        assert "schema: 2" in text or f"schema: {FRONTMATTER_SCHEMA_VERSION}" in text


def test_dry_run_writes_nothing(isolated_env, writable_skills: Path):
    """--dry-run reports counts but no store file, no backup, no
    frontmatter mutation."""
    before_first = (writable_skills / sorted(p.name for p in writable_skills.iterdir())[0] / "SKILL.md").read_text()

    stats = migrate_to_sqlite(skills_dirs=[writable_skills], dry_run=True)
    assert stats.skills_migrated == 1
    assert stats.verdicts_synthesized == 6
    assert stats.backup_dir is None

    # Store file should not exist yet.
    store = Store()
    assert not store.path.exists() or store.count_verdicts() == 0

    # Frontmatter unchanged.
    after_first = (writable_skills / sorted(p.name for p in writable_skills.iterdir())[0] / "SKILL.md").read_text()
    assert before_first == after_first


def test_double_migration_refuses_without_force(
    isolated_env, writable_skills: Path
):
    migrate_to_sqlite(skills_dirs=[writable_skills])
    with pytest.raises(RuntimeError, match="already contains verdicts"):
        migrate_to_sqlite(skills_dirs=[writable_skills])


def test_double_migration_with_force_double_counts(
    isolated_env, writable_skills: Path
):
    """Force is a foot-gun: 6 verdicts become 12 — documented behaviour."""
    migrate_to_sqlite(skills_dirs=[writable_skills])
    migrate_to_sqlite(skills_dirs=[writable_skills], force=True)
    store = Store()
    assert store.count_verdicts() == 12


def test_manifest_records_each_migrated_file(
    isolated_env, writable_skills: Path
):
    stats = migrate_to_sqlite(skills_dirs=[writable_skills])
    manifest_path = stats.backup_dir / "manifest.json"
    assert manifest_path.exists()
    data = json.loads(manifest_path.read_text())
    assert data["store_path"]
    entries = data["entries"]
    assert len(entries) == 1  # only the migrated skill is backed up
    e = entries[0]
    assert "skill_name" in e and "src_path" in e and "backup_path" in e
    assert "sha_before" in e and len(e["sha_before"]) == 64  # SHA-256 hex
    assert Path(e["backup_path"]).exists()


def test_rollback_restores_files_and_renames_store(
    isolated_env, writable_skills: Path
):
    """rollback() copies backups back over originals and renames the
    store so a re-attempt starts fresh."""
    first_name = sorted(p.name for p in writable_skills.iterdir())[0]
    first_md = writable_skills / first_name / "SKILL.md"
    pre_migration = first_md.read_text()

    stats = migrate_to_sqlite(skills_dirs=[writable_skills])
    assert "schema: 2" in first_md.read_text()
    store_before = Store()
    assert store_before.path.exists()

    n_restored = rollback(stats.backup_dir)
    assert n_restored == 1
    assert first_md.read_text() == pre_migration  # byte-identical restore

    # Store file renamed aside.
    assert not store_before.path.exists()
    siblings = list(store_before.path.parent.glob(store_before.path.name + ".rolled-back-*"))
    assert len(siblings) == 1


def test_rollback_missing_manifest_raises(isolated_env, tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="manifest.json"):
        rollback(tmp_path / "nonexistent-backup")


def test_synthetic_timestamps_fall_outside_recent_window(
    isolated_env, writable_skills: Path
):
    """Synthetic rows are clamped before the regression window so
    Phase 3 sees them only as baseline, never as recent."""
    migrate_to_sqlite(skills_dirs=[writable_skills])
    store = Store()
    # Pull every synthesised verdict; assert each occurred_at is
    # at least SYNTHETIC_BASELINE_OFFSET_DAYS in the past.
    with store._connect() as conn:
        cur = conn.execute("SELECT occurred_at FROM verdicts")
        timestamps = [row[0] for row in cur.fetchall()]
    assert len(timestamps) == 6
    cutoff = datetime.now(timezone.utc) - timedelta(
        days=SYNTHETIC_BASELINE_OFFSET_DAYS - 1
    )
    for t in timestamps:
        ts = datetime.fromisoformat(t.rstrip("Z")).replace(tzinfo=timezone.utc)
        assert ts < cutoff, f"synthetic timestamp {t} too recent"


def test_megacore_auto_discovers_migrated_store(
    isolated_env, writable_skills: Path, fake_embedder, tmp_path: Path
):
    """After migration, constructing :class:`MegaCore` (without an
    explicit ``store=...``) picks up the SQLite store automatically.

    This is the silent-activation property: a user who runs
    ``migrate-to-sqlite`` once and never changes any code starts getting
    dual-write on the next hook fire.
    """
    migrate_to_sqlite(skills_dirs=[writable_skills])
    from mega_tron.cache import Cache
    from mega_tron.core import MegaCore

    core = MegaCore(
        skills_dirs=[writable_skills],
        embedder=fake_embedder,
        cache=Cache(path=tmp_path / "cache.npz"),
    )
    assert core.store is not None
    assert core.store.path == Store().path


def test_recording_after_migration_dual_writes(
    isolated_env, writable_skills: Path, fake_embedder, tmp_path: Path
):
    """A new verdict recorded post-migration lands in both SQLite and
    the SKILL.md ``mega_meta:`` block."""
    migrate_to_sqlite(skills_dirs=[writable_skills])
    from mega_tron.cache import Cache
    from mega_tron.core import MegaCore, Verdict
    from mega_tron.verdicts.mega_meta import read_meta

    seeded_name = sorted(p.name for p in writable_skills.iterdir())[0]
    core = MegaCore(
        skills_dirs=[writable_skills],
        embedder=fake_embedder,
        cache=Cache(path=tmp_path / "cache.npz"),
    )
    core.record_verdict(
        Verdict(
            skill_name=seeded_name,
            verdict="HELPFUL",
            host="hermes",
            reason="post-migration evidence",
            session_id="post-migration-session",
        )
    )

    # SQLite: 6 synthetic + 1 real = 7 verdicts.
    assert Store().count_verdicts() == 7

    # Frontmatter: counter bumped from 5 → 6.
    skill_md = writable_skills / seeded_name / "SKILL.md"
    fm_meta = read_meta(skill_md)
    assert fm_meta.helpful_count == 6
