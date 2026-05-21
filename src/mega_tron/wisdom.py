"""Wisdom-curator candidate provider — opt-in background ignition.

Opt-in extension that augments MSR's local cosine prefilter with skill
candidates curated by the MEGA-Code wisdom gateway. The gateway returns
SKILL.md candidates ranked against the user's prompt; they're extracted
into ``~/.local/share/mega-code/skills/<name>/`` by mega-engine's
``skill_installer``. MSR's router then auto-discovers that directory
just like any other skill root — the wisdom-sourced skills go through
the same cosine prefilter + eval-blend rerank as locally-discovered
ones. We do NOT use the gateway's own score as a prior signal.

Gate: ``MEGA_WITH_WISDOM=1`` env var. Default off — the wisdom gateway
is an external service requiring auth + network access, neither of
which MSR's hermetic default path makes available.

This module is the IGNITION layer: it fires ``wisdom_curator.py search``
as a detached subprocess and returns immediately. The router/daemon
layer is responsible for holding the in-flight handle across turns and
choosing when to merge the result into the candidate pool.

Reference: the ``mega-engine`` skill ships ``wisdom_curator.py`` under
``scripts/``; point ``MEGA_WISDOM_CURATOR_PATH`` at it.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

# ---------------------------------------------------------------------------
# Configuration & environment gates
# ---------------------------------------------------------------------------

DEFAULT_WORK_DIR = Path.home() / ".local" / "share" / "mega-tron" / "wisdom"
DEFAULT_PROJECT_ID = "msr-router"
DEFAULT_TOP_K = 20

# The download target used by mega-engine's skill_installer. MSR's
# auto-discovery should include this path (or a config-driven equivalent)
# so wisdom-sourced skills participate in the cosine prefilter.
WISDOM_SKILLS_DIR = Path.home() / ".local" / "share" / "mega-code" / "skills"

ENV_ENABLED = "MEGA_WITH_WISDOM"
ENV_CURATOR_PATH = "MEGA_WISDOM_CURATOR_PATH"


def is_enabled() -> bool:
    """True iff ``MEGA_WITH_WISDOM`` is set to a truthy value (1/true/yes/on)."""
    v = os.environ.get(ENV_ENABLED, "").strip().lower()
    return v in {"1", "true", "yes", "on"}


def resolve_curator_path() -> Path | None:
    """Resolve ``wisdom_curator.py`` location.

    Priority:
      1. ``MEGA_WISDOM_CURATOR_PATH`` env var (explicit override).
      2. Returns ``None`` if unset or the path does not exist — callers
         should surface a configuration error.
    """
    raw = os.environ.get(ENV_CURATOR_PATH, "")
    if not raw:
        return None
    p = Path(raw).expanduser()
    return p if p.exists() else None


@dataclass(frozen=True)
class WisdomConfig:
    """Static config for one ignition site (process or daemon-resident)."""

    curator_path: Path
    work_dir: Path = DEFAULT_WORK_DIR
    project_id: str = DEFAULT_PROJECT_ID
    top_k: int = DEFAULT_TOP_K
    skills_dir: Path = WISDOM_SKILLS_DIR


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WisdomSkill:
    name: str
    installed: bool
    status: str  # "installed" | "skipped" | "failed:<ExcClass>"
    abs_path: Path


@dataclass(frozen=True)
class WisdomResult:
    """Parsed iteration metadata from a completed wisdom-curator call.

    The router does not consume any per-iteration selection logic — wisdom
    SKILL.md files land on disk under :data:`WISDOM_SKILLS_DIR` and the
    cosine prefilter walks them like any other skill root. We keep this
    type around for observability / daemon-side bookkeeping (how many
    skills were installed, what they cost, when they arrived).
    """

    session_id: str
    skills: list[WisdomSkill]
    wisdoms: list[dict]  # raw gateway entries: wisdom_id / name / score / references
    skills_root: Path
    token_count: int
    cost_usd: float
    iteration_path: Path

    @property
    def installed_skill_dirs(self) -> list[Path]:
        return [s.abs_path for s in self.skills if s.installed]

    @classmethod
    def from_iteration_json(cls, path: Path) -> "WisdomResult":
        data = json.loads(path.read_text())
        skills_root = Path(data.get("skills_root", str(WISDOM_SKILLS_DIR)))
        skills = [
            WisdomSkill(
                name=s["name"],
                installed=bool(s.get("installed", False)),
                status=str(s.get("status", "unknown")),
                abs_path=skills_root / s["name"],
            )
            for s in data.get("skills", [])
        ]
        usage = data.get("usage", {}) or {}
        return cls(
            session_id=str(data.get("session_id", "")),
            skills=skills,
            wisdoms=list(data.get("wisdoms", [])),
            skills_root=skills_root,
            token_count=int(usage.get("token_count", 0) or 0),
            cost_usd=float(usage.get("cost_usd", 0.0) or 0.0),
            iteration_path=path,
        )


@dataclass
class WisdomHandle:
    """In-flight (or completed) ``wisdom_curator.py search`` invocation.

    The subprocess is detached (``start_new_session=True``); ``status()``
    polls ``Popen.poll()`` + the iteration JSON's filesystem appearance.
    """

    proc: subprocess.Popen
    started_at: float
    query: str
    expected_iter_dir: Path

    def status(self) -> Literal["in_flight", "ready", "failed", "no_result"]:
        rc = self.proc.poll()
        if rc is None:
            return "in_flight"
        if rc != 0:
            return "failed"
        return "ready" if self._latest_iteration() else "no_result"

    def wait(self, timeout: float | None = None) -> int:
        return self.proc.wait(timeout=timeout)

    def result(self) -> WisdomResult | None:
        if self.status() != "ready":
            return None
        path = self._latest_iteration()
        return WisdomResult.from_iteration_json(path) if path else None

    def _latest_iteration(self) -> Path | None:
        if not self.expected_iter_dir.exists():
            return None
        # Only consider iteration files modified at-or-after our start time —
        # otherwise a stale file from a prior run would falsely report ready.
        recent = [
            p for p in self.expected_iter_dir.glob("*.json")
            if p.stat().st_mtime + 1.0 >= self.started_at  # 1s slack for clock skew
        ]
        if not recent:
            return None
        return max(recent, key=lambda p: p.stat().st_mtime)


# ---------------------------------------------------------------------------
# Ignition
# ---------------------------------------------------------------------------


def _has_uv() -> bool:
    return shutil.which("uv") is not None


def ignite(
    query: str,
    *,
    situation: str = "",
    symptoms: str = "",
    goals: str = "",
    config: WisdomConfig | None = None,
    silent: bool = False,
) -> WisdomHandle:
    """Fire ``wisdom_curator.py search`` in the background. Returns immediately.

    The subprocess is detached so the caller can exit without killing it.
    Use ``handle.status()`` / ``handle.result()`` to poll completion.

    Args:
        query: The user prompt. Maps to ``--situation`` if ``situation`` is empty.
        situation / symptoms / goals: Optional explicit decomposition. The
            gateway joins them server-side into a single query string.
        config: Optional explicit config; defaults to env-var-driven resolution.
        silent: When True, redirect subprocess stdout/stderr to ``/dev/null``
            instead of buffered pipes. Required for long-lived daemon hosts
            so the wisdom-curator's progress output doesn't fill its pipe
            buffer and block the subprocess. Smoke-test invocations leave
            this False so debug output is observable on failure.

    Raises:
        RuntimeError: if ``MEGA_WISDOM_CURATOR_PATH`` is unset or invalid.
    """
    if config is None:
        cp = resolve_curator_path()
        if cp is None:
            raise RuntimeError(
                f"{ENV_CURATOR_PATH} env var is unset or points to a non-existent "
                "file. Set it to the path of mega-engine's wisdom_curator.py."
            )
        config = WisdomConfig(curator_path=cp)

    config.work_dir.mkdir(parents=True, exist_ok=True)

    sit = situation or query
    base_args = [
        "search",
        "--project-id", config.project_id,
        "--event", "msr/ignite",
        "--top-k", str(config.top_k),
        "--situation", sit,
    ]
    if symptoms:
        base_args += ["--symptoms", symptoms]
    if goals:
        base_args += ["--goals", goals]

    # wisdom_curator.py uses PEP-723 inline script metadata (`# /// script`),
    # so `uv run <path>` is the canonical invocation — it auto-installs the
    # script's declared deps in an ephemeral venv. We require uv on PATH;
    # without it, the script's httpx + python-dotenv deps would have to be
    # satisfied by MSR's own venv, which is the wrong scope.
    if not _has_uv():
        raise RuntimeError(
            "uv not on PATH. wisdom_curator.py is a PEP-723 script and "
            "requires uv to resolve its inline dependencies."
        )
    cmd = ["uv", "run", str(config.curator_path), *base_args]

    started_at = time.time()
    stdout = subprocess.DEVNULL if silent else subprocess.PIPE
    stderr = subprocess.DEVNULL if silent else subprocess.PIPE
    proc = subprocess.Popen(
        cmd,
        cwd=str(config.work_dir),
        stdout=stdout,
        stderr=stderr,
        start_new_session=True,
    )

    expected_iter_dir = (
        config.work_dir / ".mega" / "feedback" / "projects"
        / config.project_id / "iterations"
    )
    return WisdomHandle(
        proc=proc,
        started_at=started_at,
        query=query,
        expected_iter_dir=expected_iter_dir,
    )


# ---------------------------------------------------------------------------
# Smoke test entrypoint — `python -m mega_tron.wisdom "<query>"`
# ---------------------------------------------------------------------------


def _smoke_main() -> int:
    import argparse

    p = argparse.ArgumentParser(
        prog="python -m mega_tron.wisdom",
        description="Wisdom-curator ignition smoke test.",
    )
    p.add_argument("query", help="Query to send to the MEGA-Code wisdom gateway")
    p.add_argument(
        "--wait",
        type=float,
        default=60.0,
        help="Max seconds to wait for the subprocess (default: 60)",
    )
    p.add_argument(
        "--no-wait",
        action="store_true",
        help="Fire and exit immediately (don't poll for completion)",
    )
    args = p.parse_args()

    print(f"[wisdom] enabled={is_enabled()}", file=sys.stderr)
    cp = resolve_curator_path()
    if cp is None:
        print(
            f"[wisdom] FATAL: {ENV_CURATOR_PATH} is unset or invalid. "
            "Point it at mega-engine's wisdom_curator.py.",
            file=sys.stderr,
        )
        return 2
    print(f"[wisdom] curator: {cp}", file=sys.stderr)
    print(f"[wisdom] uv available: {_has_uv()}", file=sys.stderr)

    try:
        h = ignite(args.query)
    except RuntimeError as e:
        print(f"[wisdom] FATAL: {e}", file=sys.stderr)
        return 2

    print(
        f"[wisdom] fired pid={h.proc.pid} at t={h.started_at:.3f}",
        file=sys.stderr,
    )
    print(f"[wisdom] expected iter dir: {h.expected_iter_dir}", file=sys.stderr)

    if args.no_wait:
        print(f"[wisdom] exit immediately, status={h.status()}", file=sys.stderr)
        return 0

    print(f"[wisdom] polling up to {args.wait}s...", file=sys.stderr)
    deadline = time.time() + args.wait
    last_status = None
    while time.time() < deadline:
        st = h.status()
        if st != last_status:
            print(f"[wisdom] t+{time.time() - h.started_at:.1f}s status={st}", file=sys.stderr)
            last_status = st
        if st in {"ready", "failed", "no_result"}:
            break
        time.sleep(0.5)

    elapsed = time.time() - h.started_at
    final_status = h.status()
    print(f"[wisdom] final status={final_status} elapsed={elapsed:.2f}s", file=sys.stderr)

    res = h.result()
    if res is None:
        try:
            out, err = h.proc.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            out, err = b"", b""
        if out:
            print(f"[wisdom] subprocess stdout (tail 500B):\n{out.decode(errors='replace')[-500:]}", file=sys.stderr)
        if err:
            print(f"[wisdom] subprocess stderr (tail 1KB):\n{err.decode(errors='replace')[-1000:]}", file=sys.stderr)
        return 1

    print(f"[wisdom] session_id={res.session_id}")
    print(f"[wisdom] tokens={res.token_count} cost_usd={res.cost_usd:.4f}")
    print(f"[wisdom] skills_root={res.skills_root}")
    print(f"[wisdom] skills ({len(res.skills)}):")
    for s in res.skills:
        marker = "✓" if s.installed else "✗"
        print(f"  {marker} {s.name:30s} {s.status:25s} {s.abs_path}")
    print(f"[wisdom] iteration JSON: {res.iteration_path}")
    return 0


if __name__ == "__main__":
    sys.exit(_smoke_main())
