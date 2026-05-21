"""Codex CLI version detection — futureproofing against `$SkillName` rule drift.

The `$SkillName` must-use trigger and the 2% inline-skill budget are stable
from codex 0.130.x onward but could shift in a future major rewrite. We
cache the detected version once per machine and warn (don't fail) on drift.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

# Major version range we've verified the $SkillName rule against.
# Update this tuple when promoting a new minor as tested.
TESTED_MAJOR_RANGE = (0, 1)  # accept any 0.x.y; flag if version reads 1.x or higher
CACHE_PATH = Path.home() / ".cache" / "mega-tron" / "codex_version.txt"


def _read_cached() -> str | None:
    try:
        return CACHE_PATH.read_text().strip() or None
    except FileNotFoundError:
        return None
    except OSError:
        return None


def _write_cache(version: str) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(version)
    except OSError:
        # Cache is best-effort; never break the caller.
        pass


def _run_codex_version(timeout_s: float = 2.0) -> str | None:
    try:
        result = subprocess.run(
            ["codex", "--version"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", result.stdout or result.stderr)
    return m.group(0) if m else None


def detect_version(*, refresh: bool = False) -> str | None:
    """Return cached or freshly-detected `codex --version` (e.g. "0.130.0").

    Returns None when codex CLI is not on PATH.
    """
    if not refresh:
        cached = _read_cached()
        if cached:
            return cached
    fresh = _run_codex_version()
    if fresh:
        _write_cache(fresh)
    return fresh


def warn_if_untested(version: str | None) -> None:
    """Emit a one-line stderr warning if `version` is outside TESTED_MAJOR_RANGE.

    Silent under MEGA_QUIET=1. Silent when version is unknown (we already
    log that elsewhere on a best-effort basis).
    """
    if os.environ.get("MEGA_QUIET", "").strip() not in ("", "0"):
        return
    if not version:
        return
    m = re.match(r"(\d+)\.(\d+)", version)
    if not m:
        return
    major = int(m.group(1))
    lo, hi = TESTED_MAJOR_RANGE
    if major < lo or major > hi:
        print(
            f"[codex_compat] codex {version} is outside tested major range "
            f"{lo}.x–{hi}.x; the $SkillName must-use rule may have changed.",
            file=sys.stderr,
            flush=True,
        )
