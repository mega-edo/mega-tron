"""Codex UserPromptSubmit hook handler.

Tests cover:
- stdin JSON parsing (well-formed + malformed)
- stdout JSON shape matches codex's UserPromptSubmitCommandOutputWire
- empty-prompt / missing-skills-dir / wrong-event passthrough
- end-to-end: top-K names + their skill_dir/description meta appear in
  additionalContext (first-fire)
- subsequent turns are a true noop — search-CLI / skill-used /
  evaluation guidance lives in AGENTS.md (planted by `install`), not
  in per-turn hook output.
"""
from __future__ import annotations

import argparse
import io
import json
import uuid
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

import pytest

from mega_tron.hosts.codex.hook import cmd_hook


@pytest.fixture(autouse=True)
def _isolate_hook_env(tmp_path, monkeypatch):
    """Pin first-fire markers + semantic mode for every hook test.

    - XDG_RUNTIME_DIR → tmp so session markers don't leak between tests.
    - MEGA_MODE=semantic — hook tests must not actually call codex /
      litellm. Tests that want to exercise the agentic path do so via
      the dedicated tests/test_agentic.py with stub backends.
    """
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
    """Hook payload with a unique session_id so first-fire fires fresh."""
    base = {
        "hook_event_name": "UserPromptSubmit",
        "prompt": prompt,
        "session_id": f"test-{uuid.uuid4()}",
    }
    base.update(extra)
    return base


def _run_hook(stdin_str: str, args: argparse.Namespace) -> tuple[int, str, str]:
    stdin = io.StringIO(stdin_str)
    stdout = io.StringIO()
    stderr = io.StringIO()
    with patch("sys.stdin", stdin), redirect_stdout(stdout), redirect_stderr(stderr):
        rc = cmd_hook(args)
    return rc, stdout.getvalue(), stderr.getvalue()


def test_hook_emits_additional_context(fake_embedder, fixtures_dir, tmp_path):
    cache = tmp_path / "bge.npz"
    args = _hook_args(
        skills_dir=str(fixtures_dir / "skills"),
        cache_path=str(cache),
    )
    with patch(
        "mega_tron.hosts.codex.hook._make_embedder",
        return_value=fake_embedder,
    ):
        rc, out, _ = _run_hook(json.dumps(_payload("validate webhook hmac signature")), args)
    assert rc == 0
    data = json.loads(out)
    assert data["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    ctx = data["hookSpecificOutput"]["additionalContext"]
    # The hook context replaces codex's native skill catalog with a
    # non-forcing candidate list: header, candidate line, ranked picks
    # with paths + descriptions, and the eval contract. No `$` token and
    # no MUST-use rule — the model judges fit, the router doesn't force.
    assert ctx.startswith("## Skills (selected for this turn")
    assert "Candidate skills for this task" in ctx
    assert "webhook-signer" in ctx  # FakeEmbedder keyword-weights this
    assert str(fixtures_dir / "skills" / "webhook-signer" / "SKILL.md") in ctx
    # The router's own scaffolding must not emit `$<picked-name>` (which
    # would trigger codex's MUST-use rule) — but we allow `$` to appear
    # inside a skill's own description, since that's user-authored
    # content that codex would have rendered anyway via its native
    # catalog. Check the candidate line + meta-block bullets directly.
    assert "$webhook-signer" not in ctx
    candidate_line = next(
        line for line in ctx.splitlines() if line.startswith("Candidate skills")
    )
    assert "$" not in candidate_line
    for line in ctx.splitlines():
        if line.startswith("- "):  # meta-block entry — bare name only
            assert not line.startswith("- $")
    assert "MUST use" not in ctx
    assert "Trigger rules" not in ctx
    assert "<skill-used" in ctx  # tag-emission reminder is inline
    # search-CLI guidance lives in AGENTS.md, not per-turn.
    assert "mega-tron find" not in ctx
    assert "mega-tron search" not in ctx


def test_hook_empty_prompt_returns_no_context(fake_embedder, fixtures_dir, tmp_path):
    args = _hook_args(skills_dir=str(fixtures_dir / "skills"), cache_path=str(tmp_path / "c.npz"))
    payload = {"hook_event_name": "UserPromptSubmit", "prompt": "   "}
    with patch("mega_tron.hosts.codex.hook._make_embedder", return_value=fake_embedder):
        rc, out, _ = _run_hook(json.dumps(payload), args)
    assert rc == 0
    assert out == ""  # no JSON emitted → codex treats as no-op


def test_hook_wrong_event_name_is_passthrough(fake_embedder, fixtures_dir, tmp_path):
    args = _hook_args(skills_dir=str(fixtures_dir / "skills"), cache_path=str(tmp_path / "c.npz"))
    payload = {"hook_event_name": "PreToolUse", "prompt": "x"}
    with patch("mega_tron.hosts.codex.hook._make_embedder", return_value=fake_embedder):
        rc, out, _ = _run_hook(json.dumps(payload), args)
    assert rc == 0
    assert out == ""


def test_hook_malformed_json_does_not_crash(tmp_path):
    args = _hook_args(skills_dir=str(tmp_path), cache_path=str(tmp_path / "c.npz"))
    rc, out, err = _run_hook("not valid json}", args)
    assert rc == 0
    assert out == ""
    assert "invalid input JSON" in err


def test_hook_missing_skills_dir_is_passthrough(fake_embedder, tmp_path):
    args = _hook_args(
        skills_dir=str(tmp_path / "does-not-exist"),
        cache_path=str(tmp_path / "c.npz"),
    )
    with patch("mega_tron.hosts.codex.hook._make_embedder", return_value=fake_embedder):
        rc, out, err = _run_hook(json.dumps(_payload("anything")), args)
    assert rc == 0
    assert out == ""
    assert "skills dir" in err


def test_hook_respects_top_k_and_prepend_k(fake_embedder, fixtures_dir, tmp_path):
    args = _hook_args(
        skills_dir=str(fixtures_dir / "skills"),
        cache_path=str(tmp_path / "c.npz"),
        top_k=2,
        prepend_k=2,
    )
    with patch("mega_tron.hosts.codex.hook._make_embedder", return_value=fake_embedder):
        rc, out, _ = _run_hook(json.dumps(_payload("webhook hmac signature")), args)
    data = json.loads(out)
    ctx = data["hookSpecificOutput"]["additionalContext"]
    # With prepend_k=2 the candidate line names exactly 2 skills.
    candidate_line = next(
        line for line in ctx.splitlines() if line.startswith("Candidate skills")
    )
    # Names are bare (no `$`) and comma-separated between the colon and
    # the period that closes the list.
    after_colon = candidate_line.split(":", 1)[1]
    names_segment = after_colon.split(".", 1)[0]
    candidate_names = [n.strip() for n in names_segment.split(",") if n.strip()]
    assert len(candidate_names) == 2
    assert "$" not in candidate_line


def test_hook_subsequent_turn_is_noop(fake_embedder, fixtures_dir, tmp_path):
    """Only the first hook of a session emits a routing prefix. Every
    subsequent turn returns empty stdout — search-CLI / evaluation
    guidance lives in AGENTS.md, not in per-turn additionalContext."""
    args = _hook_args(
        skills_dir=str(fixtures_dir / "skills"),
        cache_path=str(tmp_path / "c.npz"),
    )
    session_id = f"session-{uuid.uuid4()}"
    first = {
        "hook_event_name": "UserPromptSubmit",
        "prompt": "validate webhook hmac signature",
        "session_id": session_id,
    }
    second = {
        "hook_event_name": "UserPromptSubmit",
        "prompt": "now refactor the URL parser",
        "session_id": session_id,
    }
    with patch("mega_tron.hosts.codex.hook._make_embedder", return_value=fake_embedder):
        rc1, out1, _ = _run_hook(json.dumps(first), args)
        rc2, out2, _ = _run_hook(json.dumps(second), args)

    assert rc1 == 0 and rc2 == 0
    ctx1 = json.loads(out1)["hookSpecificOutput"]["additionalContext"]
    # First fire: full skills block emitted, no embedded search-CLI hint
    # (that guidance lives in AGENTS.md).
    assert ctx1.startswith("## Skills (selected for this turn")
    assert "Candidate skills for this task" in ctx1
    assert "mega-tron find" not in ctx1
    assert "mega-tron search" not in ctx1
    # Subsequent: hook emits nothing — codex treats it as a no-op turn.
    assert out2 == ""
