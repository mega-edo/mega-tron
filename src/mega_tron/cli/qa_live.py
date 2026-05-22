"""End-to-end QA self-check for mega-tron.

Drives one non-interactive call against every wired host, captures the
inline verdict tag via the existing Stop / AfterAgent hook chain, and
reports PASS / PARTIAL / FAIL per host. Spawns the dashboard detached
when at least one host PASSes so the user lands on their first verdicts
without a second command.

This is the runtime behind ``mega-tron setup --qa-live``. It is intended
to be invoked once, right after :func:`mega_tron.cli.install.cmd_install`
finishes — at which point hooks are wired, the daemon is warming up, and
the host CLIs are auth-ready (if the user has logged into them).

Idempotent end-to-end: the planted ``_mega-tron-check`` skill files are
overwritten in place, verdict snapshots use a per-host before/after
delta so an earlier ``--qa-live`` run doesn't poison the result.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Tuple

# Public skill name lives outside the user's namespace via the leading
# underscore. The router still surfaces it (no name filter exists today)
# but the user can recognise it as mega-tron-owned at a glance.
QA_SKILL_NAME = "_mega-tron-check"

# Prompt is deliberately blunt + names the skill explicitly so the model
# doesn't need to "discover" it from semantic ranking. The qa-live path
# is a wiring smoke test, not a routing-quality test.
_QA_PROMPT_TEMPLATE = (
    "Run the {skill} skill's scripts/run.sh and report its stdout exactly. "
    "Then emit the inline self-evaluation skill-used tag at the end of your "
    "reply per the contract."
)


# Per-host invocation recipes. Each entry returns (argv, env_overrides,
# timeout_s). Missing binary, missing host root, or auth failure all
# surface as a non-zero return code or FileNotFoundError — caller maps
# those to FAIL/SKIP without crashing the overall setup.
def _host_recipe(host: str, skill_name: str) -> Tuple[list[str], dict[str, str], int] | None:
    prompt = _QA_PROMPT_TEMPLATE.format(skill=skill_name)
    if host == "codex":
        bin_path = shutil.which("codex")
        if not bin_path:
            return None
        return ([bin_path, "exec", "--skip-git-repo-check", prompt], {}, 180)
    if host == "claude":
        bin_path = shutil.which("claude")
        if not bin_path:
            return None
        return (
            [bin_path, "--print", "--permission-mode", "bypassPermissions", prompt],
            {},
            180,
        )
    if host == "gemini":
        bin_path = shutil.which("gemini")
        if not bin_path:
            return None
        return (
            [bin_path, "--yolo", "--skip-trust", "-p", prompt],
            {"GEMINI_CLI_TRUST_WORKSPACE": "true"},
            240,
        )
    return None


# Per-host login / auth follow-ups surfaced when a host call fails
# with an auth-shaped error. Keep these short and actionable — the
# user sees this line *immediately after* an end-to-end check
# fails, so the next step needs to be obvious.
_HOST_LOGIN_HINTS: dict[str, str] = {
    "codex": "run `codex login` (or set OPENAI_API_KEY) and retry",
    "claude": "run `claude login` (or set ANTHROPIC_API_KEY) and retry",
    "gemini": "run `gemini` once and complete the OAuth prompt, or set GEMINI_API_KEY, then retry",
}

# Substrings (lowercased) that strongly suggest an auth / quota /
# rate-limit error rather than a real bug. Used to upgrade a generic
# "rc=N" failure into a "needs login" hint so the user isn't left
# guessing whether to debug or just `xxx login`.
_AUTH_FAILURE_HINTS: tuple[str, ...] = (
    "login",
    "unauthorized",
    "unauthenticated",
    "authentication",
    "401",
    "403",
    "api key",
    "api_key",
    "credentials",
    "expired",
    "token",
    "quota",
    "rate limit",
    "rate-limit",
    "ratelimit",
    "billing",
    "permission denied",
    "must be logged in",
    "not signed in",
    "sign in",
    "no oauth",
    "oauth",
)


def _looks_like_auth_failure(stderr: str | None, stdout: str | None) -> bool:
    """Return True if the failure output contains an auth-shaped hint.

    Both streams are scanned: some host CLIs print auth errors to
    stdout (e.g. when JSON output is on). Best-effort substring match
    — a few false positives are fine since the worst case is showing
    the user an extra "login" hint that doesn't apply.
    """
    blob = ((stderr or "") + "\n" + (stdout or "")).lower()
    return any(h in blob for h in _AUTH_FAILURE_HINTS)


_HOST_ROOTS: dict[str, Path] = {
    "codex": Path.home() / ".codex" / "skills",
    "claude": Path.home() / ".claude" / "skills",
    "gemini": Path.home() / ".gemini" / "skills",
}

# SQLite verdicts.host column uses the longer raw names for claude/gemini.
_HOST_DB_NAMES: dict[str, str] = {
    "codex": "codex",
    "claude": "claude_code",
    "gemini": "gemini_cli",
}

_HOST_LABELS: dict[str, str] = {
    "codex": "codex",
    "claude": "claude",
    "gemini": "gemini",
}


def unplant_qa_skill(host: str) -> bool:
    """Remove the QA marker skill from ``host``'s skills root.

    Idempotent: missing root / missing skill dir are both treated as
    success (the goal is "this skill is not present" — already-absent
    is fine). Returns True if a directory was actually removed.

    Called by the host uninstallers so `mega-tron setup --uninstall`
    cleans up any marker left behind by a prior `qa-live` run.
    """
    import shutil

    root = _HOST_ROOTS.get(host)
    if root is None or not root.exists():
        return False
    skill_dir = root / QA_SKILL_NAME
    if not skill_dir.exists():
        return False
    shutil.rmtree(skill_dir, ignore_errors=True)
    return not skill_dir.exists()


def _plant_qa_skill(host: str) -> Path | None:
    """Idempotently create the QA skill under the host's skills root.

    Returns the planted SKILL.md path, or None if the host root is
    missing on disk (host present but never set up — skip rather than
    create a partial scaffold).
    """
    root = _HOST_ROOTS.get(host)
    if root is None or not root.exists():
        return None
    skill_dir = root / QA_SKILL_NAME
    scripts_dir = skill_dir / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)

    label = _HOST_LABELS[host]
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        "---\n"
        f"name: {QA_SKILL_NAME}\n"
        "description: 'USE WHEN: mega-tron is verifying end-to-end verdict capture. "
        f"Run scripts/run.sh and report its stdout in the reply.'\n"
        "---\n"
        "\n"
        f"# {QA_SKILL_NAME}\n"
        "\n"
        "mega-tron self-check skill. Run `scripts/run.sh`, report its stdout exactly.\n",
        encoding="utf-8",
    )

    run_sh = scripts_dir / "run.sh"
    run_sh.write_text(
        "#!/usr/bin/env sh\n"
        f'echo "MEGA-TRON-CHECK-OK ({label})"\n',
        encoding="utf-8",
    )
    run_sh.chmod(0o755)
    return skill_md


def _snapshot_verdict_count(host: str) -> int:
    """Return current count of QA-skill verdicts for ``host`` in SQLite.

    Returns 0 if the store can't be opened — qa-live treats missing store
    as a clean slate.
    """
    try:
        from mega_tron.verdicts.store import Store
    except Exception:  # noqa: BLE001
        return 0
    db_name = _HOST_DB_NAMES.get(host, host)
    try:
        store = Store()
        store.initialize()
        with store._connect() as conn:  # noqa: SLF001 (intentional: count-only read)
            row = conn.execute(
                "SELECT COUNT(*) FROM verdicts WHERE host=? AND skill_name=?",
                (db_name, QA_SKILL_NAME),
            ).fetchone()
        return int(row[0]) if row else 0
    except Exception:  # noqa: BLE001
        return 0


def _run_host_call(host: str, skill_name: str) -> tuple[str, str, float]:
    """Drive one non-interactive call. Returns (status, detail, elapsed_s).

    status ∈ {"CALLED", "SKIP", "FAIL", "NEEDS_LOGIN"} — verdict
    verification happens after this returns and may downgrade
    CALLED → PARTIAL. NEEDS_LOGIN is reserved for failures whose stderr
    looks like an authentication problem; the caller surfaces the
    per-host login hint instead of a raw stderr tail.
    """
    recipe = _host_recipe(host, skill_name)
    if recipe is None:
        return ("SKIP", "CLI binary not on PATH", 0.0)
    argv, env_overrides, timeout_s = recipe
    env = {**os.environ, **env_overrides}
    started = time.monotonic()
    try:
        result = subprocess.run(
            argv,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except FileNotFoundError:
        return ("SKIP", "CLI binary disappeared mid-call", time.monotonic() - started)
    except subprocess.TimeoutExpired:
        return (
            "FAIL",
            f"host call timed out after {timeout_s}s",
            time.monotonic() - started,
        )
    except Exception as e:  # noqa: BLE001
        return ("FAIL", f"subprocess error: {e}", time.monotonic() - started)
    elapsed = time.monotonic() - started
    if result.returncode != 0:
        if _looks_like_auth_failure(result.stderr, result.stdout):
            hint = _HOST_LOGIN_HINTS.get(host, "complete CLI auth and retry")
            return ("NEEDS_LOGIN", hint, elapsed)
        stderr_tail = (result.stderr or "").strip().splitlines()[-1:] or [""]
        return ("FAIL", f"rc={result.returncode}: {stderr_tail[0][:120]}", elapsed)
    return ("CALLED", "completed cleanly", elapsed)


def _launch_dashboard_detached(port: int = 7531) -> int | None:
    """Spawn ``mega-tron dashboard`` as a detached background process.

    Mirrors :func:`mega_tron.daemon.spawn_detached` so the dashboard
    outlives ``setup``'s parent shell. The browser open is best-effort
    — if no GUI, the user still has the URL.
    """
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "mega_tron.cli", "dashboard", "--no-open",
             "--port", str(port)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except (OSError, ImportError):
        return None
    # Give the HTTP server a beat to bind before we open the browser
    # (otherwise the first GET races the listen() and returns connection
    # refused, which is a confusing first impression).
    time.sleep(1.5)
    try:
        import webbrowser

        webbrowser.open(f"http://127.0.0.1:{port}/")
    except Exception:  # noqa: BLE001
        pass
    return proc.pid


def run_qa_live(wired_hosts: list[str]) -> int:
    """End-to-end self-check across every wired host.

    Returns 0 if at least one host PASSes, 1 if every host fails or is
    skipped. Always returns rather than raising — setup must keep its
    exit code regardless of qa-live outcome.
    """
    if not wired_hosts:
        print(
            "[check] --qa-live skipped: no hosts wired this run.",
            file=sys.stderr,
        )
        return 1

    print(
        "\n[check] --qa-live: verifying end-to-end verdict capture across "
        f"{len(wired_hosts)} host(s)...",
        file=sys.stderr,
    )

    # 1. Plant QA skills idempotently. A wired host is one mega-tron
    #    just registered hooks for — meaning the user has the host's
    #    profile dir (~/.codex etc) or the binary on PATH. We still
    #    re-check the skills/ root in case the user removed it after
    #    setup; missing root = host effectively uninstalled, skip with
    #    a clear note rather than create files in an empty directory.
    planted: list[str] = []
    for host in wired_hosts:
        recipe = _host_recipe(host, QA_SKILL_NAME)
        if recipe is None:
            print(
                f"[check] {_HOST_LABELS.get(host, host):7s} SKIP    "
                f"`{host}` CLI not on PATH — install it (or add ~/.local/bin to PATH) and re-run",
                file=sys.stderr,
            )
            continue
        if _plant_qa_skill(host) is not None:
            planted.append(host)
        else:
            print(
                f"[check] {_HOST_LABELS.get(host, host):7s} SKIP    "
                f"no ~/{_HOST_ROOTS[host].relative_to(Path.home())} directory — skip this host",
                file=sys.stderr,
            )

    if not planted:
        print(
            "[check] no usable host detected; nothing to verify. "
            "Install at least one of codex / claude / gemini (and log in) before --qa-live.",
            file=sys.stderr,
        )
        return 1

    # 2. Snapshot per-host verdict counts so we can detect new inserts
    #    cleanly across re-runs.
    before: dict[str, int] = {h: _snapshot_verdict_count(h) for h in planted}

    # 3. Drive each host. The hooks fire automatically as part of the
    #    host's own lifecycle — we just need a clean return.
    results: dict[str, tuple[str, str, float]] = {}
    for host in planted:
        results[host] = _run_host_call(host, QA_SKILL_NAME)

    # 4. Verify verdict propagation. CALLED + verdict landed = PASS;
    #    CALLED but no verdict = PARTIAL (host ran but tag wasn't
    #    captured — likely tracker / regex bug or model didn't tag).
    after: dict[str, int] = {h: _snapshot_verdict_count(h) for h in planted}
    final: dict[str, tuple[str, str, float]] = {}
    for host in planted:
        status, detail, elapsed = results[host]
        if status == "CALLED":
            delta = after[host] - before[host]
            if delta > 0:
                final[host] = ("PASS", f"verdict captured ({delta} new row)", elapsed)
            else:
                final[host] = (
                    "PARTIAL",
                    "host ran but no verdict reached SQLite",
                    elapsed,
                )
        else:
            final[host] = (status, detail, elapsed)

    # 5. Per-host summary. NEEDS_LOGIN gets a wider status column so
    #    the per-host login hint isn't squashed against the timing.
    n_pass = 0
    needs_login: list[str] = []
    for host in planted:
        status, detail, elapsed = final[host]
        if status == "PASS":
            n_pass += 1
        elif status == "NEEDS_LOGIN":
            needs_login.append(host)
        label = _HOST_LABELS.get(host, host)
        print(
            f"[check] {label:7s} {status:11s} ({elapsed:.1f}s)  {detail}",
            file=sys.stderr,
        )
    print(
        f"[check] {n_pass}/{len(planted)} host(s) verified end-to-end.",
        file=sys.stderr,
    )

    # 6. At least one PASS → spawn dashboard + open browser.
    if n_pass == 0:
        if needs_login:
            for h in needs_login:
                print(
                    f"[check] {_HOST_LABELS.get(h, h)}: "
                    f"{_HOST_LOGIN_HINTS.get(h, 'authenticate the CLI first')}.",
                    file=sys.stderr,
                )
            print(
                "[check] After logging in, re-run `mega-tron setup --qa-live` "
                "(no need to re-do `uv tool install`).",
                file=sys.stderr,
            )
        else:
            print(
                "[check] no host PASSed; skipping dashboard launch.",
                file=sys.stderr,
            )
        return 1

    pid = _launch_dashboard_detached()
    if pid is None:
        print(
            "[check] dashboard launch skipped (subprocess failed). "
            "Run `mega-tron dashboard` manually.",
            file=sys.stderr,
        )
    else:
        print(
            f"[check] dashboard launched in background (PID {pid}) at "
            "http://127.0.0.1:7531/ — opening your browser. "
            f"Stop with `kill {pid}` or just close the tab.",
            file=sys.stderr,
        )
    return 0
