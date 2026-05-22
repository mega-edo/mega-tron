"""Tests for `mega-tron upgrade`.

We deliberately test the *helpers* — process discovery, argv parsing,
config/rc inference — rather than driving the full end-to-end command
(which would require spawning real daemon/dashboard processes and
running `uv tool install`). The full e2e flow is integration-tested
manually per `docs/agent installation.md`; these tests pin the seams
where bugs would silently break that flow.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mega_tron.cli.upgrade import (
    _RunningProc,
    _classify,
    _infer_claude_mode,
    _infer_profile,
    _parse_dashboard_args,
)


# --- _parse_dashboard_args --- #


def test_parse_dashboard_args_space_separated():
    host, port, no_open = _parse_dashboard_args(
        [
            "/usr/bin/python3", "-m", "mega_tron", "dashboard",
            "--host", "172.18.0.1", "--port", "7531",
        ]
    )
    assert host == "172.18.0.1"
    assert port == 7531
    assert no_open is False


def test_parse_dashboard_args_equals_form():
    host, port, no_open = _parse_dashboard_args(
        ["mega-tron", "dashboard", "--host=0.0.0.0", "--port=8080", "--no-open"]
    )
    assert host == "0.0.0.0"
    assert port == 8080
    assert no_open is True


def test_parse_dashboard_args_no_flags():
    """Dashboard launched with defaults — all None / False."""
    host, port, no_open = _parse_dashboard_args(["mega-tron", "dashboard"])
    assert host is None
    assert port is None
    assert no_open is False


def test_parse_dashboard_args_skips_malformed_port():
    """--port with non-int value: keep port=None instead of crashing."""
    host, port, _ = _parse_dashboard_args(
        ["mega-tron", "dashboard", "--port", "not-a-number", "--host", "h"]
    )
    assert host == "h"
    assert port is None


# --- _classify --- #


@pytest.mark.parametrize(
    "argv,expected",
    [
        # The canonical detached-daemon shape (what spawn_detached uses).
        (
            ["python", "-m", "mega_tron.cli", "daemon", "serve"],
            "daemon",
        ),
        # Plain `mega-tron dashboard ...`
        (
            ["mega-tron", "dashboard", "--port", "7531"],
            "dashboard",
        ),
        # `python -m mega_tron.cli dashboard --host ...` (rare but legal)
        (
            ["python", "-m", "mega_tron.cli", "dashboard", "--host", "0.0.0.0"],
            "dashboard",
        ),
        # An unrelated mega-tron invocation — should NOT be a restart
        # candidate.
        (
            ["mega-tron", "search", "anything"],
            "other",
        ),
        # Pure search hook fired during a host turn — should NOT
        # be classified as daemon just because the word appears as
        # subcommand of an unrelated invocation.
        (
            ["mega-tron", "hook"],
            "other",
        ),
        (
            [],
            "other",
        ),
    ],
)
def test_classify(argv, expected):
    assert _classify(argv) == expected


# --- _infer_profile --- #


def test_infer_profile_multilingual(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg_dir = tmp_path / ".config" / "mega-tron"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.toml").write_text('embedder_model = "BAAI/bge-m3"\n')
    assert _infer_profile() == "multilingual"


def test_infer_profile_en_fast(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg_dir = tmp_path / ".config" / "mega-tron"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.toml").write_text(
        'embedder_model = "BAAI/bge-small-en-v1.5"\n'
    )
    assert _infer_profile() == "en-fast"


def test_infer_profile_en_quality_skillret(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg_dir = tmp_path / ".config" / "mega-tron"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.toml").write_text(
        'embedder_model = "Tiamz/SKILLRET-Embedding-0.6B"\n'
    )
    assert _infer_profile() == "en-quality"


def test_infer_profile_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert _infer_profile() is None


# --- _infer_claude_mode --- #


def test_infer_claude_mode_active_in_zshrc(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".zshrc").write_text(
        "# >>> mega-tron (managed) >>>\n"
        "export MEGA_CLAUDE_NATIVE_MODE=active\n"
        "# <<< mega-tron <<<\n"
    )
    assert _infer_claude_mode() == "active"


def test_infer_claude_mode_strict_in_bashrc(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".bashrc").write_text(
        'export MEGA_CLAUDE_NATIVE_MODE="strict"\n'
    )
    assert _infer_claude_mode() == "strict"


def test_infer_claude_mode_no_export_means_passive(tmp_path, monkeypatch):
    """Empty rc still counts as 'rc exists' → default passive."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".zshrc").write_text("# nothing here\n")
    assert _infer_claude_mode() == "passive"


def test_infer_claude_mode_no_rc_at_all_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert _infer_claude_mode() is None


def test_infer_claude_mode_handles_inline_comment(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".zshrc").write_text(
        "export MEGA_CLAUDE_NATIVE_MODE=active # set by mega-tron setup\n"
    )
    assert _infer_claude_mode() == "active"


def test_infer_claude_mode_rejects_garbage_value(tmp_path, monkeypatch):
    """Foreign value (typo, manual edit) is treated as no setting → passive."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".zshrc").write_text(
        "export MEGA_CLAUDE_NATIVE_MODE=loud\n"
    )
    # Foreign value rejected; falls through to passive because the rc
    # file does exist.
    assert _infer_claude_mode() == "passive"


# --- _RunningProc shape --- #


def test_running_proc_defaults():
    p = _RunningProc(pid=1234, kind="dashboard", argv=["x"])
    assert p.host is None
    assert p.port is None
    assert p.no_open is False
