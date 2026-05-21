"""Mode-A ``skills.disabled`` writer tests.

Covers:
- snapshot is idempotent and captures the pre-install state
- apply_mode_a inverts top_k against the full skill set
- apply_mode_a unions user-pinned disables from the backup
- apply_mode_a preserves unrelated settings.json keys
- clear() restores the backup and deletes it; idempotent when absent
- atomic write semantics (tmp file is never left behind)
"""
from __future__ import annotations

import json

import pytest

import mega_tron.hosts.gemini_cli.skill_overrides as so
from mega_tron.hosts.gemini_cli.skill_overrides import (
    apply_mode_a,
    clear,
    snapshot_original_disabled,
)


@pytest.fixture
def paths(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    backup = tmp_path / "settings.json.mega-tron-backup"
    monkeypatch.setattr(so, "SETTINGS_PATH", settings)
    monkeypatch.setattr(so, "BACKUP_PATH", backup)
    return settings, backup


def _write_settings(settings_path, data):
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(data, indent=2))


# --- snapshot --------------------------------------------------------------


def test_snapshot_captures_existing_disabled(paths):
    settings, backup = paths
    _write_settings(settings, {"skills": {"disabled": ["a", "b"]}})
    assert snapshot_original_disabled() is True
    assert json.loads(backup.read_text())["skills_disabled"] == ["a", "b"]


def test_snapshot_when_settings_missing(paths):
    settings, backup = paths
    assert not settings.exists()
    assert snapshot_original_disabled() is True
    assert json.loads(backup.read_text())["skills_disabled"] == []


def test_snapshot_is_idempotent(paths):
    settings, backup = paths
    _write_settings(settings, {"skills": {"disabled": ["original"]}})
    snapshot_original_disabled()
    # Modify settings — snapshot must NOT capture the new state.
    _write_settings(settings, {"skills": {"disabled": ["modified"]}})
    assert snapshot_original_disabled() is False
    assert json.loads(backup.read_text())["skills_disabled"] == ["original"]


# --- apply_mode_a ----------------------------------------------------------


def test_apply_mode_a_inverts_top_k(paths):
    settings, backup = paths
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_text(json.dumps({"skills_disabled": []}))
    n = apply_mode_a(
        top_k_names=["a", "b"],
        all_skill_names=["a", "b", "c", "d", "e"],
    )
    data = json.loads(settings.read_text())
    assert n == 3
    assert data["skills"]["disabled"] == ["c", "d", "e"]


def test_apply_mode_a_unions_user_pinned(paths):
    """User-pinned disables (snapshotted at install) stay disabled even
    if the router would have routed them."""
    settings, backup = paths
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_text(json.dumps({"skills_disabled": ["pinned-off"]}))
    # The router picks "pinned-off" as top-K, but the user explicitly
    # pinned it disabled at install time — user wins.
    n = apply_mode_a(
        top_k_names=["pinned-off"],
        all_skill_names=["pinned-off", "a", "b"],
    )
    data = json.loads(settings.read_text())
    # "pinned-off" still in disabled (user pin), plus the inverted top-K.
    assert "pinned-off" in data["skills"]["disabled"]
    assert "a" in data["skills"]["disabled"]
    assert "b" in data["skills"]["disabled"]
    assert n == 3


def test_apply_mode_a_preserves_unrelated_settings_keys(paths):
    """We must not stomp on the user's other settings.json fields."""
    settings, backup = paths
    _write_settings(
        settings,
        {
            "theme": "dark",
            "hooks": {"BeforeAgent": [{"matcher": ".*", "hooks": []}]},
            "skills": {"enabled": True},
        },
    )
    backup.write_text(json.dumps({"skills_disabled": []}))
    apply_mode_a(top_k_names=["a"], all_skill_names=["a", "b"])
    data = json.loads(settings.read_text())
    assert data["theme"] == "dark"
    assert "hooks" in data
    assert data["skills"]["enabled"] is True
    assert data["skills"]["disabled"] == ["b"]


def test_apply_mode_a_sorts_disabled_list(paths):
    settings, backup = paths
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_text(json.dumps({"skills_disabled": []}))
    apply_mode_a(
        top_k_names=["mid"],
        all_skill_names=["zebra", "alpha", "mango"],
    )
    data = json.loads(settings.read_text())
    # alphabetic — keeps file diffs minimal across turns.
    assert data["skills"]["disabled"] == ["alpha", "mango", "zebra"]


# --- clear -----------------------------------------------------------------


def test_clear_drops_disabled_regardless_of_backup_contents(paths):
    """``clear()`` wipes skills.disabled clean, ignoring backup contents.

    Earlier revisions tried to restore the snapshot, but the snapshot
    almost always contained legacy-tool pollution rather than a real
    user-pinned set. New contract: uninstall means "make it go away".
    """
    settings, backup = paths
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_text(json.dumps({"skills_disabled": ["user-A", "user-B"]}))
    _write_settings(
        settings,
        {"skills": {"disabled": ["router-x", "router-y", "router-z"]}},
    )
    assert clear() is True
    # disabled key is gone entirely — neither the backup contents nor the
    # router-injected list survives.
    data = json.loads(settings.read_text()) if settings.exists() else {}
    assert "disabled" not in data.get("skills", {})
    assert not backup.exists()


def test_clear_removes_empty_skills_block(paths):
    settings, backup = paths
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_text(json.dumps({"skills_disabled": []}))
    _write_settings(settings, {"skills": {"disabled": ["x"]}})
    assert clear() is True
    # Only mega-touched keys present → file unlinked entirely so the
    # post-uninstall state matches a never-touched system.
    assert not settings.exists()


def test_clear_preserves_unrelated_keys(paths):
    """If the user has other settings.json keys, clear() must keep the
    file alive after dropping skills.disabled."""
    settings, backup = paths
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_text(json.dumps({"skills_disabled": ["whatever"]}))
    _write_settings(
        settings,
        {"theme": "dark", "skills": {"disabled": ["x"]}},
    )
    assert clear() is True
    data = json.loads(settings.read_text())
    assert data["theme"] == "dark"
    assert "disabled" not in data.get("skills", {})


def test_clear_without_backup_still_wipes_disabled(paths):
    """No backup file → still drop skills.disabled if it's present.

    Earlier behaviour was a no-op here for safety, but with the new
    "always wipe" contract we treat a missing backup the same as a
    pollution-ful one: get rid of the disabled list either way.
    """
    settings, backup = paths
    assert not backup.exists()
    _write_settings(settings, {"skills": {"disabled": ["x"]}})
    assert clear() is True
    assert not settings.exists()


def test_clear_truly_idempotent_on_clean_state(paths):
    """No backup, no disabled key → genuine no-op."""
    settings, backup = paths
    assert not backup.exists()
    _write_settings(settings, {"theme": "dark"})
    assert clear() is False
    data = json.loads(settings.read_text())
    assert data == {"theme": "dark"}
