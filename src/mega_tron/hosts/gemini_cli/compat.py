"""Gemini CLI version detection.

Gemini CLI ships as ``@google/gemini-cli`` on npm. We probe ``gemini
--version`` to learn the installed version (used for diagnostics and
future version-gated install behavior). Returns ``None`` cleanly when
the binary is missing — install/uninstall must keep working in
unit-test environments where Gemini CLI isn't on PATH.
"""
from __future__ import annotations

import shutil
import subprocess


def gemini_cli_version() -> str | None:
    """Return the installed Gemini CLI version string, or None if absent.

    The exact ``--version`` output format may shift across releases. We
    return the raw first line stripped of whitespace and let callers
    pattern-match if they need to gate on a specific version. The
    minimum supported Gemini CLI version for this adapter is the one
    that ships ``BeforeAgent``/``AfterAgent`` hooks (early 2026).
    """
    if shutil.which("gemini") is None:
        return None
    try:
        result = subprocess.run(
            ["gemini", "--version"],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    line = (result.stdout or result.stderr or "").splitlines()
    return line[0].strip() if line else None


__all__ = ["gemini_cli_version"]
