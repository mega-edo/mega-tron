"""Per-user config and skill-directory discovery.

- Skill directories are auto-discovered from the standard locations
  (``~/.claude/skills``, ``~/.codex/skills``, ``$CODEX_HOME/skills``)
  and unioned with any directories the user has registered via
  ``mega-tron dirs add <path>``. Registered dirs persist in
  ``~/.config/mega-tron/config.toml`` (or
  ``$XDG_CONFIG_HOME``).
- The default embedder is :data:`DEFAULT_EMBEDDER_MODEL`
  (SkillRet-Embedding-0.6B). Users can swap to any sentence-transformers
  model via ``[embedder] model = "..."`` in the config, with no code
  changes.

Why not env-vars only: a config file gives the CLI ``dirs add/remove``
something durable to mutate, and lets users keep an audit of registered
roots. ``MEGA_SKILL_DIRS`` (colon-separated) is honoured as a runtime
override so CI and one-shot scripts don't need to mutate the file.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover — older Python fallback
    import tomli as tomllib  # type: ignore


# ---------------------------------------------------------------------------
# Defaults — change these to flip the system-wide default.
# ---------------------------------------------------------------------------

DEFAULT_EMBEDDER_MODEL = "BAAI/bge-m3"
"""Sentence-transformers model id for the default embedder.

BGE-M3 (BAAI, XLM-RoBERTa-large backbone, 568M params) hits the
sweet spot for a default — multilingual (100+ languages, Korean /
Hindi / Chinese / Japanese all first-class), fast on CPU (~440
embed/min in our 100-skill benchmark), MIT-licensed, and 27M+
downloads of in-the-wild validation. Dense embedding only (we don't
use BGE-M3's sparse / ColBERT modes).

MTEB multilingual ~63. Slightly below Qwen3-0.6B (64.33) but ~22x
faster on CPU because XLM-R is encoder-only and well-optimized in
sentence-transformers.

Alternatives users can swap in:
  - Multilingual + accuracy:  Qwen/Qwen3-Embedding-0.6B (slower)
  - English-only + accuracy:  ThakiCloud/SKILLRET-Embedding-0.6B
  - Fastest tiny multilingual: intfloat/multilingual-e5-small (118M)

`mega-tron setup` offers three pre-tuned profiles via an interactive
prompt (or the ``--profile {en-quality,en-fast,multilingual}`` flag
for non-interactive installs):

  - en-quality   → ThakiCloud/SKILLRET-Embedding-0.6B  (best F1, slower)
  - en-fast      → BAAI/bge-small-en-v1.5              (near-best F1, fastest)
  - multilingual → BAAI/bge-m3                         (this default)

Swap any time with: `mega-tron embedder set <huggingface-model-id>`
"""

DEFAULT_PREFILTER = 50
"""Cosine top-N pulled into the eval-blend reranker (or returned directly
when no skill carries ``mega_meta`` evidence)."""

DEFAULT_TOP_K = 5
"""Final returned skill count for ``mega-tron search``."""


# ---------------------------------------------------------------------------
# Standard, hard-coded skill-directory locations.
# ---------------------------------------------------------------------------


def _standard_skill_dirs() -> list[Path]:
    """The fixed list of well-known skill directories, regardless of whether
    they exist on disk. Filtering to existing dirs is the caller's job —
    callers that want to ``dirs add`` an empty future location can still
    do so without pre-creating the folder.

    Order is significant: earlier entries are scanned first, and when a
    skill ``name:`` field collides across dirs the *first* directory's
    SKILL.md wins (see :func:`mega_tron.router.load_skills`).

    ``.../skills/.system`` is the cache codex itself unpacks its bundled
    sample skills into (``codex-rs/skills/src/lib.rs:system_cache_root_dir``).
    We include it after the user's own ``~/.codex/skills`` so a custom
    skill always shadows the bundled equivalent.
    """
    out: list[Path] = []
    home = Path.home()
    out.append(home / ".claude" / "skills")
    out.append(home / ".codex" / "skills")
    out.append(home / ".gemini" / "skills")
    out.append(home / ".hermes" / "skills")
    # Host-neutral agent skills convention. Multiple agent tools (Ruflo,
    # autonomous-agents, etc.) share this slot; mega-tron surfaces it as
    # a peer of the per-host slots above so a skill installed once at
    # ``~/.agents/skills/`` is visible to every host the router serves.
    out.append(home / ".agents" / "skills")
    # NOTE: ``~/.local/share/mega-code/skills`` is intentionally NOT in
    # this list. That directory is owned by the MEGA-Code wisdom
    # gateway (see :mod:`mega_tron.wisdom`) and is gated behind
    # ``MEGA_WITH_WISDOM=1`` — opt-in via :func:`_wisdom_skill_dirs`
    # rather than the always-on default discovery.
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        out.append(Path(codex_home).expanduser() / "skills")
    # Codex bundled / system skills cache. Listed last so user customizations
    # win on `name:` collision (Router.load_skills is first-dir-wins).
    out.append(home / ".codex" / "skills" / ".system")
    if codex_home:
        out.append(Path(codex_home).expanduser() / "skills" / ".system")
    return out


def _claude_plugin_skill_dirs() -> list[Path]:
    """Find skill roots inside the Claude Code plugin tree.

    Claude Code stores plugins in TWO trees, both of which can carry
    routable skills:

    1. ``~/.claude/plugins/marketplaces/<m>/{plugins|external_plugins}/<p>/skills/``
       — the marketplace mirror. Some plugins keep their full package
       (agents/, commands/, hooks/, skills/) directly under the
       marketplace path.

    2. ``~/.claude/plugins/cache/<m>/<p>/<version>/skills/`` — the
       installed-package cache. Other plugins (and the official
       Claude superpowers tree) only carry their actual code under
       this versioned path; the marketplace entry alongside it is
       just a manifest stub. ``installed_plugins.json`` records the
       *active* version per plugin — we read it so we only surface
       the version Claude Code itself is loading at runtime, never
       stale older versions that happen to be left on disk.

    Falling back gracefully matters: if the manifest is missing or
    malformed (fresh install, hand-edited file, schema change), we
    skip the cache leg entirely rather than guessing. The marketplace
    leg always runs so users still see hookify / frontend-design /
    etc. even when the cache tree can't be read.
    """
    out: list[Path] = []
    plugins_root = Path.home() / ".claude" / "plugins"
    if not plugins_root.is_dir():
        return out

    # --- Leg 1: marketplace mirror -----------------------------------
    marketplaces = plugins_root / "marketplaces"
    if marketplaces.is_dir():
        try:
            market_iter = sorted(marketplaces.iterdir())
        except OSError:
            market_iter = []
        for marketplace in market_iter:
            if not marketplace.is_dir():
                continue
            # Both ``plugins/`` and ``external_plugins/`` are first-class
            # plugin containers — checked separately so a marketplace
            # that exposes only one of them is still picked up.
            for container_name in ("plugins", "external_plugins"):
                container = marketplace / container_name
                if not container.is_dir():
                    continue
                try:
                    plugin_iter = sorted(container.iterdir())
                except OSError:
                    continue
                for plugin in plugin_iter:
                    skills = plugin / "skills"
                    if skills.is_dir():
                        out.append(skills)

    # --- Leg 2: installed-package cache (versioned, manifest-pinned) -
    manifest = plugins_root / "installed_plugins.json"
    if manifest.is_file():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            entries = (data or {}).get("plugins") or {}
        except (OSError, json.JSONDecodeError):
            entries = {}
        # Each plugin maps to a list of install records (Claude allows
        # multiple scopes — user, project, etc.). We surface every
        # install_path that has a skills/ subdir, deduped.
        seen: set[Path] = set(out)
        for records in entries.values():
            if not isinstance(records, list):
                continue
            for rec in records:
                if not isinstance(rec, dict):
                    continue
                install_path = rec.get("installPath")
                if not isinstance(install_path, str) or not install_path:
                    continue
                skills = Path(install_path) / "skills"
                # Resolve to a stable form for dedup against the
                # marketplace leg; some marketplaces symlink cache
                # contents through `marketplaces/.../plugins/<name>/`.
                try:
                    resolved = skills.resolve()
                except OSError:
                    resolved = skills
                if skills.is_dir() and resolved not in seen:
                    seen.add(resolved)
                    out.append(skills)

    return out


def _codex_plugin_skill_dirs() -> list[Path]:
    """Find skill roots inside the Codex CLI plugin cache.

    Codex stores its installed plugins under
    ``~/.codex/plugins/cache/<marketplace>/<plugin>/<commit-hash>/skills/``;
    the ``<commit-hash>`` directory holds the actual checkout for the
    pinned plugin version. We pick the most-recent commit-hash per
    plugin so a user with multiple cached versions only gets the live
    one into the routing pool.
    """
    out: list[Path] = []
    plugin_root = Path.home() / ".codex" / "plugins" / "cache"
    if not plugin_root.is_dir():
        return out
    try:
        market_iter = sorted(plugin_root.iterdir())
    except OSError:
        return out
    for marketplace in market_iter:
        if not marketplace.is_dir():
            continue
        try:
            plugin_iter = sorted(marketplace.iterdir())
        except OSError:
            continue
        for plugin in plugin_iter:
            if not plugin.is_dir():
                continue
            # Pick the newest commit-hash dir (mtime). Older snapshots
            # are intentionally skipped — they would race name-wise
            # against the live install for no benefit.
            try:
                hash_dirs = [h for h in plugin.iterdir() if h.is_dir()]
            except OSError:
                continue
            if not hash_dirs:
                continue
            hash_dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            skills = hash_dirs[0] / "skills"
            if skills.is_dir():
                out.append(skills)
    return out


def _gemini_plugin_skill_dirs() -> list[Path]:
    """Find skill roots inside the Gemini CLI plugin tree.

    Gemini does not currently expose a plugin marketplace with skill
    catalogs (Antigravity transition is in flight); this function is
    a forward-looking stub so the dashboard can attribute per-host
    plugin contributions uniformly when Gemini adds the feature.
    Returns an empty list today.
    """
    return []


# ---------------------------------------------------------------------------
# Config file — paths, schema, IO.
# ---------------------------------------------------------------------------


def config_dir() -> Path:
    """``$XDG_CONFIG_HOME/mega-tron`` (falls back to
    ``~/.config/mega-tron``)."""
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg) if xdg else (Path.home() / ".config")
    return base / "mega-tron"


def config_path() -> Path:
    return config_dir() / "config.toml"


# ---------------------------------------------------------------------------
# SQLite store path (Phase 2+).
# ---------------------------------------------------------------------------


def data_dir() -> Path:
    """``$XDG_DATA_HOME/mega-tron`` (falls back to
    ``~/.local/share/mega-tron``).

    Distinct from :func:`config_dir` because the SQLite store is *data*,
    not configuration. Following XDG keeps backups, syncs, and dotfile
    tooling working out of the box.
    """
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(xdg) if xdg else (Path.home() / ".local" / "share")
    return base / "mega-tron"


def store_path() -> Path:
    """Resolve the SQLite verdict-store path.

    Resolution order:
      1. ``MEGA_TRON_STORE`` env (CI/tests use this to keep store
         files isolated per process; library users overriding the store
         location at runtime should set this before constructing
         :class:`MegaCore`).
      2. :func:`data_dir` / ``store.db`` (the default).

    The directory is *not* auto-created here — callers (the
    :class:`Store` constructor, in particular) materialise it lazily so
    a read-only inspection of the path stays side-effect free.
    """
    raw = os.environ.get("MEGA_TRON_STORE", "").strip()
    if raw:
        return Path(raw).expanduser()
    return data_dir() / "store.db"


@dataclass
class Config:
    """User-overridable config. Persists to :func:`config_path`."""

    embedder_model: str = DEFAULT_EMBEDDER_MODEL
    extra_skill_dirs: list[Path] = field(default_factory=list)

    @classmethod
    def load(cls) -> "Config":
        """Read ``config.toml``. Missing file → all defaults."""
        path = config_path()
        if not path.exists():
            return cls()
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            return cls()
        emb = data.get("embedder", {}) or {}
        skills = data.get("skills", {}) or {}
        return cls(
            embedder_model=str(emb.get("model") or DEFAULT_EMBEDDER_MODEL),
            extra_skill_dirs=[
                Path(str(d)).expanduser()
                for d in (skills.get("extra_dirs") or [])
                if isinstance(d, (str, Path))
            ],
        )

    def save(self) -> None:
        """Write back as TOML — minimal hand-rolled emitter so we don't
        pull tomli-w as a new dep. Schema is small and stable."""
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        lines: list[str] = [
            "# mega-tron user config.",
            "# Edit by hand or via `mega-tron dirs add/remove` and",
            "# `mega-tron embedder set <hf-model-id>`.",
            "",
            "[embedder]",
            f'model = "{self.embedder_model}"',
            "",
            "[skills]",
            "extra_dirs = [",
        ]
        for d in self.extra_skill_dirs:
            lines.append(f'    "{d}",')
        lines.append("]")
        lines.append("")
        path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# Skill-directory discovery — the public API consumers use.
# ---------------------------------------------------------------------------


def env_extra_dirs() -> list[Path]:
    """Colon-separated ``MEGA_SKILL_DIRS`` (runtime override). Empty when
    unset."""
    raw = os.environ.get("MEGA_SKILL_DIRS", "").strip()
    if not raw:
        return []
    return [Path(p).expanduser() for p in raw.split(":") if p.strip()]


def _wisdom_skill_dirs() -> list[Path]:
    """MEGA-Code wisdom-gateway skill cache, included only when the user
    has opted in via ``MEGA_WITH_WISDOM=1``.

    The wisdom-curator (see :mod:`mega_tron.wisdom`) downloads
    gateway-curated skills into :data:`mega_tron.wisdom.WISDOM_SKILLS_DIR`.
    Auto-discovering that root makes those skills participate in the
    cosine prefilter / eval-blend rerank like any other skill source.

    Lowest-priority placement (called after every other source in
    :func:`discover_skill_dirs`) ensures local skills shadow external
    wisdom skills on ``name:`` collision — the user's own
    ``webhook-signer`` always wins over a wisdom-cached variant.
    """
    # Avoid an import cycle at module load: wisdom imports nothing from
    # config, but config touching wisdom directly would couple them.
    # Lazy-import is cheap and only fires when the env var is set.
    from .wisdom import WISDOM_SKILLS_DIR, is_enabled

    if not is_enabled():
        return []
    return [WISDOM_SKILLS_DIR]


def discover_skill_dirs(
    *,
    config: Config | None = None,
    extra: Iterable[str | Path] = (),
    existing_only: bool = True,
) -> list[Path]:
    """Return the de-duplicated, ordered list of skill-root directories.

    Precedence (highest priority first, which matters for name-collision
    resolution in :func:`Router.warmup`):

    1. ``extra=`` argument (callers passing CLI ``--skills-dir`` flags).
    2. Standard locations: ``~/.claude/skills``, ``~/.codex/skills``,
       ``~/.gemini/skills``, ``~/.hermes/skills``, ``$CODEX_HOME/skills``.
    3. Per-host plugin trees:
       - Claude marketplace
         (``~/.claude/plugins/marketplaces/<m>/{plugins|external_plugins}/<p>/skills``)
       - Codex plugin cache
         (``~/.codex/plugins/cache/<m>/<p>/<commit-hash>/skills``)
       - Gemini extensions (stub; the marketplace doesn't carry skills yet).
    4. ``[skills] extra_dirs`` from the user's config.toml.
    5. ``MEGA_SKILL_DIRS`` env (colon-separated).
    6. MEGA-Code wisdom skill cache, if ``MEGA_WITH_WISDOM=1`` (lowest
       priority — local skills always shadow wisdom skills on
       ``name:`` collision).

    With ``existing_only=True`` (default) we drop paths that don't exist
    yet, so a freshly-installed user with only ``~/.claude/skills``
    populated doesn't get warnings about missing ``~/.codex/skills``.
    Pass ``existing_only=False`` for tools that *want* to surface
    registered-but-missing dirs (e.g. ``dirs list``).
    """
    if config is None:
        config = Config.load()
    candidates: list[Path] = []
    candidates.extend(Path(p).expanduser() for p in extra)
    candidates.extend(_standard_skill_dirs())
    # Per-host plugin trees — Claude marketplace, Codex plugin cache,
    # Gemini extensions (when available). mega-tron unifies them into
    # one routing pool, but the dashboard's vanilla-cost math attributes
    # each tree back to its owner host (see dashboard/api.py).
    # Listed after the host-native dirs so a user-edited skill in
    # ~/.claude/skills still wins on name collision, but ahead of
    # the user's config.toml extra_dirs so plugin authors get a
    # working default with zero configuration.
    candidates.extend(_claude_plugin_skill_dirs())
    candidates.extend(_codex_plugin_skill_dirs())
    candidates.extend(_gemini_plugin_skill_dirs())
    candidates.extend(config.extra_skill_dirs)
    candidates.extend(env_extra_dirs())
    candidates.extend(_wisdom_skill_dirs())

    seen: set[Path] = set()
    out: list[Path] = []
    for p in candidates:
        try:
            resolved = p.resolve(strict=False)
        except OSError:
            resolved = p
        if resolved in seen:
            continue
        seen.add(resolved)
        if existing_only and not resolved.exists():
            continue
        out.append(resolved)
    return out


# ---------------------------------------------------------------------------
# Mutators used by `mega-tron dirs <op>`.
# ---------------------------------------------------------------------------


def add_skill_dir(path: str | Path) -> tuple[Config, bool]:
    """Persist ``path`` into the config. Returns (new_config, added_flag);
    ``added_flag=False`` means the path was already registered."""
    p = Path(path).expanduser().resolve(strict=False)
    cfg = Config.load()
    existing = {d.resolve(strict=False) for d in cfg.extra_skill_dirs}
    if p in existing:
        return cfg, False
    cfg.extra_skill_dirs.append(p)
    cfg.save()
    return cfg, True


def remove_skill_dir(path: str | Path) -> tuple[Config, bool]:
    """Remove ``path`` from the config. Returns (new_config, removed_flag);
    ``removed_flag=False`` means the path was not registered."""
    p = Path(path).expanduser().resolve(strict=False)
    cfg = Config.load()
    before = len(cfg.extra_skill_dirs)
    cfg.extra_skill_dirs = [
        d for d in cfg.extra_skill_dirs if d.resolve(strict=False) != p
    ]
    if len(cfg.extra_skill_dirs) == before:
        return cfg, False
    cfg.save()
    return cfg, True


def set_embedder_model(model_id: str) -> Config:
    """Persist a new default embedder model id. Returns the saved config."""
    cfg = Config.load()
    cfg.embedder_model = model_id
    cfg.save()
    return cfg


__all__ = [
    "DEFAULT_EMBEDDER_MODEL",
    "DEFAULT_PREFILTER",
    "DEFAULT_TOP_K",
    "Config",
    "add_skill_dir",
    "config_dir",
    "config_path",
    "data_dir",
    "discover_skill_dirs",
    "env_extra_dirs",
    "remove_skill_dir",
    "set_embedder_model",
    "store_path",
]
