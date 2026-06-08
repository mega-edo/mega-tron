"""Unit tests for the shared dashboard/daemon process-discovery helpers.

These were extracted from ``upgrade.py`` into ``dashboard_procs`` so
qa-live can reuse them (fix for #3). ``test_cli_upgrade.py`` covers the
classify/parse helpers via the upgrade module's re-export; here we pin
the canonical location and exercise the new ``_port_is_bound`` probe
against a real ephemeral socket.
"""
from __future__ import annotations

import socket

from mega_tron.cli.dashboard_procs import (
    _RunningProc,
    _classify,
    _parse_dashboard_args,
    _port_is_bound,
)


def test_helpers_importable_from_canonical_module():
    # The shared module is the canonical home; upgrade.py re-exports.
    from mega_tron.cli.upgrade import _classify as upgrade_classify
    from mega_tron.cli.dashboard_procs import _classify as shared_classify

    assert upgrade_classify is shared_classify


def test_classify_dashboard_and_daemon():
    assert _classify(["python", "-m", "mega_tron.cli", "dashboard"]) == "dashboard"
    assert _classify(["mega-tron", "daemon", "serve"]) == "daemon"
    assert _classify(["python", "-m", "mega_tron.cli", "search", "x"]) == "other"
    assert _classify([]) == "other"


def test_classify_does_not_match_substring_tokens():
    """Regression (#3 live-test finding): a process whose command line
    merely CONTAINS 'dashboard'/'daemon' as part of a larger token must
    NOT be classified as one — that misclassification made qa-live
    wrongly skip its spawn. Matching is per whole argv token."""
    # The exact false positive caught during live verification: a
    # `python -c "...import _launch_dashboard_detached..."` probe.
    assert (
        _classify(["python", "-c", "from x import _launch_dashboard_detached"])
        == "other"
    )
    assert _classify(["mega-tron", "why", "dashboard setup"]) == "other"
    assert _classify(["mega-tron", "search", "daemonize my service"]) == "other"


def test_parse_dashboard_args_both_shapes():
    host, port, no_open = _parse_dashboard_args(
        ["dashboard", "--host", "172.18.0.1", "--port", "7531", "--no-open"]
    )
    assert (host, port, no_open) == ("172.18.0.1", 7531, True)

    host, port, no_open = _parse_dashboard_args(
        ["dashboard", "--host=0.0.0.0", "--port=9000"]
    )
    assert (host, port, no_open) == ("0.0.0.0", 9000, False)


def test_running_proc_defaults():
    p = _RunningProc(pid=1, kind="daemon")
    assert p.host is None and p.port is None and p.no_open is False


def test_port_is_bound_detects_live_listener():
    """A real listening socket on 127.0.0.1 must be detected; its port
    once closed must read as free."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))  # ephemeral port
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert _port_is_bound(port) == "127.0.0.1"
    finally:
        srv.close()
    # After close, the port is free again (give the OS a moment is
    # unnecessary for connect_ex — a refused connect is immediate).
    assert _port_is_bound(port) is None


def test_port_is_bound_unused_port_returns_none():
    # Pick a port we just closed so it's almost certainly free.
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.close()
    assert _port_is_bound(port) is None
