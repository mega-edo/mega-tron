"""Gemini CLI ``AfterAgent`` hook — single-phase silent verdict capture.

Mirrors :mod:`mega_tron.hosts.codex.stop_hook` and
:mod:`mega_tron.hosts.claude_code.stop_hook` adapted to Gemini CLI's
hook wire format. All three hosts now share the same flow: the model
emits inline ``<skill-used name="..." verdict="..." reason="..."/>``
tags in its final reply (per the inline self-eval contract planted by
``build_gemini_hook_context``), the hook tails the transcript via
:func:`mega_tron.tracker.scan_transcript`, parses those tags, persists
verdicts through :func:`mega_tron.verdicts.writer.persist_verdicts`,
and emits empty stdout.

Wire schema (Gemini CLI AfterAgent — see
https://geminicli.com/docs/hooks/reference/):

    {
      "session_id": "...",
      "transcript_path": "...",
      "cwd": "...",
      "hook_event_name": "AfterAgent",
      "prompt": "...",
      "prompt_response": "...",
      "stop_hook_active": true | false,
      "timestamp": "..."
    }

Output JSON:
- Always:    {}   (empty stdout — Gemini proceeds with stop; we never block)
- Failure:   {}   (silent fail-open; we never break Gemini's exit)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _default_skills_dir() -> Path:
    return Path.home() / ".gemini" / "skills"


def _emit_empty() -> int:
    return 0


def cmd_gemini_stop_hook(args: argparse.Namespace) -> int:
    """AfterAgent entry point. Reads stdin JSON, captures inline
    verdicts from the transcript, writes empty stdout."""
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as e:
        print(f"[mega-tron gemini-stop] invalid input JSON: {e}", file=sys.stderr)
        return _emit_empty()

    event_name = data.get("hook_event_name") or data.get("hookEventName")
    if event_name and event_name != "AfterAgent":
        return _emit_empty()

    # Belt-and-suspenders: never re-enter even if Gemini re-fires AfterAgent.
    if bool(data.get("stop_hook_active")):
        return _emit_empty()

    skills_dir = Path(
        args.skills_dir
        or os.environ.get("MEGA_SKILLS_DIR")
        or _default_skills_dir()
    )
    if not skills_dir.exists():
        return _emit_empty()

    return _capture_inline_verdicts(data, skills_dir)


def _capture_inline_verdicts(data: dict, skills_dir: Path) -> int:
    """Pull `<skill-used ... verdict=...>` tags out of the transcript
    and persist them. Empty stdout in every code path."""
    transcript_path = data.get("transcript_path")
    if not transcript_path or not isinstance(transcript_path, str):
        return _emit_empty()
    path = Path(transcript_path)
    if not path.exists():
        return _emit_empty()

    from mega_tron.tracker import scan_transcript

    try:
        scan = scan_transcript(path, skills_dir)
    except Exception as e:  # noqa: BLE001
        print(
            f"[mega-tron gemini-stop] transcript scan failed: {e}",
            file=sys.stderr,
        )
        return _emit_empty()

    if not scan.invocations:
        return _emit_empty()

    # Admission gate: see mega_tron.hosts._verdict_gate. Gemini was
    # the worst-affected host — 15 routes / 0 verdicts ever — because
    # Gemini's transcript records assistant text but does NOT log tool
    # invocations as discrete events. The old `scripts/` echo gate had
    # no operational trace to find unless the model coincidentally
    # pasted the path into its reply. Routes-membership lookup fixes
    # this.
    from mega_tron.hosts._verdict_gate import filter_invocations

    session_id = data.get("session_id") or data.get("sessionId")
    session_id_str = session_id if isinstance(session_id, str) else None
    gate = filter_invocations(
        invocations=scan.invocations,
        session_id=session_id_str,
        host="gemini_cli",
    )
    verdicts = gate.admitted
    skipped_no_verdict = gate.skipped_no_verdict
    skipped_claimed_only = gate.skipped_not_in_catalog

    if skipped_claimed_only:
        print(
            f"[mega-tron gemini-stop] {len(skipped_claimed_only)} skill(s) tagged "
            f"but not in this session's routed catalog (via={gate.via}) "
            f"({', '.join(skipped_claimed_only[:3])}"
            f"{'...' if len(skipped_claimed_only) > 3 else ''}); "
            "likely hallucinated names — not treated as verdicts.",
            file=sys.stderr,
        )

    if not verdicts:
        if skipped_no_verdict:
            print(
                f"[mega-tron gemini-stop] {len(skipped_no_verdict)} skill(s) "
                f"self-reported without an inline verdict attribute "
                f"({', '.join(skipped_no_verdict[:3])}"
                f"{'...' if len(skipped_no_verdict) > 3 else ''}); "
                "no SKILL.md updates this turn.",
                file=sys.stderr,
            )
        return _emit_empty()

    from mega_tron.verdicts.writer import persist_verdicts

    outcome = persist_verdicts(
        skills_dir=skills_dir,
        verdicts=verdicts,
        host="gemini_cli",
        session_id=session_id_str,
        log_prefix="[mega-tron gemini-stop]",
    )
    for skill_name, err in outcome.errors:
        print(
            f"[mega-tron gemini-stop] failed to update {skill_name}: {err}",
            file=sys.stderr,
        )
    print(
        f"[mega-tron gemini-stop] updated {outcome.updated}/{len(verdicts)} "
        f"skill mega_meta blocks ({outcome.skipped_missing} missing, "
        f"{outcome.skipped_invalid} invalid; "
        f"{len(skipped_no_verdict)} tagged without verdict attr)",
        file=sys.stderr,
    )
    return _emit_empty()
