"""Dashboard HTTP server — binding, routing, JSON encoding."""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import pytest

from mega_tron.dashboard import api
from mega_tron.dashboard.server import make_server
from mega_tron.verdicts.store import Store


@pytest.fixture
def server_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("MEGA_SKILL_DIRS", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("MEGA_TRON_STORE", str(tmp_path / "store.db"))
    monkeypatch.setenv(
        "MEGA_TRON_VERDICT_EMBEDDINGS", str(tmp_path / "ve.npz")
    )
    monkeypatch.delenv("MEGA_WITH_WISDOM", raising=False)
    api.reset_store_singleton_for_tests()
    api.reset_iter_skills_cache_for_tests()
    store = Store(path=tmp_path / "store.db")
    store.initialize()
    yield tmp_path, store
    api.reset_store_singleton_for_tests()
    api.reset_iter_skills_cache_for_tests()


@contextmanager
def _serving(host: str = "127.0.0.1", port: int = 0):
    """Spin a real server in a background thread on an OS-assigned
    port. Yields the bound URL."""
    server = make_server(host, port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    bound_host, bound_port = server.server_address[:2]
    url = f"http://{bound_host}:{bound_port}"
    try:
        yield url
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def _get(url: str) -> tuple[int, bytes]:
    with urllib.request.urlopen(url, timeout=2.0) as resp:
        return resp.status, resp.read()


def _get_404_tolerant(url: str) -> tuple[int, bytes]:
    """GET that doesn't raise on 4xx; returns (status, body)."""
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


# --------------------------------------------------------------------------- #
# Binding + routing
# --------------------------------------------------------------------------- #


def test_server_binds_loopback_and_serves_index(server_env):
    with _serving() as url:
        status, body = _get(url + "/")
        assert status == 200
        assert b"mega-tron dashboard" in body


def test_unknown_route_returns_404(server_env):
    with _serving() as url:
        status, body = _get_404_tolerant(url + "/no-such-path")
        assert status == 404


def test_static_app_js_served(server_env):
    with _serving() as url:
        status, body = _get(url + "/static/app.js")
        assert status == 200
        # Full app.js (T3) is substantial; placeholder was ~400 bytes
        # so this also catches a regression where the build leaves a
        # stale placeholder in the wheel.
        assert len(body) > 1_000


def test_static_d3_vendor_present(server_env):
    """d3.v7.min.js must ship in the wheel — the treemap depends on
    it and the UX choice was 'vendor, do not CDN'."""
    with _serving() as url:
        status, body = _get(url + "/static/d3.v7.min.js")
        assert status == 200
        # Real d3 v7 minified is ~280KB; placeholder stub was ~200B.
        assert len(body) > 100_000


def test_static_index_renders_shell(server_env):
    """The shell must reference app.js + style.css so the page loads
    everything it needs. d3.v7.min.js ships in the wheel (see
    `test_static_d3_is_real`) but is not currently linked from
    index.html — the treemap that motivated vendoring it was never
    wired up, so the asset is dormant. Keep the file shipped so the
    eventual treemap re-add doesn't require a new release."""
    with _serving() as url:
        status, body = _get(url + "/")
        assert status == 200
        text = body.decode("utf-8")
        assert "/static/app.js" in text
        assert "/static/style.css" in text


def test_static_path_traversal_blocked(server_env):
    with _serving() as url:
        status, _body = _get_404_tolerant(url + "/static/../etc/passwd")
        assert status in (403, 404)  # either is acceptable


# --------------------------------------------------------------------------- #
# JSON endpoints
# --------------------------------------------------------------------------- #


def test_api_overview_smoke(server_env):
    """The headline integration check: server up, GET /api/overview
    returns JSON with the expected top-level keys."""
    with _serving() as url:
        status, body = _get(url + "/api/overview")
        assert status == 200
        payload = json.loads(body)
        for key in ("total", "used", "unused", "by_host", "net_harmful_count"):
            assert key in payload


def test_api_skills_returns_list(server_env):
    with _serving() as url:
        status, body = _get(url + "/api/skills")
        assert status == 200
        assert isinstance(json.loads(body), list)


def test_api_verdicts_returns_list(server_env):
    with _serving() as url:
        status, body = _get(url + "/api/verdicts?limit=10")
        assert status == 200
        assert isinstance(json.loads(body), list)


def test_api_skill_unknown_returns_404(server_env):
    with _serving() as url:
        status, _body = _get_404_tolerant(url + "/api/skill/does-not-exist")
        assert status == 404


# --------------------------------------------------------------------------- #
# Orphans (GET list + POST bulk delete)
# --------------------------------------------------------------------------- #


def test_api_orphans_returns_list(server_env):
    _home, store = server_env
    store.record_verdict(
        skill_name="ghost", verdict="HELPFUL",
        reason="synthetic verdict for unit test",
        host="codex", session_id="g1",
    )
    with _serving() as url:
        status, body = _get(url + "/api/orphans")
        assert status == 200
        rows = json.loads(body)
        assert isinstance(rows, list)
        assert any(r["name"] == "ghost" for r in rows)


def test_api_orphans_bulk_delete_round_trip(server_env):
    _home, store = server_env
    store.record_verdict(
        skill_name="dead", verdict="HARMFUL",
        reason="synthetic verdict for unit test",
        host="codex", session_id="d1",
    )
    with _serving() as url:
        # Confirm visible first
        _status, body = _get(url + "/api/orphans")
        assert any(r["name"] == "dead" for r in json.loads(body))

        # POST the bulk delete
        req = urllib.request.Request(
            url + "/api/orphans/delete-bulk",
            data=json.dumps({"names": ["dead"]}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            result = json.loads(resp.read())
        assert result["deleted"] == [{"name": "dead", "verdicts_removed": 1}]

        # Now invisible
        _status, body = _get(url + "/api/orphans")
        assert all(r["name"] != "dead" for r in json.loads(body))


def test_api_orphans_bulk_delete_rejects_malformed_body(server_env):
    with _serving() as url:
        req = urllib.request.Request(
            url + "/api/orphans/delete-bulk",
            data=json.dumps({"names": "not-a-list"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            assert False, "expected HTTPError"
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
