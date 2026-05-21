"""Gemini CLI ``AfterAgent`` hook — 2-phase in-turn evaluation.

Mirrors :mod:`mega_tron.hosts.codex.stop_hook` but adapted to Gemini
CLI's hook wire format and lifecycle.

Phase 1 (``stop_hook_active=false`` / absent):
  - Read ``transcript_path`` from stdin JSON
  - Scan for invoked skills via :func:`mega_tron.tracker.scan_transcript`
  - If any: emit ``{"decision":"deny","reason":"<eval prompt>"}`` →
    Gemini runs one more turn answering the eval prompt
  - If none: emit ``{}`` → Gemini exits the turn normally

Phase 2 (``stop_hook_active=true``):
  - Read the eval answer from ``data["prompt_response"]`` (Gemini puts
    the final assistant text directly on AfterAgent's stdin — no
    transcript tailing needed, unlike Claude Code)
  - Parse the sentinel-fenced verdict JSON
  - For each verdict: update SKILL.md's ``mega_meta`` block via
    :func:`mega_tron.verdicts.mega_meta.apply_evaluations`
  - Drop a per-session ``eval-gemini-<id>`` marker so subsequent
    AfterAgent invocations on the same session never re-enter Phase 1
  - Emit ``{}`` → Gemini exits cleanly (no further retry)

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

Phase-1 uses Gemini's ``decision: "deny"`` (with the eval prompt as
``reason``) — Codex uses ``"block"`` and Claude Code uses a different
mechanism, but Gemini's docs document ``"deny"`` as the retry trigger
for AfterAgent (the ``reason`` field is sent back to the model as a new
prompt).

Loop-guard: Phase 2 NEVER returns ``decision:"deny"``, so the retry
chain is structurally bounded to depth 1. As an extra defence against
buggy ``stop_hook_active`` semantics across Gemini versions, the first
Phase-2 entry for a session writes
``$XDG_RUNTIME_DIR/mega-tron/eval-gemini-<session_id>`` — any later
AfterAgent invocation for the same session sees that marker and emits
an empty ``{}`` regardless of ``stop_hook_active``.
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
    return Path.home() / ".gemini" / "skills"


def _emit_empty() -> int:
    return 0


def _emit_deny(reason: str) -> int:
    """Emit Gemini's AfterAgent "retry with this prompt" envelope.

    Gemini sends the ``reason`` string back to the model as a new prompt
    and re-enters AfterAgent with ``stop_hook_active=true`` after the
    model replies.
    """
    json.dump({"decision": "deny", "reason": reason}, sys.stdout)
    return 0


def _loop_guard_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or os.environ.get("TMPDIR") or "/tmp"
    return Path(base) / "mega-tron"


def _loop_guard_marker(session_id: str | None) -> Path | None:
    if not session_id or not isinstance(session_id, str):
        return None
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_") or "anon"
    return _loop_guard_dir() / f"eval-gemini-{safe}"


def _phase2_already_ran(session_id: str | None) -> bool:
    marker = _loop_guard_marker(session_id)
    return marker is not None and marker.exists()


def _mark_phase2_ran(session_id: str | None) -> None:
    marker = _loop_guard_marker(session_id)
    if marker is None:
        return
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
    except OSError:
        pass


def _build_eval_prompt(invoked: list[str]) -> str:
    """Build the continuation prompt asking Gemini to self-evaluate.

    Bullet form uses the bare skill name (Codex uses ``$<name>`` because
    its system prompt has a must-use-on-dollar-prefix rule; Gemini has
    no such trigger). Wraps the shared eval prompt with a Gemini-specific
    preamble that bans further ``activate_skill`` calls on this turn.
    """
    from mega_tron.hosts.eval_prompt import build_eval_prompt

    preamble = (
        "This turn is ending. Before it does, evaluate the skills you used "
        "in your work.\n\n"
        "This is an evaluation turn — do NOT invoke any further skills (no "
        "new `activate_skill` calls, no new `<skill-used/>` tags). Only "
        "emit the verdict JSON described below.\n\n"
    )
    shared = build_eval_prompt(
        invoked=invoked,
        trigger_token="",
        sentinel_start=EVAL_SENTINEL_START,
        sentinel_end=EVAL_SENTINEL_END,
    )
    # The shared builder opens with "Before this session ends, evaluate
    # the skills you used in your work." — drop that line so we don't
    # duplicate the Gemini preamble.
    shared_body = shared.split("\n", 2)[2] if shared.count("\n") >= 2 else shared
    return preamble + shared_body


def _parse_verdicts(prompt_response: str) -> list[dict]:
    """Pull verdicts out of Gemini's eval-turn response. Tolerant of stray prose."""
    obj = extract_json(
        prompt_response,
        start=EVAL_SENTINEL_START,
        end=EVAL_SENTINEL_END,
        fallback_key="evaluations",
    )
    if not obj:
        return []
    ev = obj.get("evaluations")
    return ev if isinstance(ev, list) else []


def cmd_gemini_stop_hook(args: argparse.Namespace) -> int:
    """AfterAgent entry point. Reads stdin JSON, writes stdout JSON."""
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as e:
        print(f"[mega-tron gemini-stop] invalid input JSON: {e}", file=sys.stderr)
        return _emit_empty()

    event_name = data.get("hook_event_name") or data.get("hookEventName")
    if event_name and event_name != "AfterAgent":
        return _emit_empty()

    skills_dir = Path(
        args.skills_dir
        or os.environ.get("MEGA_SKILLS_DIR")
        or _default_skills_dir()
    )
    if not skills_dir.exists():
        return _emit_empty()

    session_id = data.get("session_id") or data.get("sessionId")
    if _phase2_already_ran(session_id):
        # Defensive: if Gemini somehow fires AfterAgent again on a session
        # whose eval turn we've already consumed, bail out — Phase 2 is
        # idempotent but we don't want to scan the transcript again and
        # accidentally re-enter Phase 1 against the eval-turn assistant
        # text (which may legitimately contain `<skill-used/>` tags that
        # already had their verdicts applied).
        return _emit_empty()

    stop_hook_active = bool(data.get("stop_hook_active"))

    if stop_hook_active:
        return _phase2_persist(data, skills_dir, session_id)
    return _phase1_ask_eval(data, skills_dir)


def _phase1_ask_eval(data: dict, skills_dir: Path) -> int:
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

    # Drop ``claimed_use`` — skills that appeared in the assistant's
    # text via a `<skill-used .../>` tag but left no operational trace
    # (no scripts/* run, no SKILL.md read). Otherwise documentation,
    # status-report, or debugging-session transcripts that *quote*
    # the tag format would surface as bogus eval candidates and the
    # Phase-2 verdict prompt would burn a turn on noise.
    names = sorted(
        name for name, inv in scan.invocations.items()
        if inv.label != "claimed_use"
    )
    if not names:
        return _emit_empty()
    return _emit_deny(_build_eval_prompt(names))


def _phase2_persist(data: dict, skills_dir: Path, session_id: str | None) -> int:
    # Gemini's AfterAgent puts the final model text directly on stdin
    # as `prompt_response` — no transcript tailing required.
    prompt_response = data.get("prompt_response") or ""
    if not isinstance(prompt_response, str):
        return _emit_empty()

    verdicts = _parse_verdicts(prompt_response)
    # Mark Phase-2 as having run for this session before doing any work,
    # so even a parse-failure won't let a future AfterAgent re-enter
    # Phase 1 against the eval-turn output.
    _mark_phase2_ran(session_id)

    if not verdicts:
        print(
            "[mega-tron gemini-stop] no verdicts parsed from prompt_response; "
            "proceeding without update",
            file=sys.stderr,
        )
        return _emit_empty()

    from mega_tron.verdicts.writer import persist_verdicts

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
        f"{outcome.skipped_missing} missing, {outcome.skipped_invalid} invalid)",
        file=sys.stderr,
    )
    return _emit_empty()


__all__ = ["cmd_gemini_stop_hook"]
