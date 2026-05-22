"""Claude Code `Stop` hook — single-phase silent verdict capture.

Mirrors :mod:`mega_tron.stop_hook` but consumes the Claude Code wire
format.

Why single-phase now (was 2-phase):
  Claude Code surfaces the ``reason`` field of ``{"decision":"block",
  "reason":"..."}`` directly to the user's chat UI — there is no
  hidden / system-only channel for Stop hooks (verified against
  https://code.claude.com/docs/en/hooks). The old 2-phase flow asked
  Claude to spend a full turn answering an evaluation prompt, which
  meant the prompt itself flashed on screen at the end of every
  session. Users (rightly) complained.

  We now ask the model to inline its verdict in the same
  `<skill-used name="..." verdict="..." reason="..."/>` tag it already
  emits in its final reply (see prepender's BeforeAgent / UserPromptSubmit
  contract). The Stop hook then tails the transcript, parses those
  tags, persists verdicts, and emits empty stdout. Nothing user-visible.

Wire schema (Claude Code Stop hook, per
https://code.claude.com/docs/en/hooks):

    {
      "session_id": "...",
      "transcript_path": "...",
      "cwd": "...",
      "hook_event_name": "Stop",
      "stop_hook_active": true | false
    }

Output JSON:
- Always:    {}   (Claude proceeds with stop; we never block)
- Failure:   {}   (silent fail-open; we never break Claude's exit)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _default_skills_dir() -> Path:
    """Claude Code hook entry: prefer ``~/.claude/skills`` since that's
    Claude's native root, but defer to the auto-discovery union when
    available."""
    from mega_tron.config import discover_skill_dirs

    dirs = discover_skill_dirs()
    if dirs:
        return dirs[0]
    return Path.home() / ".claude" / "skills"


def _emit_empty() -> int:
    return 0


def _emit_block(reason: str) -> int:
    json.dump({"decision": "block", "reason": reason}, sys.stdout)
    return 0


def cmd_claude_stop_hook(args: argparse.Namespace) -> int:
    """Read Claude Code Stop hook JSON, capture inline verdicts silently.

    Single-phase design: never emit ``{"decision":"block",...}`` because
    Claude surfaces that reason to the user. Instead, walk the transcript
    we were just handed, pull every ``<skill-used ... verdict="...">``
    tag the model already emitted in its final reply, and persist the
    verdicts directly. Stdout stays empty so Claude stops cleanly with
    no UI surface.
    """
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as e:
        print(
            f"[mega-tron claude-stop-hook] invalid input JSON: {e}",
            file=sys.stderr,
        )
        return _emit_empty()

    event_name = data.get("hook_event_name") or data.get("hookEventName")
    if event_name and event_name != "Stop":
        return _emit_empty()

    # Guard against an infinite Stop-loop in case some future Claude
    # build re-fires Stop after our handler runs. We never block, so
    # this is belt-and-suspenders, but cheap.
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
    """Pull `<skill-used ... verdict=...>` tags out of the transcript and
    persist them. Returns empty stdout in every path so the user never
    sees a continuation prompt."""
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
            f"[mega-tron claude-stop-hook] transcript scan failed: {e}",
            file=sys.stderr,
        )
        return _emit_empty()

    if not scan.invocations:
        return _emit_empty()

    # Admission gate: see mega_tron.hosts._verdict_gate. The model's
    # tag is admitted when its skill name appears in this session's
    # routes-table catalog. The previous gate required a `scripts/`
    # path echo in the transcript — but most skills don't ship a
    # scripts/ directory, so it silently dropped almost every
    # legitimate verdict (Claude transcripts logged ~5k assistant
    # messages with 0 admitted verdicts in practice).
    from mega_tron.hosts._verdict_gate import filter_invocations

    session_id = data.get("session_id")
    session_id_str = session_id if isinstance(session_id, str) else None
    gate = filter_invocations(
        invocations=scan.invocations,
        session_id=session_id_str,
        host="claude_code",
    )
    verdicts = gate.admitted
    skipped_no_verdict = gate.skipped_no_verdict
    skipped_claimed_only = gate.skipped_not_in_catalog

    if skipped_claimed_only:
        print(
            f"[mega-tron claude-stop-hook] {len(skipped_claimed_only)} "
            f"skill(s) tagged but not in this session's routed catalog "
            f"(via={gate.via}) "
            f"({', '.join(skipped_claimed_only[:3])}"
            f"{'...' if len(skipped_claimed_only) > 3 else ''}); "
            "likely hallucinated names — not treated as verdicts.",
            file=sys.stderr,
        )

    if not verdicts:
        if skipped_no_verdict:
            print(
                f"[mega-tron claude-stop-hook] {len(skipped_no_verdict)} "
                f"skill(s) self-reported without an inline verdict "
                f"attribute ({', '.join(skipped_no_verdict[:3])}"
                f"{'...' if len(skipped_no_verdict) > 3 else ''}); "
                "no SKILL.md updates this turn.",
                file=sys.stderr,
            )
        return _emit_empty()

    from mega_tron.verdicts.writer import persist_verdicts

    outcome = persist_verdicts(
        skills_dir=skills_dir,
        verdicts=verdicts,
        host="claude_code",
        session_id=session_id_str,
        log_prefix="[mega-tron claude-stop-hook]",
    )
    for skill_name, err in outcome.errors:
        print(
            f"[mega-tron claude-stop-hook] failed to update {skill_name}: {err}",
            file=sys.stderr,
        )
    print(
        f"[mega-tron claude-stop-hook] updated {outcome.updated}/"
        f"{len(verdicts)} skill mega_meta blocks "
        f"({outcome.skipped_missing} missing, "
        f"{outcome.skipped_invalid} invalid; "
        f"{len(skipped_no_verdict)} tagged without verdict attr)",
        file=sys.stderr,
    )
    return _emit_empty()
