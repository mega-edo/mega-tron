"""Host adapters for mega-tron.

Each subpackage (``codex``, ``claude_code``, ``gemini_cli``) implements the
glue between a specific agent host and the host-agnostic :class:`MegaCore`
defined in :mod:`mega_tron.core`.

A host adapter is responsible for:

1. **Install / uninstall** — wiring mega-tron into the host's
   configuration (hooks, settings.json, config.yaml, …) so that user
   prompts and session-end events reach :class:`MegaCore`.
2. **Wire-format translation** — converting between the host's transcript
   / hook JSON schema and :class:`Verdict` / :class:`RankedSkill` objects
   that the core understands.
3. **Output formatting** — rendering the core's ranked skill list into
   the prompt-prefix or additional-context shape the host expects.

The core itself never imports from this package; hosts depend on the
core. Adding a new host is a matter of dropping a new subpackage here
that subclasses :class:`HostAdapter` from :mod:`.base`.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path


# Per-host detection signals. A host is "detected" if either the
# user-profile directory exists OR the binary is on PATH. We err on
# the inclusive side — a stray ``~/.codex/`` from an old install
# triggers detection, which is fine because the installer is
# idempotent and only writes managed sentinel blocks.
_HOST_HOME_DIRS: dict[str, str] = {
    "codex": ".codex",
    "claude": ".claude",
    "gemini": ".gemini",
}
_HOST_BINARIES: dict[str, str] = {
    "codex": "codex",
    "claude": "claude",
    "gemini": "gemini",
}


def is_host_present(host: str) -> bool:
    """Return whether ``host`` looks installed on this machine.

    Two signals, either is sufficient:
      1. The host's profile directory exists under ``$HOME``.
      2. The host's CLI binary is on ``PATH``.

    ``CODEX_HOME`` overrides the Codex profile location (Codex CLI
    convention); we honour it so test sandboxes and non-default
    installs are detected.
    """
    home = Path.home()
    if host == "codex":
        codex_home = os.environ.get("CODEX_HOME", "").strip()
        if codex_home and Path(codex_home).expanduser().exists():
            return True
    dir_name = _HOST_HOME_DIRS.get(host)
    if dir_name and (home / dir_name).exists():
        return True
    binary = _HOST_BINARIES.get(host)
    if binary and shutil.which(binary):
        return True
    return False


def detect_hosts() -> list[str]:
    """Return the subset of supported hosts present on this machine,
    in the canonical install order (codex, claude, gemini)."""
    return [h for h in ("codex", "claude", "gemini") if is_host_present(h)]


# Short host names used everywhere in the UX (dashboard chips, CLI
# default --target, etc.). The SQLite ``verdicts.host`` column stores
# the longer names ``claude_code`` / ``gemini_cli`` (set by the four
# stop_hooks) for back-compat. The dashboard and any future surface
# that displays host-pivoted data MUST funnel raw DB values through
# :func:`normalize_host` so the UI stays consistent.
_HOST_ALIASES: dict[str, str] = {
    "claude_code": "claude",
    "gemini_cli": "gemini",
}


def normalize_host(raw: str | None) -> str:
    """Map a raw ``verdicts.host`` value to the short display name.

    ``"claude_code" -> "claude"``, ``"gemini_cli" -> "gemini"``; every
    other value (including ``"codex"``, ``"claude"``, ``"hermes"``,
    ``"other"``, ``None``) is identity. ``None`` becomes ``"other"`` so
    callers can use the result as a dict key without special-casing.
    """
    if raw is None:
        return "other"
    return _HOST_ALIASES.get(raw, raw)


def infer_host_from_skill_dir(skill_dir: Path) -> str:
    """Map a skill directory's parent root to a short host name.

    Suffix-match the parent against the canonical standard roots from
    :func:`mega_tron.config._standard_skill_dirs`:

      ``~/.claude/skills``           -> ``"claude"``
      ``~/.codex/skills``            -> ``"codex"``
      ``~/.codex/skills/.system``    -> ``"codex"``
      ``$CODEX_HOME/skills``         -> ``"codex"``
      ``$CODEX_HOME/skills/.system`` -> ``"codex"``
      ``~/.gemini/skills``           -> ``"gemini"``
      ``~/.hermes/skills``           -> ``"hermes"``
      ``~/.agents/skills``           -> ``"agents"``

    Anything else (custom registered roots via ``mega-tron dirs add``,
    wisdom cache, ``MEGA_SKILL_DIRS``) falls back to ``"other"``.
    """
    parent = skill_dir.parent
    parent_resolved = parent.resolve(strict=False)
    try:
        parent_str = str(parent_resolved)
    except Exception:  # noqa: BLE001
        parent_str = str(parent)

    # Path-suffix tokens we recognise. Order matters: ``.system`` is a
    # child of ``.codex/skills`` so the more specific match wins by
    # being checked via the canonical-root suffix below, not by
    # ordering — but listing codex variants together keeps the table
    # readable.
    suffix_to_host: list[tuple[str, str]] = [
        ("/.claude/skills", "claude"),
        ("/.codex/skills/.system", "codex"),
        ("/.codex/skills", "codex"),
        ("/.gemini/skills", "gemini"),
        ("/.hermes/skills", "hermes"),
        ("/.agents/skills", "agents"),
    ]
    for suffix, host in suffix_to_host:
        if parent_str.endswith(suffix):
            return host

    # Plugin marketplace trees: anything under ``~/.claude/plugins/``,
    # ``~/.codex/plugins/``, or ``~/.gemini/plugins/`` belongs to that
    # host. Substring (not suffix) match because each plugin nests its
    # skills several levels deep (marketplaces/<m>/plugins/<p>/skills,
    # cache/<m>/<p>/<commit>/skills, etc.) and we don't want to
    # enumerate every variant.
    for plugin_marker, host in (
        ("/.claude/plugins/", "claude"),
        ("/.codex/plugins/", "codex"),
        ("/.gemini/plugins/", "gemini"),
    ):
        if plugin_marker in parent_str:
            return host

    # ``$CODEX_HOME`` can point anywhere; check it explicitly.
    codex_home = os.environ.get("CODEX_HOME", "").strip()
    if codex_home:
        try:
            ch = Path(codex_home).expanduser().resolve(strict=False)
            if parent_resolved == ch / "skills" or parent_resolved == ch / "skills" / ".system":
                return "codex"
        except Exception:  # noqa: BLE001
            pass

    return "other"


__all__ = [
    "detect_hosts",
    "is_host_present",
    "infer_host_from_skill_dir",
    "normalize_host",
]
