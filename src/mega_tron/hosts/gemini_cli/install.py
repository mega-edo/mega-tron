"""Gemini CLI installer.

Wires mega-tron into Gemini CLI via three touch points (vs. Claude's
two — same JSON-settings shape plus a Mode-A backup):

1. ``~/.gemini/settings.json``: register ``BeforeAgent`` + ``AfterAgent``
   hooks under the ``hooks`` key, sentinel-tagged for safe ``--uninstall``.
2. ``~/.gemini/GEMINI.md``: inject persistent guidance about the
   router and the self-eval contract, between HTML-comment sentinels.
3. ``~/.gemini/settings.json.mega-tron-backup``: snapshot the user's
   pre-install ``skills.disabled`` array so Mode-A can restore it on
   ``--uninstall``.

The hook entry schema follows Gemini CLI's documented format
(https://geminicli.com/docs/hooks/writing-hooks/):

    {
      "hooks": {
        "BeforeAgent": [
          {
            "matcher": ".*",
            "hooks": [
              {"type": "command", "command": "mega-tron gemini-hook"}
            ]
          }
        ]
      }
    }

Managed entries are tagged with a sibling ``_megaTronManaged`` key.
Gemini's hook reader documents only ``matcher`` and ``hooks`` as
required, so the extra key is ignored at runtime but lets ``--uninstall``
identify our entries precisely. Entries from an earlier installer
generation (legacy key ``_megaOptimusManaged``) are detected too so
re-running ``setup`` silently refreshes them onto the current key.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from mega_tron import __version__ as MEGA_TRON_VERSION
from mega_tron.self_eval_contract import render_install_tagging_guide


# --- Constants --------------------------------------------------------------

# Pinned to the running package version so `uv tool install --reinstall
# mega-tron && mega-tron setup` automatically refreshes the sentinel
# in the user's settings.json on the next setup invocation.
MANAGED_VERSION = f"{MEGA_TRON_VERSION}-gemini"
MANAGED_KEY = "_megaTronManaged"
# Legacy sentinel keys from prior installer generations.
LEGACY_MANAGED_KEYS = ("_megaOptimusManaged",)

SETTINGS_PATH = Path.home() / ".gemini" / "settings.json"
GEMINI_MD_PATH = Path.home() / ".gemini" / "GEMINI.md"

GEMINI_SENTINEL_START = (
    "<!-- >>> mega-tron (managed; edit between sentinels at your own "
    "risk) >>> -->"
)
GEMINI_SENTINEL_END = "<!-- <<< mega-tron <<< -->"

# Tagging-contract paragraph is the single source of truth in
# :mod:`mega_tron.self_eval_contract`.
GEMINI_BLOCK_BODY = (
    "## Skill routing (mega-tron)\n"
    "\n"
    "The first prompt of every session is auto-routed: a BeforeAgent hook\n"
    "injects a \"Skills (selected for this turn)\" block with the top-K ranked\n"
    "picks, their absolute paths, descriptions, and the self-evaluation\n"
    "contract. Strongly prefer activating one of the surfaced skills via the\n"
    "`activate_skill` tool — they were semantically ranked against the\n"
    "user's prompt.\n"
    "\n"
    "For subsequent turns (or any time you need a skill that wasn't\n"
    "pre-routed) call `mega-tron search \"<short task description>\"`. By\n"
    "default it prints `name + skill_dir + description` for the top-K; add\n"
    "`--output bodies` to dump each pick's full SKILL.md instead.\n"
    "\n"
    + render_install_tagging_guide(hook_name="AfterAgent")
    + "\n"
)


MANAGED_EVENTS = ("BeforeAgent", "AfterAgent")


# --- Hook entry construction ------------------------------------------------


def _resolve_hook_command(executable: str | None, suffix: str) -> str:
    """Pick the command Gemini CLI should invoke for a given hook.

    Delegates to the shared resolver in
    :mod:`mega_tron.hosts._hook_command`, which prefers an absolute
    path to ``mega-tron`` so the hook still fires when Gemini's
    subprocess environment has a slimmed PATH.
    """
    from mega_tron.hosts._hook_command import resolve_hook_command

    return resolve_hook_command(executable, suffix)


def _hook_entry(command: str) -> dict:
    """Build a managed BeforeAgent / AfterAgent hook group.

    Schema follows Gemini CLI's documented format. We tag the group
    with :data:`MANAGED_KEY` (a non-standard sibling key, ignored by
    Gemini) so ``--uninstall`` can find and remove just our entry.
    """
    return {
        "matcher": ".*",
        MANAGED_KEY: MANAGED_VERSION,
        "hooks": [
            {
                "type": "command",
                "command": command,
            }
        ],
    }


def _is_managed_hook(entry: dict) -> bool:
    """True if the hook entry was managed by mega-tron — current or legacy
    installer generation."""
    if not isinstance(entry, dict):
        return False
    if MANAGED_KEY in entry:
        return True
    return any(legacy_key in entry for legacy_key in LEGACY_MANAGED_KEYS)


# --- settings.json merge ----------------------------------------------------


def _read_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError as e:
        print(
            f"[install --target gemini] {path} is not valid JSON ({e}); "
            "refusing to overwrite. Back it up and re-run.",
            file=sys.stderr,
        )
        raise


def _write_settings(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not data:
        if path.exists():
            path.unlink()
        return
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _merge_hook_entry(existing: dict, entry: dict, *, event: str) -> dict:
    """Insert/replace our managed entry under ``hooks.<event>``.

    Preserves user-owned hooks (anything without our managed key,
    including no legacy sentinel).
    """
    out = dict(existing)
    hooks = dict(out.get("hooks") or {})
    entries = list(hooks.get(event, []))
    entries = [e for e in entries if not _is_managed_hook(e)]
    entries.append(entry)
    hooks[event] = entries
    out["hooks"] = hooks
    return out


def _strip_managed_hooks(existing: dict) -> dict:
    """Remove our managed entries from every managed event."""
    if not isinstance(existing.get("hooks"), dict):
        return existing
    out = dict(existing)
    hooks = dict(out["hooks"])
    for event in MANAGED_EVENTS:
        if event not in hooks:
            continue
        kept = [e for e in hooks.get(event, []) if not _is_managed_hook(e)]
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event, None)
    if hooks:
        out["hooks"] = hooks
    else:
        out.pop("hooks", None)
    return out


# --- GEMINI.md guidance block -----------------------------------------------


def render_gemini_md_block() -> str:
    header = (
        f"<!-- mega-tron v{MANAGED_VERSION} —"
        " re-run `mega-tron install --target gemini` to update;"
        " `--uninstall` to remove. -->\n"
    )
    return (
        f"{GEMINI_SENTINEL_START}\n{header}{GEMINI_BLOCK_BODY.rstrip()}\n"
        f"{GEMINI_SENTINEL_END}\n"
    )


def _install_gemini_md(path: Path) -> None:
    from mega_tron.hosts.codex.install import _replace_block

    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    new_block = render_gemini_md_block()
    updated = _replace_block(
        existing,
        new_block,
        start_marker=GEMINI_SENTINEL_START,
        end_marker=GEMINI_SENTINEL_END,
    )
    if updated == existing:
        print(
            f"[install --target gemini] GEMINI.md block in {path} "
            "already up to date.",
            file=sys.stderr,
        )
        return
    path.write_text(updated, encoding="utf-8")
    print(
        f"[install --target gemini] wrote mega-tron block into {path}.",
        file=sys.stderr,
    )


def _uninstall_gemini_md(path: Path) -> None:
    from mega_tron.hosts.codex.install import _strip_block

    if not path.exists():
        print(
            f"[install --target gemini] {path} not present; skipping "
            "GEMINI.md uninstall.",
            file=sys.stderr,
        )
        return
    before = path.read_text(encoding="utf-8")
    after = _strip_block(
        before,
        start_marker=GEMINI_SENTINEL_START,
        end_marker=GEMINI_SENTINEL_END,
    )
    if after == before:
        print(
            f"[install --target gemini] no managed block in {path}.",
            file=sys.stderr,
        )
        return
    if not after.strip():
        path.unlink()
        print(
            f"[install --target gemini] removed {path} (only contained "
            "mega-tron block).",
            file=sys.stderr,
        )
        return
    path.write_text(after, encoding="utf-8")
    print(
        f"[install --target gemini] removed mega-tron block from {path}.",
        file=sys.stderr,
    )


# --- Top-level orchestration -------------------------------------------------


def run_install_gemini(args: argparse.Namespace) -> int:
    """Top-level Gemini CLI installer entry point.

    Honors ``args.uninstall``, ``args.print_only``, ``args.no_warmup``,
    ``args.hook_command`` from the shared install argparser. Codex-only
    touch points (shell rc, AGENTS.md, codex config.toml, SHA trust
    stamping) are simply skipped here.
    """
    settings_path = SETTINGS_PATH
    gemini_md_path = GEMINI_MD_PATH

    if getattr(args, "uninstall", False):
        return _uninstall(settings_path, gemini_md_path)

    hook_exe = getattr(args, "hook_command", None)
    ba_entry = _hook_entry(_resolve_hook_command(hook_exe, "gemini-hook"))
    aa_entry = _hook_entry(_resolve_hook_command(hook_exe, "gemini-stop-hook"))

    if getattr(args, "print_only", False):
        sys.stdout.write(
            json.dumps(
                {
                    "settings_path": str(settings_path),
                    "gemini_md_path": str(gemini_md_path),
                    "beforeAgent_entry": ba_entry,
                    "afterAgent_entry": aa_entry,
                    "gemini_md_block": render_gemini_md_block(),
                },
                indent=2,
            )
            + "\n"
        )
        return 0

    # 1. Snapshot original skills.disabled BEFORE we register hooks, so
    #    Mode-A on the very first session can find a stable baseline.
    try:
        from mega_tron.hosts.gemini_cli.skill_overrides import (
            snapshot_original_disabled,
        )

        if snapshot_original_disabled():
            print(
                "[install --target gemini] snapshotted original "
                "skills.disabled for safe Mode-A restore on uninstall.",
                file=sys.stderr,
            )
    except Exception as e:  # noqa: BLE001
        print(
            f"[install --target gemini] skills.disabled snapshot skipped: "
            f"{e}",
            file=sys.stderr,
        )

    # 2. settings.json — merge in both hook entries.
    try:
        current = _read_settings(settings_path)
    except json.JSONDecodeError:
        return 1

    merged = _merge_hook_entry(current, ba_entry, event="BeforeAgent")
    merged = _merge_hook_entry(merged, aa_entry, event="AfterAgent")
    if merged != current:
        if settings_path.exists():
            backup = settings_path.with_suffix(settings_path.suffix + ".bak")
            shutil.copy2(settings_path, backup)
            print(
                f"[install --target gemini] backed up {settings_path} "
                f"→ {backup}",
                file=sys.stderr,
            )
        _write_settings(settings_path, merged)
        print(
            f"[install --target gemini] wrote managed hooks into "
            f"{settings_path}.",
            file=sys.stderr,
        )
    else:
        print(
            f"[install --target gemini] hooks in {settings_path} already "
            "up to date.",
            file=sys.stderr,
        )

    # 3. GEMINI.md — guidance block.
    _install_gemini_md(gemini_md_path)

    # 4. Warmup — reuse the Codex installer's warmup, it's CLI-agnostic.
    if not getattr(args, "no_warmup", False):
        from mega_tron.hosts.codex.install import _run_warmup

        _run_warmup(getattr(args, "skills_dir", None))

    # 5. Mode-A advisory: `skills.disabled` may require a Gemini restart
    #    to take effect per the configuration reference. Surface this so
    #    the user doesn't think the catalog compression is broken.
    print(
        "[install --target gemini] Mode-A note: `skills.disabled` is "
        "documented as Requires-Restart in Gemini's settings reference. "
        "Restart `gemini` after install for catalog compression to take "
        "effect. Set MEGA_GEMINI_MODE=passive to disable Mode-A.",
        file=sys.stderr,
    )

    print(
        "[install --target gemini] done. Restart any open `gemini` "
        "sessions for the hooks to take effect.",
        file=sys.stderr,
    )
    return 0


def _uninstall(settings_path: Path, gemini_md_path: Path) -> int:
    """Remove managed hook entries, GEMINI.md block, and restore the
    user's original ``skills.disabled`` from the backup file."""
    try:
        current = _read_settings(settings_path)
    except json.JSONDecodeError:
        return 1
    stripped = _strip_managed_hooks(current)
    if stripped != current:
        _write_settings(settings_path, stripped)
        print(
            f"[install --target gemini] removed managed hooks from "
            f"{settings_path}.",
            file=sys.stderr,
        )
    else:
        print(
            f"[install --target gemini] no managed hooks found in "
            f"{settings_path}.",
            file=sys.stderr,
        )

    _uninstall_gemini_md(gemini_md_path)

    # Mode-A cleanup: wipe skills.disabled clean and remove the
    # install-time backup. The backup almost always captured legacy-tool
    # pollution rather than a genuine user-pinned set, so we drop the
    # key entirely regardless of contents.
    try:
        from mega_tron.hosts.gemini_cli.skill_overrides import (
            clear as clear_overrides,
        )

        if clear_overrides():
            print(
                "[install --target gemini] wiped skills.disabled and "
                "removed the install-time backup.",
                file=sys.stderr,
            )
    except Exception as e:  # noqa: BLE001
        print(
            f"[install --target gemini] skills.disabled cleanup skipped: "
            f"{e}",
            file=sys.stderr,
        )

    print("[install --target gemini] uninstall complete.", file=sys.stderr)
    return 0


__all__ = ["run_install_gemini", "MANAGED_VERSION", "MANAGED_KEY"]
