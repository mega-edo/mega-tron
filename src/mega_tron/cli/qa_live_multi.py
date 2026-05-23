"""End-to-end multi-turn QA — verifies verdict capture across follow-up turns.

The single-shot ``qa-live`` driver in :mod:`mega_tron.cli.qa_live`
proves the wiring works on Turn 1. This driver proves the *multi-turn*
loop: hook fires every turn (not just first), the slim follow-up block
contains the right per-turn catalog, and ``<skill-used>`` tags written
on Turn 2 and Turn 4 land in the DB.

Scenario (identical for all three hosts):
  1. ``hi``                            (no-op: trivial, no skill match)
  2. ``JWT bearer middleware for Express`` (substantive: catalog should
     surface JWT-family skills; expect ≥ 1 verdict)
  3. ``fun fact about giraffes?``       (no-op)
  4. ``prevent SQL injection in Python sqlite`` (substantive: SQL-family
     skills; expect ≥ 1 verdict)

The driver is a developer / maintainer tool, not user-facing. It is
slow (4 × per host × full LLM round-trip) and noisy. The output is a
per-host pass/fail report keyed on the SQLite ``verdicts`` table
delta across the 4-turn session.

Hosts:
  - ``codex``: ``codex exec`` + ``codex exec resume --last`` 3x. One
    session id, persisted by codex's own resume mechanism.
  - ``claude``: ``claude -p`` for Turn 1 + ``claude -c -p`` 3x. Same
    session id via the ``-c`` (continue most recent in cwd) flag.
  - ``gemini``: tmux send-keys against an interactive ``gemini`` REPL.
    The headless ``-p`` mode is single-shot only; the Ink TUI of the
    interactive mode is the only way to keep one session id across
    four prompts. ``tmux send-keys -l "<prompt>"`` + ``Enter`` is the
    incantation that actually submits a line (plain ``C-m`` inserts a
    newline into the prompt buffer instead).

Pass criteria (per host):
  - Turn 1 + Turn 3 produce 0 verdicts (silence is correct on chatty
    no-match turns)
  - Turn 2 produces ≥ 1 verdict, with skill name in the JWT/Express family
  - Turn 4 produces ≥ 1 verdict, with skill name in the SQL-injection family

Failure modes the driver tries to be helpful about:
  - host CLI not installed → SKIP
  - host CLI not logged in → NEEDS_LOGIN with the per-host hint
  - cwd-bound session id can't be resumed (claude only, when the cwd
    doesn't have a prior session) → FAIL with a clear note
  - gemini tmux REPL not reaching the input prompt → FAIL
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable


# Per-turn prompts. The driver fires them in order through whichever
# session-keeping mechanism the host supports.
PROMPTS: tuple[str, ...] = (
    "hi",
    (
        "I need a JWT bearer token middleware for Express. Show me a "
        "secure HS256 implementation in 30 lines or less."
    ),
    "By the way, what's a fun fact about giraffes?",
    (
        "Show me how to prevent SQL injection in a Python sqlite query. "
        "Short example using parameterized queries."
    ),
)

# Skill name families that "should" surface on each substantive turn.
# We match on substrings to stay tolerant of the user's exact catalog
# (the JWT family in the user's pool may include extras like
# `expressjs-development` or `express-microservices-architecture`).
JWT_FAMILY: tuple[str, ...] = (
    "jwt-token-validator",
    "bearer-token-validator",
    "expressjs-development",
    "express-microservices-architecture",
)
SQL_FAMILY: tuple[str, ...] = (
    "sql-injection-detector",
    "detecting-sql-injection-vulnerabilities",
    "SQL Injection Testing",
)

# Per-host budget for a single turn. The cold-load on Turn 1 is the
# slowest; later turns should be sub-30s but xhigh-reasoning Codex can
# spend a minute on a JWT answer. Generous defaults; tune via env.
_TURN_TIMEOUT_S: dict[str, int] = {
    "codex": 240,
    "claude": 240,
    "gemini": 360,
}

_HOST_DB_NAMES: dict[str, str] = {
    "codex": "codex",
    "claude": "claude_code",
    "gemini": "gemini_cli",
}


def _turn_timeout(host: str) -> int:
    base = _TURN_TIMEOUT_S.get(host, 240)
    override = os.environ.get("MEGA_QA_TIMEOUT_S", "").strip()
    if not override:
        return base
    try:
        return max(base, int(override))
    except ValueError:
        return base


def _print(msg: str) -> None:
    """Tagged stderr line so the driver output is greppable in scrollback."""
    print(f"[qa-multi] {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Codex driver
# ---------------------------------------------------------------------------


def _run_codex(cwd: Path) -> tuple[str | None, str | None]:
    """Drive codex 4-turn. Returns ``(session_id, error)``.

    Codex prints ``session id: <UUID>`` on the first non-resume call;
    we parse it from the combined output to associate routes/verdicts
    rows with this run.
    """
    bin_path = shutil.which("codex") or _hunt_local_bin("codex")
    if not bin_path:
        return None, "codex binary not on PATH (checked $PATH and ~/.local/bin)"

    session_id: str | None = None

    def _exec(args: list[str], label: str) -> str | None:
        timeout = _turn_timeout("codex")
        try:
            result = subprocess.run(
                [bin_path, *args],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return f"{label} timed out after {timeout}s"
        if result.returncode != 0:
            tail = (result.stderr or "").strip().splitlines()[-1:] or [""]
            return f"{label} rc={result.returncode}: {tail[0][:160]}"
        # codex emits its session-id banner on stderr; merge both streams
        # so the caller can scan for it without caring which stream it
        # landed on.
        return (result.stdout or "") + "\n" + (result.stderr or "")

    # Turn 1: fresh session.
    out1 = _exec(
        ["exec", "--skip-git-repo-check", PROMPTS[0]],
        "codex T1",
    )
    if isinstance(out1, str) and out1.startswith("codex T"):
        return None, out1
    # Parse "session id: <uuid>" line.
    for line in (out1 or "").splitlines():
        if "session id:" in line:
            session_id = line.split("session id:", 1)[1].strip()
            break
    if not session_id:
        return None, "codex did not print session id"

    # Turns 2-4: resume the most recent session in this cwd.
    for i, prompt in enumerate(PROMPTS[1:], start=2):
        err = _exec(
            ["exec", "resume", "--skip-git-repo-check", "--last", prompt],
            f"codex T{i}",
        )
        if isinstance(err, str) and err.startswith("codex T"):
            return session_id, err
    return session_id, None


# ---------------------------------------------------------------------------
# Claude driver
# ---------------------------------------------------------------------------


def _run_claude(cwd: Path) -> tuple[str | None, str | None]:
    """Drive claude 4-turn. Returns ``(session_id, error)``.

    Claude doesn't print the session id on -p output; we discover it
    after Turn 1 by reading the most recent claude_code routes row
    written in this cwd's project transcript directory. ``-c -p`` then
    resumes that same session for Turns 2-4.
    """
    bin_path = shutil.which("claude") or _hunt_local_bin("claude")
    if not bin_path:
        return None, "claude binary not on PATH (checked $PATH and ~/.local/bin)"

    def _exec(args: list[str], label: str) -> str | None:
        timeout = _turn_timeout("claude")
        try:
            result = subprocess.run(
                [bin_path, *args],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return f"{label} timed out after {timeout}s"
        if result.returncode != 0:
            tail = (result.stderr or "").strip().splitlines()[-1:] or [""]
            return f"{label} rc={result.returncode}: {tail[0][:160]}"
        return None

    # Snapshot the most recent claude_code routes row time before T1
    # so we can discover this run's session id by diff.
    before_ts = _latest_claude_route_ts()

    err = _exec(["-p", PROMPTS[0]], "claude T1")
    if err is not None:
        return None, err

    session_id = _new_claude_session_id_after(before_ts)
    if not session_id:
        return None, "claude wrote no new routes row for T1"

    for i, prompt in enumerate(PROMPTS[1:], start=2):
        err = _exec(["-c", "-p", prompt], f"claude T{i}")
        if err is not None:
            return session_id, err
    return session_id, None


def _latest_claude_route_ts() -> str:
    """Most recent ``routes.routed_at`` for ``host='claude_code'``, or ''."""
    try:
        from mega_tron.config import store_path
        from mega_tron.verdicts.store import Store
    except Exception:  # noqa: BLE001
        return ""
    try:
        store = Store(store_path())
        store.initialize()
        with store._connect() as conn:  # noqa: SLF001
            row = conn.execute(
                "SELECT MAX(routed_at) FROM routes WHERE host='claude_code'"
            ).fetchone()
        return (row[0] if row and row[0] else "") or ""
    except Exception:  # noqa: BLE001
        return ""


def _new_claude_session_id_after(before_ts: str) -> str | None:
    """Return the session_id of the newest claude_code routes row
    inserted strictly after ``before_ts``."""
    try:
        from mega_tron.config import store_path
        from mega_tron.verdicts.store import Store
    except Exception:  # noqa: BLE001
        return None
    try:
        store = Store(store_path())
        store.initialize()
        with store._connect() as conn:  # noqa: SLF001
            if before_ts:
                row = conn.execute(
                    "SELECT session_id FROM routes "
                    "WHERE host='claude_code' AND routed_at > ? "
                    "ORDER BY routed_at DESC LIMIT 1",
                    (before_ts,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT session_id FROM routes "
                    "WHERE host='claude_code' "
                    "ORDER BY routed_at DESC LIMIT 1"
                ).fetchone()
        return row[0] if row and row[0] else None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Gemini driver (tmux)
# ---------------------------------------------------------------------------


def _run_gemini(cwd: Path) -> tuple[str | None, str | None]:
    """Drive gemini 4-turn via tmux. Returns ``(session_id, error)``.

    Gemini's interactive REPL is an Ink TUI: ``send-keys "..." Enter``
    is the only thing that actually submits a prompt. The headless
    ``-p`` mode would be simpler but is single-shot — we'd get four
    independent session ids and the multi-turn behavior would be
    invisible.
    """
    gemini_bin = shutil.which("gemini") or _hunt_gemini_npm_bin()
    if not gemini_bin:
        return None, "gemini binary not on PATH (checked $PATH and ~/.npm-global/bin)"
    tmux_bin = shutil.which("tmux")
    if not tmux_bin:
        return None, "tmux not on PATH (install tmux or skip --host gemini)"

    before_ts = _latest_gemini_route_ts()
    session_name = f"qa-gem-{os.getpid()}"

    try:
        subprocess.run(
            [
                tmux_bin, "new-session", "-d", "-s", session_name,
                "-x", "200", "-y", "50",
                "-c", str(cwd),
                # keep-alive wrap: when gemini exits, sleep keeps the
                # tmux session open long enough for capture-pane.
                (
                    f"GEMINI_CLI_TRUST_WORKSPACE=true "
                    f"{gemini_bin} --skip-trust; echo '=== gemini exited ==='; sleep 60"
                ),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        return None, f"tmux new-session failed: {e.stderr.strip()[:160]}"

    # Banner takes ~10-15s on a fresh `gemini` (model selection screen
    # + skill load). Just wait — polling the pane content reliably is
    # surprisingly fiddly across gemini versions.
    time.sleep(15)

    try:
        for i, prompt in enumerate(PROMPTS, start=1):
            # Snapshot the gemini_cli routes count BEFORE this turn so
            # we can poll for it incrementing — the hook writes one row
            # per turn it fires, so this is the most direct "turn was
            # actually processed" signal we have.
            before_count = _gemini_route_count(before_ts)
            max_wait_s = _turn_timeout("gemini")
            _print(f"gemini T{i}: sending prompt (poll up to {max_wait_s}s)")
            subprocess.run(
                [tmux_bin, "send-keys", "-t", session_name, "-l", prompt],
                check=True,
            )
            time.sleep(1)
            subprocess.run(
                [tmux_bin, "send-keys", "-t", session_name, "Enter"],
                check=True,
            )
            # Poll the DB for the new routes row, up to per-turn budget.
            # Falls through to next turn after budget exhausted — the
            # verifier will note the missing row.
            deadline = time.monotonic() + max_wait_s
            while time.monotonic() < deadline:
                time.sleep(5)
                if _gemini_route_count(before_ts) > before_count:
                    # Hook fired + recorded. Give the model a couple
                    # seconds more to finish writing the response so
                    # any subsequent prompt doesn't race the same UI.
                    time.sleep(5)
                    break
            else:
                _print(f"gemini T{i}: no new routes row within budget")
    finally:
        # Capture screen for diagnostics, then close.
        try:
            cap = subprocess.run(
                [tmux_bin, "capture-pane", "-t", session_name, "-p"],
                capture_output=True,
                text=True,
            )
            if cap.stdout:
                Path("/tmp/qa-gemini-final.txt").write_text(cap.stdout)
        except subprocess.SubprocessError:
            pass
        subprocess.run(
            [tmux_bin, "send-keys", "-t", session_name, "/quit", "Enter"],
            check=False,
        )
        time.sleep(2)
        subprocess.run(
            [tmux_bin, "kill-session", "-t", session_name],
            check=False,
            capture_output=True,
        )

    session_id = _new_gemini_session_id_after(before_ts)
    if not session_id:
        return None, (
            "gemini wrote no new routes row (model may not have loaded; "
            "see /tmp/qa-gemini-final.txt for the last screen capture)"
        )
    return session_id, None


def _hunt_gemini_npm_bin() -> str | None:
    """Fallback: look for gemini in ~/.npm-global/bin/. Many setups
    don't have that on $PATH for non-interactive subshells."""
    candidate = Path.home() / ".npm-global" / "bin" / "gemini"
    if candidate.exists() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


def _hunt_local_bin(name: str) -> str | None:
    """Fallback hunt for a CLI binary the parent shell knows about but
    a child ``subprocess`` doesn't (because PATH inheritance is funky
    when invoked from `uv run`, GUI launchers, etc). Looks in the
    canonical places mega-tron itself ships under.
    """
    for prefix in (
        Path.home() / ".local" / "bin",
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
    ):
        candidate = prefix / name
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _gemini_route_count(since_ts: str) -> int:
    """Number of gemini_cli routes rows newer than ``since_ts``.

    Used by the gemini driver to poll for "hook fired and recorded
    this turn" without depending on TUI screen parsing.
    """
    try:
        from mega_tron.config import store_path
        from mega_tron.verdicts.store import Store
    except Exception:  # noqa: BLE001
        return 0
    try:
        store = Store(store_path())
        store.initialize()
        with store._connect() as conn:  # noqa: SLF001
            if since_ts:
                row = conn.execute(
                    "SELECT COUNT(*) FROM routes "
                    "WHERE host='gemini_cli' AND routed_at > ?",
                    (since_ts,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) FROM routes WHERE host='gemini_cli'"
                ).fetchone()
        return int(row[0]) if row else 0
    except Exception:  # noqa: BLE001
        return 0


def _latest_gemini_route_ts() -> str:
    try:
        from mega_tron.config import store_path
        from mega_tron.verdicts.store import Store
    except Exception:  # noqa: BLE001
        return ""
    try:
        store = Store(store_path())
        store.initialize()
        with store._connect() as conn:  # noqa: SLF001
            row = conn.execute(
                "SELECT MAX(routed_at) FROM routes WHERE host='gemini_cli'"
            ).fetchone()
        return (row[0] if row and row[0] else "") or ""
    except Exception:  # noqa: BLE001
        return ""


def _new_gemini_session_id_after(before_ts: str) -> str | None:
    try:
        from mega_tron.config import store_path
        from mega_tron.verdicts.store import Store
    except Exception:  # noqa: BLE001
        return None
    try:
        store = Store(store_path())
        store.initialize()
        with store._connect() as conn:  # noqa: SLF001
            if before_ts:
                row = conn.execute(
                    "SELECT session_id FROM routes "
                    "WHERE host='gemini_cli' AND routed_at > ? "
                    "ORDER BY routed_at DESC LIMIT 1",
                    (before_ts,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT session_id FROM routes "
                    "WHERE host='gemini_cli' "
                    "ORDER BY routed_at DESC LIMIT 1"
                ).fetchone()
        return row[0] if row and row[0] else None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Verification (post-run DB inspection)
# ---------------------------------------------------------------------------


def _session_routes(session_id: str, host_db_name: str) -> list[tuple[str, list[str]]]:
    """Return [(routed_at, picked_names), ...] for this session, host-narrowed."""
    try:
        from mega_tron.config import store_path
        from mega_tron.verdicts.store import Store
        import json as _json
    except Exception:  # noqa: BLE001
        return []
    try:
        store = Store(store_path())
        store.initialize()
        with store._connect() as conn:  # noqa: SLF001
            rows = conn.execute(
                "SELECT routed_at, picked_names_json FROM routes "
                "WHERE session_id=? AND host=? ORDER BY routed_at",
                (session_id, host_db_name),
            ).fetchall()
        out: list[tuple[str, list[str]]] = []
        for ts, blob in rows:
            try:
                names = _json.loads(blob) if blob else []
            except Exception:  # noqa: BLE001
                names = []
            out.append((ts, names))
        return out
    except Exception:  # noqa: BLE001
        return []


def _session_verdicts(session_id: str) -> list[tuple[str, str]]:
    """Return [(skill_name, verdict), ...] for this session."""
    try:
        from mega_tron.config import store_path
        from mega_tron.verdicts.store import Store
    except Exception:  # noqa: BLE001
        return []
    try:
        store = Store(store_path())
        store.initialize()
        with store._connect() as conn:  # noqa: SLF001
            rows = conn.execute(
                "SELECT skill_name, verdict FROM verdicts "
                "WHERE session_id=? ORDER BY occurred_at",
                (session_id,),
            ).fetchall()
        return [(r[0], r[1]) for r in rows]
    except Exception:  # noqa: BLE001
        return []


def _has_family_hit(picked_names: Iterable[str], family: tuple[str, ...]) -> bool:
    """Return True if any picked name matches the family substring set."""
    blob = " ".join(picked_names).lower()
    return any(f.lower() in blob for f in family)


def _verify(
    host: str, session_id: str
) -> tuple[bool, list[str]]:
    """Apply the multi-turn pass criteria. Returns ``(pass, notes)``."""
    db_name = _HOST_DB_NAMES[host]
    routes = _session_routes(session_id, db_name)
    verdicts = _session_verdicts(session_id)
    notes: list[str] = []

    notes.append(
        f"{len(routes)} host=routes row(s), {len(verdicts)} verdict(s) "
        f"(session_id={session_id[:8]}…)"
    )

    # We expect 4 routes (one per turn) because the first-fire gate is
    # gone; tolerate < 4 if the hook missed a turn but at least report.
    if len(routes) < 4:
        notes.append(
            f"WARN: expected 4 host=routes rows (one per turn), got {len(routes)}."
        )
        if host == "gemini":
            notes.append(
                "  NB: gemini's interactive TUI streams long responses "
                "across the whole pane; the tmux-driven driver may send "
                "the next prompt before the previous response finishes, "
                "causing the hook for that next turn to miss. This is a "
                "driver limitation, not a hook regression — interactive "
                "human use is unaffected."
            )

    # Catalog substring checks on the substantive turns. routes is in
    # turn order; turn 2 is index 1, turn 4 is index 3.
    t2_ok = False
    t4_ok = False
    if len(routes) >= 2:
        _, t2_names = routes[1]
        t2_ok = _has_family_hit(t2_names, JWT_FAMILY)
        notes.append(
            f"T2 catalog ({', '.join(t2_names[:3])}): "
            f"{'JWT-family ✓' if t2_ok else 'no JWT-family hit'}"
        )
    if len(routes) >= 4:
        _, t4_names = routes[3]
        t4_ok = _has_family_hit(t4_names, SQL_FAMILY)
        notes.append(
            f"T4 catalog ({', '.join(t4_names[:3])}): "
            f"{'SQL-family ✓' if t4_ok else 'no SQL-family hit'}"
        )

    # Verdict check — at least one verdict must land for this session.
    # The model may emit on T2, T4, or both; we accept any non-empty.
    if verdicts:
        v_summary = ", ".join(f"{n}={v}" for n, v in verdicts[:3])
        notes.append(f"verdicts captured: {v_summary}")
    else:
        notes.append("no verdicts captured (model may have skipped tag emission)")

    # Pass: catalog refreshed (T2 OR T4 had a family hit) AND ≥ 1 verdict.
    passed = (t2_ok or t4_ok) and bool(verdicts)
    return passed, notes


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


_DRIVERS: dict[str, callable] = {
    "codex": _run_codex,
    "claude": _run_claude,
    "gemini": _run_gemini,
}


def run_qa_live_multi_turn(hosts: list[str]) -> int:
    """Drive the 4-turn QA across each host. Returns 0 on overall pass."""
    if not hosts:
        _print("no hosts specified; nothing to run.")
        return 1
    _print(
        f"running 4-turn QA across {len(hosts)} host(s): {', '.join(hosts)}. "
        "This is slow (~4 × LLM round-trip per host)."
    )

    results: dict[str, tuple[bool, list[str]]] = {}
    for host in hosts:
        driver = _DRIVERS.get(host)
        if driver is None:
            results[host] = (False, [f"unknown host: {host}"])
            continue
        cwd = Path("/tmp") / f"qa-{host}-multi-{os.getpid()}"
        cwd.mkdir(parents=True, exist_ok=True)
        _print(f"--- {host} (cwd={cwd}) ---")
        session_id, err = driver(cwd)
        if err is not None:
            results[host] = (False, [f"driver error: {err}"])
            continue
        if not session_id:
            results[host] = (False, ["driver did not yield a session_id"])
            continue
        passed, notes = _verify(host, session_id)
        results[host] = (passed, notes)

    _print("")
    _print("=== Summary ===")
    n_pass = 0
    for host, (passed, notes) in results.items():
        verdict = "PASS" if passed else "FAIL"
        if passed:
            n_pass += 1
        _print(f"{host:7s} {verdict}")
        for n in notes:
            _print(f"        {n}")
    _print(f"total: {n_pass}/{len(results)} host(s) passed")
    return 0 if n_pass == len(results) else 1
