"""Claude Code UserPromptSubmit hook handler tests.

Mirrors the Codex hook tests in tests/test_hook.py: cover stdin parsing,
empty-prompt / wrong-event passthrough, and the first-fire end-to-end
path with a fixture skills directory.

The shared fixture pins:
- ``XDG_RUNTIME_DIR`` to tmp so the per-session ``seen-claude-*`` marker
  doesn't leak between tests
- ``MEGA_MODE=semantic`` so the hook never tries to shell out to an LLM
  backend during tests (agentic path is exercised in test_agentic.py)
- ``MEGA_DAEMON=0`` so the hook stays in-process — no socket dance
- ``MEGA_QUIET=1`` to silence stderr noise
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

from mega_tron.hosts.claude_code.hook import cmd_claude_hook


@pytest.fixture(autouse=True)
def _isolate_hook_env(tmp_path, monkeypatch):
    runtime = tmp_path / "xdg"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("MEGA_MODE", "semantic")
    monkeypatch.setenv("MEGA_DAEMON", "0")
    monkeypatch.setenv("MEGA_QUIET", "1")
    yield


def _hook_args(**overrides) -> argparse.Namespace:
    defaults = dict(skills_dir=None, top_k=5, prepend_k=3, cache_path=None)
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _payload(prompt: str, **extra) -> dict:
    base = {
        "hook_event_name": "UserPromptSubmit",
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
            rc = cmd_claude_hook(args or _hook_args())
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
    rc, out, _ = _run(_payload("anything", hook_event_name="PreToolUse"))
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
    produce a non-empty additionalContext envelope with slash-style names."""
    fixtures = Path(__file__).parent / "fixtures" / "skills"
    assert fixtures.exists()

    args = _hook_args(
        skills_dir=str(fixtures),
        cache_path="/tmp/mega-tron-test-cache.npz",
    )
    rc, out, err = _run(_payload("Implement HMAC-SHA256 webhook signature verification"), args)
    assert rc == 0, f"hook failed: {err}"
    assert out, f"expected JSON output, got empty stdout. stderr: {err}"
    data = json.loads(out)
    assert data["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    ctx = data["hookSpecificOutput"]["additionalContext"]
    # Slash-style invocation (Claude Code) rather than $SkillName (Codex)
    assert "/" in ctx
    # The Claude prepender uses "Strongly prefer" instead of Codex's "Use"
    assert "Strongly prefer" in ctx or "skills" in ctx.lower()


def test_subsequent_turn_emits_slim_block():
    """Follow-up turns re-rank against the new prompt and emit a slim
    catalog block — name/path/desc only, no "How to use" prose, no
    inline self-eval contract (those live persistently in CLAUDE.md).
    This lets multi-turn Claude conversations keep getting fresh
    per-turn catalogs without the ~1800-tok first-fire bloat."""
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
    # First fire carries the prose + contract; follow-up does not.
    assert "### How to use these skills" in ctx1
    assert "### How to use these skills" not in ctx2
    assert "verdict=" in ctx1
    assert "verdict=" not in ctx2
    assert len(ctx2) < len(ctx1) * 0.5
