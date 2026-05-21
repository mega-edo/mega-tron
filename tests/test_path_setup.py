"""Tests for the `mega-tron install` PATH auto-update.

The behavior under test:

- ``ensure_on_path`` adds the mega-tron binary directory to the user's
  shell config when it isn't already on $PATH, and is a no-op when it is.
- Re-running is idempotent — no duplicate blocks.
- Sentinel-bracketed so ``remove_from_path`` strips exactly our block.
- Picks the right config file per shell (zshenv / bashrc / fish conf.d).
- Never touches user-authored content outside the sentinel block.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from mega_tron.cli import path_setup
from mega_tron.cli.path_setup import (
    FISH_SENTINEL_END,
    FISH_SENTINEL_START,
    SENTINEL_END,
    SENTINEL_START,
    PathSetupResult,
    ensure_on_path,
    remove_from_path,
)


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch) -> Path:
    """Isolate Path.home() and HOME so we never touch the real homedir."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


@pytest.fixture
def fake_bin_dir(tmp_path: Path, monkeypatch) -> Path:
    """Pretend mega-tron lives at ``<tmp>/bin/mega-tron``."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    binary = bin_dir / "mega-tron"
    binary.write_text("#!/bin/sh\necho fake mega-tron\n")
    binary.chmod(0o755)
    monkeypatch.setattr("sys.argv", [str(binary)])
    return bin_dir


@pytest.fixture
def zsh_shell(monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/zsh")


@pytest.fixture
def bash_shell(monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/bash")


@pytest.fixture
def fish_shell(monkeypatch):
    monkeypatch.setenv("SHELL", "/usr/local/bin/fish")


@pytest.fixture
def empty_path(monkeypatch):
    """Force a $PATH that does NOT contain the fake bin_dir."""
    monkeypatch.setenv("PATH", "/usr/bin:/bin")


# --- Detection -------------------------------------------------------------


def test_resolve_bin_dir_via_argv(fake_bin_dir):
    assert path_setup.resolve_bin_dir() == fake_bin_dir


def test_resolve_bin_dir_returns_none_when_invisible(monkeypatch):
    monkeypatch.setattr("sys.argv", [""])
    monkeypatch.setattr(path_setup.shutil, "which", lambda _: None)
    assert path_setup.resolve_bin_dir() is None


# --- ensure_on_path: already present -----------------------------------------


def test_ensure_on_path_noop_when_already_on_path(
    fake_home, fake_bin_dir, zsh_shell, monkeypatch
):
    """If the bin dir is already on $PATH, no rc file is touched."""
    monkeypatch.setenv("PATH", f"{fake_bin_dir}:/usr/bin")
    rc = fake_home / ".zshenv"
    assert not rc.exists()

    result = ensure_on_path(print_to=None)

    assert result.already_on_path is True
    assert result.wrote is False
    assert not rc.exists()


# --- ensure_on_path: zsh path ------------------------------------------------


def test_ensure_on_path_writes_zshenv_block(
    fake_home, fake_bin_dir, zsh_shell, empty_path
):
    """First install: zshenv is created with our sentinel block."""
    result = ensure_on_path(print_to=None)
    rc = fake_home / ".zshenv"
    body = rc.read_text()

    assert result.wrote is True
    assert result.already_on_path is False
    assert result.rc_file == rc
    assert SENTINEL_START in body
    assert SENTINEL_END in body
    assert str(fake_bin_dir) in body
    assert 'export PATH=' in body
    # POSIX-clean guard so re-sourcing is a no-op when already present.
    assert 'case ":$PATH:"' in body


def test_ensure_on_path_is_idempotent_zsh(
    fake_home, fake_bin_dir, zsh_shell, empty_path
):
    """Re-running yields exactly one sentinel block."""
    ensure_on_path(print_to=None)
    ensure_on_path(print_to=None)
    rc = fake_home / ".zshenv"
    body = rc.read_text()
    assert body.count(SENTINEL_START) == 1
    assert body.count(SENTINEL_END) == 1


def test_ensure_on_path_preserves_user_content(
    fake_home, fake_bin_dir, zsh_shell, empty_path
):
    """User-authored lines before/after the sentinel block survive."""
    rc = fake_home / ".zshenv"
    rc.write_text("# my custom alias\nalias ll='ls -al'\n")

    ensure_on_path(print_to=None)
    body = rc.read_text()
    assert "alias ll='ls -al'" in body
    assert SENTINEL_START in body
    assert str(fake_bin_dir) in body


# --- ensure_on_path: bash + fish --------------------------------------------


def test_ensure_on_path_writes_bashrc_for_bash(
    fake_home, fake_bin_dir, bash_shell, empty_path
):
    ensure_on_path(print_to=None)
    rc = fake_home / ".bashrc"
    assert rc.exists()
    assert SENTINEL_START in rc.read_text()
    # zshenv must NOT be touched under bash.
    assert not (fake_home / ".zshenv").exists()


def test_ensure_on_path_writes_fish_conf_d(
    fake_home, fake_bin_dir, fish_shell, empty_path
):
    ensure_on_path(print_to=None)
    rc = fake_home / ".config" / "fish" / "conf.d" / "mega-tron.fish"
    assert rc.exists()
    body = rc.read_text()
    assert FISH_SENTINEL_START in body
    assert "fish_add_path" in body


# --- remove_from_path --------------------------------------------------------


def test_remove_from_path_strips_sentinel_block_only(
    fake_home, fake_bin_dir, zsh_shell, empty_path
):
    """User content outside the sentinel survives a remove call."""
    rc = fake_home / ".zshenv"
    rc.write_text("# my custom alias\nalias ll='ls -al'\n")
    ensure_on_path(print_to=None)
    assert SENTINEL_START in rc.read_text()

    removed = remove_from_path(print_to=None)
    body = rc.read_text()

    assert removed is True
    assert SENTINEL_START not in body
    assert SENTINEL_END not in body
    assert str(fake_bin_dir) not in body
    # User alias still there.
    assert "alias ll='ls -al'" in body


def test_remove_from_path_is_noop_when_absent(
    fake_home, fake_bin_dir, zsh_shell
):
    """No sentinel block → no write, no error."""
    rc = fake_home / ".zshenv"
    rc.write_text("# unrelated content\n")
    removed = remove_from_path(print_to=None)
    assert removed is False
    assert rc.read_text() == "# unrelated content\n"


def test_remove_from_path_handles_missing_rc(
    fake_home, fake_bin_dir, zsh_shell
):
    """No rc file at all → returns False, no exception."""
    removed = remove_from_path(print_to=None)
    assert removed is False


# --- Edge case: same block already on file, but $PATH not refreshed ----------


def test_ensure_on_path_skips_write_when_block_already_matches(
    fake_home, fake_bin_dir, zsh_shell, empty_path
):
    """Block on disk matches what we'd write → don't re-write the file."""
    # First write
    ensure_on_path(print_to=None)
    rc = fake_home / ".zshenv"
    first_mtime = rc.stat().st_mtime_ns

    # Force a perceptible mtime gap, then re-run.
    import time

    time.sleep(0.01)
    result = ensure_on_path(print_to=None)
    second_mtime = rc.stat().st_mtime_ns

    assert result.wrote is False
    assert first_mtime == second_mtime


# --- Return shape ------------------------------------------------------------


def test_path_setup_result_shape(fake_home, fake_bin_dir, zsh_shell, empty_path):
    result = ensure_on_path(print_to=None)
    assert isinstance(result, PathSetupResult)
    assert result.bin_dir == fake_bin_dir
    assert result.rc_file == fake_home / ".zshenv"
