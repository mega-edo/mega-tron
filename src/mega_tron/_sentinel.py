"""Shared sentinel-bracketed JSON extractor.

Both the Stop hook (Phase 2 verdict parse) and the agentic search pipeline
ask the model to emit a JSON object between literal sentinel strings so we
can parse it back even when the model wraps it in prose or markdown fences.

Pattern:

    <<<MEGA_SENTINEL_START_<TAG>>>>
    { ... json ... }
    <<<MEGA_SENTINEL_END_<TAG>>>>

Tolerates:
- code fences (```json ... ```) around the payload
- stray prose before/after the sentinels
- complete missing sentinels (falls back to first bare JSON object that
  contains the expected top-level key, if `fallback_key` is given).
"""
from __future__ import annotations

import json
import re


def make_sentinels(tag: str) -> tuple[str, str]:
    """Return (start, end) sentinel strings for the given tag."""
    return (f"<<<MEGA_SENTINEL_START_{tag}>>>", f"<<<MEGA_SENTINEL_END_{tag}>>>")


def extract_json(
    text: str,
    *,
    start: str,
    end: str,
    fallback_key: str | None = None,
) -> dict | None:
    """Pull one JSON object out of ``text``.

    Args:
        text: model output (usually ``last_assistant_message``).
        start: sentinel string marking the start of the payload.
        end: sentinel string marking the end.
        fallback_key: if the sentinel-bracketed form isn't found, scan for
            the first ``{...}`` containing this key as a bare JSON object.

    Returns:
        Parsed dict, or ``None`` if nothing parsable was found.
    """
    if not text:
        return None
    sentinel_re = re.compile(
        re.escape(start) + r"\s*(.*?)\s*" + re.escape(end), re.DOTALL
    )
    m = sentinel_re.search(text)
    if m:
        body = m.group(1).strip()
        body = re.sub(r"^```(?:json)?\s*", "", body, flags=re.IGNORECASE)
        body = re.sub(r"\s*```$", "", body).strip()
        try:
            obj = json.loads(body)
        except json.JSONDecodeError:
            return None
        return obj if isinstance(obj, dict) else None

    if fallback_key:
        needle = '"' + fallback_key + '"'
        idx = 0
        while True:
            key_pos = text.find(needle, idx)
            if key_pos < 0:
                break
            open_pos = text.rfind("{", 0, key_pos)
            if open_pos < 0:
                idx = key_pos + len(needle)
                continue
            close_pos = _find_matching_brace(text, open_pos)
            if close_pos < 0:
                idx = key_pos + len(needle)
                continue
            try:
                obj = json.loads(text[open_pos : close_pos + 1])
            except json.JSONDecodeError:
                idx = key_pos + len(needle)
                continue
            if isinstance(obj, dict) and fallback_key in obj:
                return obj
            idx = close_pos + 1
    return None


def _find_matching_brace(text: str, open_pos: int) -> int:
    """Return the index of the brace closing the one at ``open_pos``,
    or -1 if no match. Ignores braces inside double-quoted strings."""
    depth = 0
    i = open_pos
    in_str = False
    escape = False
    while i < len(text):
        c = text[i]
        if in_str:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    return -1
