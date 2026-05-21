"""Manage ``skillOverrides`` in ``~/.claude/settings.local.json`` to give
the router full control over which skill descriptions Claude Code shows
in its native catalog (Mode A).

Background
==========

Claude Code loads SKILL.md frontmatter ``description`` fields into the
model's context at session start, capped at 1% of the model's context
window (configurable via ``skillListingBudgetFraction``). When the cap
overflows it drops the descriptions of skills the user has invoked
least, which is a reasonable LRU heuristic but ignores per-prompt
relevance.

The ``skillOverrides`` setting (per
https://code.claude.com/docs/en/skills#override-skill-visibility-from-settings)
lets you set each skill to one of four states:

  - ``"on"``                 — full description listed (default)
  - ``"name-only"``          — just the name, no description
  - ``"user-invocable-only"``— hidden from Claude, only ``/slash`` works
  - ``"off"``                — hidden everywhere

Mode P (passive overlay): leave skillOverrides alone, add additionalContext
on top. Two catalogs coexist; the model picks.

Mode A (active downgrade): set every non-top-K skill to ``"name-only"``
so the native catalog only shows full descriptions for the router's
picks. Router fully owns description visibility per turn.

Implementation notes
====================

We **own the entire ``skillOverrides`` key** in ``settings.local.json``
when Mode A is active. There is no sentinel mechanism for JSON, so a
mixed mode where the user sets some overrides and we set others would
race. If a user wants to pin a particular skill to ``"off"``, they can
add it to a separate ``mega-tron`` config and we'll merge it on top.
This module currently does the simple thing: replace the whole key.

Restore on uninstall: remove the ``skillOverrides`` key entirely (the
default is "on" for every skill, which is the state before we
installed).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable


SETTINGS_LOCAL_PATH = Path.home() / ".claude" / "settings.local.json"


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not data:
        if path.exists():
            path.unlink()
        return
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def apply_mode_a(
    top_k_names: Iterable[str],
    *,
    all_skill_names: Iterable[str],
    settings_path: Path | None = None,
) -> int:
    """Downgrade every non-top-K skill to ``"name-only"`` in
    ``settings.local.json``. Top-K skills are removed from
    ``skillOverrides`` (treated as ``"on"`` by default).

    Returns the number of skills set to ``name-only``.

    Caller's responsibility: pass the *full* skill name set so we can
    invert it correctly. Passing only the top-K leaves all other skills
    in their previous override state, which silently misbehaves.
    """
    if settings_path is None:
        # Lookup at call time so monkeypatching the module attribute
        # (in tests, or per-environment overrides) takes effect even when
        # the caller imports apply_mode_a once at module import time.
        settings_path = SETTINGS_LOCAL_PATH

    top_set = set(top_k_names)
    overrides: dict[str, str] = {}
    for name in all_skill_names:
        if name in top_set:
            continue  # implicit "on"
        overrides[name] = "name-only"

    data = _read(settings_path)
    data["skillOverrides"] = overrides
    _write(settings_path, data)
    return len(overrides)


def clear(settings_path: Path | None = None) -> bool:
    """Remove our ``skillOverrides`` key entirely (revert to Mode P).

    Returns True if a change was made.
    """
    if settings_path is None:
        settings_path = SETTINGS_LOCAL_PATH
    data = _read(settings_path)
    if "skillOverrides" not in data:
        return False
    data.pop("skillOverrides", None)
    _write(settings_path, data)
    return True


__all__ = ["SETTINGS_LOCAL_PATH", "apply_mode_a", "clear"]
