"""Claude Code Stop hook handler tests — single-phase silent capture.

The Stop hook never emits ``{"decision":"block", ...}`` because Claude
Code surfaces that reason in the user's chat UI (verified against
https://code.claude.com/docs/en/hooks — there is no Stop-event-only
hidden channel). Instead the hook scans the transcript for inline
`<skill-used name=... verdict=... reason=.../>` tags the model placed
in its final reply, persists those verdicts to SKILL.md / SQLite, and
emits empty stdout.

Covers:
- Passthrough: empty / malformed / wrong-event input never blocks
- Inline-tag capture: ``<skill-used verdict="..." />`` in the
  transcript → SKILL.md mega_meta block updated, stdout empty
- Verdict-less tags: ``<skill-used name="..."/>`` with no verdict
  attribute → no SKILL.md write, stderr explains why
- Mixed verdicts: HELPFUL + HARMFUL in one turn → both apply
- Malformed transcript → no crash, no write, stdout empty
"""
from __future__ import annotations

import argparse
import io
import json
import shutil
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest

from mega_tron.hosts.claude_code.stop_hook import cmd_claude_stop_hook


@pytest.fixture
def fixture_skills(tmp_path):
    """Copy the bundled fixture skills into an isolated tmp dir so the
    mega_meta block updates don't pollute the real fixtures."""
    src = Path(__file__).parent / "fixtures" / "skills"
    dst = tmp_path / "skills"
    shutil.copytree(src, dst)
    return dst


def _args(skills_dir: Path) -> argparse.Namespace:
    return argparse.Namespace(skills_dir=str(skills_dir))


def _run(payload: dict | str, args: argparse.Namespace) -> tuple[int, str, str]:
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    stdout = io.StringIO()
    stderr = io.StringIO()
    with patch("sys.stdin", io.StringIO(raw)):
        with redirect_stdout(stdout), redirect_stderr(stderr):
            rc = cmd_claude_stop_hook(args)
    return rc, stdout.getvalue(), stderr.getvalue()


def _claude_assistant_line(text: str) -> str:
    """Render one Claude Code transcript event for an assistant message."""
    return (
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": text}],
                },
            }
        )
        + "\n"
    )


def _claude_tool_use_line(skill_name: str, skills_root: Path) -> str:
    """Render one Claude Code transcript event for a Bash tool call that
    invokes a `<skills_root>/<skill_name>/scripts/...` script.

    Pairs with :func:`_claude_assistant_line` to build transcripts that
    pass the stop-hook's ``claimed_use`` rejection — i.e. transcripts
    that have both an inline ``<skill-used .../>`` tag AND a real
    operational trace of running that skill's scripts.
    """
    cmd = f"bash {skills_root}/{skill_name}/scripts/run.sh --check"
    return (
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": f"toolu_{skill_name}",
                            "name": "Bash",
                            "input": {"command": cmd},
                        }
                    ],
                },
            }
        )
        + "\n"
    )


def _write_transcript(path: Path, lines: list[str]) -> None:
    path.write_text("".join(lines))


# --- Passthrough -----------------------------------------------------------


def test_empty_stdin_exits_zero(fixture_skills):
    rc, out, _ = _run("", _args(fixture_skills))
    assert rc == 0
    assert out == ""


def test_malformed_json_exits_zero(fixture_skills):
    rc, out, err = _run("not json {{", _args(fixture_skills))
    assert rc == 0
    assert out == ""
    assert "invalid input JSON" in err


def test_wrong_event_is_noop(fixture_skills):
    rc, out, _ = _run(
        {"hook_event_name": "PreToolUse", "stop_hook_active": False},
        _args(fixture_skills),
    )
    assert rc == 0
    assert out == ""


def test_missing_transcript_is_noop(fixture_skills):
    rc, out, _ = _run(
        {
            "hook_event_name": "Stop",
            "stop_hook_active": False,
            "transcript_path": "/tmp/does-not-exist.jsonl",
        },
        _args(fixture_skills),
    )
    assert rc == 0
    assert out == ""


# --- Single-phase silent verdict capture -----------------------------------


def test_inline_verdict_tag_updates_skill_md_silently(tmp_path, fixture_skills):
    """The canonical happy path: the model places a single inline tag
    carrying name + verdict + reason in its final reply. The hook must
    persist the verdict and emit empty stdout (no user-visible
    {"decision":"block"} envelope)."""
    transcript = tmp_path / "session.jsonl"
    _write_transcript(
        transcript,
        [
            # Operational trace: a real Bash tool_use that actually ran
            # the skill's script. Required by the stop-hook's
            # ``claimed_use`` rejection rule — without an invocation
            # the inline tag is treated as discussion-only noise.
            _claude_tool_use_line("webhook-signer", fixture_skills),
            _claude_assistant_line(
                'Done. <skill-used name="webhook-signer" verdict="HELPFUL" '
                'reason="webhook-signer/scripts/verify.py confirmed the '
                'HMAC-SHA256 header and tests/webhooks.py::test_constant_'
                'time_compare now passes."/>'
            ),
        ],
    )
    rc, out, err = _run(
        {
            "hook_event_name": "Stop",
            "stop_hook_active": False,
            "transcript_path": str(transcript),
            "session_id": "silent-1",
        },
        _args(fixture_skills),
    )
    assert rc == 0
    assert out == ""  # critical: nothing leaks to the user
    assert "updated 1/1" in err
    skill_md = (fixture_skills / "webhook-signer" / "SKILL.md").read_text()
    assert "mega_meta" in skill_md
    assert "helpful_count" in skill_md


def test_tag_without_verdict_attribute_is_skipped(tmp_path, fixture_skills):
    """A `<skill-used>` tag with no `verdict=` attribute carries no
    signal — exactly the same as omitting the tag entirely. The hook
    logs that it saw the tag but writes nothing to SKILL.md."""
    transcript = tmp_path / "session.jsonl"
    _write_transcript(
        transcript,
        [
            _claude_assistant_line(
                'Done. <skill-used name="webhook-signer" reason="ran it"/>'
            )
        ],
    )
    rc, out, err = _run(
        {
            "hook_event_name": "Stop",
            "stop_hook_active": False,
            "transcript_path": str(transcript),
        },
        _args(fixture_skills),
    )
    assert rc == 0
    assert out == ""
    assert "without an inline verdict attribute" in err
    skill_md = (fixture_skills / "webhook-signer" / "SKILL.md").read_text()
    assert "helpful_count" not in skill_md


def test_no_invocations_emits_empty_stdout(tmp_path, fixture_skills):
    transcript = tmp_path / "session.jsonl"
    _write_transcript(
        transcript, [_claude_assistant_line("Just a regular reply, no tags.")]
    )
    rc, out, _ = _run(
        {
            "hook_event_name": "Stop",
            "stop_hook_active": False,
            "transcript_path": str(transcript),
        },
        _args(fixture_skills),
    )
    assert rc == 0
    assert out == ""


def test_discussion_only_tag_quote_is_not_treated_as_verdict(
    tmp_path, fixture_skills
):
    """A `<skill-used .../>` tag *quoted* in conversation — e.g. a
    debugging session, a documentation paragraph, or a status report
    that mentions the tag format — must NOT be persisted as a verdict.

    Regression test for a real bug where this conversation itself
    silently inflated webhook-signer's helpful_count by every turn that
    mentioned the tag in explanatory text. The fix: ``claimed_use``
    (tag present, no operational trace) is rejected at stop-hook
    admission time.
    """
    transcript = tmp_path / "session.jsonl"
    _write_transcript(
        transcript,
        [
            # Pure prose discussion that happens to include the tag
            # syntax for illustration. There is NO Read tool call, NO
            # Bash tool call, NO scripts/* invocation — only text.
            _claude_assistant_line(
                'In my previous answer I included the literal markup '
                '<skill-used name="webhook-signer" verdict="HELPFUL" '
                'reason="discussing the tag format itself"/> as part '
                'of an explanation, not as an actual skill grade.'
            )
        ],
    )
    rc, out, err = _run(
        {
            "hook_event_name": "Stop",
            "stop_hook_active": False,
            "transcript_path": str(transcript),
            "session_id": "discussion-only-test",
        },
        _args(fixture_skills),
    )
    assert rc == 0
    assert out == ""
    # Operator-visible log line confirms the gate fired. The legacy
    # fallback path is taken because this test's session_id was never
    # routed, and ``claimed_use`` invocations are still rejected there.
    assert "not in this session's routed catalog" in err
    assert "via=legacy" in err
    # And critically: the SKILL.md was NOT touched.
    skill_md = (fixture_skills / "webhook-signer" / "SKILL.md").read_text()
    assert "helpful_count" not in skill_md


def test_mixed_helpful_and_harmful_both_apply(tmp_path, fixture_skills):
    """HELPFUL + HARMFUL tags in the same final reply must both land in
    their respective SKILL.md mega_meta blocks."""
    transcript = tmp_path / "session.jsonl"
    _write_transcript(
        transcript,
        [
            # Operational traces for both skills — required so the
            # inline tags below pass the ``claimed_use`` rejection rule.
            _claude_tool_use_line("webhook-signer", fixture_skills),
            _claude_tool_use_line("jwt-verifier", fixture_skills),
            _claude_assistant_line(
                'Used the verifier. '
                '<skill-used name="webhook-signer" verdict="HELPFUL" '
                'reason="HMAC matched; tests/webhooks.py passes."/> '
                '<skill-used name="jwt-verifier" verdict="HARMFUL" '
                'reason="skipped the aud claim — src/auth/middleware.py '
                'accepted a token minted for another service."/>'
            ),
        ],
    )
    rc, out, err = _run(
        {
            "hook_event_name": "Stop",
            "stop_hook_active": False,
            "transcript_path": str(transcript),
        },
        _args(fixture_skills),
    )
    assert rc == 0
    assert out == ""
    assert "updated 2/2" in err
    webhook_md = (fixture_skills / "webhook-signer" / "SKILL.md").read_text()
    jwt_md = (fixture_skills / "jwt-verifier" / "SKILL.md").read_text()
    assert "helpful_count" in webhook_md
    assert "harmful_count" in jwt_md


def test_stop_hook_active_is_belt_and_suspenders_noop(
    tmp_path, fixture_skills
):
    """If Claude ever re-fires Stop with stop_hook_active=true, we must
    not re-process the same transcript and double-count verdicts. The
    handler exits silently."""
    transcript = tmp_path / "session.jsonl"
    _write_transcript(
        transcript,
        [
            _claude_assistant_line(
                '<skill-used name="webhook-signer" verdict="HELPFUL" '
                'reason="x"/>'
            )
        ],
    )
    rc, out, _ = _run(
        {
            "hook_event_name": "Stop",
            "stop_hook_active": True,
            "transcript_path": str(transcript),
        },
        _args(fixture_skills),
    )
    assert rc == 0
    assert out == ""
    skill_md = (fixture_skills / "webhook-signer" / "SKILL.md").read_text()
    # No write because stop_hook_active=true short-circuits.
    assert "helpful_count" not in skill_md


def test_malformed_transcript_no_crash(tmp_path, fixture_skills):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("{ this is not valid jsonl\nneither is this\n")
    rc, out, _ = _run(
        {
            "hook_event_name": "Stop",
            "stop_hook_active": False,
            "transcript_path": str(transcript),
        },
        _args(fixture_skills),
    )
    assert rc == 0
    assert out == ""
