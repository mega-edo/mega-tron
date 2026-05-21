"""Gemini CLI ``AfterAgent`` hook — 2-phase ask-eval / persist.

Phase 1: ``stop_hook_active=false`` → scan transcript → emit
``decision:"deny"`` + eval prompt (sent back to Gemini as a new prompt).
Phase 2: ``stop_hook_active=true`` → parse ``prompt_response`` → write
``mega_meta``. Loop-guard: a per-session marker prevents Phase 2 from
re-entering Phase 1 even if Gemini's ``stop_hook_active`` semantics shift
across versions.
"""
from __future__ import annotations

import argparse
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest

from mega_tron.hosts.gemini_cli.stop_hook import (
    EVAL_SENTINEL_END,
    EVAL_SENTINEL_START,
    cmd_gemini_stop_hook,
)
from mega_tron.verdicts.mega_meta import read_meta


@pytest.fixture(autouse=True)
def _isolate_loop_guard(tmp_path, monkeypatch):
    """Per-test runtime dir so the eval-gemini-<id> marker can't leak."""
    runtime = tmp_path / "xdg"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    yield


def _args(**overrides) -> argparse.Namespace:
    defaults = dict(skills_dir=None)
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _run(stdin_str: str, args: argparse.Namespace) -> tuple[int, str, str]:
    stdin = io.StringIO(stdin_str)
    stdout = io.StringIO()
    stderr = io.StringIO()
    with patch("sys.stdin", stdin), redirect_stdout(stdout), redirect_stderr(stderr):
        rc = cmd_gemini_stop_hook(args)
    return rc, stdout.getvalue(), stderr.getvalue()


def _write_skill(skills_dir: Path, name: str) -> Path:
    d = skills_dir / name
    d.mkdir(parents=True)
    md = d / "SKILL.md"
    md.write_text(
        f'---\nname: {name}\ndescription: "USE WHEN: x"\n---\n\nbody\n'
    )
    return md


def _write_transcript(path: Path, events: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")


def _assistant_msg(text: str) -> dict:
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        },
    }


def _exec_command(skills_dir: Path, skill_name: str) -> dict:
    """Render one transcript event for an `exec_command` call that
    invokes a `<skills_dir>/<skill_name>/scripts/...` script.

    Pairs with :func:`_assistant_msg` so the resulting transcript carries
    *both* an inline ``<skill-used .../>`` tag and a real operational
    trace — required for the stop-hook's Phase-1 admission rule that
    rejects ``claimed_use`` (tag without invocation).
    """
    cmd = f"bash {skills_dir}/{skill_name}/scripts/run.sh --check"
    return {
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "name": "exec_command",
            "arguments": json.dumps({"cmd": cmd}),
        },
    }


# --- Phase 1 ----------------------------------------------------------------


def test_phase1_emits_deny_with_eval_prompt(tmp_path):
    """Phase 1 emits ``decision:"deny"`` (Gemini's retry trigger) plus the
    sentinel-fenced eval prompt naming the invoked skill."""
    skills = tmp_path / "skills"
    skills.mkdir()
    _write_skill(skills, "webhook-signer")
    transcript = tmp_path / "rollout.jsonl"
    _write_transcript(
        transcript,
        [
            # Operational trace so the Phase-1 admission rule treats the
            # inline tag as an actual invocation rather than a quoted
            # mention.
            _exec_command(skills, "webhook-signer"),
            _assistant_msg('<skill-used name="webhook-signer" reason="HMAC"/>'),
        ],
    )
    payload = {
        "hook_event_name": "AfterAgent",
        "stop_hook_active": False,
        "transcript_path": str(transcript),
        "session_id": "g-sess-1",
        "cwd": "/tmp",
        "prompt": "validate this webhook",
        "prompt_response": "Done.",
    }
    rc, out, _ = _run(json.dumps(payload), _args(skills_dir=str(skills)))
    assert rc == 0
    data = json.loads(out)
    # Gemini uses "deny" (Codex uses "block")
    assert data["decision"] == "deny"
    # Bare name in bullets (Codex uses $-prefix)
    assert "webhook-signer" in data["reason"]
    assert "$webhook-signer" not in data["reason"]
    assert EVAL_SENTINEL_START in data["reason"]
    assert EVAL_SENTINEL_END in data["reason"]


def test_phase1_prompt_includes_evidence_preamble(tmp_path):
    """The Phase-1 prompt must carry the evidence + INCONCLUSIVE rubric."""
    skills = tmp_path / "skills"
    skills.mkdir()
    _write_skill(skills, "x")
    transcript = tmp_path / "rollout.jsonl"
    _write_transcript(
        transcript,
        [
            _exec_command(skills, "x"),
            _assistant_msg('<skill-used name="x"/>'),
        ],
    )
    payload = {
        "hook_event_name": "AfterAgent",
        "stop_hook_active": False,
        "transcript_path": str(transcript),
        "session_id": "g-sess-evidence",
    }
    rc, out, _ = _run(json.dumps(payload), _args(skills_dir=str(skills)))
    assert rc == 0
    reason = json.loads(out)["reason"]
    assert "git diff --stat" in reason
    assert "INCONCLUSIVE" in reason
    assert "evidence" in reason.lower()
    # Critical Gemini-specific guidance: do not re-invoke skills on the eval turn.
    assert "do NOT invoke" in reason or "evaluation turn" in reason


def test_phase1_no_invocations_passes_through(tmp_path):
    """No <skill-used/> tags in the transcript → quiet exit, no retry."""
    skills = tmp_path / "skills"
    skills.mkdir()
    transcript = tmp_path / "rollout.jsonl"
    _write_transcript(transcript, [_assistant_msg("Just did stuff. No skills.")])
    payload = {
        "hook_event_name": "AfterAgent",
        "stop_hook_active": False,
        "transcript_path": str(transcript),
        "session_id": "g-sess-noinv",
    }
    rc, out, _ = _run(json.dumps(payload), _args(skills_dir=str(skills)))
    assert rc == 0
    assert out == ""  # no deny → Gemini exits cleanly


# --- Phase 2 ----------------------------------------------------------------


def test_phase2_parses_verdict_and_updates_skill(tmp_path):
    """Phase 2 reads ``prompt_response`` (NOT last_assistant_message — that's
    Codex) and updates the skill's mega_meta block."""
    skills = tmp_path / "skills"
    skills.mkdir()
    skill_md = _write_skill(skills, "webhook-signer")
    verdict_payload = json.dumps(
        {
            "evaluations": [
                {
                    "skill": "webhook-signer",
                    "verdict": "HELPFUL",
                    "reason": "Caught HMAC mismatch in fixture",
                }
            ]
        }
    )
    prompt_response = (
        "Sure thing.\n"
        f"{EVAL_SENTINEL_START}\n{verdict_payload}\n{EVAL_SENTINEL_END}\n"
    )
    payload = {
        "hook_event_name": "AfterAgent",
        "stop_hook_active": True,
        "session_id": "g-sess-42",
        "prompt": "what did you do?",
        "prompt_response": prompt_response,
    }
    rc, out, _ = _run(json.dumps(payload), _args(skills_dir=str(skills)))
    assert rc == 0
    assert out == ""  # empty stdout → Gemini exits without another retry

    meta = read_meta(skill_md)
    assert meta.helpful_count == 1
    assert "Caught HMAC mismatch" in meta.helpful_contexts[0]
    assert meta.last_session_id == "g-sess-42"


def test_phase2_no_verdicts_does_not_retry(tmp_path):
    """If the model didn't comply, Phase 2 must emit ``{}`` (never deny)."""
    skills = tmp_path / "skills"
    skills.mkdir()
    payload = {
        "hook_event_name": "AfterAgent",
        "stop_hook_active": True,
        "session_id": "g-sess-noncomply",
        "prompt_response": "I forgot to format my answer correctly.",
    }
    rc, out, err = _run(json.dumps(payload), _args(skills_dir=str(skills)))
    assert rc == 0
    assert out == ""  # no decision:deny — structural retry-chain bound at depth 1
    assert "no verdicts parsed" in err


def test_phase2_inconclusive_verdict_does_not_touch_skill(tmp_path):
    """INCONCLUSIVE means 'no signal' — counters and SKILL.md stay untouched."""
    skills = tmp_path / "skills"
    skills.mkdir()
    skill_md = _write_skill(skills, "webhook-signer")
    original = skill_md.read_text()

    prompt_response = (
        f"{EVAL_SENTINEL_START}\n"
        '{"evaluations":[{"skill":"webhook-signer","verdict":"INCONCLUSIVE","reason":"no clear evidence"}]}\n'
        f"{EVAL_SENTINEL_END}"
    )
    payload = {
        "hook_event_name": "AfterAgent",
        "stop_hook_active": True,
        "session_id": "g-sess-inc",
        "prompt_response": prompt_response,
    }
    rc, out, err = _run(json.dumps(payload), _args(skills_dir=str(skills)))
    assert rc == 0
    assert out == ""
    assert skill_md.read_text() == original
    meta = read_meta(skill_md)
    assert meta.helpful_count == 0
    assert meta.harmful_count == 0
    assert "skipped 1 INCONCLUSIVE" in err


# --- Loop-guard -------------------------------------------------------------


def test_phase2_marker_blocks_subsequent_after_agent_fires(tmp_path):
    """Once Phase 2 has run for a session, any later AfterAgent fire for
    the SAME session emits ``{}`` regardless of ``stop_hook_active`` —
    even if the next fire happens to look like a Phase-1 transcript with
    fresh <skill-used/> tags. This is the Gemini loop-guard."""
    skills = tmp_path / "skills"
    skills.mkdir()
    _write_skill(skills, "webhook-signer")

    # First fire: Phase 2 with empty verdict body — marker is dropped.
    payload_phase2 = {
        "hook_event_name": "AfterAgent",
        "stop_hook_active": True,
        "session_id": "g-loop",
        "prompt_response": "no verdicts here",
    }
    rc, out, _ = _run(json.dumps(payload_phase2), _args(skills_dir=str(skills)))
    assert rc == 0
    assert out == ""

    # Second fire on SAME session: simulate Gemini somehow re-firing
    # AfterAgent with stop_hook_active=False, with a transcript that has
    # a <skill-used/> tag. Without the loop-guard we'd emit a fresh deny.
    transcript = tmp_path / "rollout.jsonl"
    _write_transcript(
        transcript,
        [_assistant_msg('<skill-used name="webhook-signer"/>')],
    )
    payload_refire = {
        "hook_event_name": "AfterAgent",
        "stop_hook_active": False,
        "session_id": "g-loop",  # same session id!
        "transcript_path": str(transcript),
    }
    rc, out, _ = _run(json.dumps(payload_refire), _args(skills_dir=str(skills)))
    assert rc == 0
    assert out == ""  # marker present → emit {} regardless


def test_phase2_marker_does_not_block_different_session(tmp_path):
    """The loop-guard is per-session — a different session id still routes."""
    skills = tmp_path / "skills"
    skills.mkdir()
    _write_skill(skills, "webhook-signer")
    transcript = tmp_path / "rollout.jsonl"
    _write_transcript(
        transcript,
        [
            _exec_command(skills, "webhook-signer"),
            _assistant_msg('<skill-used name="webhook-signer"/>'),
        ],
    )

    # Drop marker for session A.
    _run(
        json.dumps(
            {
                "hook_event_name": "AfterAgent",
                "stop_hook_active": True,
                "session_id": "g-sess-A",
                "prompt_response": "no verdicts",
            }
        ),
        _args(skills_dir=str(skills)),
    )

    # Session B Phase 1: should NOT be blocked by session A's marker.
    payload = {
        "hook_event_name": "AfterAgent",
        "stop_hook_active": False,
        "session_id": "g-sess-B",
        "transcript_path": str(transcript),
    }
    rc, out, _ = _run(json.dumps(payload), _args(skills_dir=str(skills)))
    assert rc == 0
    data = json.loads(out)
    assert data["decision"] == "deny"


# --- Guards / passthroughs --------------------------------------------------


def test_wrong_event_name_passes_through(tmp_path):
    """SessionStart, BeforeTool, etc. must pass through silently."""
    skills = tmp_path / "skills"
    skills.mkdir()
    payload = {"hook_event_name": "SessionStart", "stop_hook_active": False}
    rc, out, _ = _run(json.dumps(payload), _args(skills_dir=str(skills)))
    assert rc == 0
    assert out == ""


def test_malformed_input_json_does_not_crash(tmp_path):
    skills = tmp_path / "skills"
    skills.mkdir()
    rc, out, err = _run("not json}", _args(skills_dir=str(skills)))
    assert rc == 0
    assert out == ""
    assert "invalid input JSON" in err


def test_missing_skills_dir_is_noop(tmp_path):
    payload = {
        "hook_event_name": "AfterAgent",
        "stop_hook_active": False,
        "transcript_path": str(tmp_path / "anything.jsonl"),
        "session_id": "x",
    }
    args = _args(skills_dir=str(tmp_path / "nope"))
    rc, out, _ = _run(json.dumps(payload), args)
    assert rc == 0
    assert out == ""
