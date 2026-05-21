"""Resolve the `mega-tron` command string that host hooks should invoke.

Every host adapter (Codex, Claude Code, Gemini CLI, Hermes) writes a hook
entry into the host's config file (`hooks.json` / `settings.json` /
`config.yaml`) whose command string ultimately re-launches `mega-tron`.
Naively writing `"mega-tron <suffix>"` assumes the entry point is on
`PATH` when the host's hook runner forks the subprocess — which is only
true for users who ran `uv tool install` or `pip install --user` into a
PATH-visible location. Users who installed via `uv pip install -e .`
inside a project venv have the binary only under `.venv/bin/`, where
host subprocesses cannot find it. The hook then silently fails with
`command not found` and the user sees no routing at all.

This module centralises the resolution so all four host installers go
through one well-tested path:

1. If the user passed `--hook-command <path>`, honour that verbatim.
2. Otherwise run `shutil.which("mega-tron")` to find the entry point
   on whatever PATH `install` itself is running under (this catches
   both `uv tool install` and project-venv `uv pip install -e .` since
   the venv's `bin/` is on PATH inside `uv run`).
3. If `which` returns nothing, look for `mega-tron` next to the
   current Python interpreter (`sys.executable`). This covers the rare
   case where install is launched from a venv whose `bin/` isn't on
   PATH.
4. Fall back to bare `mega-tron` and print a warning so the user
   knows their hooks may not fire.

The absolute-path form is preferred over bare `mega-tron` even when the
binary IS on PATH, because host subprocess environments can have a
slimmed-down PATH that omits the user's shell PATH augmentations.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path


def resolve_hook_command(
    executable: str | None,
    suffix: str,
    *,
    binary_name: str = "mega-tron",
) -> str:
    """Build the full hook command string for a host config entry.

    Parameters
    ----------
    executable:
        User-supplied override (typically from `--hook-command`).
        If truthy, used verbatim and the suffix appended.
    suffix:
        Subcommand to append, e.g. ``"hook"``, ``"gemini-hook"``,
        ``"claude-stop-hook"``. Whitespace-trimmed.
    binary_name:
        Defaults to ``"mega-tron"``. Override only for testing.

    Returns
    -------
    A single shell-runnable command string. Always absolute-path
    when resolution succeeded, falling back to bare ``mega-tron`` with
    a stderr warning if no path could be found.
    """
    suffix = suffix.strip()
    if executable:
        return f"{executable} {suffix}".strip()

    # 1. PATH lookup — works for `uv tool install` (global) and for
    #    project-venv installs as long as install ran inside the venv.
    found = shutil.which(binary_name)
    if found:
        return f"{found} {suffix}".strip()

    # 2. Sibling of current interpreter — covers venvs whose bin/ is
    #    not on PATH at install time. Do NOT `.resolve()` here: uv-managed
    #    venvs symlink `.venv/bin/python` to a hidden interpreter under
    #    `~/.local/share/uv/python/.../bin/`, and resolving the symlink
    #    jumps out of the venv where the entry-point script does not exist.
    #    We want the venv's own `bin/` directory, which is the lexical
    #    parent of `sys.executable` before any symlink resolution.
    sibling = Path(sys.executable).parent / binary_name
    if sibling.exists() and sibling.is_file():
        return f"{sibling} {suffix}".strip()

    # 3. Last-resort fallback. Warn loudly so the user can fix it
    #    before relying on routing.
    print(
        f"[install] warning: could not locate {binary_name!r} on PATH or "
        f"next to {sys.executable}. Hook entries will use bare "
        f"{binary_name!r} and may fail at runtime. Install with "
        f"`uv tool install .` to put {binary_name!r} on the system "
        f"PATH, or pass `--hook-command </abs/path/to/{binary_name}>` "
        f"to `mega-tron install`.",
        file=sys.stderr,
    )
    return f"{binary_name} {suffix}".strip()


__all__ = ["resolve_hook_command"]
