"""Package entry point so ``python -m mega_tron.cli`` works.

:func:`mega_tron.daemon.spawn_detached` and any external orchestrator
(launchd, systemd-user, Task Scheduler) expect to invoke the CLI as
a module, not via the ``mega-tron`` shell shim — the latter is a
trampoline that only exists when ``uv tool install`` set it up, but
``python -m mega_tron.cli`` works as long as the package is on
``sys.path``. Without a ``__main__.py`` here, ``-m mega_tron.cli``
raises ``No module named mega_tron.cli.__main__`` and the daemon
detach fails silently (DEVNULL stderr) on every spawn.
"""
from __future__ import annotations

import sys

from mega_tron.cli.parser import main


if __name__ == "__main__":
    sys.exit(main())
