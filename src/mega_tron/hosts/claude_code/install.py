"""Claude Code installer.

Wires mega-tron into Claude Code via two touch points (vs. Codex's
four — no shell wrapper, no SHA trust stamping needed):

1. ``~/.claude/settings.json``: register UserPromptSubmit + Stop hooks
   under the ``hooks`` key, sentinel-tagged for safe ``--uninstall``.
2. ``~/.claude/CLAUDE.md``: inject persistent guidance about the
   router and the self-eval contract, between HTML-comment sentinels.

The hook entry schema matches Claude Code's documented format
(https://code.claude.com/docs/en/hooks):

    {
      "hooks": {
        "UserPromptSubmit": [
          {
            "matcher": ".*",
            "hooks": [
              {"type": "command", "command": "mega-tron claude-hook"}
            ]
          }
        ]
      }
    }

To make entries identifiable without polluting the documented schema
with custom keys (which Claude Code may reject), we tag managed
entries with a sibling JSON key ``_megaTronManaged`` at the hook-group
level. Claude Code currently ignores unknown keys in hook groups
(verified via the schema spec where only ``matcher`` and ``hooks`` are
documented as required).

Entries left behind by an earlier installer iteration (legacy key
``_megaOptimusManaged``) are detected too so re-running ``setup``
silently refreshes them onto the current key + version.
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
# inside the user's settings.json on the next setup invocation.
MANAGED_VERSION = f"{MEGA_TRON_VERSION}-claude"
MANAGED_KEY = "_megaTronManaged"
# Legacy sentinel keys from prior installer generations. Detected as
# "managed-by-us" so re-running setup migrates them onto MANAGED_KEY.
LEGACY_MANAGED_KEYS = ("_megaOptimusManaged",)

SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
CLAUDE_MD_PATH = Path.home() / ".claude" / "CLAUDE.md"

CLAUDE_SENTINEL_START = (
    "<!-- >>> mega-tron (managed; edit between sentinels at your own "
    "risk) >>> -->"
)
CLAUDE_SENTINEL_END = "<!-- <<< mega-tron <<< -->"

# Tagging-contract paragraph is the single source of truth in
# :mod:`mega_tron.self_eval_contract`.
CLAUDE_BLOCK_BODY = (
    "## Skill routing (mega-tron)\n"
    "\n"
    "The first prompt of every session is auto-routed: a hook injects a\n"
    "\"Skills (selected for this turn)\" block with the top-K ranked picks,\n"
    "their absolute paths, descriptions, and the self-evaluation contract.\n"
    "Strongly prefer using one of the skills the hook surfaces — they were\n"
    "semantically ranked against the user's prompt.\n"
    "\n"
    "For subsequent turns (or any time you need a skill that wasn't pre-routed)\n"
    "call `mega-tron search \"<short task description>\"`. By default it\n"
    "prints `name + skill_dir + description` for the top-K; add\n"
    "`--output bodies` to dump each pick's full SKILL.md instead.\n"
    "\n"
    + render_install_tagging_guide(hook_name="Stop")
    + "\n"
)


MANAGED_EVENTS = ("UserPromptSubmit", "Stop")


# --- Hook entry construction ------------------------------------------------


def _resolve_hook_command(executable: str | None, suffix: str) -> str:
    """Pick the command Claude Code should invoke for a given hook.

    Delegates to the shared resolver in
    :mod:`mega_tron.hosts._hook_command`, which prefers an absolute
    path to ``mega-tron`` so the hook still fires when Claude's
    subprocess environment has a slimmed PATH.
    """
    from mega_tron.hosts._hook_command import resolve_hook_command

    return resolve_hook_command(executable, suffix)


def _hook_entry(command: str) -> dict:
    """Build the managed UserPromptSubmit / Stop hook group.

    Schema follows Claude Code's documented format. We tag the group
    with :data:`MANAGED_KEY` (a non-standard sibling key, ignored by
    Claude Code) so ``--uninstall`` can find and remove just our entry
    without touching user-added hooks.
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
            f"[install --target claude] {path} is not valid JSON ({e}); "
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
    # Atomic write so a crash mid-merge can't corrupt the user's settings.
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


# --- CLAUDE.md guidance block -----------------------------------------------


def render_claude_md_block() -> str:
    header = (
        f"<!-- mega-tron v{MANAGED_VERSION} —"
        " re-run `mega-tron install --target claude` to update;"
        " `--uninstall` to remove. -->\n"
    )
    return (
        f"{CLAUDE_SENTINEL_START}\n{header}{CLAUDE_BLOCK_BODY.rstrip()}\n"
        f"{CLAUDE_SENTINEL_END}\n"
    )


def _install_claude_md(path: Path) -> None:
    # Reuse the shared replace helper from the Codex installer — it's
    # marker-agnostic, just takes start/end strings.
    from mega_tron.hosts.codex.install import _replace_block

    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    new_block = render_claude_md_block()
    updated = _replace_block(
        existing,
        new_block,
        start_marker=CLAUDE_SENTINEL_START,
        end_marker=CLAUDE_SENTINEL_END,
    )
    if updated == existing:
        print(
            f"[install --target claude] CLAUDE.md block in {path} "
            "already up to date.",
            file=sys.stderr,
        )
        return
    path.write_text(updated, encoding="utf-8")
    print(
        f"[install --target claude] wrote mega-tron block into {path}.",
        file=sys.stderr,
    )


def _uninstall_claude_md(path: Path) -> None:
    from mega_tron.hosts.codex.install import _strip_block

    if not path.exists():
        print(
            f"[install --target claude] {path} not present; skipping "
            "CLAUDE.md uninstall.",
            file=sys.stderr,
        )
        return
    before = path.read_text(encoding="utf-8")
    after = _strip_block(
        before,
        start_marker=CLAUDE_SENTINEL_START,
        end_marker=CLAUDE_SENTINEL_END,
    )
    if after == before:
        print(
            f"[install --target claude] no managed block in {path}.",
            file=sys.stderr,
        )
        return
    if not after.strip():
        path.unlink()
        print(
            f"[install --target claude] removed {path} (only contained "
            "mega-tron block).",
            file=sys.stderr,
        )
        return
    path.write_text(after, encoding="utf-8")
    print(
        f"[install --target claude] removed mega-tron block from {path}.",
        file=sys.stderr,
    )


# --- Top-level orchestration -------------------------------------------------


def run_install_claude(args: argparse.Namespace) -> int:
    """Top-level Claude Code installer entry point.

    Honors ``args.uninstall``, ``args.print_only``, ``args.no_warmup``,
    ``args.hook_command`` from the shared install argparser. Touch points
    Codex install doesn't have (shell rc, AGENTS.md, codex config.toml,
    trust stamping) are simply skipped here.
    """
    settings_path = SETTINGS_PATH
    claude_md_path = CLAUDE_MD_PATH

    if getattr(args, "uninstall", False):
        return _uninstall(settings_path, claude_md_path)

    hook_exe = getattr(args, "hook_command", None)
    ups_entry = _hook_entry(_resolve_hook_command(hook_exe, "claude-hook"))
    stop_entry = _hook_entry(_resolve_hook_command(hook_exe, "claude-stop-hook"))

    if getattr(args, "print_only", False):
        # Useful for users who want to vet the JSON before letting us
        # touch their settings file.
        sys.stdout.write(
            json.dumps(
                {
                    "settings_path": str(settings_path),
                    "claude_md_path": str(claude_md_path),
                    "userPromptSubmit_entry": ups_entry,
                    "stop_entry": stop_entry,
                    "claude_md_block": render_claude_md_block(),
                },
                indent=2,
            )
            + "\n"
        )
        return 0

    # 1. settings.json — merge in both hook entries.
    try:
        current = _read_settings(settings_path)
    except json.JSONDecodeError:
        return 1

    merged = _merge_hook_entry(current, ups_entry, event="UserPromptSubmit")
    merged = _merge_hook_entry(merged, stop_entry, event="Stop")
    if merged != current:
        # Defensive backup so the user can recover if something goes wrong.
        if settings_path.exists():
            backup = settings_path.with_suffix(settings_path.suffix + ".bak")
            shutil.copy2(settings_path, backup)
            print(
                f"[install --target claude] backed up {settings_path} "
                f"→ {backup}",
                file=sys.stderr,
            )
        _write_settings(settings_path, merged)
        print(
            f"[install --target claude] wrote managed hooks into "
            f"{settings_path}.",
            file=sys.stderr,
        )
    else:
        print(
            f"[install --target claude] hooks in {settings_path} already "
            "up to date.",
            file=sys.stderr,
        )

    # 2. CLAUDE.md — guidance block.
    _install_claude_md(claude_md_path)

    # 3. Warmup — reuse the Codex installer's warmup, it's CLI-agnostic.
    if not getattr(args, "no_warmup", False):
        from mega_tron.hosts.codex.install import _run_warmup

        _run_warmup(getattr(args, "skills_dir", None))

    print(
        "[install --target claude] done. Restart any open `claude` "
        "sessions for the hooks to take effect.",
        file=sys.stderr,
    )
    return 0


def _uninstall(settings_path: Path, claude_md_path: Path) -> int:
    """Remove our managed entries; leave user-owned hooks intact.

    Also clears any Mode A skillOverrides left behind in settings.local.json
    so the native catalog reverts to default behavior.
    """
    try:
        current = _read_settings(settings_path)
    except json.JSONDecodeError:
        return 1
    stripped = _strip_managed_hooks(current)
    if stripped != current:
        _write_settings(settings_path, stripped)
        print(
            f"[install --target claude] removed managed hooks from "
            f"{settings_path}.",
            file=sys.stderr,
        )
    else:
        print(
            f"[install --target claude] no managed hooks found in "
            f"{settings_path}.",
            file=sys.stderr,
        )
    _uninstall_claude_md(claude_md_path)

    # Mode A leftover cleanup
    try:
        from mega_tron.hosts.claude_code.skill_overrides import clear as clear_overrides

        if clear_overrides():
            print(
                "[install --target claude] cleared skillOverrides from "
                "settings.local.json (Mode A residue).",
                file=sys.stderr,
            )
    except Exception as e:  # noqa: BLE001
        print(
            f"[install --target claude] skillOverrides clear skipped: {e}",
            file=sys.stderr,
        )

    print("[install --target claude] uninstall complete.", file=sys.stderr)
    return 0
