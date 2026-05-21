"""Claude Code installer tests.

Covers:
- Stage 0 stub no longer applies — real implementation must hit settings.json + CLAUDE.md
- settings.json merge is idempotent (running twice → same content)
- Existing user-owned hooks are preserved
- --uninstall removes only managed entries (user hooks survive)
- --print-only doesn't touch any files
- CLAUDE.md guidance block is sentinel-bounded and idempotent
- --uninstall on a missing settings.json is a no-op (doesn't crash)
"""
from __future__ import annotations

import argparse
import io
import json
from contextlib import redirect_stderr, redirect_stdout

import pytest

import mega_tron.hosts.claude_code.install as ic
from mega_tron.hosts.claude_code.install import (
    MANAGED_KEY,
    MANAGED_VERSION,
    _hook_entry,
    _merge_hook_entry,
    _resolve_hook_command,
    _strip_managed_hooks,
    render_claude_md_block,
    run_install_claude,
)


def _args(**overrides) -> argparse.Namespace:
    defaults = dict(
        target="claude",
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
    """Redirect SETTINGS_PATH + CLAUDE_MD_PATH into a tmp home for each test."""
    settings = tmp_path / ".claude" / "settings.json"
    claude_md = tmp_path / ".claude" / "CLAUDE.md"
    monkeypatch.setattr(ic, "SETTINGS_PATH", settings)
    monkeypatch.setattr(ic, "CLAUDE_MD_PATH", claude_md)
    return settings, claude_md


# --- Hook entry shape -------------------------------------------------------


def test_hook_entry_shape():
    e = _hook_entry("mega-tron claude-hook")
    assert e["matcher"] == ".*"
    assert e[MANAGED_KEY] == MANAGED_VERSION
    assert e["hooks"] == [{"type": "command", "command": "mega-tron claude-hook"}]


def test_resolve_hook_command_uses_absolute_path_when_on_path(monkeypatch):
    """When PATH resolution succeeds, the entry uses the absolute path
    so the hook still fires inside Claude's slimmed subprocess env."""
    import mega_tron.hosts._hook_command as hc

    monkeypatch.setattr(hc.shutil, "which", lambda name: "/opt/bin/mega-tron")
    assert (
        _resolve_hook_command(None, "claude-hook")
        == "/opt/bin/mega-tron claude-hook"
    )


def test_resolve_hook_command_user_override_passthrough():
    assert (
        _resolve_hook_command("/opt/bin/megacli", "claude-stop-hook")
        == "/opt/bin/megacli claude-stop-hook"
    )


# --- settings.json merge ----------------------------------------------------


def test_merge_into_empty_settings():
    entry = _hook_entry("mega-tron claude-hook")
    out = _merge_hook_entry({}, entry, event="UserPromptSubmit")
    assert out["hooks"]["UserPromptSubmit"] == [entry]


def test_merge_preserves_user_hooks():
    user_entry = {
        "matcher": "Bash",
        "hooks": [{"type": "command", "command": "user-custom.sh"}],
    }
    existing = {"hooks": {"UserPromptSubmit": [user_entry]}}
    entry = _hook_entry("mega-tron claude-hook")
    out = _merge_hook_entry(existing, entry, event="UserPromptSubmit")
    ups = out["hooks"]["UserPromptSubmit"]
    assert user_entry in ups
    assert entry in ups


def test_merge_replaces_stale_managed_entry():
    """Re-running install replaces our previous entry rather than appending."""
    old = _hook_entry("mega-tron claude-hook")
    old[MANAGED_KEY] = "0.0.0-old"  # simulate an old version on disk
    existing = {"hooks": {"UserPromptSubmit": [old]}}
    new_entry = _hook_entry("mega-tron claude-hook")
    out = _merge_hook_entry(existing, new_entry, event="UserPromptSubmit")
    ups = out["hooks"]["UserPromptSubmit"]
    assert old not in ups
    assert new_entry in ups
    assert len(ups) == 1


def test_strip_keeps_user_hooks():
    user_entry = {
        "matcher": "Bash",
        "hooks": [{"type": "command", "command": "user-custom.sh"}],
    }
    managed = _hook_entry("mega-tron claude-hook")
    existing = {
        "hooks": {
            "UserPromptSubmit": [user_entry, managed],
            "Stop": [_hook_entry("mega-tron claude-stop-hook")],
        }
    }
    out = _strip_managed_hooks(existing)
    assert out["hooks"]["UserPromptSubmit"] == [user_entry]
    assert "Stop" not in out["hooks"]  # was only managed → dropped entirely


# --- End-to-end installer flow ---------------------------------------------


def test_install_writes_settings_and_claude_md(isolated_home):
    settings_path, claude_md_path = isolated_home
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        rc = run_install_claude(_args())
    assert rc == 0
    assert settings_path.exists()
    data = json.loads(settings_path.read_text())
    ups = data["hooks"]["UserPromptSubmit"]
    stop = data["hooks"]["Stop"]
    assert any(MANAGED_KEY in e for e in ups)
    assert any(MANAGED_KEY in e for e in stop)
    assert claude_md_path.exists()
    md = claude_md_path.read_text()
    assert "mega-tron" in md
    assert ic.CLAUDE_SENTINEL_START in md
    assert ic.CLAUDE_SENTINEL_END in md


def test_install_is_idempotent(isolated_home):
    settings_path, claude_md_path = isolated_home
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        run_install_claude(_args())
        first_settings = settings_path.read_text()
        first_md = claude_md_path.read_text()
        run_install_claude(_args())
        second_settings = settings_path.read_text()
        second_md = claude_md_path.read_text()
    assert first_settings == second_settings
    assert first_md == second_md


def test_install_preserves_existing_user_hooks(isolated_home):
    settings_path, _ = isolated_home
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    user_data = {
        "hooks": {
            "UserPromptSubmit": [
                {
                    "matcher": "*",
                    "hooks": [{"type": "command", "command": "user-script.sh"}],
                }
            ]
        }
    }
    settings_path.write_text(json.dumps(user_data, indent=2))
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        rc = run_install_claude(_args())
    assert rc == 0
    after = json.loads(settings_path.read_text())
    ups = after["hooks"]["UserPromptSubmit"]
    # User hook + our managed hook both present
    assert any(
        e.get("hooks", [{}])[0].get("command") == "user-script.sh" for e in ups
    )
    assert any(MANAGED_KEY in e for e in ups)


def test_print_only_does_not_modify_files(isolated_home):
    settings_path, claude_md_path = isolated_home
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        rc = run_install_claude(_args(print_only=True))
    assert rc == 0
    assert not settings_path.exists()
    assert not claude_md_path.exists()
    payload = json.loads(stdout.getvalue())
    assert "userPromptSubmit_entry" in payload
    assert "stop_entry" in payload
    assert "claude_md_block" in payload


def test_uninstall_removes_only_managed(isolated_home):
    settings_path, claude_md_path = isolated_home
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        run_install_claude(_args())
    # Add a user hook before uninstalling
    data = json.loads(settings_path.read_text())
    data["hooks"]["UserPromptSubmit"].append(
        {"matcher": "Edit", "hooks": [{"type": "command", "command": "lint.sh"}]}
    )
    settings_path.write_text(json.dumps(data, indent=2))
    with redirect_stderr(stderr):
        run_install_claude(_args(uninstall=True))
    after = json.loads(settings_path.read_text())
    ups = after.get("hooks", {}).get("UserPromptSubmit", [])
    assert len(ups) == 1
    assert ups[0]["hooks"][0]["command"] == "lint.sh"
    assert MANAGED_KEY not in ups[0]
    # CLAUDE.md was uninstalled too
    assert not claude_md_path.exists() or "mega-tron" not in claude_md_path.read_text()


def test_uninstall_missing_settings_is_noop(isolated_home):
    settings_path, claude_md_path = isolated_home
    assert not settings_path.exists()
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        rc = run_install_claude(_args(uninstall=True))
    assert rc == 0


def test_claude_md_block_contains_self_eval_contract():
    block = render_claude_md_block()
    assert "<skill-used" in block
    assert "Stop hook" in block
    assert MANAGED_VERSION in block
