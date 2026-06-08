"""`mega-tron upgrade` — refresh the wheel AND every running process.

Why this command exists: `uv tool install --force --reinstall` only
replaces the wheel on disk. Any mega-tron Python process that's already
running (daemon, dashboard, sometimes a long-lived hook) keeps the OLD
wheel loaded in memory until it exits on its own. The classic failure
mode (reported in the field 2026-05):

  - User's reverse-proxy (Traefik / nginx) is wired to a dashboard the
    user previously launched with ``--host 172.18.0.1 --port 7531 &``.
    The dashboard process has the old wheel in RAM.
  - Agent runs ``uv tool install --force --reinstall`` (new wheel on
    disk) and then ``mega-tron setup`` (which only spawns the *daemon*,
    not the dashboard).
  - Net result: ``mega-tron --version`` reports the new build, but the
    dashboard the user sees through their public URL is the *old* build,
    because the old dashboard process is still serving on 172.18.0.1
    and never died.
  - Agent reads ``ss -tlnp``, sees two listeners, and can't tell which
    one the user actually wants — defers the decision back to the user,
    who guesses wrong and kills the new build.

``mega-tron upgrade`` collapses the whole sequence into one command:

  1. Detect every running daemon / dashboard process AND record its
     bind args (--host / --port for dashboard; socket path for daemon).
  2. Refresh the wheel (`uv tool install --force --reinstall ...`).
  3. Gracefully stop the recorded processes (SIGTERM, then SIGKILL
     after a grace window if needed).
  4. Re-spawn the dashboard with the SAME bind args using the new
     binary, so any reverse proxy in front of it keeps working. The
     daemon is re-spawned by `mega-tron setup` (the normal warm-up
     path) so we don't duplicate that logic here.
  5. Re-run `mega-tron setup` non-interactively, inferring the
     embedder profile + Claude native mode from the user's existing
     config + shell rc (the same inference the agent-installation
     guide tells the agent to do — `mega-tron upgrade` is the
     codified version of that).

Best-effort throughout. A failure in step (1)/(3)/(4) prints a clear
message but doesn't block the remaining steps — the worst case is the
user has to kill an old process by hand, which is exactly the
pre-upgrade status quo.
"""
from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

# Dashboard / daemon process discovery lives in a shared module so qa-live
# can reuse it without pulling in the kill/respawn machinery below. These
# names are re-exported (imported but not all used directly here) for
# backward compat: tests + this module import them from
# ``mega_tron.cli.upgrade``.
from mega_tron.cli.dashboard_procs import (  # noqa: F401
    _RunningProc,
    _read_cmdline,
    _classify,
    _parse_dashboard_args,
    _discover_running,
)


# How long we wait for a TERM'd process to exit before sending KILL.
_TERM_GRACE_S = 4.0
# How long we wait for a freshly-spawned dashboard to start serving.
_DASHBOARD_BOOT_S = 5.0


# --------------------------------------------------------------------------- #
# Process discovery
# --------------------------------------------------------------------------- #
#
# These helpers were moved to ``dashboard_procs`` so ``qa-live`` can reuse
# them without importing the kill/respawn machinery below. Re-exported here
# under their original names for backward compat (tests + this module's own
# code import them from ``mega_tron.cli.upgrade``).
from mega_tron.cli.dashboard_procs import (  # noqa: E402
    _RunningProc,
    _read_cmdline,
    _classify,
    _parse_dashboard_args,
    _discover_running,
)


# --------------------------------------------------------------------------- #
# Kill + respawn
# --------------------------------------------------------------------------- #


def _graceful_kill(pid: int) -> bool:
    """SIGTERM, wait up to _TERM_GRACE_S, then SIGKILL if still alive.
    Returns True if the process is gone by the end."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False

    deadline = time.monotonic() + _TERM_GRACE_S
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)  # signal 0 = "are you alive?"
        except ProcessLookupError:
            return True
        time.sleep(0.1)

    # Still alive — force.
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    # Give the OS one breath to reap.
    time.sleep(0.2)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def _respawn_dashboard(proc: _RunningProc, mega_tron_bin: str) -> int | None:
    """Spawn a new ``mega-tron dashboard`` with ``proc``'s bind args.
    Returns the child PID, or None if the spawn failed.
    """
    cmd = [mega_tron_bin, "dashboard"]
    if proc.host is not None:
        cmd += ["--host", proc.host]
    if proc.port is not None:
        cmd += ["--port", str(proc.port)]
    # An auto-respawned dashboard should never pop a browser tab —
    # this is an upgrade, not a first install. Force --no-open even if
    # the original invocation didn't pass it (the user already has the
    # dashboard URL in front of them; they don't need a new tab).
    if "--no-open" not in cmd:
        cmd.append("--no-open")

    try:
        child = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except (OSError, FileNotFoundError) as e:
        print(
            f"[upgrade] could not respawn dashboard ({e}); "
            f"start it manually:\n  {' '.join(cmd)}",
            file=sys.stderr,
        )
        return None

    # Confirm it's actually listening on the requested port (if any) —
    # if it crashed on boot the user would otherwise discover that the
    # next time they reload the page.
    if proc.port:
        bind_host = proc.host or "127.0.0.1"
        deadline = time.monotonic() + _DASHBOARD_BOOT_S
        import socket as _socket
        while time.monotonic() < deadline:
            try:
                with _socket.create_connection((bind_host, proc.port), timeout=0.5):
                    return child.pid
            except OSError:
                time.sleep(0.2)
        # Didn't come up in time. Don't kill it — could be slow first
        # boot — but warn the user so they're not surprised.
        print(
            f"[upgrade] dashboard pid {child.pid} did not answer on "
            f"{bind_host}:{proc.port} within {_DASHBOARD_BOOT_S:.0f}s. "
            f"Check ``ps -p {child.pid}`` if the URL stays dead.",
            file=sys.stderr,
        )
    return child.pid


# --------------------------------------------------------------------------- #
# Wheel refresh
# --------------------------------------------------------------------------- #


def _refresh_wheel(source: str | None) -> bool:
    """Invoke ``uv tool install --force --reinstall``. Returns True on
    success. ``source`` is either a local path (preferred when the user
    has a clone) or None for PyPI.
    """
    if shutil.which("uv") is None:
        print(
            "[upgrade] `uv` not found on PATH — cannot refresh the wheel.",
            file=sys.stderr,
        )
        return False

    cmd = ["uv", "tool", "install", "--force", "--reinstall"]
    if source:
        cmd += ["--from", source, "mega-tron"]
    else:
        cmd += ["mega-tron"]

    print(f"[upgrade] refreshing wheel: {' '.join(cmd)}", file=sys.stderr)
    try:
        # Pass through stdout/stderr so the user sees uv's progress.
        rc = subprocess.call(cmd)
    except OSError as e:
        print(f"[upgrade] uv invocation failed: {e}", file=sys.stderr)
        return False
    return rc == 0


def _guess_source() -> str | None:
    """Find a likely git-clone location of mega-tron to install from.

    PyPI isn't published yet (as of 2026-05) so most installs go from a
    local clone. We look in the common spots; if none have a
    ``pyproject.toml`` that names mega-tron, return None and let the
    caller fall back to PyPI / a fresh clone.
    """
    candidates = [
        Path("/tmp/mega-tron"),
        Path.home() / "mega-tron",
        Path.home() / "code" / "mega-tron",
        Path.home() / "src" / "mega-tron",
        Path.home() / "Downloads" / "mega-tron",
        Path.cwd(),
    ]
    for c in candidates:
        py = c / "pyproject.toml"
        if py.exists():
            try:
                text = py.read_text()
            except OSError:
                continue
            if "mega-tron" in text or "mega_tron" in text:
                return str(c)
    return None


def _fresh_clone_to_tmp() -> str | None:
    """Last-resort: clone the repo into /tmp/mega-tron and return that
    path. Matches the fallback described in docs/agent installation.md
    section 3a."""
    if shutil.which("git") is None:
        return None
    dest = Path("/tmp/mega-tron")
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    try:
        rc = subprocess.call(
            ["git", "clone", "https://github.com/mega-edo/mega-tron.git", str(dest)],
            stdout=subprocess.DEVNULL,
        )
    except OSError:
        return None
    return str(dest) if rc == 0 else None


# --------------------------------------------------------------------------- #
# Inferred-args setup
# --------------------------------------------------------------------------- #


def _infer_profile() -> str | None:
    """Read ~/.config/mega-tron/config.toml and map embedder_model →
    one of {multilingual, en-quality, en-fast}. Returns None when the
    file is absent — caller falls back to the CLI's own picker."""
    cfg = Path.home() / ".config" / "mega-tron" / "config.toml"
    if not cfg.exists():
        return None
    try:
        text = cfg.read_text()
    except OSError:
        return None
    # We don't want a hard tomllib dep here — a substring match on the
    # model name is plenty (the config file is shallow and human-edited).
    if "BAAI/bge-m3" in text:
        return "multilingual"
    if "BAAI/bge-small-en-v1.5" in text:
        return "en-fast"
    if "SKILLRET" in text or "skillret" in text:
        return "en-quality"
    return None


def _infer_claude_mode() -> str | None:
    """Grep the user's shell rc for ``MEGA_CLAUDE_NATIVE_MODE=...``.
    Returns ``passive`` when no export exists (matches the CLI default),
    or the mode otherwise. Returns None only if both rc files are
    unreadable."""
    rcs = [Path.home() / ".zshrc", Path.home() / ".bashrc"]
    saw_a_file = False
    for rc in rcs:
        if not rc.exists():
            continue
        try:
            text = rc.read_text()
        except OSError:
            continue
        saw_a_file = True
        for line in text.splitlines():
            stripped = line.strip()
            if "MEGA_CLAUDE_NATIVE_MODE" not in stripped:
                continue
            # Tolerate "export MEGA_CLAUDE_NATIVE_MODE=active" and
            # "MEGA_CLAUDE_NATIVE_MODE=active" both.
            if "=" not in stripped:
                continue
            val = stripped.split("=", 1)[1].strip().strip('"').strip("'")
            # Inline comments after the value.
            val = val.split("#", 1)[0].strip()
            if val in ("passive", "active", "strict"):
                return val
    return "passive" if saw_a_file else None


def _run_setup(mega_tron_bin: str) -> int:
    """Run ``mega-tron setup`` non-interactively with inferred args.

    If we can't infer one of the flags we omit it — ``setup`` then
    falls back to its own default (passive for native mode; the
    interactive picker for profile, which we suppress via
    MEGA_TRON_NONINTERACTIVE).
    """
    profile = _infer_profile()
    mode = _infer_claude_mode()

    cmd = [mega_tron_bin, "setup"]
    if profile:
        cmd += ["--profile", profile]
    if mode:
        cmd += ["--claude-native-mode", mode]

    env = os.environ.copy()
    env["MEGA_TRON_NONINTERACTIVE"] = "1"

    print(f"[upgrade] re-running setup: {' '.join(cmd)}", file=sys.stderr)
    try:
        return subprocess.call(cmd, env=env)
    except OSError as e:
        print(f"[upgrade] setup invocation failed: {e}", file=sys.stderr)
        return 1


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def cmd_upgrade(args: argparse.Namespace) -> int:
    """Refresh the wheel + restart every running mega-tron process +
    re-run setup. One-stop upgrade.

    Exit codes:
        0  — everything succeeded
        1  — wheel refresh failed (nothing else attempted)
        2  — wheel refreshed but at least one process couldn't be
             restarted; user should restart it manually (the printed
             message names the pid + bind args)
    """
    # -- Step 1: discover running processes ---------------------------------
    running = _discover_running()
    if running:
        print(
            f"[upgrade] found {len(running)} running mega-tron "
            f"process(es); will restart them with the new wheel.",
            file=sys.stderr,
        )
        for p in running:
            print(
                f"  - pid {p.pid:>6}  {p.kind:<10}  "
                f"{('host=' + (p.host or '-') + ' port=' + str(p.port or '-')) if p.kind == 'dashboard' else ''}",
                file=sys.stderr,
            )
    else:
        print(
            "[upgrade] no running mega-tron processes detected — "
            "wheel refresh only.",
            file=sys.stderr,
        )

    # -- Step 2: refresh wheel ----------------------------------------------
    source = args.source or _guess_source()
    if not source and not args.from_pypi:
        # No local clone. Try PyPI first (it's a no-op when mega-tron
        # isn't published, and uv will fall through to the next path).
        # If PyPI doesn't have it, fresh-clone to /tmp/mega-tron.
        print(
            "[upgrade] no local clone found; trying PyPI, then a "
            "fresh /tmp/mega-tron clone as fallback.",
            file=sys.stderr,
        )
        if not _refresh_wheel(None):
            cloned = _fresh_clone_to_tmp()
            if not cloned or not _refresh_wheel(cloned):
                print(
                    "[upgrade] wheel refresh failed (PyPI + fresh "
                    "clone both unavailable). Aborting.",
                    file=sys.stderr,
                )
                return 1
    else:
        if not _refresh_wheel(source):
            print(
                f"[upgrade] wheel refresh from {source or 'PyPI'} "
                "failed. Aborting before any process is touched.",
                file=sys.stderr,
            )
            return 1

    # Resolve the new binary path. `uv tool install` puts it back in
    # the same place; if for some reason it's not on PATH any more,
    # we let `which` decide and fall through to a sensible default.
    mega_tron_bin = shutil.which("mega-tron") or str(
        Path.home() / ".local" / "bin" / "mega-tron"
    )

    # -- Step 3: kill the old processes -------------------------------------
    failed_kills: list[_RunningProc] = []
    for p in running:
        if not _graceful_kill(p.pid):
            failed_kills.append(p)

    if failed_kills:
        for p in failed_kills:
            print(
                f"[upgrade] could not stop pid {p.pid} ({p.kind}); "
                "you may need to kill it manually as the process owner.",
                file=sys.stderr,
            )

    # -- Step 4: respawn dashboards -----------------------------------------
    failed_spawn = 0
    for p in running:
        if p.kind != "dashboard":
            continue
        if p in failed_kills:
            print(
                f"[upgrade] skipping respawn for pid {p.pid} — old "
                "process is still alive on the port.",
                file=sys.stderr,
            )
            failed_spawn += 1
            continue
        new_pid = _respawn_dashboard(p, mega_tron_bin)
        if new_pid is None:
            failed_spawn += 1
        else:
            bind = (
                f" on {p.host or '127.0.0.1'}:{p.port or 7531}"
                if p.host or p.port else ""
            )
            print(
                f"[upgrade] respawned dashboard{bind} as pid {new_pid}.",
                file=sys.stderr,
            )

    # The daemon doesn't need an explicit respawn — `mega-tron setup`
    # (step 5) auto-spawns one on a cache miss, and the hook layer will
    # pull it in too on the next host turn.

    # -- Step 5: re-run setup -----------------------------------------------
    setup_rc = _run_setup(mega_tron_bin)

    if failed_kills or failed_spawn:
        return 2
    if setup_rc != 0:
        print(
            f"[upgrade] setup returned rc={setup_rc}; wheel and "
            "process restart succeeded but inspect the log above.",
            file=sys.stderr,
        )
        return 2
    print("[upgrade] done.", file=sys.stderr)
    return 0
