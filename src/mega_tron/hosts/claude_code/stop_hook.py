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

from mega_tron._sentinel import extract_json, make_sentinels

EVAL_SENTINEL_START, EVAL_SENTINEL_END = make_sentinels("VERDICT")


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


def _build_eval_prompt(invoked: list[str]) -> str:
    """Build the continuation prompt asking Claude to self-evaluate.

    Delegates to the shared host-agnostic builder; the only host
    quirk is Claude's ``/skill-name`` slash-command trigger token.
    """
    from mega_tron.hosts.eval_prompt import build_eval_prompt

    return build_eval_prompt(
        invoked=invoked,
        trigger_token="/",
        sentinel_start=EVAL_SENTINEL_START,
        sentinel_end=EVAL_SENTINEL_END,
    )


def _parse_verdicts(last_message: str) -> list[dict]:
    """Pull verdicts out of Claude's last message. Tolerant of stray prose."""
    obj = extract_json(
        last_message,
        start=EVAL_SENTINEL_START,
        end=EVAL_SENTINEL_END,
        fallback_key="evaluations",
    )
    if not obj:
        return []
    ev = obj.get("evaluations")
    return ev if isinstance(ev, list) else []


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

    # Build verdict records from the inline tags, with two admission rules:
    #
    # 1. Skills tagged without a `verdict=` attribute are skipped
    #    (treated as INCONCLUSIVE by omission — no signal to the
    #    rank-blend, no SKILL.md write).
    #
    # 2. ``claimed_use`` invocations are rejected. ``claimed_use`` means
    #    the model emitted a `<skill-used .../>` tag in text but left no
    #    operational trace — no script run, no SKILL.md read. In
    #    practice this matches three cases:
    #      (a) Genuine "I'd recommend skill X" answers without using
    #          the skill — evidence value is low either way.
    #      (b) Conversation that quotes the tag format itself (docs,
    #          status reports, this kind of debugging session) — pure
    #          noise; counting it would silently inflate counters.
    #      (c) Stale tags carried over from earlier turns in the same
    #          long-lived transcript.
    #    Admitting only ``informed_use`` (tag + invocation) and
    #    ``silent_use`` (invocation but no tag, no verdict to extract
    #    anyway) keeps the store honest. The cost — losing the (a)
    #    cases — is acceptable because the rank-blend already prefers
    #    skills with proven *usage* history over recommendation-only
    #    mentions.
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
        # Use the model's most recent verdict + matching reason. The tag
        # parser appends in document order, so the last entry is the
        # final-reply verdict.
        verdict_label = inv.verdicts[-1]
        reason = inv.reasons[-1] if inv.reasons else ""
        verdicts.append({"skill": name, "verdict": verdict_label, "reason": reason})

    if skipped_claimed_only:
        print(
            f"[mega-tron claude-stop-hook] {len(skipped_claimed_only)} "
            f"skill(s) tagged without an operational trace "
            f"({', '.join(skipped_claimed_only[:3])}"
            f"{'...' if len(skipped_claimed_only) > 3 else ''}); "
            "discussion-only mentions are not treated as verdicts.",
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

    session_id = data.get("session_id")
    outcome = persist_verdicts(
        skills_dir=skills_dir,
        verdicts=verdicts,
        host="claude_code",
        session_id=session_id if isinstance(session_id, str) else None,
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
        f"(skipped {outcome.skipped_inconclusive} INCONCLUSIVE, "
        f"{outcome.skipped_missing} missing, "
        f"{outcome.skipped_invalid} invalid; "
        f"{len(skipped_no_verdict)} tagged without verdict attr)",
        file=sys.stderr,
    )
    return _emit_empty()
