"""`mega-tron build-cache` — populate the embedding cache.

Internal subcommand invoked by the install-time wrapper / hooks (not
the typical interactive surface). Calls Router.warmup(), which embeds
every SKILL.md that isn't already cached and reports
embedded/reused/invalid counts.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mega_tron.cli._common import _make_router


def cmd_build_cache(args: argparse.Namespace) -> int:
    # `mega-tron install` spawns this command as a detached background
    # process and writes its PID to a lockfile so subsequent installs in
    # the same `--target auto` run don't duplicate the work. Clean the
    # lockfile up on exit (success or failure) so a *future* install can
    # claim a fresh warmup if needed.
    lock_path = Path.home() / ".cache" / "mega-tron" / "warmup.pid"
    try:
        router = _make_router(args)
        n_new, n_reused, invalid = router.warmup()
        print(
            f"[build-cache] embedded={n_new} reused={n_reused} invalid={len(invalid)} "
            f"cache={router.cache.path}",
            file=sys.stderr,
        )
        for err in invalid:
            print(f"  invalid: {err}", file=sys.stderr)
        return 0
    finally:
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass
