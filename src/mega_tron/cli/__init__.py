"""mega-tron CLI — thin re-export hub.

Subcommand implementations live in per-file modules under this package:
build_cache, search, install, hooks, evaluate, daemon, why,
dirs, skills, embedder_cmd, stats, maintenance, verdicts. Shared
helpers live in :mod:`mega_tron.cli._common`; the argparse wiring +
``main()`` entry point lives in :mod:`mega_tron.cli.parser`.

The re-exports here exist so external callers (and tests) that did
``from mega_tron.cli import cmd_install, _legacy_megaoptimus_files, …``
keep working — this surface is the package's public API.

The ``_LEGACY_CACHE_WARNED`` flag is the single source of truth in
:mod:`mega_tron.cli._common`. Read access from ``mega_tron.cli``
goes through :func:`__getattr__` so tests still see the live value.
Reset between tests by assigning to ``cli._common._LEGACY_CACHE_WARNED``
(see ``tests/test_legacy_cache_warning.py``).
"""
from __future__ import annotations

from mega_tron.cli import _common
from mega_tron.cli._common import (
    Config,
    _add_cache_path,
    _build_agentic,
    _default_cache_path,
    _embedder_slug,
    _make_router,
    _maybe_warn_legacy_cache,
    _resolve_cache_path,
    _resolve_mode,
    _resolve_skills_dirs,
)
from mega_tron.cli.build_cache import cmd_build_cache
from mega_tron.cli.daemon import cmd_daemon
from mega_tron.cli.dirs import cmd_dirs
from mega_tron.cli.embedder_cmd import cmd_embedder
from mega_tron.cli.evaluate import cmd_evaluate
from mega_tron.cli.hooks import (
    cmd_claude_hook,
    cmd_claude_stop_hook,
    cmd_gemini_hook,
    cmd_gemini_stop_hook,
    cmd_hook,
    cmd_stop_hook,
)
from mega_tron.cli.install import (
    _LEGACY_MARKERS,
    _install_targets,
    _legacy_megaoptimus_files,
    cmd_install,
)
from mega_tron.cli.maintenance import (
    cmd_compact_embeddings,
    cmd_export_frontmatter,
    cmd_migrate_to_sqlite,
)
from mega_tron.cli.parser import main
from mega_tron.cli.search import cmd_search
from mega_tron.cli.skills import cmd_skills
from mega_tron.cli.stats import cmd_stats
from mega_tron.cli.verdicts import cmd_regressions, cmd_search_verdicts
from mega_tron.cli.why import cmd_why


def __getattr__(name: str):
    """Proxy mutable state from _common so tests that do
    ``mega_tron.cli._LEGACY_CACHE_WARNED = False`` land on the single source.
    """
    if name == "_LEGACY_CACHE_WARNED":
        return _common._LEGACY_CACHE_WARNED
    raise AttributeError(f"module 'mega_tron.cli' has no attribute {name!r}")


__all__ = [
    # entry point
    "main",
    # 22 cmd_* subcommands
    "cmd_build_cache",
    "cmd_claude_hook",
    "cmd_claude_stop_hook",
    "cmd_compact_embeddings",
    "cmd_daemon",
    "cmd_dirs",
    "cmd_embedder",
    "cmd_evaluate",
    "cmd_export_frontmatter",
    "cmd_gemini_hook",
    "cmd_gemini_stop_hook",
    "cmd_hook",
    "cmd_install",
    "cmd_migrate_to_sqlite",
    "cmd_regressions",
    "cmd_search",
    "cmd_search_verdicts",
    "cmd_skills",
    "cmd_stats",
    "cmd_stop_hook",
    "cmd_why",
    # public helpers (referenced by tests + scripts)
    "Config",
    "_add_cache_path",
    "_build_agentic",
    "_default_cache_path",
    "_embedder_slug",
    "_install_targets",
    "_legacy_megaoptimus_files",
    "_make_router",
    "_maybe_warn_legacy_cache",
    "_resolve_cache_path",
    "_resolve_mode",
    "_resolve_skills_dirs",
    "_LEGACY_MARKERS",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
