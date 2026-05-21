"""`mega-tron dashboard` — launch the local HTTP observability UI."""
from __future__ import annotations

import argparse
import sys


def cmd_dashboard(args: argparse.Namespace) -> int:
    """Launch the local HTTP dashboard.

    Loopback-bound by default (``127.0.0.1``); pass ``--host 0.0.0.0``
    only if you have a deliberate reason (the dashboard has no auth,
    so any bind beyond loopback exposes verdict-edit endpoints).

    Imports are deferred to the function body so the rest of the CLI
    isn't penalised by ``http.server`` / ``threading`` import cost on
    every invocation.
    """
    from mega_tron.dashboard.server import serve

    if not getattr(args, "no_open", False):
        # Best-effort: never let a webbrowser problem (no GUI, sandbox,
        # SSH session) block ``serve_forever`` — log and move on.
        try:
            import webbrowser

            webbrowser.open(f"http://{args.host}:{args.port}/")
        except Exception as exc:  # noqa: BLE001
            print(
                f"[mega-tron dashboard] webbrowser.open skipped: {exc}",
                file=sys.stderr,
            )

    serve(args.host, args.port)
    return 0
