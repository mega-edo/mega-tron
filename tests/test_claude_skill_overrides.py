"""Mode A: skillOverrides management in settings.local.json.

Covers the helper module in isolation:
- apply_mode_a writes the correct downgrade map (non-top-K → name-only)
- top-K skills are *not* present in the map (= implicit "on")
- clear() removes only our key, preserves user-owned settings
- empty top-K → all skills name-only
- preserves user-owned siblings in settings.local.json

The claude_hook end-to-end Mode A path is exercised via env var in a
separate test below.
"""
from __future__ import annotations

import argparse
import io
import json
import shutil
import uuid
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest

from mega_tron.hosts.claude_code.skill_overrides import apply_mode_a, clear


@pytest.fixture
def settings_local(tmp_path):
    return tmp_path / ".claude" / "settings.local.json"


def test_apply_mode_a_downgrades_non_top_k(settings_local):
    n = apply_mode_a(
        top_k_names=["a", "b"],
        all_skill_names=["a", "b", "c", "d"],
        settings_path=settings_local,
    )
    assert n == 2
    data = json.loads(settings_local.read_text())
    assert data["skillOverrides"] == {"c": "name-only", "d": "name-only"}


def test_apply_mode_a_overwrites_previous_run(settings_local):
    apply_mode_a(["a"], all_skill_names=["a", "b", "c"], settings_path=settings_local)
    apply_mode_a(["b"], all_skill_names=["a", "b", "c"], settings_path=settings_local)
    data = json.loads(settings_local.read_text())
    # Second call wins; a is back to implicit "on", b is implicit "on", c stays downgraded
    assert data["skillOverrides"] == {"a": "name-only", "c": "name-only"}


def test_apply_mode_a_preserves_user_siblings(settings_local):
    settings_local.parent.mkdir(parents=True, exist_ok=True)
    settings_local.write_text(
        json.dumps(
            {"otherUserKey": {"foo": "bar"}, "permissions": {"deny": ["Bash(rm)"]}}
        )
    )
    apply_mode_a(
        top_k_names=["a"],
        all_skill_names=["a", "b"],
        settings_path=settings_local,
    )
    data = json.loads(settings_local.read_text())
    assert data["otherUserKey"] == {"foo": "bar"}
    assert data["permissions"] == {"deny": ["Bash(rm)"]}
    assert data["skillOverrides"] == {"b": "name-only"}


def test_clear_removes_only_skill_overrides(settings_local):
    settings_local.parent.mkdir(parents=True, exist_ok=True)
    settings_local.write_text(
        json.dumps(
            {
                "skillOverrides": {"a": "name-only"},
                "userKey": "preserve me",
            }
        )
    )
    changed = clear(settings_path=settings_local)
    assert changed is True
    data = json.loads(settings_local.read_text())
    assert "skillOverrides" not in data
    assert data["userKey"] == "preserve me"


def test_clear_idempotent_when_no_overrides(settings_local):
    settings_local.parent.mkdir(parents=True, exist_ok=True)
    settings_local.write_text(json.dumps({"userKey": "x"}))
    changed = clear(settings_path=settings_local)
    assert changed is False
    data = json.loads(settings_local.read_text())
    assert data == {"userKey": "x"}


def test_clear_missing_file_is_noop(settings_local):
    assert not settings_local.exists()
    changed = clear(settings_path=settings_local)
    assert changed is False


# --- End-to-end: claude_hook honors MEGA_CLAUDE_NATIVE_MODE=active ---------


@pytest.fixture
def skills(tmp_path):
    src = Path(__file__).parent / "fixtures" / "skills"
    dst = tmp_path / "skills"
    shutil.copytree(src, dst)
    return dst


def test_hook_mode_a_writes_settings_local(tmp_path, skills, monkeypatch):
    """End-to-end: env var flips the hook into Mode A and a
    settings.local.json appears with non-top-K skills downgraded."""
    runtime = tmp_path / "xdg"
    runtime.mkdir()
    settings_local = tmp_path / ".claude" / "settings.local.json"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("MEGA_MODE", "semantic")
    monkeypatch.setenv("MEGA_DAEMON", "0")
    monkeypatch.setenv("MEGA_QUIET", "1")
    monkeypatch.setenv("MEGA_CLAUDE_NATIVE_MODE", "active")
    # Redirect the helper's default path into our tmp file
    monkeypatch.setattr(
        "mega_tron.hosts.claude_code.skill_overrides.SETTINGS_LOCAL_PATH", settings_local
    )

    from mega_tron.hosts.claude_code.hook import cmd_claude_hook

    args = argparse.Namespace(
        skills_dir=str(skills),
        top_k=5,
        prepend_k=3,
        cache_path=str(tmp_path / "cache.npz"),
    )
    payload = {
        "hook_event_name": "UserPromptSubmit",
        "prompt": "validate HMAC webhook",
        "session_id": f"mode-a-{uuid.uuid4()}",
        "cwd": "/tmp",
        "transcript_path": "/tmp/fake.jsonl",
    }
    stdout = io.StringIO()
    stderr = io.StringIO()
    with patch("sys.stdin", io.StringIO(json.dumps(payload))):
        with redirect_stdout(stdout), redirect_stderr(stderr):
            rc = cmd_claude_hook(args)
    assert rc == 0

    # Hook still emitted additionalContext (Mode A is an OVERLAY on top
    # of native catalog control, both fire together)
    data = json.loads(stdout.getvalue())
    assert "additionalContext" in data["hookSpecificOutput"]

    # And settings.local.json was written with downgrade map
    assert settings_local.exists()
    settings_data = json.loads(settings_local.read_text())
    overrides = settings_data["skillOverrides"]
    # Non-empty, all values are "name-only"
    assert overrides
    assert all(v == "name-only" for v in overrides.values())
    # The top-3 picks should NOT be in the map
    ctx = data["hookSpecificOutput"]["additionalContext"]
    top_picks = [
        line[3:].split()[0]
        for line in ctx.splitlines()
        if line.strip().startswith("- /")
    ]
    assert top_picks, "expected non-empty picks"
    for name in top_picks:
        assert name not in overrides, (
            f"top pick {name!r} unexpectedly in skillOverrides downgrade map"
        )


def test_hook_strict_mode_behaves_like_active(tmp_path, skills, monkeypatch):
    """``MEGA_CLAUDE_NATIVE_MODE=strict`` triggers the same per-turn
    skillOverrides write as ``active``. The only thing strict adds on
    top is the install-time shell wrapper — that's tested separately.
    Hook-layer behaviour must be identical so the two modes share the
    same routing semantics at runtime.
    """
    runtime = tmp_path / "xdg"
    runtime.mkdir()
    settings_local = tmp_path / ".claude" / "settings.local.json"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("MEGA_MODE", "semantic")
    monkeypatch.setenv("MEGA_DAEMON", "0")
    monkeypatch.setenv("MEGA_QUIET", "1")
    monkeypatch.setenv("MEGA_CLAUDE_NATIVE_MODE", "strict")
    monkeypatch.setattr(
        "mega_tron.hosts.claude_code.skill_overrides.SETTINGS_LOCAL_PATH",
        settings_local,
    )

    from mega_tron.hosts.claude_code.hook import cmd_claude_hook

    args = argparse.Namespace(
        skills_dir=str(skills),
        top_k=5,
        prepend_k=3,
        cache_path=str(tmp_path / "cache.npz"),
    )
    payload = {
        "hook_event_name": "UserPromptSubmit",
        "prompt": "validate HMAC webhook",
        "session_id": f"mode-strict-{uuid.uuid4()}",
        "cwd": "/tmp",
        "transcript_path": "/tmp/fake.jsonl",
    }
    stdout = io.StringIO()
    stderr = io.StringIO()
    with patch("sys.stdin", io.StringIO(json.dumps(payload))):
        with redirect_stdout(stdout), redirect_stderr(stderr):
            rc = cmd_claude_hook(args)
    assert rc == 0
    assert settings_local.exists(), (
        "strict must write skillOverrides just like active"
    )
    settings_data = json.loads(settings_local.read_text())
    overrides = settings_data["skillOverrides"]
    assert overrides
    assert all(v == "name-only" for v in overrides.values())


def test_hook_default_passive_mode_does_not_touch_settings_local(
    tmp_path, skills, monkeypatch
):
    """Default Mode P must NOT write settings.local.json."""
    runtime = tmp_path / "xdg"
    runtime.mkdir()
    settings_local = tmp_path / ".claude" / "settings.local.json"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("MEGA_MODE", "semantic")
    monkeypatch.setenv("MEGA_DAEMON", "0")
    monkeypatch.setenv("MEGA_QUIET", "1")
    # No MEGA_CLAUDE_NATIVE_MODE → default passive
    monkeypatch.delenv("MEGA_CLAUDE_NATIVE_MODE", raising=False)
    monkeypatch.setattr(
        "mega_tron.hosts.claude_code.skill_overrides.SETTINGS_LOCAL_PATH", settings_local
    )

    from mega_tron.hosts.claude_code.hook import cmd_claude_hook

    args = argparse.Namespace(
        skills_dir=str(skills),
        top_k=5,
        prepend_k=3,
        cache_path=str(tmp_path / "cache.npz"),
    )
    payload = {
        "hook_event_name": "UserPromptSubmit",
        "prompt": "validate HMAC webhook",
        "session_id": f"mode-p-{uuid.uuid4()}",
        "cwd": "/tmp",
        "transcript_path": "/tmp/fake.jsonl",
    }
    stdout = io.StringIO()
    stderr = io.StringIO()
    with patch("sys.stdin", io.StringIO(json.dumps(payload))):
        with redirect_stdout(stdout), redirect_stderr(stderr):
            rc = cmd_claude_hook(args)
    assert rc == 0
    assert not settings_local.exists()
