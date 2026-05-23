"""`mega-tron install` — wire mega-tron into Codex CLI.

End-user UX:

    uv tool install mega-tron
    mega-tron install              # injects shell wrapper + codex hook
    exec $SHELL                            # new shell picks up wrapper
    codex exec --prompt "do X"             # auto-routed via shell wrapper
    codex                                  # auto-routed via UserPromptSubmit hook

Two install points, both idempotent:

1. **Shell wrapper** (~/.zshrc or ~/.bashrc, between sentinel comments) —
   intercepts `codex exec --prompt …` to pre-stage CODEX_HOME with the
   top-K skills and inject a must-use prefix.

2. **Codex UserPromptSubmit hook** (~/.codex/hooks.json, JSON merge with
   sentinel marker) — fires on every interactive turn, reads the prompt
   from stdin JSON, returns a must-use `additionalContext` via stdout JSON.

Together they cover both `codex exec` (non-interactive) and `codex` (REPL).
`--uninstall` removes exactly our managed sections from both files.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from mega_tron.self_eval_contract import render_install_tagging_guide

WRAPPER_TEMPLATE = Path(__file__).parent / "templates" / "codex_wrapper.sh"

SENTINEL_START = "# >>> mega-tron (managed; edit between sentinels at your own risk) >>>"
SENTINEL_END = "# <<< mega-tron <<<"

# AGENTS.md uses HTML comment sentinels so the managed block stays invisible
# in rendered Markdown but is still trivial to locate + replace + remove.
AGENTS_MD_PATH = Path.home() / ".codex" / "AGENTS.md"
AGENTS_SENTINEL_START = "<!-- >>> mega-tron (managed; edit between sentinels at your own risk) >>> -->"
AGENTS_SENTINEL_END = "<!-- <<< mega-tron <<< -->"

# ~/.codex/config.toml — we patch this so codex stops emitting its built-in
# 8k-char skill catalog (and the must-use rule baked into that block). The
# router then takes over end-to-end: dynamic top-K + must-use rule + meta
# block are all emitted by the UserPromptSubmit hook on each first-fire
# turn. Sentinel comments delimit our managed block so `--uninstall`
# restores codex's default catalog behaviour cleanly.
CODEX_CONFIG_TOML_PATH = Path.home() / ".codex" / "config.toml"
CODEX_CONFIG_SENTINEL_START = "# >>> mega-tron (managed; edit between sentinels at your own risk) >>>"
CODEX_CONFIG_SENTINEL_END = "# <<< mega-tron <<<"

# Second sentinel pair inside the same config.toml — pre-trusts the
# UserPromptSubmit + Stop hooks we registered. Codex 2026 gates user
# hooks behind a trust check; without an entry under `[hooks.state."<key>"]`
# our hooks land in `Untrusted` and silently never fire. Stamping the
# trust hash at install time means the user never has to drop into
# `/hooks` interactively. The sentinel is separate so `--uninstall`
# can strip the trust block + catalog block independently.
CODEX_TRUST_SENTINEL_START = "# >>> mega-tron hook-trust (managed) >>>"
CODEX_TRUST_SENTINEL_END = "# <<< mega-tron hook-trust <<<"

# Marker we plant on every hook entry we own, so we can find + replace + remove
# our entries inside a hooks.json that may also contain the user's own hooks.
# Version is pinned to the running package so `uv tool install --reinstall
# mega-tron && mega-tron setup` automatically refreshes the sentinel on the
# next setup invocation.
from mega_tron import __version__ as _MEGA_TRON_VERSION

HOOK_MANAGED_KEY = "_mega_tron_managed"
HOOK_MANAGED_VERSION = _MEGA_TRON_VERSION
# Legacy sentinel keys from prior installer generations. Detected as
# "managed-by-us" so re-running setup migrates them onto HOOK_MANAGED_KEY.
LEGACY_HOOK_MANAGED_KEYS = ("_mega_optimus_managed",)

# Persistent system-prompt guidance injected into AGENTS.md so the LLM sees
# it on every turn (codex re-renders AGENTS.md into the system prompt each
# session). The UserPromptSubmit hook stays silent on subsequent turns —
# the guidance lives here, the per-turn routing prefix lives in the hook.
# The tagging-contract paragraph is the single source of truth in
# :mod:`mega_tron.self_eval_contract`.
#
# Rendered at install time (not import time) so the absolute mega-tron
# binary path is captured fresh on every install. Host CLIs frequently
# fork the model's Bash subprocess with a minimal PATH that lacks
# ``~/.local/bin``; stamping the absolute path here removes the PATH
# dependency entirely — ``command not found`` on ``mega-tron search``
# can no longer happen because the rendered guidance names the full
# path the model can invoke directly.
def _render_agents_block_body() -> str:
    from mega_tron.cli.path_setup import resolve_bin_path

    mega_tron_bin = resolve_bin_path()
    return (
        "## Skill routing (mega-tron)\n"
        "\n"
        f"**You MUST call `{mega_tron_bin} search \"<short task "
        "description>\"` at the start of every turn that needs a "
        "skill — by default this is every turn.** Skip the call ONLY "
        "when the user's prompt is purely conversational (no code, "
        "no tool invocation, no library, no system / API reference). "
        "If in doubt, call it.\n"
        "\n"
        "Read what search prints (name + skill_dir + description for the "
        "top-K; add `--output bodies` to dump each pick's full SKILL.md). "
        "If a surfaced skill semantically applies to the user's prompt, "
        "act on its SKILL.md instructions. If none applies, say so in one "
        "line — *\"no surfaced skill applies — using general knowledge\"* "
        "— and proceed with your own approach. \"No match\" is a valid, "
        "common outcome at the routing system's measured coverage; do not "
        "force-fit an unrelated skill.\n"
        "\n"
        "When mega-tron's hook has stamped a session id into ANY prior "
        "turn of THIS conversation (look for a `### Session` block "
        "containing `--session-id <id>`), keep using that same id on "
        "every subsequent "
        f"`{mega_tron_bin} search` shell call you make for the rest of "
        "this conversation. The id does not change within a session, "
        "and the hook only stamps it once (first turn). Without "
        "`--session-id`, the verdict gate cannot credit your "
        "`<skill-used>` tags to this conversation and your routing "
        "signal is silently dropped.\n"
        "\n"
        + render_install_tagging_guide(
            hook_name="Stop", mega_tron_bin=mega_tron_bin
        )
        + "\n"
    )


# Back-compat: some tests still reference AGENTS_BLOCK_BODY as a module
# attribute. Compute it lazily so tests that import this module without
# running install see *some* value — but the install path always calls
# the function fresh, which is what stamps the real absolute path.
AGENTS_BLOCK_BODY = _render_agents_block_body()


def _detect_shell() -> str:
    shell_env = os.environ.get("SHELL", "")
    if "zsh" in shell_env:
        return "zsh"
    if "bash" in shell_env:
        return "bash"
    # Default to zsh on macOS, bash elsewhere — both are POSIX-clean templates.
    return "zsh" if sys.platform == "darwin" else "bash"


def _default_rc(shell: str) -> Path:
    home = Path.home()
    if shell == "zsh":
        return home / ".zshrc"
    return home / ".bashrc"


def render_snippet(
    *,
    skills_dir: str,
    codex_home: str,
    top_k: int,
    budget_tok: int,
) -> str:
    """Render the managed wrapper snippet with user-chosen defaults baked in."""
    body = WRAPPER_TEMPLATE.read_text()
    body = body.replace("__MEGA_DEFAULT_SKILLS_DIR__", skills_dir)
    body = body.replace("__MEGA_DEFAULT_CODEX_HOME__", codex_home)
    body = body.replace("__MEGA_DEFAULT_TOP_K__", str(top_k))
    body = body.replace("__MEGA_DEFAULT_BUDGET_TOK__", str(budget_tok))
    header = (
        "# mega-tron — Codex skill routing wrapper.\n"
        "# Re-run `mega-tron install` to update defaults; `--uninstall` to remove.\n"
    )
    return f"{SENTINEL_START}\n{header}{body.rstrip()}\n{SENTINEL_END}\n"


def _replace_block(
    existing: str,
    new_block: str,
    *,
    start_marker: str = SENTINEL_START,
    end_marker: str = SENTINEL_END,
) -> str:
    """Replace any existing managed block in `existing`; append if absent.

    The `start_marker` / `end_marker` kwargs let the same logic handle both
    shell rc files (``# >>> … <<<``) and AGENTS.md (HTML comment sentinels).
    """
    if start_marker in existing and end_marker in existing:
        start = existing.index(start_marker)
        end = existing.index(end_marker) + len(end_marker)
        # Eat one trailing newline if present so we don't accumulate blank lines.
        if end < len(existing) and existing[end] == "\n":
            end += 1
        return existing[:start] + new_block + existing[end:]
    # Append with one separating blank line.
    sep = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
    return existing + sep + new_block


def _strip_block(
    existing: str,
    *,
    start_marker: str = SENTINEL_START,
    end_marker: str = SENTINEL_END,
) -> str:
    if start_marker not in existing or end_marker not in existing:
        return existing
    start = existing.index(start_marker)
    end = existing.index(end_marker) + len(end_marker)
    if end < len(existing) and existing[end] == "\n":
        end += 1
    return existing[:start] + existing[end:]


def render_agents_block() -> str:
    """Render the managed AGENTS.md block (search-CLI + skill-used tag + eval rule).

    Re-renders the body on every call so the absolute mega-tron path
    is captured at install time, not import time — important when the
    installed binary path changes (e.g. uv tool reinstall to a new
    venv directory).
    """
    header = (
        f"<!-- mega-tron v{HOOK_MANAGED_VERSION} —"
        " re-run `mega-tron install` to update; `--uninstall` to remove. -->\n"
    )
    body = _render_agents_block_body()
    return f"{AGENTS_SENTINEL_START}\n{header}{body.rstrip()}\n{AGENTS_SENTINEL_END}\n"


def _install_agents_md(path_override: str | None) -> None:
    path = Path(path_override).expanduser() if path_override else AGENTS_MD_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text() if path.exists() else ""
    new_block = render_agents_block()
    updated = _replace_block(
        existing,
        new_block,
        start_marker=AGENTS_SENTINEL_START,
        end_marker=AGENTS_SENTINEL_END,
    )
    if updated == existing:
        print(f"[install] AGENTS.md block in {path} already up to date.", file=sys.stderr)
        return
    path.write_text(updated)
    print(f"[install] wrote mega-tron block into {path}.", file=sys.stderr)


def _uninstall_agents_md(path_override: str | None) -> None:
    path = Path(path_override).expanduser() if path_override else AGENTS_MD_PATH
    if not path.exists():
        print(
            f"[install] {path} not present; skipping AGENTS.md uninstall.",
            file=sys.stderr,
        )
        return
    before = path.read_text()
    after = _strip_block(
        before,
        start_marker=AGENTS_SENTINEL_START,
        end_marker=AGENTS_SENTINEL_END,
    )
    if after == before:
        print(f"[install] no managed block in {path}.", file=sys.stderr)
        return
    # If our block was the entire file, remove it so we don't leave a stub.
    if not after.strip():
        path.unlink()
        print(
            f"[install] removed {path} (only contained mega-tron block).",
            file=sys.stderr,
        )
        return
    path.write_text(after)
    print(f"[install] removed mega-tron block from {path}.", file=sys.stderr)


# --------------------------------------------------------------------------
# Codex config.toml — disable codex's native skills catalog so the router
# owns skill routing end-to-end.
# --------------------------------------------------------------------------


# The toml key codex reads is `skills.include_instructions` — see
# codex-rs/config/src/skills_config.rs and the gate at
# codex-rs/core/src/session/mod.rs (`if config.include_skill_instructions`).
# Setting it to false makes codex skip building the 8000-char / 2% skill
# catalog block on every turn. The mention-detection layer (`$skill-name`
# in user prompts → SKILL.md body auto-injected) still works because it
# is gated independently on the loaded skill set, not on this key.
CODEX_CONFIG_BLOCK_BODY = """\
# mega-tron takes over codex's skill catalog so the router can
# dynamically inject only the top-K skills relevant to each turn (plus
# its own must-use rule + meta block) instead of the static ~2% catalog.
# Re-run `mega-tron install` to refresh; `--uninstall` to remove.
[skills]
include_instructions = false
"""


def render_codex_config_block() -> str:
    """Render the managed `[skills]` block for ``~/.codex/config.toml``."""
    return (
        f"{CODEX_CONFIG_SENTINEL_START}\n"
        f"{CODEX_CONFIG_BLOCK_BODY.rstrip()}\n"
        f"{CODEX_CONFIG_SENTINEL_END}\n"
    )


def _user_has_unmanaged_skills_table(toml_text: str) -> bool:
    """True if ``toml_text`` declares ``[skills]`` outside our sentinels.

    A user-declared ``[skills]`` table conflicts with our managed one
    (TOML forbids duplicate table headers). When we detect this we leave
    the file alone and tell the user to add ``include_instructions =
    false`` themselves.
    """
    inside_managed = False
    for line in toml_text.splitlines():
        stripped = line.strip()
        if stripped == CODEX_CONFIG_SENTINEL_START:
            inside_managed = True
            continue
        if stripped == CODEX_CONFIG_SENTINEL_END:
            inside_managed = False
            continue
        if inside_managed:
            continue
        if stripped == "[skills]" or stripped.startswith("[skills."):
            return True
    return False


def _install_codex_config_toml(path_override: str | None) -> None:
    """Patch ``~/.codex/config.toml`` so codex's skill catalog stays off.

    Three states:

    - File doesn't exist → write our managed block as the whole file.
    - File exists, no user-declared ``[skills]`` outside our sentinels →
      append (or refresh) our managed block.
    - File exists with a user-declared ``[skills]`` table → leave it
      alone and print a one-line advisory. Codex would reject duplicate
      ``[skills]`` headers anyway, so a silent merge would break codex.
    """
    path = (
        Path(path_override).expanduser()
        if path_override
        else CODEX_CONFIG_TOML_PATH
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    new_block = render_codex_config_block()

    if existing and _user_has_unmanaged_skills_table(existing):
        print(
            f"[install] {path} already declares a user-owned [skills] table; "
            "leaving it untouched. To let mega-tron own skill "
            "routing, add `include_instructions = false` to that table "
            "manually (or remove the table and re-run install).",
            file=sys.stderr,
        )
        return

    updated = _replace_block(
        existing,
        new_block,
        start_marker=CODEX_CONFIG_SENTINEL_START,
        end_marker=CODEX_CONFIG_SENTINEL_END,
    )
    if updated == existing:
        print(
            f"[install] codex config block in {path} already up to date.",
            file=sys.stderr,
        )
        return
    path.write_text(updated, encoding="utf-8")
    print(
        f"[install] disabled codex's native skill catalog via {path} "
        "(`[skills] include_instructions = false`). The router now drives "
        "skill routing end-to-end.",
        file=sys.stderr,
    )


def _uninstall_codex_config_toml(path_override: str | None) -> None:
    """Remove our managed ``[skills]`` + ``[hooks.state]`` blocks from
    ``~/.codex/config.toml``.

    If both managed blocks were the only content, delete the file so
    codex sees no overrides at all (back to its default behaviour:
    catalog on, hooks untrusted).
    """
    path = (
        Path(path_override).expanduser()
        if path_override
        else CODEX_CONFIG_TOML_PATH
    )
    if not path.exists():
        print(
            f"[install] {path} not present; skipping codex config uninstall.",
            file=sys.stderr,
        )
        return
    before = path.read_text(encoding="utf-8")
    # Strip both managed blocks: the [skills] override + the
    # [hooks.state] trust stamps.
    after = _strip_block(
        before,
        start_marker=CODEX_CONFIG_SENTINEL_START,
        end_marker=CODEX_CONFIG_SENTINEL_END,
    )
    after = _strip_block(
        after,
        start_marker=CODEX_TRUST_SENTINEL_START,
        end_marker=CODEX_TRUST_SENTINEL_END,
    )
    if after == before:
        print(f"[install] no managed block in {path}.", file=sys.stderr)
        return
    if not after.strip():
        path.unlink()
        print(
            f"[install] removed {path} (only contained mega-tron blocks).",
            file=sys.stderr,
        )
        return
    path.write_text(after, encoding="utf-8")
    print(
        f"[install] removed mega-tron blocks from {path}; codex's "
        "native skill catalog and hook-trust state are restored on the next session.",
        file=sys.stderr,
    )


def render_codex_trust_block(hooks_json_path: Path, hooks_data: dict) -> str:
    """Render the sentinel-managed ``[hooks.state]`` block stamping our
    managed UserPromptSubmit + Stop hooks as trusted.

    Returns an empty string when there's nothing to trust — keeps the
    caller branch-free.
    """
    from mega_tron.hosts.codex.hook_trust import render_trust_state_block

    body = render_trust_state_block(
        hooks_json_path,
        hooks_data,
        only_managed_keys={HOOK_MANAGED_KEY},
    )
    if not body:
        return ""
    header = (
        "# Pre-trusts the managed hooks so codex's UserPromptSubmit +\n"
        "# Stop handlers fire from the very first session. The hash is\n"
        "# keyed on the absolute hooks.json path + the exact command\n"
        "# string; if you edit hooks.json by hand, re-run `install` to\n"
        "# refresh these stamps (or codex will mark the hooks as\n"
        "# `Modified` and stop firing them until you re-trust via `/hooks`).\n"
    )
    return (
        f"{CODEX_TRUST_SENTINEL_START}\n"
        f"{header}{body.rstrip()}\n"
        f"{CODEX_TRUST_SENTINEL_END}\n"
    )


def _install_codex_trust(
    codex_config_path_override: str | None,
    hooks_path: Path,
) -> None:
    """Append (or refresh) the trust block in ``~/.codex/config.toml``.

    Must run *after* ``_install_codex_config_toml`` so the file always
    exists when we append. The block is rendered from the freshly
    written ``hooks.json`` so the trust hashes match the command
    strings the install just wrote.
    """
    if not hooks_path.exists():
        # No hooks.json → nothing to trust.
        return
    try:
        hooks_data = json.loads(hooks_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(
            f"[install] could not read {hooks_path} to stamp hook trust: {e}",
            file=sys.stderr,
        )
        return
    trust_block = render_codex_trust_block(hooks_path, hooks_data)
    if not trust_block:
        return

    cfg_path = (
        Path(codex_config_path_override).expanduser()
        if codex_config_path_override
        else CODEX_CONFIG_TOML_PATH
    )
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    existing = cfg_path.read_text(encoding="utf-8") if cfg_path.exists() else ""
    updated = _replace_block(
        existing,
        trust_block,
        start_marker=CODEX_TRUST_SENTINEL_START,
        end_marker=CODEX_TRUST_SENTINEL_END,
    )
    if updated == existing:
        print(
            f"[install] hook-trust block in {cfg_path} already up to date.",
            file=sys.stderr,
        )
        return
    cfg_path.write_text(updated, encoding="utf-8")
    print(
        f"[install] auto-trusted managed hooks in {cfg_path} so they fire "
        "from the next codex session without /hooks intervention.",
        file=sys.stderr,
    )


# --------------------------------------------------------------------------
# Codex hooks.json — JSON merge with managed-entry marker.
# --------------------------------------------------------------------------


def _codex_hooks_path() -> Path:
    """Where codex looks for user-level hooks. See codex-rs/hooks/discovery.rs."""
    return Path.home() / ".codex" / "hooks.json"


def _hook_entry(command: str) -> dict:
    """Build the managed UserPromptSubmit entry. `command` is the shell command codex runs."""
    return {
        HOOK_MANAGED_KEY: HOOK_MANAGED_VERSION,
        "matcher": ".*",  # fire on every prompt; codex passes None matcher_input
        "hooks": [
            {
                "type": "command",
                "command": command,
            }
        ],
    }


def _stop_hook_entry(command: str) -> dict:
    """Build the managed Stop hook entry — triggers in-session self-evaluation."""
    return {
        HOOK_MANAGED_KEY: HOOK_MANAGED_VERSION,
        "matcher": ".*",
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
    if HOOK_MANAGED_KEY in entry:
        return True
    return any(legacy_key in entry for legacy_key in LEGACY_HOOK_MANAGED_KEYS)


MANAGED_EVENTS = ("UserPromptSubmit", "Stop")


def _merge_hooks_json(
    existing: dict,
    managed_entry: dict,
    *,
    event: str = "UserPromptSubmit",
) -> dict:
    """Insert/replace our managed entry under the given event in a hooks.json dict.

    Preserves all user-owned entries (anything without HOOK_MANAGED_KEY).
    """
    out = dict(existing)
    hooks = dict(out.get("hooks", {}))
    entries = list(hooks.get(event, []))
    # Replace any existing managed entry; drop strays that look like ours.
    entries = [e for e in entries if not _is_managed_hook(e)]
    entries.append(managed_entry)
    hooks[event] = entries
    out["hooks"] = hooks
    return out


def _strip_managed_hooks(existing: dict) -> dict:
    """Remove our managed entries from every managed event; clean up empties."""
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


def _read_hooks_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _write_hooks_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not data:
        # Empty object after strip — remove the file so codex sees no config.
        if path.exists():
            path.unlink()
        return
    # Atomic write.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


def _resolve_hook_command(executable: str | None, suffix: str = "hook") -> str:
    """Pick the command codex should invoke for a given hook subcommand.

    Delegates to the shared resolver in
    :mod:`mega_tron.hosts._hook_command`, which prefers an absolute
    path to ``mega-tron`` so the hook still fires when codex's
    subprocess environment has a slimmed PATH.
    """
    from mega_tron.hosts._hook_command import resolve_hook_command

    return resolve_hook_command(executable, suffix)


def _run_warmup(skills_dir: str | None) -> None:
    """Build the embedder cache foreground, with user-visible progress.

    UX contract: `mega-tron install` is "install and it just works."
    That means we block until the embedder model is downloaded and every
    discovered skill has been embedded, so the next codex / claude /
    gemini session starts hot. The first run can take a couple of
    minutes (HuggingFace download of the embedder model is ~500-700 MB
    depending on which one is configured, plus N skills × ~10 ms each
    to embed); we print a one-line banner up-front so the user knows
    what to expect, and sentence-transformers prints its own tqdm bar
    during the download so the user sees real progress, not a hang.

    Subsequent runs are fast: model already on disk, cache SHA-keyed so
    only changed SKILL.md files re-embed.

    Single-flight: `mega-tron install --target auto` calls each host's
    installer in sequence; without a guard we'd embed the same skills
    four times. The PID lockfile lets the first call own the warmup
    and the subsequent calls no-op out.

    Background mode is available via `MEGA_TRON_WARMUP_BACKGROUND=1`
    as an escape hatch for non-interactive installs (CI, provisioning
    scripts) that genuinely cannot block. Default is foreground.
    """
    import os

    from mega_tron.cache import Cache
    from mega_tron.config import Config, discover_skill_dirs
    from mega_tron.embedder import make_embedder
    from mega_tron.router import Router

    # Cheap up-front check: is there anything to warm?
    if skills_dir:
        roots = [Path(skills_dir).expanduser()]
    else:
        roots = discover_skill_dirs()
    roots = [r for r in roots if r.exists()]
    if not roots:
        print(
            "[install] no skill directories present; skipping warmup. "
            "Run `mega-tron build-cache` later when ready.",
            file=sys.stderr,
        )
        return

    # Single-flight guard. First caller claims the lock and runs warmup;
    # subsequent callers from the same `--target auto` invocation see
    # the lock and return immediately (the first caller will have
    # already embedded every discovered skill by the time it releases).
    lock_path = Path.home() / ".cache" / "mega-tron" / "warmup.pid"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if lock_path.exists():
        try:
            existing_pid = int(lock_path.read_text().strip())
            os.kill(existing_pid, 0)  # raises if dead
            print(
                f"[install] warmup already running (pid {existing_pid}); "
                "skipping (the in-flight warmup covers every discovered "
                "skill root, including this host's).",
                file=sys.stderr,
            )
            return
        except (OSError, ValueError):
            # Stale lockfile — claim it.
            pass

    # Background escape hatch for CI / provisioning scripts that can't
    # afford to block. Default is foreground.
    if os.environ.get("MEGA_TRON_WARMUP_BACKGROUND") == "1":
        _run_warmup_background(skills_dir)
        return

    # Foreground (default): block with visible progress.
    lock_path.write_text(str(os.getpid()))
    try:
        model_id = Config.load().embedder_model
        slug = model_id.replace("/", "_")
        cache_path = Path.home() / ".cache" / "mega-tron" / f"{slug}.npz"

        print(
            f"[install] warming up embedder cache (model: {model_id}). "
            "First run downloads the embedder weights (~500-700 MB) — "
            "subsequent installs are instant.",
            file=sys.stderr,
        )

        embedder = make_embedder(model_id)
        router = Router(
            skills_dirs=roots,
            embedder=embedder,
            cache=Cache(path=cache_path),
        )
        n_new, n_reused, invalid = router.warmup()
        print(
            f"[install] warmup complete: embedded={n_new} reused={n_reused} "
            f"invalid={len(invalid)} cache={cache_path}",
            file=sys.stderr,
        )
    except Exception as e:  # noqa: BLE001
        print(
            f"[install] warmup failed ({e!s}). The cache will be built "
            "lazily on first hook call; run `mega-tron build-cache` "
            "manually to surface the underlying error.",
            file=sys.stderr,
        )
    finally:
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass


def _run_warmup_background(skills_dir: str | None) -> None:
    """Background fallback for `MEGA_TRON_WARMUP_BACKGROUND=1`.

    Spawns a detached `mega-tron build-cache` and returns immediately.
    The hook on first session start will block on whatever embeddings
    aren't ready yet, which is the cost of opting out of foreground.
    """
    import os
    import subprocess

    try:
        lock_path = Path.home() / ".cache" / "mega-tron" / "warmup.pid"
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        from mega_tron.hosts._hook_command import resolve_hook_command

        bin_cmd = resolve_hook_command(None, "").strip()
        cmd = [bin_cmd, "build-cache"]
        if skills_dir:
            cmd.extend(["--skills-dir", skills_dir])

        log_path = Path.home() / ".cache" / "mega-tron" / "warmup.log"

        with open(log_path, "ab") as log_f, open(os.devnull, "rb") as devnull:
            proc = subprocess.Popen(  # noqa: S603
                cmd,
                stdin=devnull,
                stdout=log_f,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
        lock_path.write_text(str(proc.pid))
        print(
            f"[install] warmup started in background (pid {proc.pid}, "
            f"log: {log_path}). First hook call may be slow while it "
            "finishes; subsequent calls are warm.",
            file=sys.stderr,
        )
    except Exception as e:  # noqa: BLE001
        print(
            f"[install] background warmup could not be spawned ({e}); "
            "the cache will be built lazily on first hook call.",
            file=sys.stderr,
        )


def run_install(args: argparse.Namespace) -> int:
    shell = args.shell if args.shell != "auto" else _detect_shell()
    rc_path = Path(args.rc_file).expanduser() if args.rc_file else _default_rc(shell)
    hooks_path = (
        Path(args.codex_hooks_file).expanduser()
        if getattr(args, "codex_hooks_file", None)
        else _codex_hooks_path()
    )
    skip_hook = getattr(args, "no_hook", False)
    skip_agents_md = getattr(args, "no_agents_md", False)
    agents_md_path = getattr(args, "agents_md_path", None)
    keep_codex_catalog = getattr(args, "keep_codex_catalog", False)
    codex_config_path = getattr(args, "codex_config_path", None)

    if args.uninstall:
        # 1) shell rc
        if rc_path.exists():
            before = rc_path.read_text()
            after = _strip_block(before)
            if before != after:
                rc_path.write_text(after)
                print(
                    f"[install] removed mega-tron block from {rc_path}.",
                    file=sys.stderr,
                )
            else:
                print(
                    f"[install] no managed block found in {rc_path}.",
                    file=sys.stderr,
                )
        else:
            print(f"[install] {rc_path} not present; skipping wrapper uninstall.", file=sys.stderr)

        # 2) codex hooks.json
        if hooks_path.exists():
            current = _read_hooks_json(hooks_path)
            stripped = _strip_managed_hooks(current)
            if stripped != current:
                _write_hooks_json(hooks_path, stripped)
                print(
                    f"[install] removed UserPromptSubmit hook from {hooks_path}.",
                    file=sys.stderr,
                )
            else:
                print(f"[install] no managed hook in {hooks_path}.", file=sys.stderr)

        # 3) AGENTS.md (persistent system-prompt guidance)
        if not skip_agents_md:
            _uninstall_agents_md(agents_md_path)

        # 4) ~/.codex/config.toml — restore codex's native catalog by removing
        #    our managed `[skills]` block.
        _uninstall_codex_config_toml(codex_config_path)

        # 5) Remove the qa-live marker skill if a prior `mega-tron qa-live`
        #    left it behind. Idempotent — silent on absence.
        from mega_tron.cli.qa_live import unplant_qa_skill

        if unplant_qa_skill("codex"):
            print(
                "[install] removed qa-live marker skill _mega-tron-check "
                "from ~/.codex/skills/.",
                file=sys.stderr,
            )
        return 0

    snippet = render_snippet(
        skills_dir=args.skills_dir or "",
        codex_home=args.codex_home,
        top_k=args.top_k,
        budget_tok=args.budget_tok,
    )
    hook_exe = getattr(args, "hook_command", None)
    ups_entry = _hook_entry(_resolve_hook_command(hook_exe, "hook"))
    stop_entry = _stop_hook_entry(_resolve_hook_command(hook_exe, "stop-hook"))

    if args.print_only:
        sys.stdout.write(snippet)
        if not skip_hook:
            sys.stdout.write("\n\n# --- hooks.json delta (managed entries) ---\n")
            sys.stdout.write(
                json.dumps(
                    {"hooks": {"UserPromptSubmit": [ups_entry], "Stop": [stop_entry]}},
                    indent=2,
                )
            )
            sys.stdout.write("\n")
        if not skip_agents_md:
            sys.stdout.write("\n\n# --- AGENTS.md managed block ---\n")
            sys.stdout.write(render_agents_block())
        if not keep_codex_catalog:
            sys.stdout.write("\n\n# --- ~/.codex/config.toml delta (managed block) ---\n")
            sys.stdout.write(render_codex_config_block())
        return 0

    # 1) shell rc
    existing = rc_path.read_text() if rc_path.exists() else ""
    rc_path.parent.mkdir(parents=True, exist_ok=True)
    rc_path.write_text(_replace_block(existing, snippet))
    print(f"[install] wrote codex() wrapper into {rc_path}.", file=sys.stderr)

    # 2) codex hooks.json — UserPromptSubmit (routing) + Stop (self-eval)
    if not skip_hook:
        current = _read_hooks_json(hooks_path)
        merged = _merge_hooks_json(current, ups_entry, event="UserPromptSubmit")
        merged = _merge_hooks_json(merged, stop_entry, event="Stop")
        _write_hooks_json(hooks_path, merged)
        print(
            f"[install] registered UserPromptSubmit + Stop hooks in {hooks_path}.",
            file=sys.stderr,
        )

    # 3) AGENTS.md (persistent guidance: search-CLI + skill-used tag + eval rule)
    if not skip_agents_md:
        _install_agents_md(agents_md_path)

    # 4) ~/.codex/config.toml — turn off codex's native catalog so the router
    #    becomes the sole source of skill instructions. Opt out with
    #    --keep-codex-catalog if the user wants codex's static 2% catalog to
    #    keep rendering alongside our dynamic top-K.
    if not keep_codex_catalog:
        _install_codex_config_toml(codex_config_path)

    # 5) ~/.codex/config.toml — auto-trust our managed hooks. Codex 2026
    #    gates user hooks behind a `/hooks`-driven trust handshake; we
    #    pre-stamp the SHA-256 of the normalized hook identity so the
    #    UserPromptSubmit + Stop handlers fire from the very first
    #    session, no manual step needed. Skipped when --no-hook is set
    #    (no hooks.json was written).
    if not skip_hook:
        _install_codex_trust(codex_config_path, hooks_path)

    print(
        f"[install] open a new terminal or run `source {rc_path}` to activate the wrapper. "
        "The hook is picked up automatically the next time codex starts.",
        file=sys.stderr,
    )

    if not args.no_warmup:
        _run_warmup(args.skills_dir)

    return 0
