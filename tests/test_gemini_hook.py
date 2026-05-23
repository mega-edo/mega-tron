"""Gemini CLI ``BeforeAgent`` hook handler tests.

Mirrors :mod:`tests.test_claude_hook`. The hook reads Gemini's BeforeAgent
stdin envelope, runs the router, and emits a
``hookSpecificOutput.additionalContext`` envelope shaped for Gemini
(``activate_skill`` tool naming, no slash prefix, AfterAgent grading
contract). Subsequent turns in the same session are no-ops because the
persistent guidance lives in ``~/.gemini/GEMINI.md`` (Stage 3 installer).

The shared fixture pins:
- ``XDG_RUNTIME_DIR`` to tmp so the per-session ``seen-gemini-*`` marker
  doesn't leak between tests
- ``MEGA_MODE=semantic`` so the hook never tries to shell out to an LLM
  backend during tests
- ``MEGA_DAEMON=0`` so the hook stays in-process — no socket dance
- ``MEGA_QUIET=1`` to silence stderr noise
- ``MEGA_GEMINI_MODE=passive`` so Mode-A is disabled during Stage 1 tests
  (Stage 1 ships the writer as a no-op stub; Stage 3 enables it).
"""
from __future__ import annotations

import argparse
import io
import json
import uuid
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest

from mega_tron.hosts.gemini_cli.hook import cmd_gemini_hook


@pytest.fixture(autouse=True)
def _isolate_hook_env(tmp_path, monkeypatch):
    runtime = tmp_path / "xdg"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("MEGA_MODE", "semantic")
    monkeypatch.setenv("MEGA_DAEMON", "0")
    monkeypatch.setenv("MEGA_QUIET", "1")
    monkeypatch.setenv("MEGA_GEMINI_MODE", "passive")
    yield


def _hook_args(**overrides) -> argparse.Namespace:
    defaults = dict(skills_dir=None, top_k=5, prepend_k=3, cache_path=None)
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _payload(prompt: str, **extra) -> dict:
    base = {
        "hook_event_name": "BeforeAgent",
        "prompt": prompt,
        "session_id": f"test-{uuid.uuid4()}",
        "cwd": "/tmp",
        "transcript_path": "/tmp/fake.jsonl",
    }
    base.update(extra)
    return base


def _run(payload: dict | str, args: argparse.Namespace | None = None) -> tuple[int, str, str]:
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    stdout = io.StringIO()
    stderr = io.StringIO()
    with patch("sys.stdin", io.StringIO(raw)):
        with redirect_stdout(stdout), redirect_stderr(stderr):
            rc = cmd_gemini_hook(args or _hook_args())
    return rc, stdout.getvalue(), stderr.getvalue()


# --- Passthrough / no-op paths ----------------------------------------------


def test_empty_stdin_exits_zero():
    rc, out, _ = _run("")
    assert rc == 0
    assert out == ""


def test_malformed_json_exits_zero():
    rc, out, err = _run("not json {{")
    assert rc == 0
    assert out == ""
    assert "invalid input JSON" in err


def test_wrong_event_is_noop():
    """AfterAgent / SessionStart / etc. must pass through silently."""
    rc, out, _ = _run(_payload("anything", hook_event_name="AfterAgent"))
    assert rc == 0
    assert out == ""


def test_empty_prompt_is_noop():
    rc, out, _ = _run(_payload(""))
    assert rc == 0
    assert out == ""


def test_missing_skills_dir_is_noop(tmp_path):
    args = _hook_args(skills_dir=str(tmp_path / "does-not-exist"))
    rc, out, err = _run(_payload("validate this webhook"), args)
    assert rc == 0
    assert out == ""
    assert "not found" in err


# --- First-fire routing -----------------------------------------------------


def test_first_fire_emits_additional_context():
    """End-to-end: a real prompt against the bundled fixture skills should
    produce a non-empty additionalContext envelope with the BeforeAgent
    event name and activate_skill phrasing (no slash prefix)."""
    fixtures = Path(__file__).parent / "fixtures" / "skills"
    assert fixtures.exists()

    args = _hook_args(
        skills_dir=str(fixtures),
        cache_path="/tmp/mega-tron-test-cache.npz",
    )
    rc, out, err = _run(
        _payload("Implement HMAC-SHA256 webhook signature verification"), args
    )
    assert rc == 0, f"hook failed: {err}"
    assert out, f"expected JSON output, got empty stdout. stderr: {err}"
    data = json.loads(out)
    assert data["hookSpecificOutput"]["hookEventName"] == "BeforeAgent"
    ctx = data["hookSpecificOutput"]["additionalContext"]
    # Gemini-specific: activate_skill tool naming, no slash prefix on names
    assert "activate_skill" in ctx
    assert "Strongly prefer" in ctx


def test_subsequent_turn_emits_slim_block():
    """Follow-up turns re-rank with a slim block — catalog header +
    name/path/desc only. The full ~14k-char first-fire block (with
    inlined SKILL.md bodies + self-eval contract) is replaced by a
    much smaller per-turn injection. GEMINI.md owns the persistent
    self-eval contract."""
    import json as _json

    fixtures = Path(__file__).parent / "fixtures" / "skills"
    session = f"test-{uuid.uuid4()}"
    args = _hook_args(
        skills_dir=str(fixtures),
        cache_path="/tmp/mega-tron-test-cache.npz",
    )
    # First call: first-fire
    _, out1, _ = _run(_payload("validate webhook", session_id=session), args)
    # Second call: same session_id → slim block (not noop)
    rc, out2, _ = _run(_payload("another prompt", session_id=session), args)
    assert rc == 0
    assert out2 != ""

    ctx1 = _json.loads(out1)["hookSpecificOutput"]["additionalContext"]
    ctx2 = _json.loads(out2)["hookSpecificOutput"]["additionalContext"]

    # Both fire the catalog header.
    assert ctx2.startswith("## Skills (selected for this turn")
    assert "### Available skills" in ctx2
    # First fire carries the contract; follow-up does not. Inlined
    # SKILL.md bodies are also dropped on follow-up.
    assert "### How to use these skills" in ctx1
    assert "### How to use these skills" not in ctx2
    assert "verdict=" in ctx1
    assert "verdict=" not in ctx2
    assert "body: |" in ctx1
    assert "body: |" not in ctx2
    # Inlined bodies dominate Gemini's first-fire; slim should be ≪.
    assert len(ctx2) < len(ctx1) * 0.5


def test_first_fire_marker_uses_gemini_prefix(tmp_path, monkeypatch):
    """The first-fire marker must use the ``seen-gemini-`` prefix so
    Gemini sessions can't collide with Codex / Claude sessions that
    happen to share a session id."""
    from mega_tron.hosts.gemini_cli.hook import _first_fire_marker

    runtime = tmp_path / "xdg2"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    marker = _first_fire_marker("abc-123")
    assert marker is not None
    assert marker.name == "seen-gemini-abc-123"


# --- Installer / dispatcher (Stage 0 contract; sanity check) ---------------


def test_install_dispatcher_routes_gemini_target(capsys):
    """``mega-tron install --target gemini --print-only`` must reach the
    Gemini installer and emit a JSON document describing both hook entries
    plus the GEMINI.md guidance block. This is a sanity check that the
    CLI dispatcher wires through to the right installer — the actual
    installer behavior is covered in :mod:`tests.test_install_gemini`."""
    from mega_tron.cli import cmd_install

    args = argparse.Namespace(
        target="gemini",
        uninstall=False,
        print_only=True,
        no_warmup=True,
        hook_command=None,
        skills_dir=None,
    )
    rc = cmd_install(args)
    assert rc == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    # Hook command may be bare `mega-tron …` (when only PATH lookup is
    # available) or an absolute path ending in `…/mega-tron …` (when a
    # venv binary is on sys.executable's sibling path). Both are valid;
    # the dispatcher only owes us "ends with the right subcommand".
    assert payload["beforeAgent_entry"]["hooks"][0]["command"].endswith(
        "mega-tron gemini-hook"
    )
    assert payload["afterAgent_entry"]["hooks"][0]["command"].endswith(
        "mega-tron gemini-stop-hook"
    )
    assert "<skill-used" in payload["gemini_md_block"]
