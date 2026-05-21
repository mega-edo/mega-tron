"""Transcript scanner — self-report tag + exec_command path matching."""
from __future__ import annotations

import json
from pathlib import Path

from mega_tron.tracker import scan_transcript


def _write_transcript(tmp_path: Path, events: list[dict]) -> Path:
    p = tmp_path / "rollout.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return p


def _msg(role: str, text: str) -> dict:
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": role,
            "content": [{"type": "output_text", "text": text}],
        },
    }


def _exec(cmd: str, workdir: str = "/tmp") -> dict:
    return {
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "name": "exec_command",
            "arguments": json.dumps({"cmd": cmd, "workdir": workdir}),
        },
    }


def test_scan_self_report_tag(tmp_path):
    skills = tmp_path / "skills"
    skills.mkdir()
    transcript = _write_transcript(
        tmp_path,
        [
            _msg("user", "do thing"),
            _msg(
                "assistant",
                'Done.\n<skill-used name="webhook-signer" reason="HMAC check"/>',
            ),
        ],
    )
    scan = scan_transcript(transcript, skills)
    assert "webhook-signer" in scan.invocations
    inv = scan.invocations["webhook-signer"]
    assert inv.self_reported
    assert not inv.script_invoked
    assert inv.reasons == ["HMAC check"]
    assert inv.label == "claimed_use"


def test_scan_script_invocation(tmp_path):
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "webhook-signer").mkdir()
    transcript = _write_transcript(
        tmp_path,
        [
            _exec(
                f"bash {skills}/webhook-signer/scripts/run.sh --target foo",
            ),
        ],
    )
    scan = scan_transcript(transcript, skills)
    assert "webhook-signer" in scan.invocations
    inv = scan.invocations["webhook-signer"]
    assert inv.script_invoked
    assert inv.exec_count == 1
    assert inv.label == "silent_use"


def test_scan_informed_use_combines_both(tmp_path):
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "jwt-verifier").mkdir()
    transcript = _write_transcript(
        tmp_path,
        [
            _exec(f"python3 {skills}/jwt-verifier/scripts/verify.py token.txt"),
            _msg(
                "assistant",
                'Done.\n<skill-used name="jwt-verifier" reason="verified RS256"/>',
            ),
        ],
    )
    scan = scan_transcript(transcript, skills)
    inv = scan.invocations["jwt-verifier"]
    assert inv.self_reported and inv.script_invoked
    assert inv.label == "informed_use"


def test_scan_multiple_assistant_turns_aggregates_reasons(tmp_path):
    skills = tmp_path / "skills"
    skills.mkdir()
    transcript = _write_transcript(
        tmp_path,
        [
            _msg("assistant", '<skill-used name="x" reason="first call"/>'),
            _msg("assistant", '<skill-used name="x" reason="second call"/>'),
        ],
    )
    scan = scan_transcript(transcript, skills)
    assert scan.invocations["x"].reasons == ["first call", "second call"]


def test_scan_ignores_non_skill_exec(tmp_path):
    skills = tmp_path / "skills"
    skills.mkdir()
    transcript = _write_transcript(
        tmp_path,
        [
            _exec("ls /tmp"),
            _exec("git status"),
        ],
    )
    scan = scan_transcript(transcript, skills)
    assert scan.invocations == {}
    assert scan.n_exec_commands == 2


def test_scan_ignores_user_messages_for_tag(tmp_path):
    """A <skill-used> in a user message is the user typing, not the model."""
    skills = tmp_path / "skills"
    skills.mkdir()
    transcript = _write_transcript(
        tmp_path,
        [
            _msg("user", '<skill-used name="X" reason="user typed this"/>'),
        ],
    )
    scan = scan_transcript(transcript, skills)
    assert scan.invocations == {}


def test_scan_tolerates_malformed_json_lines(tmp_path):
    skills = tmp_path / "skills"
    skills.mkdir()
    p = tmp_path / "rollout.jsonl"
    good = json.dumps(_msg("assistant", '<skill-used name="x"/>'))
    p.write_text(f"{{not json\n{good}\nalso not json\n")
    scan = scan_transcript(p, skills)
    assert "x" in scan.invocations


def test_scan_self_report_underscore_variant(tmp_path):
    """C3: <skill_used> with underscore should be accepted."""
    skills = tmp_path / "skills"
    skills.mkdir()
    transcript = _write_transcript(
        tmp_path,
        [_msg("assistant", '<skill_used name="webhook-signer" reason="HMAC"/>')],
    )
    scan = scan_transcript(transcript, skills)
    assert "webhook-signer" in scan.invocations
    assert scan.invocations["webhook-signer"].reasons == ["HMAC"]


def test_scan_self_report_compact_variant(tmp_path):
    """C3: <skill name="X"/> without -used suffix should be accepted."""
    skills = tmp_path / "skills"
    skills.mkdir()
    transcript = _write_transcript(
        tmp_path,
        [_msg("assistant", 'Done. <skill name="git-amend-staged"/>')],
    )
    scan = scan_transcript(transcript, skills)
    assert "git-amend-staged" in scan.invocations
    # No reason provided in this variant.
    assert scan.invocations["git-amend-staged"].reasons == []


def test_scan_self_report_bracketed_variant(tmp_path):
    """C3: [skill: X — reason] markdown-style tag should be accepted."""
    skills = tmp_path / "skills"
    skills.mkdir()
    transcript = _write_transcript(
        tmp_path,
        [_msg("assistant", "I used [skill: jwt-verifier — RS256 token check].")],
    )
    scan = scan_transcript(transcript, skills)
    assert "jwt-verifier" in scan.invocations
    assert scan.invocations["jwt-verifier"].reasons == ["RS256 token check"]


def test_scan_self_report_bracketed_no_reason(tmp_path):
    """C3: [skill: X] without reason is still a valid invocation."""
    skills = tmp_path / "skills"
    skills.mkdir()
    transcript = _write_transcript(
        tmp_path,
        [_msg("assistant", "Used [skill: url-safe-parser] to clean the input.")],
    )
    scan = scan_transcript(transcript, skills)
    assert "url-safe-parser" in scan.invocations
    assert scan.invocations["url-safe-parser"].reasons == []
