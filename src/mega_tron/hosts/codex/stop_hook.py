"""Codex `Stop` hook — single-phase silent verdict capture.

Why single-phase now (was 2-phase):
  Codex surfaces the ``reason`` field of ``{"decision":"block",
  "reason":"..."}`` as a ``HookOutputEntry`` of kind ``Feedback`` in
  the user's terminal — there is no hidden / system-only channel for
  Stop hooks (verified in codex-rs/hooks/src/events/stop.rs). The old
  2-phase flow asked codex to spend a turn answering an evaluation
  prompt, which flashed the entire grading rubric on screen at the
  end of every session.

  We now ask the model to inline its verdict in the same
  `<skill-used name="..." verdict="..." reason="..."/>` tag it already
  emits in its final reply. The Stop hook tails the transcript (or
  uses ``last_assistant_message`` when codex provides it), parses
  those tags, persists verdicts, and emits empty stdout. Nothing
  user-visible.

Wire schema (codex-rs/hooks/src/schema/StopCommandInput):

    {
      "session_id": "...",
      "turn_id": "...",
      "transcript_path": "..." | null,
      "cwd": "...",
      "hook_event_name": "Stop",
      "model": "...",
      "permission_mode": "...",
      "stop_hook_active": true | false,
      "last_assistant_message": "..." | null
    }

Output JSON:
- Always:    {}   (empty stdout — codex proceeds with stop; we never block)
- Failure:   {}   (silent fail-open; we never break codex's exit)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _default_skills_dir() -> Path:
    return Path.home() / ".codex" / "skills"


def _emit_empty() -> int:
    return 0


def _emit_block(reason: str) -> int:
    json.dump({"decision": "block", "reason": reason}, sys.stdout)
    return 0


def cmd_stop_hook(args: argparse.Namespace) -> int:
    """Stop-hook entry point. Reads stdin JSON, captures inline verdicts
    from the transcript, writes empty stdout. Never blocks — codex's
    ``decision:"block"`` reason surfaces in the user's terminal, which
    is the UX bug this refactor is fixing.
    """
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as e:
        print(f"[mega-tron stop] invalid input JSON: {e}", file=sys.stderr)
        return _emit_empty()

    event_name = data.get("hook_event_name") or data.get("hookEventName")
    if event_name and event_name != "Stop":
        return _emit_empty()

    # Belt-and-suspenders: never re-enter even if codex re-fires Stop.
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
    """Pull `<skill-used ... verdict=...>` tags out of the transcript /
    last_assistant_message and persist them. Empty stdout in every
    code path."""
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
        print(f"[mega-tron stop] transcript scan failed: {e}", file=sys.stderr)
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
    #    inflate the counters. See the matching block in claude_code/
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
            f"[mega-tron stop] {len(skipped_claimed_only)} skill(s) tagged "
            f"without an operational trace "
            f"({', '.join(skipped_claimed_only[:3])}"
            f"{'...' if len(skipped_claimed_only) > 3 else ''}); "
            "discussion-only mentions are not treated as verdicts.",
            file=sys.stderr,
        )

    if not verdicts:
        if skipped_no_verdict:
            print(
                f"[mega-tron stop] {len(skipped_no_verdict)} skill(s) "
                f"self-reported without an inline verdict attribute "
                f"({', '.join(skipped_no_verdict[:3])}"
                f"{'...' if len(skipped_no_verdict) > 3 else ''}); "
                "no SKILL.md updates this turn.",
                file=sys.stderr,
            )
        return _emit_empty()

    # Dual-write through the verdict_writer helper: SKILL.md
    # frontmatter (legacy, canonical for cat-readability) AND the
    # SQLite ``verdicts`` time-series table (powers regression
    # analysis + the hermes cross-host hints sidecar). Best-effort
    # SQLite — frontmatter write always fires.
    from mega_tron.verdicts.writer import persist_verdicts

    session_id = data.get("session_id")
    outcome = persist_verdicts(
        skills_dir=skills_dir,
        verdicts=verdicts,
        host="codex",
        session_id=session_id if isinstance(session_id, str) else None,
        log_prefix="[mega-tron stop]",
    )
    for skill_name, err in outcome.errors:
        print(
            f"[mega-tron stop] failed to update {skill_name}: {err}",
            file=sys.stderr,
        )
    print(
        f"[mega-tron stop] updated {outcome.updated}/{len(verdicts)} "
        f"skill mega_meta blocks ({outcome.skipped_missing} missing, "
        f"{outcome.skipped_invalid} invalid; "
        f"{len(skipped_no_verdict)} tagged without verdict attr)",
        file=sys.stderr,
    )
    return _emit_empty()
