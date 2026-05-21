"""Legacy ~/.cache/mega-optimus warning surfaces exactly once when the
new ~/.cache/mega-tron cache hasn't been built yet."""
from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch

from mega_tron import cli as cli_mod
from mega_tron.cli import _maybe_warn_legacy_cache


def _reset_warned() -> None:
    """The warning fires at most once per process; reset between tests.

    The flag is owned by ``mega_tron.cli._common`` (the single source of
    truth). ``mega_tron.cli.__init__`` proxies *reads* via ``__getattr__``
    but module __setattr__ doesn't proxy, so tests reset on the real
    home in _common.
    """
    cli_mod._common._LEGACY_CACHE_WARNED = False


def test_warns_when_only_legacy_cache_exists(tmp_path: Path, capsys) -> None:
    _reset_warned()
    (tmp_path / ".cache" / "mega-optimus").mkdir(parents=True)
    with patch.object(Path, "home", return_value=tmp_path):
        _maybe_warn_legacy_cache()
    err = capsys.readouterr().err
    assert "legacy mega-optimus cache" in err
    assert str(tmp_path / ".cache" / "mega-optimus") in err
    assert "mega-tron build-cache" in err


def test_silent_when_new_cache_exists(tmp_path: Path, capsys) -> None:
    _reset_warned()
    (tmp_path / ".cache" / "mega-optimus").mkdir(parents=True)
    (tmp_path / ".cache" / "mega-tron").mkdir(parents=True)
    with patch.object(Path, "home", return_value=tmp_path):
        _maybe_warn_legacy_cache()
    assert capsys.readouterr().err == ""


def test_silent_when_no_caches_exist(tmp_path: Path, capsys) -> None:
    _reset_warned()
    with patch.object(Path, "home", return_value=tmp_path):
        _maybe_warn_legacy_cache()
    assert capsys.readouterr().err == ""


def test_fires_only_once_per_process(tmp_path: Path, capsys) -> None:
    """Multiple subcommands sharing the helper must not spam the user."""
    _reset_warned()
    (tmp_path / ".cache" / "mega-optimus").mkdir(parents=True)
    with patch.object(Path, "home", return_value=tmp_path):
        _maybe_warn_legacy_cache()
        _maybe_warn_legacy_cache()
        _maybe_warn_legacy_cache()
    err = capsys.readouterr().err
    # The warning header should appear exactly once.
    assert err.count("legacy mega-optimus cache") == 1


def test_resolve_cache_path_triggers_warning(tmp_path: Path, capsys) -> None:
    """The user-facing seam: any subcommand that takes a cache path
    pays a one-time warning if the legacy cache is around."""
    _reset_warned()
    (tmp_path / ".cache" / "mega-optimus").mkdir(parents=True)
    args = argparse.Namespace(sub_cache_path=None)
    with patch.object(Path, "home", return_value=tmp_path):
        with patch("mega_tron.cli._common.Config") as mock_cfg:
            mock_cfg.load.return_value.embedder_model = "test/model"
            path = cli_mod._resolve_cache_path(args)
    assert "test_model.npz" in str(path)
    assert "legacy mega-optimus cache" in capsys.readouterr().err
