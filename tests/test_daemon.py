"""Daemon (A1) — round-trip, fallback, ping/shutdown semantics.

We don't spawn the real BGE-loading daemon here; instead we instantiate
the server in a background thread with a FakeEmbedder factory. That keeps
the test fast (no PyTorch import) while still exercising the full socket
protocol.
"""
from __future__ import annotations

import shutil
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

from mega_tron import daemon as daemon_mod


def _short_socket_dir() -> Path:
    """Return a short directory under /tmp for AF_UNIX sockets.

    macOS caps AF_UNIX paths around 104 chars and pytest's tmp_path is too
    long. Caller owns the returned directory (use shutil.rmtree to clean up).
    """
    return Path(tempfile.mkdtemp(prefix="msr-", dir="/tmp"))


def _make_skills(tmp_path: Path) -> Path:
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "webhook-signer").mkdir()
    (skills / "webhook-signer" / "SKILL.md").write_text(
        '---\n'
        'name: webhook-signer\n'
        'description: "USE WHEN: validate webhook hmac signature"\n'
        '---\n'
    )
    (skills / "git-amend-staged").mkdir()
    (skills / "git-amend-staged" / "SKILL.md").write_text(
        '---\nname: git-amend-staged\ndescription: "USE WHEN: amend a staged git commit"\n---\n'
    )
    return skills


@pytest.fixture
def daemon_in_thread(fake_embedder):
    """Spin up the daemon in a background thread, yield its socket path."""
    sock_dir = _short_socket_dir()
    sock_path = sock_dir / "daemon.sock"

    def factory():
        return fake_embedder

    ready = threading.Event()
    t = threading.Thread(
        target=daemon_mod.serve,
        kwargs={
            "socket_path": sock_path,
            "idle_timeout_s": 30.0,
            "embedder_factory": factory,
            "ready_event": ready,
        },
        daemon=True,
    )
    t.start()
    # Daemon signals readiness once `srv.listen()` returns — no polling race.
    assert ready.wait(timeout=5.0), "daemon never signalled ready"
    yield sock_path
    daemon_mod.request_shutdown(sock_path)
    t.join(timeout=5.0)
    shutil.rmtree(sock_dir, ignore_errors=True)


def test_daemon_ping(daemon_in_thread):
    resp = daemon_mod.client_query({"op": "ping"}, socket_path=daemon_in_thread)
    assert resp is not None
    assert resp["ok"] is True
    assert "version" in resp


def test_daemon_rank_returns_additional_context(daemon_in_thread, tmp_path):
    skills = _make_skills(tmp_path)
    cache_path = tmp_path / "daemon_cache.npz"
    resp = daemon_mod.client_query(
        {
            "op": "rank",
            "prompt": "validate webhook hmac signature on incoming request",
            "skills_dir": str(skills),
            "cache_path": str(cache_path),
            "top_k": 3,
            "prepend_k": 2,
        },
        socket_path=daemon_in_thread,
    )
    assert resp is not None, "daemon returned None"
    assert resp["ok"] is True, f"daemon error: {resp}"
    ctx = resp["additional_context"]
    assert "webhook-signer" in ctx
    # additional_context replaces codex's native skill catalog with a
    # non-forcing candidate list — header, candidate line, ranked picks
    # with paths + descriptions, and the eval contract. The router
    # deliberately avoids `$` and MUST-use language so the model judges
    # fit instead of being forced.
    assert ctx.startswith("## Skills (selected for this turn")
    assert "Candidate skills for this task" in ctx
    # No `$<picked-name>` in the router's scaffolding (would trigger
    # codex's MUST-use rule). `$` may appear inside a fixture's own
    # description text — that's user-authored content, not our directive.
    assert "$webhook-signer" not in ctx
    candidate_line = next(
        line for line in ctx.splitlines() if line.startswith("Candidate skills")
    )
    assert "$" not in candidate_line
    for line in ctx.splitlines():
        if line.startswith("- "):  # meta-block entry — bare name only
            assert not line.startswith("- $")
    assert "MUST use" not in ctx
    assert "Trigger rules" not in ctx
    assert "<skill-used" in ctx
    assert str(skills) in ctx  # skill_dir path is included in the meta block


def test_daemon_unknown_op_returns_error(daemon_in_thread):
    resp = daemon_mod.client_query({"op": "nonsense"}, socket_path=daemon_in_thread)
    assert resp is not None
    assert resp["ok"] is False
    assert "unknown op" in resp["error"]


def test_daemon_empty_prompt_returns_error(daemon_in_thread, tmp_path):
    skills = _make_skills(tmp_path)
    resp = daemon_mod.client_query(
        {
            "op": "rank",
            "prompt": "   ",
            "skills_dir": str(skills),
            "cache_path": str(tmp_path / "c.npz"),
        },
        socket_path=daemon_in_thread,
    )
    assert resp["ok"] is False


def test_client_query_returns_none_when_socket_missing(tmp_path):
    """The hook's fallback contract: no socket → None → in-proc path."""
    sock = tmp_path / "no-such.sock"
    assert daemon_mod.client_query({"op": "ping"}, socket_path=sock) is None


def test_client_query_returns_none_on_invalid_response():
    """Server sends garbage — client must not crash, must return None."""
    sock_dir = _short_socket_dir()
    sock = sock_dir / "bad.sock"

    def serve_garbage():
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(str(sock))
        s.listen(1)
        conn, _ = s.accept()
        conn.recv(4096)
        conn.sendall(b"\xff\xfe garbage \xff\xfe\n")
        conn.close()
        s.close()

    t = threading.Thread(target=serve_garbage, daemon=True)
    t.start()
    for _ in range(50):
        if sock.exists():
            break
        time.sleep(0.02)
    resp = daemon_mod.client_query({"op": "ping"}, socket_path=sock)
    assert resp is None
    t.join(timeout=2.0)
    shutil.rmtree(sock_dir, ignore_errors=True)


def test_daemon_disabled_via_env(monkeypatch):
    monkeypatch.setenv("MEGA_DAEMON", "0")
    assert daemon_mod.daemon_disabled() is True
    monkeypatch.setenv("MEGA_DAEMON", "1")
    assert daemon_mod.daemon_disabled() is False
    monkeypatch.delenv("MEGA_DAEMON", raising=False)
    assert daemon_mod.daemon_disabled() is False


def test_default_socket_path_scoped_by_uid():
    p = daemon_mod.default_socket_path()
    assert str(p).endswith(".sock")
    import os

    assert str(os.getuid()) in str(p)


def test_is_running_returns_true_when_alive(daemon_in_thread):
    assert daemon_mod.is_running(daemon_in_thread) is True


def test_is_running_returns_false_when_missing(tmp_path):
    assert daemon_mod.is_running(tmp_path / "no.sock") is False


# ---------------------------------------------------------------------------
# Wisdom-curator in-flight pool — helpers + state behavior
# ---------------------------------------------------------------------------


def test_wisdom_query_key_is_deterministic():
    """Same query → same key; different queries → different keys."""
    k1 = daemon_mod._wisdom_query_key("validate webhook HMAC")
    k2 = daemon_mod._wisdom_query_key("validate webhook HMAC")
    k3 = daemon_mod._wisdom_query_key("verify JWT audience")
    assert k1 == k2
    assert k1 != k3
    # Stable length keeps the in-flight dict bounded.
    assert len(k1) == 16


def test_wisdom_inflight_ttl_defaults_to_600(monkeypatch):
    monkeypatch.delenv("MEGA_WISDOM_TTL_S", raising=False)
    assert daemon_mod._wisdom_inflight_ttl_s() == 600.0


def test_wisdom_inflight_ttl_clamps_low(monkeypatch):
    """Sub-minimum (< 60s) values clamp up — sub-second TTLs would
    effectively disable dedup."""
    monkeypatch.setenv("MEGA_WISDOM_TTL_S", "5")
    assert daemon_mod._wisdom_inflight_ttl_s() == 60.0


def test_wisdom_inflight_ttl_clamps_high(monkeypatch):
    """Above-maximum (> 1 hour) values clamp down — multi-hour caches
    would mask broken curator runs."""
    monkeypatch.setenv("MEGA_WISDOM_TTL_S", "999999")
    assert daemon_mod._wisdom_inflight_ttl_s() == 3600.0


def test_wisdom_inflight_ttl_falls_back_on_garbage(monkeypatch):
    monkeypatch.setenv("MEGA_WISDOM_TTL_S", "not-a-number")
    assert daemon_mod._wisdom_inflight_ttl_s() == 600.0


def test_daemon_state_initializes_empty_wisdom_pool():
    st = daemon_mod._DaemonState()
    assert st.wisdom_inflight == {}


def test_wisdom_check_and_fire_skipped_when_disabled(monkeypatch):
    """Without ``MEGA_WITH_WISDOM`` the check is a no-op — no curator
    subprocess, no entry inserted into the pool."""
    monkeypatch.delenv("MEGA_WITH_WISDOM", raising=False)
    st = daemon_mod._DaemonState()
    resp = st.wisdom_check_and_fire("validate webhook")
    assert resp["ok"] is True
    assert resp["status"] == "skipped"
    assert st.wisdom_inflight == {}


def test_wisdom_check_and_fire_skipped_when_curator_unset(monkeypatch):
    """Wisdom enabled but no curator path → skipped (configuration error
    surfaced via the ``reason``, never bubbled up as an exception)."""
    monkeypatch.setenv("MEGA_WITH_WISDOM", "1")
    monkeypatch.delenv("MEGA_WISDOM_CURATOR_PATH", raising=False)
    st = daemon_mod._DaemonState()
    resp = st.wisdom_check_and_fire("validate webhook")
    assert resp["ok"] is True
    assert resp["status"] == "skipped"
    assert "MEGA_WISDOM_CURATOR_PATH" in resp["reason"]


def test_wisdom_ignite_op_routes_to_state(daemon_in_thread, monkeypatch):
    """End-to-end through the daemon socket protocol: ``wisdom_ignite``
    op reaches ``_handle_request`` and returns the state's reply.

    Wisdom is left disabled in this test so the call short-circuits to
    ``skipped`` — exercises the dispatch path without spawning a real
    subprocess.
    """
    monkeypatch.delenv("MEGA_WITH_WISDOM", raising=False)
    resp = daemon_mod.client_query(
        {"op": "wisdom_ignite", "prompt": "test prompt"},
        socket_path=daemon_in_thread,
    )
    assert resp is not None
    assert resp["ok"] is True
    assert resp["status"] == "skipped"


def test_wisdom_ignite_op_rejects_empty_prompt(daemon_in_thread):
    resp = daemon_mod.client_query(
        {"op": "wisdom_ignite", "prompt": ""},
        socket_path=daemon_in_thread,
    )
    assert resp is not None
    assert resp["ok"] is False
    assert "empty prompt" in resp["error"]


def test_spawn_detached_passes_no_idle_flag(monkeypatch):
    """The auto-spawn path must launch the daemon with `--idle-timeout 0`
    so the router doesn't quietly exit after 30 idle minutes and force
    the next host turn to repay the embedder cold-load — which on
    Gemini exceeds the 60s BeforeAgent hook timeout and silently
    drops that turn's verdict.
    """
    import subprocess

    from mega_tron import daemon as daemon_mod

    captured = {}

    class _FakePopen:
        def __init__(self, argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            self.pid = 12345

    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(daemon_mod, "daemon_disabled", lambda: False)

    pid = daemon_mod.spawn_detached()
    assert pid == 12345
    argv = captured["argv"]
    assert "daemon" in argv and "serve" in argv
    # The critical assertion: the flag is present AND its value is "0".
    # Pinning both pieces so a future copy-edit can't silently revert.
    assert "--idle-timeout" in argv
    idle_idx = argv.index("--idle-timeout")
    assert argv[idle_idx + 1] == "0", (
        f"auto-spawn must pin --idle-timeout to 0 (forever); got {argv[idle_idx + 1]!r}"
    )


def test_cmd_daemon_translates_zero_idle_to_none(monkeypatch):
    """`mega-tron daemon serve --idle-timeout 0` must reach
    `daemon.serve` with `idle_timeout_s=None` (the underlying "never
    exit on idle" sentinel). Negative values are treated the same way
    to keep the surface intuitive — anything <=0 means forever.
    """
    import argparse

    from mega_tron import daemon as daemon_mod
    from mega_tron.cli.daemon import cmd_daemon

    captured = {}

    def _fake_serve(socket_path=None, idle_timeout_s=None):
        captured["socket_path"] = socket_path
        captured["idle_timeout_s"] = idle_timeout_s
        return 0

    monkeypatch.setattr(daemon_mod, "serve", _fake_serve)

    for raw in (0, 0.0, -1, -1800):
        captured.clear()
        ns = argparse.Namespace(daemon_op="serve", socket=None, idle_timeout=raw)
        rc = cmd_daemon(ns)
        assert rc == 0
        assert captured["idle_timeout_s"] is None, (
            f"idle_timeout={raw!r} should disable the idle clock entirely, "
            f"but cmd_daemon passed {captured['idle_timeout_s']!r}"
        )

    # Positive values still pass through unchanged (manual `daemon serve`
    # users may want a bounded run).
    captured.clear()
    ns = argparse.Namespace(daemon_op="serve", socket=None, idle_timeout=60.0)
    cmd_daemon(ns)
    assert captured["idle_timeout_s"] == 60.0
