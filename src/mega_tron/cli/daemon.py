"""`mega-tron daemon` — long-lived router daemon controls."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def cmd_daemon(args: argparse.Namespace) -> int:
    """Manage the long-lived router daemon (A1).

    Subcommands:
      - serve  : run the daemon in the foreground (or detached via spawn_detached)
      - status : print whether a daemon is currently reachable
      - stop   : ask the running daemon to exit
    """
    from mega_tron import daemon as daemon_mod

    op = args.daemon_op
    if op == "serve":
        return daemon_mod.serve(
            socket_path=(Path(args.socket) if args.socket else None),
            idle_timeout_s=args.idle_timeout,
        )
    if op == "status":
        path = Path(args.socket) if args.socket else daemon_mod.default_socket_path()
        running = daemon_mod.is_running(path)
        msg = "running" if running else "not running"
        print(f"socket={path}  status={msg}")
        return 0 if running else 1
    if op == "stop":
        path = Path(args.socket) if args.socket else daemon_mod.default_socket_path()
        ok = daemon_mod.request_shutdown(path)
        print("shutdown ok" if ok else "no running daemon")
        return 0 if ok else 1
    print(f"[daemon] unknown op {op!r}", file=sys.stderr)
    return 2
