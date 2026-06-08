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
from typing import NamedTuple, Tuple

# Imported at module top (not locally) so tests can monkeypatch
# ``qa_live._discover_running`` / ``qa_live._port_is_bound`` the same way
# they already patch ``qa_live.subprocess`` / ``qa_live.time``.
from mega_tron.cli.dashboard_procs import _discover_running, _port_is_bound

# Public skill name lives outside the user's namespace via the leading
# underscore. The router still surfaces it (no name filter exists today)
# but the user can recognise it as mega-tron-owned at a glance.
QA_SKILL_NAME = "_mega-tron-check"

# Prompt is deliberately blunt + names the skill explicitly so the model
# doesn't need to "discover" it from semantic ranking. The qa-live path
# is a wiring smoke test, not a routing-quality test.
#
# The trailer is spelled out — name + verdict + reason form, exact tag
# shape — because the original short version ("emit the skill-used tag
# per the contract") was being skipped by Codex / Gemini on the marker
# turn often enough to be the dominant cause of false PARTIAL results.
# A short marker prompt doesn't carry enough context for the model to
# remember a contract from the system prompt; making the trailer part
# of the instruction itself fixes that.
_QA_PROMPT_TEMPLATE = (
    "Run the {skill} skill's scripts/run.sh and report its stdout exactly.\n"
    "\n"
    "REQUIRED: end your reply with EXACTLY ONE line in this form, on its own line:\n"
    '  <skill-used name="{skill}" verdict="HELPFUL" reason="<one sentence about '
    "what scripts/run.sh did>\"/>\n"
    "\n"
    "This tag is mandatory — the mega-tron self-check fails without it."
)


# Default per-host call budgets in seconds. Tuned for a *cold* first run:
# embedder model download (130 MB – 570 MB), the host CLI's own cold-
# start, and one full provider round-trip can together breach the
# original 180 s ceiling on a fresh laptop. The user can override the
# floor via MEGA_QA_TIMEOUT_S — useful for slow links or when chaining
# qa-live behind a `setup` that just downloaded the embedder fresh.
_DEFAULT_TIMEOUTS_S: dict[str, int] = {
    "codex": 300,
    "claude": 300,
    "gemini": 360,
}


def _timeout_for(host: str) -> int:
    base = _DEFAULT_TIMEOUTS_S.get(host, 300)
    override = os.environ.get("MEGA_QA_TIMEOUT_S", "").strip()
    if not override:
        return base
    try:
        # The override is a floor, not a cap: it only widens. Anything
        # narrower than the per-host default is ignored so a misguided
        # `MEGA_QA_TIMEOUT_S=30` doesn't turn every host into FAIL.
        return max(base, int(override))
    except ValueError:
        return base


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
        return ([bin_path, "exec", "--skip-git-repo-check", prompt], {}, _timeout_for("codex"))
    if host == "claude":
        bin_path = shutil.which("claude")
        if not bin_path:
            return None
        return (
            [bin_path, "--print", "--permission-mode", "bypassPermissions", prompt],
            {},
            _timeout_for("claude"),
        )
    if host == "gemini":
        bin_path = shutil.which("gemini")
        if not bin_path:
            return None
        return (
            [bin_path, "--yolo", "--skip-trust", "-p", prompt],
            {"GEMINI_CLI_TRUST_WORKSPACE": "true"},
            _timeout_for("gemini"),
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


def _newest_transcript_for(host: str) -> Path | None:
    """Return the most recent transcript file for ``host``, or None.

    Used for post-call diagnostics: when a host call returns CALLED but
    no verdict landed (PARTIAL), we want to tell the user *whether the
    model emitted the tag at all*. The two cases need different fixes
    — see ``_diagnose_partial`` for the branch logic.
    """
    home = Path.home()
    candidates: list[Path] = []
    try:
        if host == "codex":
            sessions = home / ".codex" / "sessions"
            if sessions.exists():
                candidates = list(sessions.rglob("*.jsonl"))
        elif host == "claude":
            projects = home / ".claude" / "projects"
            if projects.exists():
                candidates = list(projects.rglob("*.jsonl"))
        elif host == "gemini":
            chats = home / ".gemini" / "tmp" / "mega-tron" / "chats"
            if chats.exists():
                candidates = list(chats.glob("*.jsonl"))
    except OSError:
        return None
    if not candidates:
        return None
    try:
        return max(candidates, key=lambda p: p.stat().st_mtime)
    except (OSError, ValueError):
        return None


def _marker_tag_in_last_assistant(host: str) -> bool:
    """Return True if the host's newest transcript ends with a well-formed
    `<skill-used name="_mega-tron-check" verdict=... />` tag in the last
    assistant message. Used to recognise the qa-live success path even
    when the host Stop hook dropped the verdict on a `claimed_use` gate
    (tag emitted but no operational trace for that name — common when
    Gemini's transcript can't log script execution, or when the model
    runs a script with a different name than the tag).

    The `_mega-tron-check` marker skill is planted by qa-live itself —
    a positive tag is sufficient proof the prompt round-trip worked
    end-to-end, even without a matching exec_command. Stop hook's
    general claimed_use rejection stays in place to protect real-world
    catalogs from hallucinated tags; this helper is the narrow qa-live
    exception.
    """
    tp = _newest_transcript_for(host)
    if tp is None:
        return False
    try:
        from mega_tron.tracker import SELF_REPORT_RE, _parse_attrs, extract_last_assistant_text

        text = extract_last_assistant_text(tp) or ""
    except Exception:  # noqa: BLE001
        return False
    for m in SELF_REPORT_RE.finditer(text):
        attrs_blob = m.group("attrs1") or ""
        if not attrs_blob:
            continue
        attrs = _parse_attrs(attrs_blob)
        if (attrs.get("name") or "").strip() == QA_SKILL_NAME:
            if (attrs.get("verdict") or "").strip().upper() in {
                "HELPFUL", "HARMFUL", "NEUTRAL"
            }:
                return True
    return False


def _diagnose_partial(host: str) -> str:
    """Build an actionable PARTIAL hint by inspecting the host's
    newest transcript.

    Outcomes:
      - transcript file missing: "host wrote no transcript" — wiring
        issue (hook not firing, or host CLI crashed before stop).
      - assistant text contains the tag: tracker rejected what the
        model actually emitted — bug on our side.
      - assistant text does NOT contain the tag: model skipped the
        contract trailer on this short marker prompt. Retry usually
        clears it.

    Critically, we only inspect *assistant-role* text. The transcript
    blob also contains the host's system / user messages (AGENTS.md,
    CLAUDE.md, GEMINI.md guidance) which themselves include literal
    `<skill-used name="..." verdict="..."/>` *example* text. Substring-
    matching the whole file would treat that example as proof the model
    emitted the tag and misdiagnose every PARTIAL as "tracker bug".
    """
    tp = _newest_transcript_for(host)
    if tp is None:
        return (
            "host wrote no transcript — likely the hook isn't firing. "
            "Verify with `cat ~/.codex/hooks.json | head` (codex) / "
            "`cat ~/.claude/settings.json | grep -A2 Stop` (claude) / "
            "`cat ~/.gemini/settings.json | grep -A2 AfterAgent` (gemini)."
        )
    try:
        from mega_tron.tracker import extract_last_assistant_text

        assistant_text = extract_last_assistant_text(tp)
    except Exception:  # noqa: BLE001
        # Defensive: if the tracker can't parse the transcript shape,
        # fall back to "could not read" rather than crashing qa-live.
        return (
            f"could not parse {tp.name} to extract assistant text; "
            "re-run qa-live once."
        )
    if "<skill-used" in (assistant_text or ""):
        return (
            f"model EMITTED the tag in {tp.name} but the tracker rejected it. "
            "This is a mega-tron bug — please re-run once, and if it persists "
            f"file an issue with {tp} attached."
        )
    return (
        f"model did NOT emit a <skill-used> tag in {tp.name}. "
        "Usually transient — re-run qa-live once. If it persists, confirm "
        "the host's guidance file has the mega-tron sentinel block: "
        f"`grep -c 'mega-tron' ~/{_GUIDANCE_FILES.get(host, 'AGENTS.md')}`."
    )


_GUIDANCE_FILES: dict[str, str] = {
    "codex": ".codex/AGENTS.md",
    "claude": ".claude/CLAUDE.md",
    "gemini": ".gemini/GEMINI.md",
}


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
            (
                f"host call timed out after {timeout_s}s. First call "
                "cold-loads the embedder + the host CLI itself; on a "
                "fresh laptop this can exceed the default. Try: "
                "`mega-tron daemon serve &` to pre-warm the router, "
                "or set MEGA_QA_TIMEOUT_S=600 and re-run qa-live."
            ),
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


class DashboardLaunch(NamedTuple):
    """Outcome of :func:`_launch_dashboard_detached`.

    ``status`` is one of:
      - ``"spawned"``  → we started a new dashboard; ``pid`` is set.
      - ``"existing"`` → one was already running; ``pid`` is None,
        ``url`` points at the dashboard we found.
      - ``"failed"``   → the spawn raised; ``pid`` is None, ``url`` None.
    """

    pid: int | None
    status: str
    url: str | None = None


def _launch_dashboard_detached(port: int = 7531) -> DashboardLaunch:
    """Spawn ``mega-tron dashboard`` as a detached background process,
    unless one is already running.

    Mirrors :func:`mega_tron.daemon.spawn_detached` so the dashboard
    outlives ``setup``'s parent shell. The browser open is best-effort
    — if no GUI, the user still has the URL.

    Guard (fix for #3): qa-live used to spawn unconditionally, which on a
    box where the user already runs a dashboard on a *non-loopback* bind
    (e.g. ``--host 172.18.0.1 --port 7531``) left a redundant
    ``127.0.0.1:7531`` listener the user had to kill by hand. We now skip
    the spawn when either check fires:
      - a mega-tron ``dashboard`` process is already running on this port
        (catches any bind address, incl. a non-loopback one — the issue's
        primary scenario), or
      - the port already answers on loopback (the backstop: a
        non-mega-tron holder, a dashboard launched without an explicit
        ``--port``, or one whose argv we couldn't parse, on ``127.0.0.1``
        / ``0.0.0.0``).
    """
    # (1) An existing mega-tron dashboard process — regardless of which
    # interface it bound. We only trust an entry whose port we actually
    # parsed and that matches our target: a ``port is None`` entry is
    # ambiguous (the argv parser found no --port), and trusting it would
    # let an unrelated process that merely *mentions* "dashboard" suppress
    # a legitimate spawn. The port check below is the backstop for a real
    # dashboard launched without an explicit --port.
    for proc in _discover_running():
        if proc.kind != "dashboard" or proc.port != port:
            continue
        host = proc.host or "127.0.0.1"
        return DashboardLaunch(None, "existing", f"http://{host}:{proc.port}/")

    # (2) Something is already bound to the port on loopback (could be a
    # non-mega-tron process, or a mega-tron one whose argv we couldn't
    # parse). Skip rather than pile a second listener on top.
    bound_host = _port_is_bound(port)
    if bound_host is not None:
        return DashboardLaunch(None, "existing", f"http://{bound_host}:{port}/")

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
        return DashboardLaunch(None, "failed", None)
    # Give the HTTP server a beat to bind before we open the browser
    # (otherwise the first GET races the listen() and returns connection
    # refused, which is a confusing first impression).
    time.sleep(1.5)
    try:
        import webbrowser

        webbrowser.open(f"http://127.0.0.1:{port}/")
    except Exception:  # noqa: BLE001
        pass
    return DashboardLaunch(proc.pid, "spawned", f"http://127.0.0.1:{port}/")


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
    # Cold-start expectation: the first qa-live after a fresh `setup`
    # is the slowest one. We surface this up front so a single timeout
    # isn't mistaken for a broken install — the runbook is "re-run
    # once", not "file a bug".
    print(
        "[check] First call is slowest: the router daemon, the embedder "
        "model, and the host CLI all cold-load. Per-host budget is "
        "5–6 min by default; widen via MEGA_QA_TIMEOUT_S=<seconds>.",
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
    #    CALLED + marker tag in transcript but no SQLite row = also PASS
    #    (Stop hook's `claimed_use` gate dropped the tag because no
    #    exec_command for that exact name was logged — Gemini transcripts
    #    can't log execs at all, and Codex models sometimes run a
    #    similarly-named user skill instead of the planted marker. The
    #    full hook+tracker+regex pipeline still ran, which is what
    #    qa-live is meant to verify).
    #    CALLED + no marker tag in transcript = PARTIAL — inspect why.
    after: dict[str, int] = {h: _snapshot_verdict_count(h) for h in planted}
    final: dict[str, tuple[str, str, float]] = {}
    for host in planted:
        status, detail, elapsed = results[host]
        if status == "CALLED":
            delta = after[host] - before[host]
            if delta > 0:
                final[host] = ("PASS", f"verdict captured ({delta} new row)", elapsed)
            elif _marker_tag_in_last_assistant(host):
                final[host] = (
                    "PASS",
                    "marker tag emitted (hook+tracker+regex verified; "
                    "Stop hook gated the SQLite row on no exec trace, "
                    "which is expected for the qa-live marker skill)",
                    elapsed,
                )
            else:
                hint = _diagnose_partial(host)
                final[host] = ("PARTIAL", hint, elapsed)
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
                "[check] After logging in, re-run `mega-tron qa-live` "
                "(no need to re-do `uv tool install` or `setup`).",
                file=sys.stderr,
            )
        else:
            # Most zero-PASS-no-login cases are cold-start timeouts or
            # the model skipping the tag on the first turn. Both are
            # routinely fixed by one retry.
            print(
                "[check] no host PASSed; skipping dashboard launch. "
                "Most first-run failures clear on a second attempt — try "
                "`mega-tron qa-live` once more. If a host stays PARTIAL "
                "or FAIL after the retry, follow the per-host hint above.",
                file=sys.stderr,
            )
        return 1

    launch = _launch_dashboard_detached()
    if launch.status == "existing":
        print(
            f"[check] using existing dashboard at {launch.url} "
            "— skipping launch.",
            file=sys.stderr,
        )
    elif launch.status == "failed":
        print(
            "[check] dashboard launch skipped (subprocess failed). "
            "Run `mega-tron dashboard` manually.",
            file=sys.stderr,
        )
    else:
        print(
            f"[check] dashboard launched in background (PID {launch.pid}) at "
            f"{launch.url} — opening your browser. "
            f"Stop with `kill {launch.pid}` or just close the tab.",
            file=sys.stderr,
        )
    return 0
