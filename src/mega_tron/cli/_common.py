"""Shared helpers used across mega-tron CLI subcommand modules.

This module owns the small utilities every subcommand needs: cache-path
resolution, skill-root discovery, Router construction, and the one-shot
legacy-cache migration warning. It also re-exports a few common imports
(Config, etc.) so subcommand files have a single import target.

The module-level ``_LEGACY_CACHE_WARNED`` flag is the single source of
truth for "have we already printed the legacy-cache warning this
process". ``mega_tron.cli.__init__`` proxies attribute access to this
name so tests can still do ``mega_tron.cli._LEGACY_CACHE_WARNED = False``
to reset between cases.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from mega_tron.cache import Cache
from mega_tron.config import Config, discover_skill_dirs
from mega_tron.embedder import make_embedder
from mega_tron.router import Router

__all__ = [
    "Cache",
    "Config",
    "Router",
    "_LEGACY_CACHE_WARNED",
    "_add_cache_path",
    "_build_agentic",
    "_default_cache_path",
    "_embedder_slug",
    "_make_router",
    "_maybe_warn_legacy_cache",
    "_resolve_cache_path",
    "_resolve_mode",
    "_resolve_skills_dirs",
    "discover_skill_dirs",
    "make_embedder",
]


def _embedder_slug(model_id: str) -> str:
    """Filesystem-safe slug for the cache filename.

    ``ThakiCloud/SKILLRET-Embedding-0.6B`` → ``ThakiCloud_SKILLRET-Embedding-0.6B``.
    We just swap path separators and keep the rest — the slug is opaque,
    and the full ``fingerprint`` (model_id + weight hash) lives inside
    the .npz itself for invalidation.
    """
    return model_id.replace("/", "_").replace(os.sep, "_")


_LEGACY_CACHE_WARNED = False


def _maybe_warn_legacy_cache() -> None:
    """One-shot stderr warning when only the legacy ``~/.cache/mega-optimus/``
    cache exists and the current ``~/.cache/mega-tron/`` directory has
    not been populated yet. Users following the migration steps see
    this once, rebuild the cache, and the warning never fires again.

    Idempotent: the module-level ``_LEGACY_CACHE_WARNED`` flag ensures
    a single process can fire the warning at most once even when
    multiple CLI subcommands share the helper.
    """
    global _LEGACY_CACHE_WARNED
    if _LEGACY_CACHE_WARNED:
        return
    new = Path.home() / ".cache" / "mega-tron"
    legacy = Path.home() / ".cache" / "mega-optimus"
    if not legacy.is_dir() or new.is_dir():
        return
    _LEGACY_CACHE_WARNED = True
    print(
        f"warning: legacy mega-optimus cache found at {legacy}.\n"
        "  mega-tron uses a fresh cache and will not reuse the legacy "
        "embeddings. Run `mega-tron build-cache` to populate the new "
        "cache. The legacy directory is safe to delete.",
        file=sys.stderr,
    )


def _default_cache_path(model_id: str | None = None) -> Path:
    """Default ``.npz`` path. Keyed on the embedder model id so swapping
    models never accidentally reuses a stale cache."""
    if model_id is None:
        model_id = Config.load().embedder_model
    return Path.home() / ".cache" / "mega-tron" / f"{_embedder_slug(model_id)}.npz"


def _resolve_cache_path(args: argparse.Namespace, model_id: str | None = None) -> Path:
    """``--cache-path`` wins; otherwise default keyed on the embedder model id."""
    _maybe_warn_legacy_cache()
    sub = getattr(args, "sub_cache_path", None)
    if sub:
        return Path(sub)
    return _default_cache_path(model_id)


def _resolve_skills_dirs(args: argparse.Namespace) -> list[Path]:
    """Build the effective list of skill roots for this invocation.

    - ``--skills-dir <path[,path,...]>`` passed: overrides auto-discovery
      — only the explicitly listed paths are searched. Use this for CI /
      one-shot scripts that need a known fixture without leaking through
      the user's global skill collections.
    - Flag omitted: fall back to :func:`discover_skill_dirs`, which
      unions the standard locations (``~/.claude/skills``,
      ``~/.codex/skills``, ``$CODEX_HOME/skills``) with any paths the
      user has registered via ``mega-tron dirs add``.
    """
    raw = getattr(args, "skills_dir", None)
    if raw:
        cli_dirs = [Path(p).expanduser() for p in str(raw).split(",") if p.strip()]
        seen: set[Path] = set()
        out: list[Path] = []
        for p in cli_dirs:
            try:
                resolved = p.resolve(strict=False)
            except OSError:
                resolved = p
            if resolved in seen:
                continue
            seen.add(resolved)
            out.append(p)
        return out
    return discover_skill_dirs()


def _make_router(args: argparse.Namespace) -> Router:
    """Build a Router using the configured embedder + auto-discovered dirs."""
    model_id = Config.load().embedder_model
    embedder = make_embedder(model_id)
    cache = Cache(path=_resolve_cache_path(args, model_id))
    use_eval = None  # let env decide
    if getattr(args, "no_eval_blend", False):
        use_eval = False
    return Router(
        skills_dirs=_resolve_skills_dirs(args),
        embedder=embedder,
        cache=cache,
        use_eval=use_eval,
    )


def _resolve_mode(args: argparse.Namespace) -> str:
    """Pick the effective search mode for :func:`cmd_search`."""
    from mega_tron._env import resolve_mode
    return resolve_mode(getattr(args, "mode", None))


def _build_agentic(args: argparse.Namespace, mode: str):
    """Build :class:`AgenticSearch` when ``--mode agentic`` is requested.

    Returns ``None`` for semantic mode or when backend init fails (the
    Router then falls through to pure cosine + eval-blend).
    """
    if mode != "agentic":
        return None
    try:
        from mega_tron.agentic import AgenticSearch
        from mega_tron.llm_backends import make_llm_backend

        return AgenticSearch(
            backend=make_llm_backend(),
            top=getattr(args, "prefilter", None),
            shortlist=getattr(args, "shortlist", None),
            top_k=getattr(args, "top_k", None),
            max_reads=getattr(args, "read_max", None),
            timeout_s=getattr(args, "timeout_s", None),
        )
    except Exception as e:  # noqa: BLE001
        print(f"[agentic] init failed, falling back to cosine: {e}", file=sys.stderr)
        return None


def _add_cache_path(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--cache-path",
        dest="sub_cache_path",
        default=None,
        help=f"Embedding cache file (default: {_default_cache_path()}).",
    )
