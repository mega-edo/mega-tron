"""Codex Stop hook — single-phase silent verdict capture.

The Codex Stop hook no longer emits ``{"decision":"block", ...}``
because codex surfaces that ``reason`` field as a ``HookOutputEntry``
of kind ``Feedback`` in the user's terminal (verified in
codex-rs/hooks/src/events/stop.rs). The hook scans the transcript for
inline ``<skill-used name=... verdict=... reason=.../>`` tags the
model placed in its final reply, persists those verdicts, and emits
empty stdout.

Covers:
- Passthrough: empty / malformed / wrong-event input never blocks
- Inline-tag capture: ``verdict="HELPFUL"`` → SKILL.md mega_meta
  updated, stdout empty
- Verdict-less tag: ``<skill-used name="x"/>`` (no verdict attr) is
  treated as 'no signal' — no SKILL.md write
- Mixed verdicts in one final reply: each lands in its own SKILL.md
- Malformed transcript: no crash, no write
- ``stop_hook_active=true`` re-entry: short-circuit so we never
  double-count
"""
from __future__ import annotations

import argparse
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from mega_tron.verdicts.mega_meta import read_meta
from mega_tron.hosts.codex.stop_hook import cmd_stop_hook


def _args(**overrides) -> argparse.Namespace:
    defaults = dict(skills_dir=None)
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _run(stdin_str: str, args: argparse.Namespace) -> tuple[int, str, str]:
    stdin = io.StringIO(stdin_str)
    stdout = io.StringIO()
    stderr = io.StringIO()
    with patch("sys.stdin", stdin), redirect_stdout(stdout), redirect_stderr(stderr):
        rc = cmd_stop_hook(args)
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
    """Render one codex transcript event for an `exec_command` call that
    invokes a `<skills_dir>/<skill_name>/scripts/...` script.

    Pairs with :func:`_assistant_msg` so the resulting transcript carries
    *both* an inline ``<skill-used .../>`` tag and a real operational
    trace — required for the stop-hook's ``claimed_use`` admission rule.
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


# --- Passthrough -----------------------------------------------------------


def test_wrong_event_name_passes_through(tmp_path):
    skills = tmp_path / "skills"
    skills.mkdir()
    payload = {"hook_event_name": "PreToolUse", "stop_hook_active": False}
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


def test_no_invocations_emits_empty_stdout(tmp_path):
    """No `<skill-used>` tag in the transcript → empty stdout. codex
    stops without surfacing anything to the user."""
    skills = tmp_path / "skills"
    skills.mkdir()
    transcript = tmp_path / "rollout.jsonl"
    _write_transcript(transcript, [_assistant_msg("Just did stuff. No tags.")])
    payload = {
        "hook_event_name": "Stop",
        "stop_hook_active": False,
        "transcript_path": str(transcript),
    }
    rc, out, _ = _run(json.dumps(payload), _args(skills_dir=str(skills)))
    assert rc == 0
    assert out == ""


# --- Single-phase silent capture -------------------------------------------


def test_inline_verdict_tag_updates_skill_md_silently(tmp_path):
    """Canonical happy path: the model emits a single inline tag
    carrying name + verdict + reason. The hook persists the verdict and
    emits empty stdout — no user-visible ``decision:block`` envelope."""
    skills = tmp_path / "skills"
    skills.mkdir()
    skill_md = _write_skill(skills, "webhook-signer")
    transcript = tmp_path / "rollout.jsonl"
    _write_transcript(
        transcript,
        [
            # Operational trace required by the ``claimed_use`` rule.
            _exec_command(skills, "webhook-signer"),
            _assistant_msg(
                'Done. <skill-used name="webhook-signer" verdict="HELPFUL" '
                'reason="Verified HMAC-SHA256 header via webhook-signer/'
                'scripts/verify.py; tests/webhooks.py::test_constant_time'
                '_compare now passes."/>'
            ),
        ],
    )
    payload = {
        "hook_event_name": "Stop",
        "stop_hook_active": False,
        "transcript_path": str(transcript),
        "session_id": "silent-1",
    }
    rc, out, err = _run(json.dumps(payload), _args(skills_dir=str(skills)))
    assert rc == 0
    assert out == ""  # critical: nothing leaks to the user
    assert "updated 1/1" in err

    meta = read_meta(skill_md)
    assert meta.helpful_count == 1
    assert meta.last_session_id == "silent-1"
    assert any("HMAC-SHA256" in c for c in meta.helpful_contexts)


def test_tag_without_verdict_attribute_is_skipped(tmp_path):
    """A `<skill-used>` tag with no `verdict=` attribute carries no
    signal — same as omitting the tag. No SKILL.md write."""
    skills = tmp_path / "skills"
    skills.mkdir()
    skill_md = _write_skill(skills, "webhook-signer")
    transcript = tmp_path / "rollout.jsonl"
    _write_transcript(
        transcript,
        [
            _assistant_msg(
                'Done. <skill-used name="webhook-signer" reason="ran it"/>'
            )
        ],
    )
    payload = {
        "hook_event_name": "Stop",
        "stop_hook_active": False,
        "transcript_path": str(transcript),
    }
    rc, out, err = _run(json.dumps(payload), _args(skills_dir=str(skills)))
    assert rc == 0
    assert out == ""
    assert "without an inline verdict attribute" in err
    meta = read_meta(skill_md)
    assert meta.helpful_count == 0
    assert meta.harmful_count == 0


def test_mixed_helpful_and_harmful_both_apply(tmp_path):
    """Two skills tagged in one final reply — each lands in its own
    SKILL.md mega_meta block."""
    skills = tmp_path / "skills"
    skills.mkdir()
    skill_a = _write_skill(skills, "webhook-signer")
    skill_b = _write_skill(skills, "jwt-verifier")
    transcript = tmp_path / "rollout.jsonl"
    _write_transcript(
        transcript,
        [
            _exec_command(skills, "webhook-signer"),
            _exec_command(skills, "jwt-verifier"),
            _assistant_msg(
                '<skill-used name="webhook-signer" verdict="HELPFUL" '
                'reason="HMAC matched; tests/webhooks.py passes."/> '
                '<skill-used name="jwt-verifier" verdict="HARMFUL" '
                'reason="Skipped the aud claim — src/auth/middleware.py '
                'accepted a token minted for another service."/>'
            ),
        ],
    )
    payload = {
        "hook_event_name": "Stop",
        "stop_hook_active": False,
        "transcript_path": str(transcript),
    }
    rc, out, err = _run(json.dumps(payload), _args(skills_dir=str(skills)))
    assert rc == 0
    assert out == ""
    assert "updated 2/2" in err
    assert read_meta(skill_a).helpful_count == 1
    assert read_meta(skill_b).harmful_count == 1


def test_legacy_inconclusive_verdict_no_ops(tmp_path):
    """Back-compat: if a model still emits ``verdict="INCONCLUSIVE"``
    (e.g. an older fork repurposing the tag), persist_verdicts treats
    it as a no-op — the counter stays at zero."""
    skills = tmp_path / "skills"
    skills.mkdir()
    skill_md = _write_skill(skills, "webhook-signer")
    transcript = tmp_path / "rollout.jsonl"
    _write_transcript(
        transcript,
        [
            _assistant_msg(
                '<skill-used name="webhook-signer" verdict="INCONCLUSIVE" '
                'reason="no clear evidence"/>'
            )
        ],
    )
    payload = {
        "hook_event_name": "Stop",
        "stop_hook_active": False,
        "transcript_path": str(transcript),
    }
    rc, out, _ = _run(json.dumps(payload), _args(skills_dir=str(skills)))
    assert rc == 0
    assert out == ""
    meta = read_meta(skill_md)
    assert meta.helpful_count == 0
    assert meta.harmful_count == 0


def test_unknown_skill_name_skipped(tmp_path):
    """A verdict naming a skill that doesn't exist in skills_dir is
    counted as skipped_missing — no crash, no false write.

    Note: we attach an ``exec_command`` invocation for the ghost
    skill so the tag passes the ``claimed_use`` admission rule. The
    ghost skill's *directory* doesn't exist on disk, so once the
    verdict reaches ``apply_evaluations`` it's classified as
    skipped_missing rather than written.
    """
    skills = tmp_path / "skills"
    skills.mkdir()
    _write_skill(skills, "real-skill")
    transcript = tmp_path / "rollout.jsonl"
    _write_transcript(
        transcript,
        [
            _exec_command(skills, "ghost-skill"),
            _assistant_msg(
                '<skill-used name="ghost-skill" verdict="HELPFUL" '
                'reason="claimed it ran but the skill dir does not exist"/>'
            ),
        ],
    )
    payload = {
        "hook_event_name": "Stop",
        "stop_hook_active": False,
        "transcript_path": str(transcript),
    }
    rc, out, err = _run(json.dumps(payload), _args(skills_dir=str(skills)))
    assert rc == 0
    assert out == ""
    assert "updated 0/1" in err


def test_stop_hook_active_is_belt_and_suspenders_noop(tmp_path):
    """If codex ever re-fires Stop with stop_hook_active=true (shouldn't
    happen since we never block, but cheap to guard), we short-circuit
    to avoid double-counting verdicts."""
    skills = tmp_path / "skills"
    skills.mkdir()
    skill_md = _write_skill(skills, "webhook-signer")
    transcript = tmp_path / "rollout.jsonl"
    _write_transcript(
        transcript,
        [
            _assistant_msg(
                '<skill-used name="webhook-signer" verdict="HELPFUL" '
                'reason="x"/>'
            )
        ],
    )
    payload = {
        "hook_event_name": "Stop",
        "stop_hook_active": True,
        "transcript_path": str(transcript),
    }
    rc, out, _ = _run(json.dumps(payload), _args(skills_dir=str(skills)))
    assert rc == 0
    assert out == ""
    # No write because stop_hook_active=true short-circuits.
    assert read_meta(skill_md).helpful_count == 0


def test_malformed_transcript_no_crash(tmp_path):
    skills = tmp_path / "skills"
    skills.mkdir()
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text("{ not valid jsonl\nneither is this\n")
    payload = {
        "hook_event_name": "Stop",
        "stop_hook_active": False,
        "transcript_path": str(transcript),
    }
    rc, out, _ = _run(json.dumps(payload), _args(skills_dir=str(skills)))
    assert rc == 0
    assert out == ""
