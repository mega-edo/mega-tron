"""Shared discovery of running mega-tron daemon / dashboard processes.

Both ``mega-tron upgrade`` (kill + respawn with the same bind args) and
``mega-tron qa-live`` (skip the dashboard spawn when one is already up)
need to answer the same two questions:

  1. Is a mega-tron dashboard / daemon already running, and on what
     bind args?  →  :func:`_discover_running`
  2. Is a TCP port already bound on this host?  →  :func:`_port_is_bound`

These were originally private helpers inside ``upgrade.py``; they were
lifted here verbatim so qa-live can reuse them without importing the
whole upgrade module (and the kill/respawn machinery that comes with
it). ``upgrade.py`` re-exports the same names for backward compat.

Pure + stdlib-only: no mega-tron imports, so there is no circular-import
risk and either consumer can import this module freely. macOS + Linux
both supported (see :func:`_read_cmdline` for the per-platform argv read).
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


# --------------------------------------------------------------------------- #
# Process discovery
# --------------------------------------------------------------------------- #


@dataclass
class _RunningProc:
    """One mega-tron process we found on the host."""

    pid: int
    kind: str  # "dashboard" | "daemon" | "other"
    argv: list[str] = field(default_factory=list)
    # Extracted bind args for dashboard. Empty for daemons.
    host: str | None = None
    port: int | None = None
    no_open: bool = False


def _read_cmdline(pid: int) -> list[str]:
    """Return argv for ``pid``. Linux uses /proc; macOS falls back to ps."""
    proc_path = Path(f"/proc/{pid}/cmdline")
    if proc_path.exists():
        try:
            raw = proc_path.read_bytes()
        except OSError:
            return []
        # /proc/<pid>/cmdline is NUL-separated, trailing NUL.
        parts = [p.decode("utf-8", "replace") for p in raw.split(b"\x00") if p]
        return parts

    # macOS / non-Linux: shell out to `ps -o command=` and split on
    # whitespace. This is lossy for arguments containing spaces, which
    # is acceptable — dashboard / daemon args here are simple flag +
    # value pairs (--host 127.0.0.1, --port 7531, --no-open).
    try:
        out = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "command="],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
        ).strip()
    except (subprocess.SubprocessError, FileNotFoundError):
        return []
    return out.split() if out else []


def _classify(argv: list[str]) -> str:
    """Return the subcommand identifier for an argv we recognize.

    Matches the subcommand as a WHOLE argv token, not a substring: an
    unrelated process whose command line merely contains the word (e.g.
    ``python -c "... _launch_dashboard_detached() ..."`` or
    ``mega-tron why "dashboard setup"``) must NOT be misclassified as a
    running dashboard — that would make qa-live wrongly skip its spawn.
    """
    if not argv:
        return "other"
    # ``mega-tron dashboard`` / ``python -m mega_tron.cli dashboard`` both
    # surface "dashboard" as a standalone token; a substring like
    # "_launch_dashboard_detached" does not.
    if "dashboard" in argv:
        return "dashboard"
    if "daemon" in argv:
        return "daemon"
    return "other"


def _parse_dashboard_args(argv: list[str]) -> tuple[str | None, int | None, bool]:
    """Pull ``--host``, ``--port``, ``--no-open`` out of an argv list.

    Handles both ``--host=X`` and ``--host X`` shapes. Returns
    ``(host, port, no_open)`` with None for un-passed flags so the
    caller can default to mega-tron's own defaults when respawning.
    """
    host: str | None = None
    port: int | None = None
    no_open = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--host="):
            host = a.split("=", 1)[1]
        elif a == "--host" and i + 1 < len(argv):
            host = argv[i + 1]
            i += 1
        elif a.startswith("--port="):
            try:
                port = int(a.split("=", 1)[1])
            except ValueError:
                pass
        elif a == "--port" and i + 1 < len(argv):
            try:
                port = int(argv[i + 1])
            except ValueError:
                pass
            i += 1
        elif a == "--no-open":
            no_open = True
        i += 1
    return host, port, no_open


def _discover_running() -> list[_RunningProc]:
    """Return every mega-tron daemon / dashboard process currently on
    this machine that we can confidently identify. Uses ``pgrep -af``
    when available (Linux + macOS); silently returns [] if it isn't —
    in that case the caller just skips whatever it would do with the
    result (kill-and-respawn for upgrade, skip-spawn for qa-live).
    """
    if shutil.which("pgrep") is None:
        return []
    try:
        # -a (or -lf on BSD pgrep) prints the full command; we use -f
        # to match the whole argv string (so `python -m mega_tron.cli
        # daemon serve` matches even though argv[0] is "python").
        out = subprocess.check_output(
            ["pgrep", "-af", "mega.tron|mega_tron"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
    except subprocess.SubprocessError:
        return []

    procs: list[_RunningProc] = []
    self_pid = os.getpid()
    parent_pid = os.getppid()
    for line in out.splitlines():
        parts = line.strip().split(maxsplit=1)
        if not parts:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if pid in (self_pid, parent_pid):
            # Skip the calling process itself + its shell parent — pgrep
            # would otherwise catch us through our own `mega-tron ...`
            # invocation and (for upgrade) we'd kill ourselves mid-run.
            continue
        argv = _read_cmdline(pid)
        if not argv:
            # Process might have just exited between pgrep and our
            # /proc read. Drop it.
            continue
        kind = _classify(argv)
        if kind == "other":
            continue
        host = port = None
        no_open = False
        if kind == "dashboard":
            host, port, no_open = _parse_dashboard_args(argv)
        procs.append(
            _RunningProc(
                pid=pid,
                kind=kind,
                argv=argv,
                host=host,
                port=port,
                no_open=no_open,
            )
        )
    return procs


# --------------------------------------------------------------------------- #
# Port probing
# --------------------------------------------------------------------------- #


def _port_is_bound(
    port: int, hosts: tuple[str, ...] = ("127.0.0.1",)
) -> str | None:
    """Return the first ``host`` where ``port`` answers a TCP connect,
    else ``None``.

    Uses ``connect_ex`` (not a bind probe): a connect to ``127.0.0.1``
    sees loopback listeners AND ``0.0.0.0`` listeners, which is exactly
    the secondary case we want to catch (a non-mega-tron process, or a
    mega-tron one whose argv we couldn't parse, already holding the
    port). It deliberately does NOT see an interface-specific listener
    like ``172.18.0.1:7531`` — that scenario is the job of
    :func:`_discover_running`, which finds the dashboard process
    regardless of its bind address. The two checks are complementary.
    """
    for host in hosts:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.3)
        try:
            if sock.connect_ex((host, port)) == 0:
                return host
        except OSError:
            pass
        finally:
            sock.close()
    return None


__all__ = [
    "_RunningProc",
    "_read_cmdline",
    "_classify",
    "_parse_dashboard_args",
    "_discover_running",
    "_port_is_bound",
]
