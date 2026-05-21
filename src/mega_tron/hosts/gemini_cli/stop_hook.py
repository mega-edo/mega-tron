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

    # Build verdict records from inline tags, with two admission rules:
    #
    # 1. Skills tagged without a `verdict=` attribute are skipped
    #    (treated as no signal — no SKILL.md write, no SQLite row).
    #
    # 2. ``claimed_use`` invocations are rejected (tag emitted in text
    #    but no operational trace — no scripts/* run, no SKILL.md read).
    #    Otherwise documentation / status-report / debugging-session
    #    transcripts that quote the ``<skill-used .../>`` form silently
    #    inflate the counters. See the matching block in codex/
    #    stop_hook.py for the full rationale.
    verdicts: list[dict] = []
    skipped_no_verdict: list[str] = []
    skipped_claimed_only: list[str] = []
    for name, inv in scan.invocations.items():
        if not inv.verdicts:
            skipped_no_verdict.append(name)
            continue
        if inv.label == "claimed_use":
            skipped_claimed_only.append(name)
            continue
        verdict_label = inv.verdicts[-1]
        reason = inv.reasons[-1] if inv.reasons else ""
        verdicts.append({"skill": name, "verdict": verdict_label, "reason": reason})

    if skipped_claimed_only:
        print(
            f"[mega-tron gemini-stop] {len(skipped_claimed_only)} skill(s) tagged "
            f"without an operational trace "
            f"({', '.join(skipped_claimed_only[:3])}"
            f"{'...' if len(skipped_claimed_only) > 3 else ''}); "
            "discussion-only mentions are not treated as verdicts.",
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

    session_id = data.get("session_id") or data.get("sessionId")
    outcome = persist_verdicts(
        skills_dir=skills_dir,
        verdicts=verdicts,
        host="gemini_cli",
        session_id=session_id if isinstance(session_id, str) else None,
        log_prefix="[mega-tron gemini-stop]",
    )
    for skill_name, err in outcome.errors:
        print(
            f"[mega-tron gemini-stop] failed to update {skill_name}: {err}",
            file=sys.stderr,
        )
    print(
        f"[mega-tron gemini-stop] updated {outcome.updated}/{len(verdicts)} "
        f"skill mega_meta blocks (skipped {outcome.skipped_inconclusive} INCONCLUSIVE, "
        f"{outcome.skipped_missing} missing, {outcome.skipped_invalid} invalid; "
        f"{len(skipped_no_verdict)} tagged without verdict attr)",
        file=sys.stderr,
    )
    return _emit_empty()
