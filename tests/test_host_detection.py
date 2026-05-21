"""Host detection — drives the ``install --target auto`` default."""
from __future__ import annotations

import argparse

import pytest

from mega_tron.hosts import detect_hosts, is_host_present


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Empty ``$HOME`` + scrubbed ``PATH`` so detection sees nothing
    unless the test materializes it explicitly."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", str(tmp_path / "_empty_bin"))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    return tmp_path


def test_detect_hosts_empty_when_nothing_present(isolated_home):
    assert detect_hosts() == []
    for host in ("codex", "claude", "gemini"):
        assert is_host_present(host) is False


@pytest.mark.parametrize(
    "host, dirname",
    [
        ("codex", ".codex"),
        ("claude", ".claude"),
        ("gemini", ".gemini"),
    ],
)
def test_detect_via_profile_dir(isolated_home, host, dirname):
    (isolated_home / dirname).mkdir()
    assert is_host_present(host) is True
    assert detect_hosts() == [host]


def test_detect_via_binary_on_path(isolated_home, monkeypatch):
    """Even with no profile dir, a binary on PATH is enough."""
    bin_dir = isolated_home / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "codex"
    fake.write_text("#!/bin/sh\necho stub\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))

    assert is_host_present("codex") is True
    assert detect_hosts() == ["codex"]


def test_detect_codex_home_env_override(isolated_home, monkeypatch):
    """``CODEX_HOME`` is the documented Codex profile override; an
    existing custom path must count as detection."""
    custom = isolated_home / "custom-codex"
    custom.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(custom))

    assert is_host_present("codex") is True


def test_detect_hosts_canonical_order(isolated_home):
    """Order must be (codex, claude, gemini) regardless of
    which subset is present — the installer iterates this list and the
    invariant keeps logs/tests deterministic."""
    for d in (".gemini", ".claude", ".codex"):
        (isolated_home / d).mkdir()
    assert detect_hosts() == ["codex", "claude", "gemini"]


# --- install --target auto dispatcher --------------------------------------


def test_install_auto_falls_back_when_nothing_detected(
    isolated_home, monkeypatch, capsys
):
    """Empty machine → fallback to all three (with a warning) so the
    install doesn't silently no-op. We patch the per-host installers
    to record invocation order without running them."""
    from mega_tron.cli import cmd_install

    called: list[str] = []
    monkeypatch.setattr(
        "mega_tron.hosts.codex.install.run_install",
        lambda args: called.append("codex") or 0,
    )
    monkeypatch.setattr(
        "mega_tron.hosts.claude_code.install.run_install_claude",
        lambda args: called.append("claude") or 0,
    )
    monkeypatch.setattr(
        "mega_tron.hosts.gemini_cli.install.run_install_gemini",
        lambda args: called.append("gemini") or 0,
    )

    args = argparse.Namespace(target="auto")
    rc = cmd_install(args)
    assert rc == 0
    assert called == ["codex", "claude", "gemini"]
    err = capsys.readouterr().err
    assert "no hosts detected" in err


def test_install_auto_only_runs_detected_hosts(
    isolated_home, monkeypatch, capsys
):
    """With only ``~/.claude`` + ``~/.gemini`` present, the dispatcher
    must skip the Codex installer entirely."""
    from mega_tron.cli import cmd_install

    (isolated_home / ".claude").mkdir()
    (isolated_home / ".gemini").mkdir()

    called: list[str] = []
    monkeypatch.setattr(
        "mega_tron.hosts.codex.install.run_install",
        lambda args: called.append("codex") or 0,
    )
    monkeypatch.setattr(
        "mega_tron.hosts.claude_code.install.run_install_claude",
        lambda args: called.append("claude") or 0,
    )
    monkeypatch.setattr(
        "mega_tron.hosts.gemini_cli.install.run_install_gemini",
        lambda args: called.append("gemini") or 0,
    )

    args = argparse.Namespace(target="auto")
    rc = cmd_install(args)
    assert rc == 0
    assert called == ["claude", "gemini"]
    err = capsys.readouterr().err
    assert "detected hosts: claude, gemini" in err
