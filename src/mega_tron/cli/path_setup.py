"""PATH auto-update so `mega-tron` resolves after `uv tool install`.

Why this exists
===============

``uv tool install mega-tron`` drops the binary into
``~/.local/bin/mega-tron`` (Linux/macOS) or
``%APPDATA%/uv/tools/.../bin`` (Windows). On a fresh macOS / Linux system
that directory is *not* on ``$PATH`` by default — the user has to either
run ``uv tool update-shell`` or hand-edit their rc files. README readers
who skip that step see ``zsh: command not found: mega-tron`` immediately
after install and rate the project ``broken`` before anything has had a
chance to fail on its own merits.

We refuse to ship that UX. ``mega-tron install`` calls
:func:`ensure_on_path` first thing, which:

1. Detects the directory containing the ``mega-tron`` binary the user
   just invoked (the absolute path of ``sys.argv[0]``).
2. Checks whether that directory is already on the user's effective
   ``$PATH``. If yes → no-op.
3. If not, writes a sentinel-bracketed ``export PATH=…`` block into
   the user's shell config (``~/.zshenv`` for zsh, ``~/.bashrc`` for
   bash, ``~/.config/fish/conf.d/mega-tron.fish`` for fish).
4. Reports back to stderr so the user knows a new shell is needed.

Idempotent: re-running mutates the same sentinel block instead of
duplicating it. ``--uninstall`` paths call :func:`remove_from_path` to
strip the block cleanly — sentinel-bracketed so we only touch lines we
own; user-added PATH entries in the same file are untouched.

zshenv is chosen over zshrc because it's read by *every* zsh entry
point (login, interactive, non-interactive scripts), so the hook
subprocess that codex/claude/gemini spawn — which is non-interactive —
still resolves ``mega-tron``. zshrc would leave non-interactive
contexts broken.
"""
from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


SENTINEL_START = "# >>> mega-tron PATH (managed; edit between sentinels at your own risk) >>>"
SENTINEL_END = "# <<< mega-tron PATH <<<"
FISH_SENTINEL_START = "# >>> mega-tron PATH (managed) >>>"
FISH_SENTINEL_END = "# <<< mega-tron PATH <<<"


@dataclass(frozen=True)
class PathSetupResult:
    """Outcome of an :func:`ensure_on_path` call.

    Attributes:
        already_on_path: ``True`` when no edit was needed.
        rc_file: the shell config file we wrote to (or would have).
        bin_dir: the directory containing the ``mega-tron`` binary.
        wrote: ``True`` when we mutated ``rc_file``.
    """

    already_on_path: bool
    rc_file: Path
    bin_dir: Path
    wrote: bool


def resolve_bin_dir() -> Path | None:
    """Find the directory containing the currently-running mega-tron binary.

    Resolution order:
      1. ``sys.argv[0]`` if it looks like a real path (covers the
         ``uv tool``-installed entrypoint, which calls Python via a
         wrapper script at ``~/.local/bin/mega-tron``).
      2. ``shutil.which("mega-tron")`` fallback (covers test harnesses
         that import the CLI module without going through the wrapper).

    Returns ``None`` only when we genuinely can't locate ourselves —
    callers should treat that as "skip PATH auto-update, the user
    invoked us through a non-standard path."
    """
    candidate = sys.argv[0] if sys.argv else ""
    if candidate and candidate not in ("", "-c"):
        p = Path(candidate)
        # Resolve symlinks but only the *immediate* one — `uv tool` ships
        # a symlink farm under ~/.local/bin pointing at a per-tool venv;
        # we want the symlink's directory (which is on $PATH after a
        # shell update), not the venv-internal target.
        if p.exists() and p.is_absolute():
            return p.parent
        if p.exists():
            return p.resolve().parent
    via_which = shutil.which("mega-tron")
    if via_which:
        return Path(via_which).parent
    return None


def resolve_bin_path() -> str:
    """Return the absolute path to the mega-tron binary, as it should
    appear inside install-time managed memory blocks (CLAUDE.md /
    AGENTS.md / GEMINI.md).

    Resolution order:
      1. :func:`resolve_bin_dir` joined with ``mega-tron`` — preferred
         since it follows the running process's own provenance.
      2. ``shutil.which("mega-tron")`` — fallback when resolve_bin_dir
         can't pin it down (test harnesses calling the API directly).
      3. The literal string ``mega-tron`` — last-resort fallback when
         we genuinely cannot locate ourselves. This degrades to the
         old PATH-dependent behaviour rather than blocking install.

    Why we need this: when a host CLI (Claude Code, Codex, Gemini)
    forks the model's Bash subprocess, that subshell may not source
    the user's interactive rc and so misses ``~/.local/bin`` on PATH.
    The model then runs ``mega-tron search ...`` and gets
    ``command not found``. Stamping the absolute path into the
    managed memory blocks removes the PATH dependency entirely —
    the model sees the full path and invokes it directly.
    """
    bin_dir = resolve_bin_dir()
    if bin_dir is not None:
        candidate = bin_dir / "mega-tron"
        if candidate.exists():
            return str(candidate)
    via_which = shutil.which("mega-tron")
    if via_which:
        return via_which
    return "mega-tron"


def _path_contains(bin_dir: Path, path_env: str) -> bool:
    """Is ``bin_dir`` already on ``$PATH``? Resolves symlinks on both
    sides so ``/Users/x/.local/bin`` matches even if ``$PATH`` has the
    canonical ``/private/Users/x/.local/bin`` form (macOS).
    """
    try:
        target = bin_dir.resolve()
    except OSError:
        target = bin_dir
    for entry in path_env.split(os.pathsep):
        if not entry:
            continue
        try:
            resolved = Path(entry).expanduser().resolve()
        except OSError:
            continue
        if resolved == target:
            return True
    return False


def _detect_shell() -> str:
    """Return ``"zsh"``, ``"bash"``, ``"fish"``, or ``"sh"``.

    Same heuristic as :mod:`mega_tron.hosts.codex.install._detect_shell`
    but we keep them separate so the host adapter and the PATH helper
    can evolve independently.
    """
    shell_env = os.environ.get("SHELL", "")
    if "zsh" in shell_env:
        return "zsh"
    if "fish" in shell_env:
        return "fish"
    if "bash" in shell_env:
        return "bash"
    return "zsh" if sys.platform == "darwin" else "bash"


def _rc_path_for(shell: str) -> Path:
    """The file we write the managed PATH export into.

    zshenv is picked over zshrc deliberately — see the module docstring.
    bashrc / fish conf.d follow each shell's idiomatic non-interactive
    config location.
    """
    home = Path.home()
    if shell == "zsh":
        return home / ".zshenv"
    if shell == "fish":
        return home / ".config" / "fish" / "conf.d" / "mega-tron.fish"
    return home / ".bashrc"


def _render_block(bin_dir: Path, shell: str) -> str:
    """Build the sentinel-bracketed PATH export block."""
    bin_str = str(bin_dir)
    header = (
        "# Auto-added by `mega-tron install` so the binary resolves in\n"
        "# every shell (including hook subprocesses). Safe to delete\n"
        "# manually — re-running `mega-tron install` re-adds it.\n"
    )
    if shell == "fish":
        body = f'fish_add_path --path --prepend "{bin_str}"\n'
        return f"{FISH_SENTINEL_START}\n{header}{body}{FISH_SENTINEL_END}\n"
    # zsh + bash: POSIX-clean conditional export so re-sourcing is a no-op
    # if the directory is already present.
    body = (
        f'case ":$PATH:" in\n'
        f'    *":{bin_str}:"*) ;;\n'
        f'    *) export PATH="{bin_str}:$PATH" ;;\n'
        f'esac\n'
    )
    return f"{SENTINEL_START}\n{header}{body}{SENTINEL_END}\n"


def _replace_block(existing: str, new_block: str, start: str, end: str) -> str:
    """Replace any existing managed PATH block; append when absent."""
    if start in existing and end in existing:
        s = existing.index(start)
        e = existing.index(end) + len(end)
        if e < len(existing) and existing[e] == "\n":
            e += 1
        return existing[:s] + new_block + existing[e:]
    sep = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
    return (existing or "") + sep + new_block


def _strip_block(existing: str, start: str, end: str) -> str:
    if start not in existing or end not in existing:
        return existing
    s = existing.index(start)
    e = existing.index(end) + len(end)
    if e < len(existing) and existing[e] == "\n":
        e += 1
    return existing[:s] + existing[e:]


def ensure_on_path(*, print_to=sys.stderr) -> PathSetupResult:
    """Add the mega-tron binary directory to the user's shell PATH if
    it isn't already there. Safe to call on every ``mega-tron install``
    invocation — idempotent."""
    bin_dir = resolve_bin_dir()
    if bin_dir is None:
        # We couldn't even find ourselves; nothing safe to do here.
        return PathSetupResult(
            already_on_path=True,
            rc_file=Path("/dev/null"),
            bin_dir=Path("/dev/null"),
            wrote=False,
        )

    shell = _detect_shell()
    rc_path = _rc_path_for(shell)
    path_env = os.environ.get("PATH", "")

    if _path_contains(bin_dir, path_env):
        return PathSetupResult(
            already_on_path=True,
            rc_file=rc_path,
            bin_dir=bin_dir,
            wrote=False,
        )

    start = FISH_SENTINEL_START if shell == "fish" else SENTINEL_START
    end = FISH_SENTINEL_END if shell == "fish" else SENTINEL_END
    block = _render_block(bin_dir, shell)
    existing = rc_path.read_text(encoding="utf-8") if rc_path.exists() else ""
    if start in existing and block.strip() in existing:
        # Same block already on file — current shell session just hasn't
        # re-sourced yet. No write needed.
        return PathSetupResult(
            already_on_path=False,
            rc_file=rc_path,
            bin_dir=bin_dir,
            wrote=False,
        )

    rc_path.parent.mkdir(parents=True, exist_ok=True)
    rc_path.write_text(_replace_block(existing, block, start, end), encoding="utf-8")

    if print_to is not None:
        print(
            f"[install] added {bin_dir} to PATH via {rc_path}.\n"
            f"          Run `exec $SHELL` or open a new terminal so "
            f"`mega-tron` resolves everywhere (including hook subprocesses).",
            file=print_to,
        )
    return PathSetupResult(
        already_on_path=False,
        rc_file=rc_path,
        bin_dir=bin_dir,
        wrote=True,
    )


def remove_from_path(*, print_to=sys.stderr) -> bool:
    """Strip the managed PATH block from the user's shell config.

    Called from ``mega-tron install --uninstall``. Touches only the
    sentinel-bracketed block we own; any user-authored PATH entries in
    the same file stay intact.

    Returns ``True`` when a block was found and removed.
    """
    shell = _detect_shell()
    rc_path = _rc_path_for(shell)
    if not rc_path.exists():
        return False
    existing = rc_path.read_text(encoding="utf-8")
    start = FISH_SENTINEL_START if shell == "fish" else SENTINEL_START
    end = FISH_SENTINEL_END if shell == "fish" else SENTINEL_END
    if start not in existing:
        return False
    rc_path.write_text(_strip_block(existing, start, end), encoding="utf-8")
    if print_to is not None:
        print(
            f"[install] removed managed PATH block from {rc_path}.",
            file=print_to,
        )
    return True


__all__ = [
    "PathSetupResult",
    "SENTINEL_START",
    "SENTINEL_END",
    "ensure_on_path",
    "remove_from_path",
    "resolve_bin_dir",
]
