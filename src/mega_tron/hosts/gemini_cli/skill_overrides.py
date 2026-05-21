"""Mode-A ``skills.disabled`` writer for Gemini CLI.

Background
==========

Gemini CLI loads SKILL.md frontmatter ``description`` fields into the
model's system prompt at session start (the discovery half of progressive
disclosure). The catalog grows linearly with the number of installed
skills — at 100 skills it's already 3–5k tokens, and the user's pool
here is 2,830 skills. mega-tron's router selects top-K per turn;
Mode-A pushes that selection into the native catalog so the LLM only
sees full descriptions for the routed picks.

Gemini's `~/.gemini/settings.json` exposes the relevant knob (per
[configuration reference](
https://geminicli.com/docs/reference/configuration/)):

    skills:
      enabled  (bool, default true)
      disabled (array of names, default [])

We drive ``skills.disabled`` to hold ``all - top_k``.

Implementation notes
====================

- Unlike Claude (which has a separate ``settings.local.json``), Gemini
  keeps everything in one file. Users may already have entries in
  ``skills.disabled``; we snapshot the original list at install time to
  ``settings.json.mega-tron-backup`` so ``--uninstall`` can restore
  it cleanly.
- Mode-A is best-effort: any write failure (read-only fs, locked file,
  malformed pre-existing JSON) is reported via the caller and the hook
  falls back to Mode-P silently.
- Atomic write: temp file + rename, so a crash during the per-turn
  rewrite can never corrupt the user's settings.
- We **own the entire ``skills.disabled`` key** while Mode-A is active.
  User-pinned permanent disables are preserved through the install
  backup (and restored on uninstall).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable


SETTINGS_PATH = Path.home() / ".gemini" / "settings.json"
BACKUP_PATH = Path.home() / ".gemini" / "settings.json.mega-tron-backup"


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


def snapshot_original_disabled(
    *,
    settings_path: Path | None = None,
    backup_path: Path | None = None,
) -> bool:
    """Capture the user's pre-install ``skills.disabled`` list to disk.

    Idempotent: if the backup file already exists, leaves it untouched
    (the first snapshot wins — we never overwrite the original after
    a re-install). Returns True if a snapshot was written, False if one
    already existed or there was nothing to snapshot.
    """
    settings_path = settings_path or SETTINGS_PATH
    backup_path = backup_path or BACKUP_PATH
    if backup_path.exists():
        return False
    data = _read(settings_path)
    skills = data.get("skills") or {}
    if not isinstance(skills, dict):
        skills = {}
    original_disabled = skills.get("disabled", [])
    if not isinstance(original_disabled, list):
        original_disabled = []
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path.write_text(
        json.dumps({"skills_disabled": list(original_disabled)}, indent=2) + "\n",
        encoding="utf-8",
    )
    return True


def _read_original_disabled(backup_path: Path) -> list[str]:
    if not backup_path.exists():
        return []
    try:
        data = json.loads(backup_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    raw = data.get("skills_disabled") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    return [str(x) for x in raw if isinstance(x, str)]


def apply_mode_a(
    top_k_names: Iterable[str],
    *,
    all_skill_names: Iterable[str],
    settings_path: Path | None = None,
    backup_path: Path | None = None,
) -> int:
    """Rewrite ``skills.disabled`` to ``sorted(all - top_k ∪ user_pinned)``.

    The user's original ``skills.disabled`` (snapshotted at install) is
    unioned into our per-turn write, so a skill the user explicitly
    pinned to disabled stays disabled even if the router would have
    routed it.

    Returns the number of skills disabled in this write.

    Caller's responsibility: pass the *full* skill name set so we can
    invert it correctly. Passing only the top-K leaves all other skills
    in the previous state, which silently misbehaves.
    """
    if settings_path is None:
        # Lookup at call time so monkeypatching the module attribute (in
        # tests, or per-environment overrides) takes effect even when
        # the caller imports apply_mode_a once at module import time.
        settings_path = SETTINGS_PATH
    if backup_path is None:
        backup_path = BACKUP_PATH

    # User wins: if the user explicitly pinned a skill to disabled at
    # install time (captured in the snapshot), keep it disabled even if
    # the router would have routed it. We strip user-pinned names from
    # the effective top-K before inverting against the full pool.
    user_pinned = set(_read_original_disabled(backup_path))
    top_set = set(top_k_names) - user_pinned
    disabled = sorted(
        n for n in set(all_skill_names) | user_pinned if n not in top_set
    )

    data = _read(settings_path)
    skills = data.get("skills") if isinstance(data.get("skills"), dict) else {}
    skills = dict(skills)
    skills["disabled"] = disabled
    data["skills"] = skills
    _write(settings_path, data)
    return len(disabled)


def clear(
    *,
    settings_path: Path | None = None,
    backup_path: Path | None = None,
) -> bool:
    """Wipe ``skills.disabled`` clean and delete the install-time backup.

    Earlier revisions tried to "restore" the pre-install ``disabled``
    list from the backup file, but in practice that backup almost always
    captured pollution from a previous-generation tool (mega-optimus,
    mega-skill-router) rather than a genuine user-pinned set. The user
    intent on uninstall is simply: make Gemini's skills.disabled go
    away. So we drop the key entirely regardless of backup contents.

    Returns True if any state was changed (either the disabled key was
    removed or the backup file was deleted).
    """
    if settings_path is None:
        settings_path = SETTINGS_PATH
    if backup_path is None:
        backup_path = BACKUP_PATH

    changed = False

    data = _read(settings_path)
    skills = data.get("skills") if isinstance(data.get("skills"), dict) else {}
    skills = dict(skills)
    if "disabled" in skills:
        skills.pop("disabled", None)
        changed = True

    if skills:
        data["skills"] = skills
    else:
        if "skills" in data:
            data.pop("skills", None)
            changed = True

    if changed:
        _write(settings_path, data)

    if backup_path.exists():
        try:
            backup_path.unlink()
            changed = True
        except OSError:
            pass

    return changed


__all__ = [
    "SETTINGS_PATH",
    "BACKUP_PATH",
    "apply_mode_a",
    "clear",
    "snapshot_original_disabled",
]
