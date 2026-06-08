"""Unit tests for ``mega-tron setup --qa-live``.

The end-to-end QA path is mostly orchestration: plant a skill, call a
host CLI, snapshot SQLite, spawn a dashboard. We don't drive a real
host CLI from CI — that would require provider auth and would gate the
test suite on external services. Instead we verify each seam:

* ``_plant_qa_skill`` writes idempotent skill files.
* ``_host_recipe`` dispatches the right argv per host and skips when
  the binary is missing.
* ``_snapshot_verdict_count`` returns 0 on a missing store, and
  reflects rows when present.
* ``run_qa_live`` returns the right exit code in the three terminal
  states (no hosts wired / all SKIP / one PASS).
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from mega_tron.cli import qa_live


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # qa_live captures host root paths at module import time. Rebuild
    # the lookup against the fake home so the tests don't reach into
    # the developer's actual ~/.codex etc.
    monkeypatch.setitem(qa_live._HOST_ROOTS, "codex", tmp_path / ".codex" / "skills")
    monkeypatch.setitem(qa_live._HOST_ROOTS, "claude", tmp_path / ".claude" / "skills")
    monkeypatch.setitem(qa_live._HOST_ROOTS, "gemini", tmp_path / ".gemini" / "skills")
    return tmp_path


def test_plant_qa_skill_creates_skill_files(fake_home):
    (fake_home / ".codex" / "skills").mkdir(parents=True)

    skill_md = qa_live._plant_qa_skill("codex")

    assert skill_md is not None
    assert skill_md.exists()
    body = skill_md.read_text()
    assert "name: _mega-tron-check" in body
    run_sh = skill_md.parent / "scripts" / "run.sh"
    assert run_sh.exists()
    assert "MEGA-TRON-CHECK-OK (codex)" in run_sh.read_text()
    # Executable bit set so the host's bash can run it directly.
    assert run_sh.stat().st_mode & 0o111


def test_plant_qa_skill_is_idempotent(fake_home):
    (fake_home / ".claude" / "skills").mkdir(parents=True)

    first = qa_live._plant_qa_skill("claude")
    second = qa_live._plant_qa_skill("claude")

    assert first == second
    # Both calls should leave a single skill directory, not duplicates.
    skill_dir = fake_home / ".claude" / "skills" / "_mega-tron-check"
    assert sorted(p.name for p in skill_dir.iterdir()) == ["SKILL.md", "scripts"]


def test_plant_qa_skill_skips_when_root_missing(fake_home):
    # No ~/.gemini/skills directory exists — host is considered absent.
    assert qa_live._plant_qa_skill("gemini") is None


def test_unplant_qa_skill_removes_marker(fake_home):
    """`mega-tron setup --uninstall` calls this. A planted marker
    skill — and everything under it — must be gone afterwards. The
    marker has description text that could otherwise drift into top-K
    rankings on real prompts."""
    (fake_home / ".codex" / "skills").mkdir(parents=True)
    qa_live._plant_qa_skill("codex")
    skill_dir = fake_home / ".codex" / "skills" / "_mega-tron-check"
    assert skill_dir.exists()

    removed = qa_live.unplant_qa_skill("codex")
    assert removed is True
    assert not skill_dir.exists()
    # Sibling root directory is untouched.
    assert (fake_home / ".codex" / "skills").exists()


def test_unplant_qa_skill_is_idempotent_on_absence(fake_home):
    """Calling unplant when there's nothing to remove must be a clean
    no-op — uninstall must never crash because qa-live was never run."""
    (fake_home / ".claude" / "skills").mkdir(parents=True)
    assert qa_live.unplant_qa_skill("claude") is False
    # Missing root entirely also returns False, no crash.
    assert qa_live.unplant_qa_skill("gemini") is False


def test_host_recipe_returns_none_when_binary_missing(monkeypatch):
    monkeypatch.setattr(qa_live.shutil, "which", lambda name: None)
    for host in ("codex", "claude", "gemini"):
        assert qa_live._host_recipe(host, "_mega-tron-check") is None


def test_host_recipe_dispatches_per_host(monkeypatch):
    monkeypatch.setattr(
        qa_live.shutil, "which", lambda name: f"/fake/bin/{name}"
    )

    argv_codex, env_codex, t_codex = qa_live._host_recipe("codex", "S")
    assert argv_codex[0] == "/fake/bin/codex"
    assert argv_codex[1] == "exec"
    assert "--skip-git-repo-check" in argv_codex
    assert env_codex == {}
    assert t_codex > 0

    argv_claude, env_claude, _ = qa_live._host_recipe("claude", "S")
    assert argv_claude[0] == "/fake/bin/claude"
    assert "--print" in argv_claude
    assert "bypassPermissions" in argv_claude

    argv_gemini, env_gemini, _ = qa_live._host_recipe("gemini", "S")
    assert argv_gemini[0] == "/fake/bin/gemini"
    assert "--yolo" in argv_gemini
    assert env_gemini.get("GEMINI_CLI_TRUST_WORKSPACE") == "true"


def test_host_recipe_unknown_host():
    assert qa_live._host_recipe("hermes", "S") is None


def test_snapshot_verdict_count_returns_zero_on_missing_store(monkeypatch, tmp_path):
    # Force the Store path to a directory that doesn't exist yet so
    # initialize() either creates it (returns 0 rows) or raises (also 0).
    fake_store_path = tmp_path / "no-such-dir" / "store.db"
    with patch("mega_tron.verdicts.store.Store") as MockStore:
        MockStore.side_effect = OSError("nope")
        assert qa_live._snapshot_verdict_count("codex") == 0
    # And on a clean store with no matching rows, it must be 0.
    assert qa_live._snapshot_verdict_count("codex") == 0


def test_snapshot_verdict_count_reads_actual_rows(monkeypatch, tmp_path):
    """End-to-end: write a real row via record_verdict() and verify the
    snapshot picks it up under the right host name (codex stays codex)
    and ignores foreign host filters (claude won't match)."""
    db_path = tmp_path / "store.db"

    from mega_tron.verdicts.store import Store

    store = Store(path=db_path)
    store.initialize()
    wrote = store.record_verdict(
        skill_name="_mega-tron-check",
        host="codex",
        session_id="qa-snapshot-test",
        verdict="HELPFUL",
        reason="test row used by qa-live snapshot test (long enough to pass quality gate)",
    )
    assert wrote, "record_verdict should have persisted the row"

    # Patch the Store() default-path resolution so qa_live's snapshot
    # opens our temp DB instead of the user's real one.
    monkeypatch.setattr(
        "mega_tron.verdicts.store.Store",
        lambda path=None: store if path is None else Store(path=path),
    )
    assert qa_live._snapshot_verdict_count("codex") == 1
    # Different host name on the same skill should not match.
    assert qa_live._snapshot_verdict_count("claude") == 0


def test_run_qa_live_returns_1_when_no_hosts_wired(capsys):
    rc = qa_live.run_qa_live([])
    assert rc == 1
    err = capsys.readouterr().err
    assert "no hosts wired" in err


def test_run_qa_live_returns_1_when_no_skills_roots(fake_home, monkeypatch, capsys):
    # Pretend the host binaries exist on PATH (so we exercise the
    # "skills root missing" branch, not the "binary missing" branch).
    monkeypatch.setattr(qa_live.shutil, "which", lambda name: f"/fake/bin/{name}")
    rc = qa_live.run_qa_live(["codex", "claude"])
    assert rc == 1
    err = capsys.readouterr().err
    # Two hosts × SKIP per host, then the summary line.
    assert err.count("SKIP") >= 2
    assert "no usable host detected" in err


def test_run_qa_live_pass_path_spawns_dashboard(fake_home, monkeypatch, capsys):
    """Happy path: one host CLI returns rc=0, verdict count jumps,
    dashboard is launched detached."""
    (fake_home / ".codex" / "skills").mkdir(parents=True)

    # Pretend `codex` exists on PATH so the recipe builds.
    monkeypatch.setattr(
        qa_live.shutil, "which", lambda name: f"/fake/bin/{name}"
    )

    # Stub the subprocess.run that drives the host CLI: pretend it
    # succeeded. We mimic the side-effect of the host's Stop hook by
    # bumping the SQLite count between before/after.
    counts = {"codex": 0}

    def fake_snapshot(host: str) -> int:
        return counts.get(host, 0)

    def fake_subprocess_run(*args, **kwargs):
        # After the host "call" completes, simulate the stop hook
        # writing a row to SQLite.
        counts["codex"] = 1
        return subprocess.CompletedProcess(args[0], 0, stdout="", stderr="")

    monkeypatch.setattr(qa_live, "_snapshot_verdict_count", fake_snapshot)
    monkeypatch.setattr(qa_live.subprocess, "run", fake_subprocess_run)

    # Capture the dashboard spawn so we don't actually open an HTTP port.
    dashboard_calls = []

    def fake_popen(argv, **kwargs):
        dashboard_calls.append((argv, kwargs))

        class _Proc:
            pid = 99999

        return _Proc()

    monkeypatch.setattr(qa_live.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(qa_live.time, "sleep", lambda s: None)
    # No dashboard already running, port free → spawn should proceed.
    # (Without these stubs the test would be flaky on a dev box that
    # happens to have a real dashboard up.)
    monkeypatch.setattr(qa_live, "_discover_running", lambda: [])
    monkeypatch.setattr(qa_live, "_port_is_bound", lambda port: None)
    # Block webbrowser.open too.
    import webbrowser

    monkeypatch.setattr(webbrowser, "open", lambda url: None)

    rc = qa_live.run_qa_live(["codex"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "PASS" in err
    assert "1/1 host" in err
    assert "dashboard launched" in err.lower()
    assert dashboard_calls, "expected dashboard subprocess to be spawned"
    argv = dashboard_calls[0][0]
    assert "dashboard" in argv
    assert "--no-open" in argv


def _stub_pass_path(fake_home, monkeypatch):
    """Wire up the minimal PASS-path stubs (host call + verdict bump)
    shared by the no-double-spawn tests. Leaves the dashboard probes
    (`_discover_running` / `_port_is_bound`) for the caller to set."""
    (fake_home / ".codex" / "skills").mkdir(parents=True)
    monkeypatch.setattr(qa_live.shutil, "which", lambda name: f"/fake/bin/{name}")
    counts = {"codex": 0}
    monkeypatch.setattr(
        qa_live, "_snapshot_verdict_count", lambda h: counts.get(h, 0)
    )

    def fake_run(*args, **kwargs):
        counts["codex"] = 1
        return subprocess.CompletedProcess(args[0], 0, stdout="", stderr="")

    monkeypatch.setattr(qa_live.subprocess, "run", fake_run)
    monkeypatch.setattr(qa_live.time, "sleep", lambda s: None)
    import webbrowser

    monkeypatch.setattr(webbrowser, "open", lambda url: None)

    popen_calls = []
    monkeypatch.setattr(
        qa_live.subprocess,
        "Popen",
        lambda argv, **kw: popen_calls.append(argv)
        or type("_P", (), {"pid": 99999})(),
    )
    return popen_calls


def test_existing_dashboard_process_skips_spawn(fake_home, monkeypatch, capsys):
    """A mega-tron dashboard already running on a NON-loopback bind
    (the issue's exact repro) must suppress the spawn — caught by the
    process check regardless of bind address (#3)."""
    popen_calls = _stub_pass_path(fake_home, monkeypatch)

    from mega_tron.cli.dashboard_procs import _RunningProc

    monkeypatch.setattr(
        qa_live,
        "_discover_running",
        lambda: [
            _RunningProc(pid=111, kind="dashboard", host="172.18.0.1", port=7531)
        ],
    )
    # Port probe must not even be needed, but stub it so a stray real
    # listener can't influence the result.
    monkeypatch.setattr(qa_live, "_port_is_bound", lambda port: None)

    rc = qa_live.run_qa_live(["codex"])
    assert rc == 0
    err = capsys.readouterr().err
    assert not popen_calls, "must NOT spawn when a dashboard already runs"
    assert "using existing dashboard" in err.lower()
    assert "172.18.0.1:7531" in err


def test_port_bound_no_process_skips_spawn(fake_home, monkeypatch, capsys):
    """No discoverable mega-tron dashboard process, but port 7531 is
    already bound on loopback (a non-mega-tron holder, or one whose
    argv we couldn't parse) → still skip the spawn."""
    popen_calls = _stub_pass_path(fake_home, monkeypatch)

    monkeypatch.setattr(qa_live, "_discover_running", lambda: [])
    monkeypatch.setattr(qa_live, "_port_is_bound", lambda port: "127.0.0.1")

    rc = qa_live.run_qa_live(["codex"])
    assert rc == 0
    err = capsys.readouterr().err
    assert not popen_calls, "must NOT spawn when the port is already bound"
    assert "using existing dashboard" in err.lower()
    assert "127.0.0.1:7531" in err


def test_nothing_running_spawns_dashboard(fake_home, monkeypatch, capsys):
    """Regression guard: with no existing process and a free port, the
    spawn must still happen (the original happy-path behavior)."""
    popen_calls = _stub_pass_path(fake_home, monkeypatch)

    monkeypatch.setattr(qa_live, "_discover_running", lambda: [])
    monkeypatch.setattr(qa_live, "_port_is_bound", lambda port: None)

    rc = qa_live.run_qa_live(["codex"])
    assert rc == 0
    err = capsys.readouterr().err
    assert popen_calls, "expected a dashboard spawn when nothing is running"
    argv = popen_calls[0]
    assert "dashboard" in argv and "--no-open" in argv
    assert "dashboard launched" in err.lower()


def test_run_qa_live_partial_when_host_runs_but_no_verdict(
    fake_home, monkeypatch, capsys
):
    """Host CLI returns rc=0 but verdict count stays flat — likely the
    model didn't emit a tag, or the tracker dropped it. We must mark
    PARTIAL and NOT launch the dashboard."""
    (fake_home / ".claude" / "skills").mkdir(parents=True)

    monkeypatch.setattr(qa_live.shutil, "which", lambda name: f"/fake/bin/{name}")
    monkeypatch.setattr(qa_live, "_snapshot_verdict_count", lambda h: 0)
    monkeypatch.setattr(
        qa_live.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a[0], 0, stdout="", stderr=""),
    )
    popen_calls = []
    monkeypatch.setattr(
        qa_live.subprocess,
        "Popen",
        lambda *a, **kw: popen_calls.append(a) or (_ for _ in ()).throw(
            AssertionError("dashboard should NOT spawn on all-PARTIAL")
        ),
    )
    monkeypatch.setattr(qa_live.time, "sleep", lambda s: None)

    rc = qa_live.run_qa_live(["claude"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "PARTIAL" in err
    assert "0/1 host" in err
    assert not popen_calls


def test_run_qa_live_skip_when_binary_missing(fake_home, monkeypatch, capsys):
    """A wired host whose CLI vanished from PATH must surface as SKIP,
    not crash the pipeline."""
    (fake_home / ".gemini" / "skills").mkdir(parents=True)
    monkeypatch.setattr(qa_live.shutil, "which", lambda name: None)

    rc = qa_live.run_qa_live(["gemini"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "SKIP" in err
    assert "not on PATH" in err


def test_looks_like_auth_failure_recognises_common_messages():
    assert qa_live._looks_like_auth_failure("Error: 401 Unauthorized", None)
    assert qa_live._looks_like_auth_failure(None, "please run `codex login` first")
    assert qa_live._looks_like_auth_failure("invalid api_key", "")
    assert qa_live._looks_like_auth_failure("rate limit exceeded", None)
    # False on a non-auth bug
    assert not qa_live._looks_like_auth_failure(
        "Traceback (most recent call last): ... ZeroDivisionError", None
    )


def test_run_qa_live_needs_login_routes_to_login_hint(
    fake_home, monkeypatch, capsys
):
    """If the host CLI returns an auth-shaped failure, we must NOT just
    print 'FAIL rc=...'; we must call out NEEDS_LOGIN and surface the
    per-host login command."""
    (fake_home / ".claude" / "skills").mkdir(parents=True)
    monkeypatch.setattr(qa_live.shutil, "which", lambda name: f"/fake/bin/{name}")
    monkeypatch.setattr(qa_live, "_snapshot_verdict_count", lambda h: 0)
    monkeypatch.setattr(
        qa_live.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(
            a[0], 1, stdout="", stderr="Error: 401 Unauthorized — please run claude login"
        ),
    )
    # Dashboard must NOT spawn when no host PASS-ed.
    popen_called = []
    monkeypatch.setattr(
        qa_live.subprocess,
        "Popen",
        lambda *a, **kw: popen_called.append(a) or (_ for _ in ()).throw(
            AssertionError("dashboard should NOT spawn when only NEEDS_LOGIN")
        ),
    )
    monkeypatch.setattr(qa_live.time, "sleep", lambda s: None)

    rc = qa_live.run_qa_live(["claude"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "NEEDS_LOGIN" in err
    assert "claude login" in err
    assert not popen_called


# ----------------------------------------------------------------------
# Cold-start tuning — timeouts, env override, preamble, diagnostics
# ----------------------------------------------------------------------


def test_timeout_for_uses_per_host_default(monkeypatch):
    """The raw defaults: codex/claude 300, gemini 360 (gemini lives on
    a slower OAuth path)."""
    monkeypatch.delenv("MEGA_QA_TIMEOUT_S", raising=False)
    assert qa_live._timeout_for("codex") == 300
    assert qa_live._timeout_for("claude") == 300
    assert qa_live._timeout_for("gemini") == 360


def test_timeout_for_respects_env_override(monkeypatch):
    """Env var widens but never narrows — `MEGA_QA_TIMEOUT_S=60` on
    codex must NOT shrink the budget below the 300s default."""
    monkeypatch.setenv("MEGA_QA_TIMEOUT_S", "600")
    assert qa_live._timeout_for("codex") == 600
    assert qa_live._timeout_for("gemini") == 600
    monkeypatch.setenv("MEGA_QA_TIMEOUT_S", "60")  # below floor
    assert qa_live._timeout_for("codex") == 300  # floor preserved
    monkeypatch.setenv("MEGA_QA_TIMEOUT_S", "garbage")
    assert qa_live._timeout_for("claude") == 300  # parse fail → default


def test_run_qa_live_prints_cold_start_preamble(
    fake_home, monkeypatch, capsys
):
    """The first line after the verifying-... banner must explain why
    the first call is slow, so a single timeout isn't read as a broken
    install."""
    monkeypatch.setattr(qa_live.shutil, "which", lambda name: None)
    qa_live.run_qa_live(["codex"])
    err = capsys.readouterr().err
    assert "First call is slowest" in err
    assert "MEGA_QA_TIMEOUT_S" in err


def test_partial_diagnostic_branches_on_transcript(fake_home, monkeypatch):
    """The PARTIAL detail must distinguish 'tag present, tracker dropped
    it' from 'model never emitted the tag'."""
    # No transcript at all → wiring hint.
    detail = qa_live._diagnose_partial("codex")
    assert "no transcript" in detail.lower()

    # Plant a fake transcript WITH a <skill-used> tag.
    sessions = fake_home / ".codex" / "sessions" / "2026" / "05" / "22"
    sessions.mkdir(parents=True)
    transcript = sessions / "rollout-tagged.jsonl"
    transcript.write_text(
        '{"payload":{"type":"message","role":"assistant",'
        '"content":[{"type":"output_text","text":"hi '
        '<skill-used name=\\"x\\" verdict=\\"HELPFUL\\" reason=\\"r\\"/>"}]}}\n',
        encoding="utf-8",
    )
    detail = qa_live._diagnose_partial("codex")
    assert "EMITTED" in detail
    assert "rollout-tagged.jsonl" in detail

    # Replace with a transcript WITHOUT the tag.
    transcript.write_text(
        '{"payload":{"type":"message","role":"assistant",'
        '"content":[{"type":"output_text","text":"no tag here"}]}}\n',
        encoding="utf-8",
    )
    detail = qa_live._diagnose_partial("codex")
    assert "did NOT emit" in detail
    # Points the user at the host's guidance file.
    assert "AGENTS.md" in detail


def test_partial_diagnostic_ignores_contract_example_in_user_message(
    fake_home,
):
    """Regression test for the false-EMITTED diagnosis reported on
    GitHub: a Codex transcript whose USER message embeds AGENTS.md
    (which itself contains a literal `<skill-used .../>` example)
    must NOT be mis-read as 'model EMITTED the tag'. The model in
    that report didn't tag — the substring matched the AGENTS.md
    contract example.
    """
    sessions = fake_home / ".codex" / "sessions" / "2026" / "05" / "22"
    sessions.mkdir(parents=True)
    transcript = sessions / "rollout-user-example-only.jsonl"
    # User role carries the AGENTS.md contract — *includes* a literal
    # `<skill-used name="..." verdict="..." reason="..."/>` example.
    # Assistant role replies with the script stdout, no trailing tag.
    transcript.write_text(
        '{"payload":{"type":"message","role":"user","content":[{"type":"input_text",'
        '"text":"AGENTS.md says: Tag form: '
        '<skill-used name=\\"<name>\\" verdict=\\"HELPFUL|HARMFUL|NEUTRAL\\" reason=\\"...\\"/>"}]}}\n'
        '{"payload":{"type":"message","role":"assistant","content":[{"type":"output_text",'
        '"text":"MEGA-TRON-CHECK-OK (codex)"}]}}\n',
        encoding="utf-8",
    )
    detail = qa_live._diagnose_partial("codex")
    assert "did NOT emit" in detail, (
        f"user-role contract example must not be mistaken for the model "
        f"emitting the tag; got: {detail!r}"
    )
    assert "EMITTED" not in detail


def test_marker_tag_in_last_assistant_finds_well_formed_tag(fake_home):
    """Codex transcript whose last assistant message ends with the
    canonical marker tag must be recognised as a PASS-equivalent, even
    when the Stop hook would otherwise reject it for `claimed_use` (no
    matching exec_command). This is the narrow exception qa-live makes
    for its own planted marker skill.
    """
    sessions = fake_home / ".codex" / "sessions" / "2026" / "05" / "23"
    sessions.mkdir(parents=True)
    transcript = sessions / "rollout-marker-emitted.jsonl"
    transcript.write_text(
        '{"payload":{"type":"message","role":"assistant","content":[{"type":"output_text",'
        '"text":"MEGA-TRON-CHECK-OK\\n<skill-used name=\\"_mega-tron-check\\" '
        'verdict=\\"HELPFUL\\" reason=\\"ran the script\\"/>"}]}}\n',
        encoding="utf-8",
    )
    assert qa_live._marker_tag_in_last_assistant("codex") is True


def test_marker_tag_in_last_assistant_rejects_wrong_skill_name(fake_home):
    """A `<skill-used>` tag with a different skill name (model coined
    its own / ran an unrelated skill) must NOT pass as a marker tag.
    The qa-live PASS exception is narrowly scoped to the planted
    marker name to avoid masking real hallucinations from succeeding
    as 'wiring OK'.
    """
    sessions = fake_home / ".codex" / "sessions" / "2026" / "05" / "23"
    sessions.mkdir(parents=True)
    transcript = sessions / "rollout-wrong-name.jsonl"
    transcript.write_text(
        '{"payload":{"type":"message","role":"assistant","content":[{"type":"output_text",'
        '"text":"<skill-used name=\\"some-other-skill\\" verdict=\\"HELPFUL\\" '
        'reason=\\"x\\"/>"}]}}\n',
        encoding="utf-8",
    )
    assert qa_live._marker_tag_in_last_assistant("codex") is False


def test_marker_tag_in_last_assistant_requires_canonical_verdict(fake_home):
    """A marker-named tag with an unrecognised verdict label is not
    accepted — guards against accidentally treating a hallucinated tag
    with weird attribute values as a PASS signal.
    """
    sessions = fake_home / ".codex" / "sessions" / "2026" / "05" / "23"
    sessions.mkdir(parents=True)
    transcript = sessions / "rollout-weird-verdict.jsonl"
    transcript.write_text(
        '{"payload":{"type":"message","role":"assistant","content":[{"type":"output_text",'
        '"text":"<skill-used name=\\"_mega-tron-check\\" verdict=\\"MAYBE\\" '
        'reason=\\"x\\"/>"}]}}\n',
        encoding="utf-8",
    )
    assert qa_live._marker_tag_in_last_assistant("codex") is False


def test_marker_tag_in_last_assistant_returns_false_on_missing_transcript(
    fake_home,  # noqa: ARG001 — fixture isolates HOME so no real transcript leaks in
):
    """Host wrote no transcript at all → cannot claim the marker
    pipeline succeeded. Returns False, which keeps the PARTIAL path
    so `_diagnose_partial` can surface the wiring issue."""
    assert qa_live._marker_tag_in_last_assistant("codex") is False


def test_qa_prompt_template_spells_out_required_tag():
    """The marker prompt must name the skill explicitly inside the
    required tag shape, so Codex / Gemini don't skip the trailer on a
    short prompt. The previous wording ('emit the inline self-eval
    skill-used tag per the contract') was the cause of repeated
    PARTIAL results across hosts."""
    rendered = qa_live._QA_PROMPT_TEMPLATE.format(skill="_mega-tron-check")
    assert "_mega-tron-check" in rendered
    assert 'name="_mega-tron-check"' in rendered
    assert "REQUIRED" in rendered
    assert "verdict=" in rendered
    # The "mandatory" wording is what nudges the model to actually
    # include the trailer even on a one-line stdout reply.
    assert "mandatory" in rendered


def test_run_qa_live_partial_surfaces_diagnosis(fake_home, monkeypatch, capsys):
    """When PARTIAL fires, the printed detail must come from
    _diagnose_partial, not the old generic 'host ran but no verdict'
    string — so the user can act on the real cause."""
    (fake_home / ".claude" / "skills").mkdir(parents=True)
    # Plant a Claude transcript that contains the tag — pretends the
    # model did the right thing but the tracker still dropped it.
    project = fake_home / ".claude" / "projects" / "-tmp-x"
    project.mkdir(parents=True)
    transcript = project / "session.jsonl"
    transcript.write_text(
        '{"type":"assistant","message":{"role":"assistant",'
        '"content":[{"type":"text",'
        '"text":"out <skill-used name=\\"x\\" verdict=\\"HELPFUL\\" reason=\\"r\\"/>"}]}}\n',
        encoding="utf-8",
    )

    monkeypatch.setattr(qa_live.shutil, "which", lambda name: f"/fake/bin/{name}")
    monkeypatch.setattr(qa_live, "_snapshot_verdict_count", lambda h: 0)
    monkeypatch.setattr(
        qa_live.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a[0], 0, stdout="", stderr=""),
    )
    monkeypatch.setattr(
        qa_live.subprocess,
        "Popen",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("dashboard should NOT spawn on PARTIAL")
        ),
    )
    monkeypatch.setattr(qa_live.time, "sleep", lambda s: None)

    rc = qa_live.run_qa_live(["claude"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "PARTIAL" in err
    # The diagnostic, not the legacy generic message:
    assert "EMITTED the tag" in err
    # Retry hint surfaces in the no-PASS summary.
    assert "second attempt" in err or "once more" in err
