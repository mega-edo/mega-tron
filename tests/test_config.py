"""Config + skill-directory discovery."""
from __future__ import annotations

from pathlib import Path

import pytest

from mega_tron import wisdom as wisdom_mod
from mega_tron.config import (
    Config,
    _standard_skill_dirs,
    discover_skill_dirs,
)


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Point HOME at a fresh tmp dir for the duration of the test.

    Also clears CODEX_HOME / MEGA_SKILL_DIRS so the discovery output is
    fully determined by what we materialize under tmp_path.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("MEGA_SKILL_DIRS", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return tmp_path


def test_standard_skill_dirs_includes_codex_system_cache(fake_home):
    """Codex unpacks its bundled sample skills into
    `$CODEX_HOME/skills/.system`. We must surface that path so the
    router can rank bundled skills alongside user skills."""
    dirs = _standard_skill_dirs()
    paths = [str(p) for p in dirs]
    assert str(fake_home / ".codex" / "skills" / ".system") in paths


def test_standard_skill_dirs_includes_gemini_and_hermes(fake_home):
    """Gemini CLI and Hermes both keep their skills under
    ``~/.gemini/skills`` and ``~/.hermes/skills`` respectively. Both
    must be in the standard discovery set so multi-host installs share
    one router view."""
    dirs = _standard_skill_dirs()
    paths = [str(p) for p in dirs]
    assert str(fake_home / ".gemini" / "skills") in paths
    assert str(fake_home / ".hermes" / "skills") in paths


def test_standard_skill_dirs_includes_host_neutral_agents_dir(fake_home):
    """Host-neutral ``~/.agents/skills`` is a first-class discovery root
    so a skill installed once at this conventional shared location is
    visible to every host the router serves — without forcing the user
    to register it with `mega-tron dirs add`.

    Note: ``~/.local/share/mega-code/skills`` is deliberately *not*
    here; it lives behind the ``MEGA_WITH_WISDOM=1`` opt-in gate via
    :mod:`mega_tron.wisdom`."""
    dirs = _standard_skill_dirs()
    paths = [str(p) for p in dirs]
    assert str(fake_home / ".agents" / "skills") in paths
    # mega-code path must stay out of the always-on default — covered
    # in the wisdom-gated discovery tests below.
    assert str(fake_home / ".local" / "share" / "mega-code" / "skills") not in paths


def test_standard_skill_dirs_orders_system_cache_after_user_skills(fake_home):
    """`Router.load_skills` is first-dir-wins on name collisions — so
    user-authored skills must outrank the bundled samples that share
    the same name."""
    dirs = _standard_skill_dirs()
    user_codex = dirs.index(fake_home / ".codex" / "skills")
    system_codex = dirs.index(fake_home / ".codex" / "skills" / ".system")
    assert user_codex < system_codex


def test_discover_skill_dirs_filters_to_existing(fake_home):
    """The standard list is fixed, but ``existing_only=True`` (default)
    drops paths that haven't been created yet."""
    # Materialize the user skill dir but not the system cache.
    (fake_home / ".codex" / "skills").mkdir(parents=True)

    found = discover_skill_dirs(existing_only=True)
    found_strs = [str(p) for p in found]
    assert str((fake_home / ".codex" / "skills").resolve()) in found_strs
    # System cache wasn't created — must not show up under existing_only.
    assert str((fake_home / ".codex" / "skills" / ".system").resolve()) not in found_strs


def test_discover_skill_dirs_surfaces_system_cache_when_present(fake_home):
    """Once codex has installed bundled skills (the `.system` cache
    exists), discover must include it."""
    (fake_home / ".codex" / "skills").mkdir(parents=True)
    (fake_home / ".codex" / "skills" / ".system").mkdir()

    found = discover_skill_dirs(existing_only=True)
    found_strs = [str(p) for p in found]
    assert str((fake_home / ".codex" / "skills" / ".system").resolve()) in found_strs


def test_discover_skill_dirs_honours_codex_home_env(fake_home, monkeypatch):
    """Custom $CODEX_HOME → both `$CODEX_HOME/skills` and
    `$CODEX_HOME/skills/.system` join the search path."""
    custom = fake_home / "custom-codex"
    (custom / "skills" / ".system").mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(custom))

    found = discover_skill_dirs(existing_only=True)
    found_strs = [str(p) for p in found]
    assert str((custom / "skills").resolve()) in found_strs
    assert str((custom / "skills" / ".system").resolve()) in found_strs


# ---------------------------------------------------------------------------
# MEGA_WITH_WISDOM toggle — wisdom-curator skill cache discovery
# ---------------------------------------------------------------------------


@pytest.fixture
def wisdom_dir(fake_home, monkeypatch):
    """Materialize a fake wisdom-cache dir under fake_home and rebind
    the module-level constant so discovery looks there.

    Returns the dir; the test decides whether to populate it.
    """
    d = fake_home / ".local" / "share" / "mega-code" / "skills"
    d.mkdir(parents=True)
    monkeypatch.setattr(wisdom_mod, "WISDOM_SKILLS_DIR", d)
    return d


def test_discover_wisdom_dir_omitted_when_disabled(wisdom_dir, monkeypatch):
    """Default: ``MEGA_WITH_WISDOM`` unset → wisdom dir NOT in discovery."""
    monkeypatch.delenv("MEGA_WITH_WISDOM", raising=False)
    (wisdom_dir / "some-skill").mkdir()  # populated, but env gate is off

    found = discover_skill_dirs(existing_only=True)
    assert wisdom_dir.resolve() not in [p.resolve() for p in found]


def test_discover_wisdom_dir_included_when_enabled(wisdom_dir, monkeypatch):
    """``MEGA_WITH_WISDOM=1`` + existing dir → appears in discovery."""
    monkeypatch.setenv("MEGA_WITH_WISDOM", "1")
    found = discover_skill_dirs(existing_only=True)
    assert wisdom_dir.resolve() in [p.resolve() for p in found]


def test_discover_wisdom_dir_placed_last(fake_home, wisdom_dir, monkeypatch):
    """Wisdom dir must be lowest priority so local skills shadow it on
    ``name:`` collision (Router.load_skills is first-dir-wins).
    """
    monkeypatch.setenv("MEGA_WITH_WISDOM", "1")
    (fake_home / ".claude" / "skills").mkdir(parents=True)
    (fake_home / ".codex" / "skills").mkdir(parents=True)

    found = discover_skill_dirs(existing_only=True)
    wisdom_idx = [p.resolve() for p in found].index(wisdom_dir.resolve())
    # Every other discovered dir must come before the wisdom dir.
    assert wisdom_idx == len(found) - 1, (
        f"wisdom dir at index {wisdom_idx} of {len(found)}; expected last"
    )


def test_discover_wisdom_dir_filtered_when_missing(fake_home, monkeypatch):
    """Even with the env set, a non-existent wisdom dir is filtered out
    under ``existing_only=True`` — fresh installs that haven't fired
    wisdom yet shouldn't see a phantom root."""
    monkeypatch.setenv("MEGA_WITH_WISDOM", "1")
    # Point the constant at a path that does not exist on disk.
    missing = fake_home / "no-such-dir" / "skills"
    monkeypatch.setattr(wisdom_mod, "WISDOM_SKILLS_DIR", missing)

    found = discover_skill_dirs(existing_only=True)
    assert missing.resolve() not in [p.resolve() for p in found]
