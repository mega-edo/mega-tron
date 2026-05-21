"""mega-tron 1.0.0 refuses to install on top of a stranded mega-optimus
installation.

The check fires in ``cmd_install`` before any host-specific installer
runs. It scans the user's home for sentinel comments, JSON managed keys,
and hook command strings that mega-optimus would have planted. On hit
it returns rc=2 with remediation instructions and does NOT mutate any
files. ``--force`` bypasses the check; ``--uninstall`` bypasses it too,
since uninstalling shouldn't be gated on the legacy state.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch

from mega_tron.cli import _legacy_megaoptimus_files, cmd_install


def _args(**overrides) -> argparse.Namespace:
    """Minimal Namespace shaped like the install parser produces."""
    base = {
        "target": "auto",
        "hermes_home": None,
        "shell": "auto",
        "rc_file": None,
        "skills_dir": None,
        "codex_home": None,
        "budget_tok": 12000,
        "top_k": 3,
        "uninstall": False,
        "force": False,
        "print_only": True,  # avoid real filesystem writes when reached
        "no_warmup": True,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


# ---- Detection helper ----


def test_no_legacy_files_when_home_is_clean(tmp_path: Path) -> None:
    """A fresh HOME (tmp_path) has no mega-optimus sentinels — detector returns []."""
    with patch.object(Path, "home", return_value=tmp_path):
        assert _legacy_megaoptimus_files() == []


def test_detects_shell_sentinel_in_zshrc(tmp_path: Path) -> None:
    zshrc = tmp_path / ".zshrc"
    zshrc.write_text(
        "# >>> mega-optimus (managed; edit between sentinels at your own risk) >>>\n"
        'export PATH="/usr/local/bin:$PATH"\n'
        "# <<< mega-optimus <<<\n"
    )
    with patch.object(Path, "home", return_value=tmp_path):
        hits = _legacy_megaoptimus_files()
    assert hits == [zshrc]


def test_detects_html_sentinel_in_claude_md(tmp_path: Path) -> None:
    claude_md = tmp_path / ".claude" / "CLAUDE.md"
    claude_md.parent.mkdir(parents=True)
    claude_md.write_text(
        "# CLAUDE.md\n\n"
        "<!-- >>> mega-optimus (managed; edit between sentinels at your own risk) >>> -->\n"
        "## Skill routing (mega-optimus)\n"
        "<!-- <<< mega-optimus <<< -->\n"
    )
    with patch.object(Path, "home", return_value=tmp_path):
        hits = _legacy_megaoptimus_files()
    assert hits == [claude_md]


def test_detects_managed_key_in_settings_json(tmp_path: Path) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        '{"hooks": {"UserPromptSubmit": [{"_mega_optimus_managed": "0.5.0", '
        '"hooks": [{"type": "command", "command": "mega-optimus claude-hook"}]}]}}'
    )
    with patch.object(Path, "home", return_value=tmp_path):
        hits = _legacy_megaoptimus_files()
    assert hits == [settings]


def test_detects_trust_sentinel_in_codex_config(tmp_path: Path) -> None:
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        "[skills]\ninclude_instructions = false\n\n"
        "# >>> mega-optimus hook-trust (managed) >>>\n"
        '[hooks.state."abc"]\nstatus = "Trusted"\n'
        "# <<< mega-optimus hook-trust <<<\n"
    )
    with patch.object(Path, "home", return_value=tmp_path):
        hits = _legacy_megaoptimus_files()
    assert hits == [config]


def test_detects_multiple_files_at_once(tmp_path: Path) -> None:
    """All hit paths returned, not just the first one."""
    rc = tmp_path / ".zshrc"
    rc.write_text("# >>> mega-optimus (managed; edit between sentinels at your own risk) >>>\n")
    agents = tmp_path / ".codex" / "AGENTS.md"
    agents.parent.mkdir(parents=True)
    agents.write_text(
        "<!-- >>> mega-optimus (managed; edit between sentinels at your own risk) >>> -->\n"
    )
    with patch.object(Path, "home", return_value=tmp_path):
        hits = _legacy_megaoptimus_files()
    assert set(hits) == {rc, agents}


# ---- cmd_install integration ----


def test_install_refuses_when_legacy_detected(tmp_path: Path, capsys) -> None:
    rc = tmp_path / ".zshrc"
    rc.write_text("# >>> mega-optimus (managed; edit between sentinels at your own risk) >>>\n")
    with patch.object(Path, "home", return_value=tmp_path):
        result = cmd_install(_args())
    assert result == 2
    err = capsys.readouterr().err
    assert "legacy mega-optimus installation detected" in err
    assert str(rc) in err
    assert "mega-optimus install --uninstall" in err
    assert "pip uninstall mega-optimus" in err
    assert "--force" in err


def test_install_force_bypasses_legacy_check(tmp_path: Path) -> None:
    """--force must let the install proceed past the legacy check."""
    rc = tmp_path / ".zshrc"
    rc.write_text("# >>> mega-optimus (managed; edit between sentinels at your own risk) >>>\n")
    with patch.object(Path, "home", return_value=tmp_path):
        # We pass target=hermes + hermes_home=tmp_path to keep the
        # downstream installer from touching the real HOME if it ran.
        # The actual install body is stubbed via print_only — but
        # codex/claude installers don't always honour that, so route
        # to a target that we'll mock instead.
        with patch("mega_tron.hosts.codex.install.run_install") as mock_codex:
            mock_codex.return_value = 0
            result = cmd_install(_args(target="codex", force=True))
    assert result == 0  # legacy check bypassed; codex installer stubbed


def test_uninstall_skips_legacy_check(tmp_path: Path) -> None:
    """Uninstalling should never be gated on detecting legacy files."""
    rc = tmp_path / ".zshrc"
    rc.write_text("# >>> mega-optimus (managed; edit between sentinels at your own risk) >>>\n")
    with patch.object(Path, "home", return_value=tmp_path):
        with patch("mega_tron.hosts.codex.install.run_install") as mock_codex:
            mock_codex.return_value = 0
            result = cmd_install(_args(target="codex", uninstall=True))
    assert result == 0


def test_install_proceeds_on_clean_home(tmp_path: Path) -> None:
    with patch.object(Path, "home", return_value=tmp_path):
        with patch("mega_tron.hosts.codex.install.run_install") as mock_codex:
            mock_codex.return_value = 0
            result = cmd_install(_args(target="codex"))
    assert result == 0
