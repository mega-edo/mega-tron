"""Gemini CLI installer tests.

Covers:
- Hook entry shape (BeforeAgent + AfterAgent, _megaOptimusManaged tag)
- settings.json merge is idempotent (running twice → same content)
- Existing user-owned hooks are preserved across install + uninstall
- ``--uninstall`` removes only managed entries
- ``--print-only`` doesn't touch any files
- GEMINI.md guidance block is sentinel-bounded and idempotent
- ``--uninstall`` on a missing settings.json is a no-op (doesn't crash)
- Install snapshots the user's pre-install ``skills.disabled``;
  uninstall restores it from the backup
"""
from __future__ import annotations

import argparse
import json

import pytest

import mega_tron.hosts.gemini_cli.install as ig
import mega_tron.hosts.gemini_cli.skill_overrides as so
from mega_tron.hosts.gemini_cli.install import (
    MANAGED_KEY,
    MANAGED_VERSION,
    _hook_entry,
    _merge_hook_entry,
    _resolve_hook_command,
    _strip_managed_hooks,
    render_gemini_md_block,
    run_install_gemini,
)


def _args(**overrides) -> argparse.Namespace:
    defaults = dict(
        target="gemini",
        uninstall=False,
        print_only=False,
        no_warmup=True,  # avoid network/disk during tests
        hook_command=None,
        skills_dir=None,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Redirect SETTINGS_PATH + GEMINI_MD_PATH + BACKUP_PATH into tmp home."""
    settings = tmp_path / ".gemini" / "settings.json"
    gemini_md = tmp_path / ".gemini" / "GEMINI.md"
    backup = tmp_path / ".gemini" / "settings.json.mega-tron-backup"
    monkeypatch.setattr(ig, "SETTINGS_PATH", settings)
    monkeypatch.setattr(ig, "GEMINI_MD_PATH", gemini_md)
    monkeypatch.setattr(so, "SETTINGS_PATH", settings)
    monkeypatch.setattr(so, "BACKUP_PATH", backup)
    return settings, gemini_md, backup


# --- Hook entry shape -------------------------------------------------------


def test_hook_entry_shape():
    entry = _hook_entry("mega-tron gemini-hook")
    assert entry["matcher"] == ".*"
    assert entry[MANAGED_KEY] == MANAGED_VERSION
    assert entry["hooks"] == [{"type": "command", "command": "mega-tron gemini-hook"}]


def test_resolve_hook_command_uses_absolute_path_when_on_path(monkeypatch):
    """When PATH resolution succeeds, the entry uses the absolute path
    so the hook still fires inside Gemini's slimmed subprocess env."""
    import mega_tron.hosts._hook_command as hc

    monkeypatch.setattr(hc.shutil, "which", lambda name: "/opt/bin/mega-tron")
    assert (
        _resolve_hook_command(None, "gemini-hook")
        == "/opt/bin/mega-tron gemini-hook"
    )


def test_resolve_hook_command_user_override_passthrough():
    assert (
        _resolve_hook_command("/usr/local/bin/mega-tron", "gemini-stop-hook")
        == "/usr/local/bin/mega-tron gemini-stop-hook"
    )


def test_resolve_hook_command_falls_back_to_bare(monkeypatch, capsys):
    """When the binary isn't found anywhere, fall back to bare command
    and surface a warning so the user can fix it."""
    import mega_tron.hosts._hook_command as hc

    monkeypatch.setattr(hc.shutil, "which", lambda name: None)
    monkeypatch.setattr(hc, "sys", type("S", (), {"executable": "/nonexistent/python", "stderr": __import__("sys").stderr}))
    out = _resolve_hook_command(None, "gemini-hook")
    assert out == "mega-tron gemini-hook"
    captured = capsys.readouterr()
    assert "could not locate" in captured.err


def test_resolve_hook_command_uv_venv_symlink_regression(monkeypatch, tmp_path):
    """Regression test for the .resolve() bug.

    uv-managed venvs symlink ``.venv/bin/python`` to a hidden interpreter
    under ``~/.local/share/uv/python/.../bin/`` where the project's
    entry-point scripts do NOT exist. Before the fix, we called
    ``.resolve()`` on ``sys.executable`` and followed the symlink out of
    the venv, so the sibling check would fail and the installer fell back
    to bare ``mega-tron`` even though the binary was right there in
    ``.venv/bin/``. This test creates the same symlink topology and
    verifies the entry-point script is now found via lexical parent
    (no symlink resolution).
    """
    import mega_tron.hosts._hook_command as hc

    # PATH lookup must miss so we fall into the sibling branch.
    monkeypatch.setattr(hc.shutil, "which", lambda name: None)

    # Real venv layout: .venv/bin/python is a symlink to a hidden uv
    # python, and the mega-tron entry-point lives next to the SYMLINK,
    # not next to the symlink target.
    venv_bin = tmp_path / "project" / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    uv_bin = tmp_path / ".local" / "share" / "uv" / "python" / "bin"
    uv_bin.mkdir(parents=True)
    real_python = uv_bin / "python3"
    real_python.write_text("#!/bin/sh\n")
    real_python.chmod(0o755)
    venv_python = venv_bin / "python"
    venv_python.symlink_to(real_python)
    venv_mega_tron = venv_bin / "mega-tron"
    venv_mega_tron.write_text("#!/usr/bin/env python\n")
    venv_mega_tron.chmod(0o755)

    monkeypatch.setattr(
        hc,
        "sys",
        type(
            "S",
            (),
            {"executable": str(venv_python), "stderr": __import__("sys").stderr},
        ),
    )

    out = _resolve_hook_command(None, "gemini-hook")

    # The fix uses lexical parent, so the entry-point next to the symlink
    # is found and we get an absolute path back. Pre-fix this returned
    # bare "mega-tron gemini-hook" with a warning.
    assert out == f"{venv_mega_tron} gemini-hook"


# --- settings.json merge primitives ----------------------------------------


def test_merge_into_empty_settings():
    entry = _hook_entry("mega-tron gemini-hook")
    out = _merge_hook_entry({}, entry, event="BeforeAgent")
    assert out["hooks"]["BeforeAgent"] == [entry]


def test_merge_preserves_user_hooks():
    user_hook = {
        "matcher": ".*",
        "hooks": [{"type": "command", "command": "echo user"}],
    }
    existing = {"hooks": {"BeforeAgent": [user_hook]}}
    entry = _hook_entry("mega-tron gemini-hook")
    out = _merge_hook_entry(existing, entry, event="BeforeAgent")
    assert user_hook in out["hooks"]["BeforeAgent"]
    assert entry in out["hooks"]["BeforeAgent"]


def test_merge_replaces_stale_managed_entry():
    stale = {
        "matcher": ".*",
        MANAGED_KEY: "0.4.99-gemini",
        "hooks": [{"type": "command", "command": "mega-tron old-cmd"}],
    }
    existing = {"hooks": {"BeforeAgent": [stale]}}
    entry = _hook_entry("mega-tron gemini-hook")
    out = _merge_hook_entry(existing, entry, event="BeforeAgent")
    # The stale managed entry is replaced — only the fresh one survives.
    assert out["hooks"]["BeforeAgent"] == [entry]


def test_strip_keeps_user_hooks():
    user_hook = {
        "matcher": ".*",
        "hooks": [{"type": "command", "command": "echo user"}],
    }
    managed = _hook_entry("mega-tron gemini-hook")
    existing = {"hooks": {"BeforeAgent": [user_hook, managed]}}
    out = _strip_managed_hooks(existing)
    assert out["hooks"]["BeforeAgent"] == [user_hook]


# --- End-to-end install / uninstall -----------------------------------------


def test_install_writes_settings_and_gemini_md(isolated_home):
    settings_path, gemini_md_path, _backup = isolated_home
    rc = run_install_gemini(_args())
    assert rc == 0
    assert settings_path.exists()
    data = json.loads(settings_path.read_text())
    # Both events registered with managed marker
    assert MANAGED_KEY in data["hooks"]["BeforeAgent"][0]
    assert MANAGED_KEY in data["hooks"]["AfterAgent"][0]
    # Command is auto-resolved to an absolute path (or bare fallback if
    # neither PATH nor sys.executable's sibling has it). Either way it
    # must end with the right subcommand suffix.
    assert (
        data["hooks"]["BeforeAgent"][0]["hooks"][0]["command"]
        .endswith("mega-tron gemini-hook")
    )
    assert (
        data["hooks"]["AfterAgent"][0]["hooks"][0]["command"]
        .endswith("mega-tron gemini-stop-hook")
    )
    assert gemini_md_path.exists()
    md = gemini_md_path.read_text()
    assert ig.GEMINI_SENTINEL_START in md
    assert ig.GEMINI_SENTINEL_END in md


def test_install_is_idempotent(isolated_home):
    settings_path, gemini_md_path, _ = isolated_home
    run_install_gemini(_args())
    first_settings = settings_path.read_text()
    first_md = gemini_md_path.read_text()
    rc = run_install_gemini(_args())
    assert rc == 0
    # Second run should produce byte-identical content (managed marker
    # version pinned, sentinel block stable).
    assert settings_path.read_text() == first_settings
    assert gemini_md_path.read_text() == first_md


def test_install_preserves_existing_user_hooks(isolated_home):
    settings_path, _md, _ = isolated_home
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    user_hook = {
        "matcher": "^my-prefix",
        "hooks": [{"type": "command", "command": "echo user-script"}],
    }
    settings_path.write_text(
        json.dumps({"hooks": {"BeforeAgent": [user_hook]}}, indent=2)
    )
    rc = run_install_gemini(_args())
    assert rc == 0
    after = json.loads(settings_path.read_text())
    bagents = after["hooks"]["BeforeAgent"]
    assert any(h.get("matcher") == "^my-prefix" for h in bagents)
    assert any(h.get(MANAGED_KEY) == MANAGED_VERSION for h in bagents)


def test_print_only_does_not_modify_files(isolated_home):
    settings_path, gemini_md_path, _ = isolated_home
    rc = run_install_gemini(_args(print_only=True))
    assert rc == 0
    # Neither file should exist after a print-only invocation.
    assert not settings_path.exists()
    assert not gemini_md_path.exists()


def test_uninstall_removes_only_managed(isolated_home):
    settings_path, _md, _ = isolated_home
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    user_hook = {
        "matcher": "^my-prefix",
        "hooks": [{"type": "command", "command": "echo user"}],
    }
    settings_path.write_text(
        json.dumps({"hooks": {"AfterAgent": [user_hook]}}, indent=2)
    )
    run_install_gemini(_args())
    run_install_gemini(_args(uninstall=True))
    after = json.loads(settings_path.read_text())
    # User's AfterAgent hook survives; our managed entries are gone.
    assert after["hooks"]["AfterAgent"] == [user_hook]
    assert "BeforeAgent" not in after.get("hooks", {})


def test_uninstall_missing_settings_is_noop(isolated_home):
    settings_path, _md, _ = isolated_home
    assert not settings_path.exists()
    rc = run_install_gemini(_args(uninstall=True))
    assert rc == 0


def test_gemini_md_block_contains_self_eval_contract():
    block = render_gemini_md_block()
    # Tagging convention — the AfterAgent hook scans for this exact tag form.
    assert "<skill-used" in block
    # Mention activate_skill (Gemini-specific) and AfterAgent (not Stop).
    assert "activate_skill" in block
    assert "AfterAgent" in block


# --- Mode-A backup / restore -----------------------------------------------


def test_install_snapshots_existing_skills_disabled(isolated_home):
    """If the user already had ``skills.disabled`` entries, install must
    snapshot them so uninstall can restore the original state."""
    settings_path, _md, backup_path = isolated_home
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps(
            {
                "skills": {"enabled": True, "disabled": ["pinned-off-skill"]},
            },
            indent=2,
        )
    )
    rc = run_install_gemini(_args())
    assert rc == 0
    assert backup_path.exists()
    backup = json.loads(backup_path.read_text())
    assert backup["skills_disabled"] == ["pinned-off-skill"]


def test_install_snapshot_is_idempotent(isolated_home):
    """Re-running install must NOT overwrite the original snapshot.

    A second snapshot would capture the post-install state (which we've
    already overwritten with router output once Mode-A runs), defeating
    the purpose of the backup."""
    settings_path, _md, backup_path = isolated_home
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps(
            {"skills": {"disabled": ["original-entry"]}}, indent=2
        )
    )
    run_install_gemini(_args())
    # Simulate Mode-A running and rewriting skills.disabled.
    data = json.loads(settings_path.read_text())
    data.setdefault("skills", {})["disabled"] = ["router-generated", "noise"]
    settings_path.write_text(json.dumps(data, indent=2))
    run_install_gemini(_args())
    # Backup must still contain the original, not the post-Mode-A state.
    backup = json.loads(backup_path.read_text())
    assert backup["skills_disabled"] == ["original-entry"]


def test_uninstall_wipes_skills_disabled_regardless_of_backup(isolated_home):
    """Uninstall must drop ``skills.disabled`` entirely.

    In practice the install-time backup usually captured prior-tool
    pollution (mega-optimus, mega-skill-router) rather than a genuine
    user-pinned set. New contract: uninstall makes skills.disabled go
    away, period — neither the backup nor the router-injected list
    survives.
    """
    settings_path, _md, backup_path = isolated_home
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps(
            {"skills": {"disabled": ["pre-install-A", "pre-install-B"]}},
            indent=2,
        )
    )
    run_install_gemini(_args())
    # Simulate Mode-A having run and rewritten skills.disabled with a huge list.
    data = json.loads(settings_path.read_text())
    data["skills"]["disabled"] = ["x", "y", "z", "w", "v"]
    settings_path.write_text(json.dumps(data, indent=2))
    run_install_gemini(_args(uninstall=True))
    # disabled key is gone entirely; pre-install entries are not restored.
    after = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    assert "disabled" not in after.get("skills", {})
    assert not backup_path.exists()


def test_uninstall_with_no_prior_disabled_removes_settings_file(isolated_home):
    """If the user had a completely empty settings.json pre-install,
    uninstall must remove the file entirely so the post-uninstall state
    matches the pre-install state (no file → no file)."""
    settings_path, _md, backup_path = isolated_home
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({}, indent=2))
    run_install_gemini(_args())
    # Simulate Mode-A populating skills.disabled.
    data = json.loads(settings_path.read_text())
    data["skills"] = {"disabled": ["x", "y"]}
    settings_path.write_text(json.dumps(data, indent=2))
    run_install_gemini(_args(uninstall=True))
    # Pre-install was effectively empty → post-uninstall is empty/absent.
    assert not settings_path.exists()
    assert not backup_path.exists()


def test_uninstall_preserves_unrelated_user_settings(isolated_home):
    """If the user has unrelated settings keys, uninstall must keep the
    file alive after dropping mega-tron's contributions."""
    settings_path, _md, backup_path = isolated_home
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps({"theme": "dark", "fontSize": 14}, indent=2)
    )
    run_install_gemini(_args())
    # Simulate Mode-A populating skills.disabled.
    data = json.loads(settings_path.read_text())
    data["skills"] = {"disabled": ["x", "y"]}
    settings_path.write_text(json.dumps(data, indent=2))
    run_install_gemini(_args(uninstall=True))
    after = json.loads(settings_path.read_text())
    # User's unrelated keys survive; managed hooks + skills.disabled are gone.
    assert after["theme"] == "dark"
    assert after["fontSize"] == 14
    assert "hooks" not in after
    assert "skills" not in after or "disabled" not in after.get("skills", {})
    assert not backup_path.exists()
