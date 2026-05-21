"""Deterministic skill-invocation detection from a Codex transcript.

Codex writes its rollout/transcript as JSONL — one event per line. We extract
two signals (LLM 0 calls):

1. **Self-report tag** — assistant messages containing
   `<skill-used name="X" reason="..."/>`. The user-prompt-submit hook tells
   codex to emit this tag whenever it invokes a routed skill, so this is the
   high-fidelity ground truth.

2. **Script invocation** — `exec_command` function calls whose `cmd` references
   a path under `<skills_root>/<name>/scripts/`. The exact filesystem ground
   truth — undeniable evidence the skill ran.

Invocations from either signal contribute to the per-skill counter. A skill
that shows up in both is "informed_use" (rare and clean); script-only is
"silent_use" (executed but not reported); tag-only is "claimed_use"
(reported but no scripts/ — fine for prompt-only skills).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path


# Canonical self-report tag now also carries an optional verdict
# attribute, e.g. `<skill-used name="X" reason="..." verdict="HELPFUL"/>`.
# Order of attributes is tolerated (verdict can come before reason).
# This single-line inline form replaces the previous two-phase Stop-hook
# protocol where mega-tron asked Claude/Codex for a separate verdict
# response on the next turn — that next turn surfaced the eval prompt
# to the user. The new contract lets the model emit name+reason+verdict
# in one tag inside the final reply, so the Stop hook can pull the
# verdict straight out of the transcript without forcing a continuation
# turn (and without exposing the contract to the user via {"decision":
# "block","reason":...} on Claude/Codex).
SELF_REPORT_RE = re.compile(
    r"""
    (?:
      # Canonical: <skill-used name="X" reason="..." verdict="..."/>
      # (also accepts skill_used and any attribute ordering)
      <skill[-_]used\b
        (?P<attrs1>(?:\s+[a-z_]+=["'][^"']*["'])+)
        \s*/?>
    |
      # Compact: <skill name="X"/>
      <skill\s+name=["'](?P<n2>[^"']+)["']\s*/?>
    |
      # Bracketed: [skill: X] or [skill: X — reason]
      \[skill:\s*(?P<n3>[A-Za-z0-9_\-.]+)
        \s*(?:[-—:]\s*(?P<r3>[^\]]+))?\]
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Per-attribute extractor for the canonical form's attribute soup. We
# parse attrs1 with a separate, simpler regex so the addition of a new
# attribute (e.g. `verdict`) doesn't require yet another optional clause
# in the main pattern.
_ATTR_RE = re.compile(r"""([a-z_]+)\s*=\s*["']([^"']*)["']""", re.IGNORECASE)


def _parse_attrs(blob: str) -> dict[str, str]:
    return {k.lower(): v for k, v in _ATTR_RE.findall(blob or "")}


@dataclass
class SkillInvocation:
    name: str
    self_reported: bool = False  # found via <skill-used> tag
    script_invoked: bool = False  # exec_command path matched skills_root/<name>/scripts/
    reasons: list[str] = field(default_factory=list)  # per-call self-reported reasons
    verdicts: list[str] = field(default_factory=list)  # inline `verdict="..."` values
    exec_count: int = 0  # how many scripts/* commands ran

    @property
    def label(self) -> str:
        if self.self_reported and self.script_invoked:
            return "informed_use"
        if self.script_invoked:
            return "silent_use"
        if self.self_reported:
            return "claimed_use"
        return "unknown"


@dataclass
class TranscriptScan:
    invocations: dict[str, SkillInvocation] = field(default_factory=dict)
    n_assistant_messages: int = 0
    n_exec_commands: int = 0

    def add_self_report(
        self,
        name: str,
        reason: str | None,
        verdict: str | None = None,
    ) -> None:
        inv = self.invocations.setdefault(name, SkillInvocation(name=name))
        inv.self_reported = True
        if reason:
            inv.reasons.append(reason.strip())
        if verdict:
            inv.verdicts.append(verdict.strip().upper())

    def add_script_invocation(self, name: str) -> None:
        inv = self.invocations.setdefault(name, SkillInvocation(name=name))
        inv.script_invoked = True
        inv.exec_count += 1


def scan_transcript(transcript_path: Path, skills_root: Path) -> TranscriptScan:
    """Walk a codex JSONL rollout and extract invocations.

    Args:
        transcript_path: codex session JSONL path (from the Stop hook input).
        skills_root: the directory containing skill subfolders (matches paths
            like `<skills_root>/<name>/scripts/<script>`).

    Returns:
        A TranscriptScan with per-skill invocations and bulk counts.
    """
    result = TranscriptScan()
    try:
        skills_root_str = str(Path(skills_root).resolve())
    except OSError:
        skills_root_str = str(skills_root)

    try:
        fh = transcript_path.open("r", encoding="utf-8", errors="replace")
    except (FileNotFoundError, OSError):
        return result

    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            _process_event(event, skills_root_str, result)
    return result


def _process_event(event: dict, skills_root_str: str, result: TranscriptScan) -> None:
    # Codex shape: {"payload": {"type": "message" | "function_call", ...}}
    payload = event.get("payload")
    if isinstance(payload, dict):
        payload_type = payload.get("type")
        if payload_type == "message":
            _process_message(payload, result)
            return
        if payload_type == "function_call":
            _process_function_call(payload, skills_root_str, result)
            return

    # Claude Code shape: {"type": "assistant", "message": {"role":"assistant",
    # "content": [{"type":"text","text":"..."}, {"type":"tool_use", ...}]}}
    event_type = event.get("type")
    if event_type == "assistant":
        msg = event.get("message")
        if isinstance(msg, dict):
            _process_claude_assistant_message(msg, skills_root_str, result)


def _process_claude_assistant_message(
    msg: dict, skills_root_str: str, result: TranscriptScan
) -> None:
    """Handle a Claude Code transcript ``assistant`` event's ``message`` block.

    Extracts:
      - <skill-used name="..."/> tags from any text content block
      - Bash tool_use invocations whose command path matches the skills root
    """
    if msg.get("role") != "assistant":
        return
    content = msg.get("content")
    if not isinstance(content, list):
        return

    text_parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text":
            t = item.get("text")
            if isinstance(t, str):
                text_parts.append(t)
        elif kind == "tool_use":
            tool_name = item.get("name")
            inp = item.get("input") or {}
            if tool_name == "Bash" and isinstance(inp, dict):
                cmd = inp.get("command") or ""
                if isinstance(cmd, str):
                    result.n_exec_commands += 1
                    matched = _match_skill_path([cmd], skills_root_str)
                    if matched:
                        result.add_script_invocation(matched)

    if not text_parts:
        return
    result.n_assistant_messages += 1
    _scan_text_for_self_reports("\n".join(text_parts), result)


def _scan_text_for_self_reports(full: str, result: TranscriptScan) -> None:
    """Pull `<skill-used>` (and bracketed) self-reports out of one text blob.

    Handles both the canonical attribute-soup form (any attribute order,
    optional ``verdict=...``) and the legacy compact / bracketed shapes.
    """
    for m in SELF_REPORT_RE.finditer(full):
        attrs_blob = m.group("attrs1") or ""
        if attrs_blob:
            attrs = _parse_attrs(attrs_blob)
            name = (attrs.get("name") or "").strip()
            reason = (attrs.get("reason") or "").strip() or None
            verdict = (attrs.get("verdict") or "").strip() or None
        else:
            name = (m.group("n2") or m.group("n3") or "").strip()
            reason_raw = m.group("r3")
            reason = reason_raw.strip() if isinstance(reason_raw, str) else None
            if reason == "":
                reason = None
            verdict = None
        if not name:
            continue
        result.add_self_report(name, reason, verdict)


def _process_message(payload: dict, result: TranscriptScan) -> None:
    if payload.get("role") != "assistant":
        return
    content = payload.get("content")
    if not isinstance(content, list):
        return
    text_parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        # Codex emits output_text, input_text, etc.; we want anything textlike.
        t = item.get("text") or item.get("output_text") or item.get("content")
        if isinstance(t, str):
            text_parts.append(t)
    if not text_parts:
        return
    result.n_assistant_messages += 1
    _scan_text_for_self_reports("\n".join(text_parts), result)


def _process_function_call(
    payload: dict, skills_root_str: str, result: TranscriptScan
) -> None:
    if payload.get("name") != "exec_command":
        return
    result.n_exec_commands += 1
    args_raw = payload.get("arguments")
    if not isinstance(args_raw, str):
        return
    try:
        args = json.loads(args_raw)
    except json.JSONDecodeError:
        return
    cmd = args.get("cmd")
    workdir = args.get("workdir") or ""
    if not isinstance(cmd, str):
        return
    haystacks = [cmd, str(workdir)]
    matched = _match_skill_path(haystacks, skills_root_str)
    if matched:
        result.add_script_invocation(matched)


def extract_last_assistant_text(transcript_path: Path) -> str:
    """Return the concatenated text of the most recent assistant message.

    Used by the Claude Code Stop hook (Phase 2) since Claude Code does not
    populate ``last_assistant_message`` in the Stop hook payload — only
    ``transcript_path``. Returns an empty string if the file is missing,
    malformed, or contains no assistant messages.

    Handles both transcript formats:
      - Codex: ``{"payload": {"type": "message", "role": "assistant", ...}}``
      - Claude Code: ``{"type": "assistant", "message": {"role": "assistant",
        "content": [{"type": "text", "text": "..."}]}}``
    """
    try:
        fh = transcript_path.open("r", encoding="utf-8", errors="replace")
    except (FileNotFoundError, OSError):
        return ""

    last_text = ""
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            # Codex shape
            payload = event.get("payload")
            if isinstance(payload, dict) and payload.get("type") == "message":
                if payload.get("role") == "assistant":
                    txt = _extract_message_text(payload.get("content"))
                    if txt:
                        last_text = txt
                continue

            # Claude Code shape
            if event.get("type") == "assistant":
                msg = event.get("message")
                if isinstance(msg, dict) and msg.get("role") == "assistant":
                    txt = _extract_message_text(msg.get("content"))
                    if txt:
                        last_text = txt
    return last_text


def _extract_message_text(content: object) -> str:
    """Join all text-bearing blocks in a Codex/Claude content list."""
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        # Codex uses output_text/input_text; Claude Code uses type="text" + text
        if item.get("type") == "text":
            t = item.get("text")
            if isinstance(t, str):
                parts.append(t)
        else:
            t = item.get("text") or item.get("output_text") or item.get("content")
            if isinstance(t, str):
                parts.append(t)
    return "\n".join(parts)


_SKILL_NAME_RE = re.compile(r"[A-Za-z0-9_.\-]+")


def _match_skill_path(haystacks: list[str], skills_root_str: str) -> str | None:
    """Find `<skills_root>/<name>/scripts/` in any of the strings and return name."""
    needle = skills_root_str.rstrip("/") + "/"
    for s in haystacks:
        idx = s.find(needle)
        if idx < 0:
            continue
        tail = s[idx + len(needle):]
        m = _SKILL_NAME_RE.match(tail)
        if not m:
            continue
        name = m.group(0)
        rest = tail[len(name):]
        # Require the scripts/ segment to follow.
        if rest.startswith("/scripts/") or rest == "/scripts":
            return name
    return None
