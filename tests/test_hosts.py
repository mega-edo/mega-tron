"""Host helpers — install-time detection + dashboard-time host inference."""
from __future__ import annotations

from pathlib import Path

import pytest

from mega_tron.hosts import (
    detect_hosts,
    infer_host_from_skill_dir,
    is_host_present,
    normalize_host,
)


# Detection tests already live in test_host_detection.py — this module
# focuses on the two helpers added for the dashboard.


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    return tmp_path


def test_normalize_host_aliases():
    assert normalize_host("claude_code") == "claude"
    assert normalize_host("gemini_cli") == "gemini"


def test_normalize_host_identity_for_short_names():
    for raw in ("codex", "claude", "gemini", "hermes", "agents", "other"):
        assert normalize_host(raw) == raw


def test_normalize_host_none_becomes_other():
    assert normalize_host(None) == "other"


def test_normalize_host_unknown_value_passes_through():
    """Unknown raw values fall through as-is — we don't want to mask new
    host names by silently aliasing them to ``other``."""
    assert normalize_host("future_host") == "future_host"


@pytest.mark.parametrize(
    "rel, expected",
    [
        (".claude/skills/webhook-signer", "claude"),
        (".codex/skills/jwt-verifier", "codex"),
        (".codex/skills/.system/imagegen", "codex"),
        (".gemini/skills/clap-parser", "gemini"),
        (".hermes/skills/redis-ratelimit", "hermes"),
        (".agents/skills/multi-tool-pipeline", "agents"),
    ],
)
def test_infer_host_from_skill_dir_known_roots(fake_home, rel, expected):
    skill_dir = fake_home / rel
    skill_dir.mkdir(parents=True)
    assert infer_host_from_skill_dir(skill_dir) == expected


def test_infer_host_unknown_root_returns_other(fake_home):
    custom = fake_home / "custom-skills" / "my-skill"
    custom.mkdir(parents=True)
    assert infer_host_from_skill_dir(custom) == "other"


def test_infer_host_respects_codex_home(fake_home, monkeypatch):
    custom_codex = fake_home / "alt-codex"
    skill_dir = custom_codex / "skills" / "k8s-rollout-restart"
    skill_dir.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(custom_codex))
    assert infer_host_from_skill_dir(skill_dir) == "codex"


def test_infer_host_codex_home_system_cache(fake_home, monkeypatch):
    custom_codex = fake_home / "alt-codex"
    skill_dir = custom_codex / "skills" / ".system" / "openai-docs"
    skill_dir.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(custom_codex))
    assert infer_host_from_skill_dir(skill_dir) == "codex"


# Existing detect_hosts contract — quick sanity that we didn't break it
# while adding the dashboard helpers in the same module.


def test_detect_and_is_host_present_still_exposed():
    # Don't care about return value here — just that the symbols are
    # callable so import wiring stays intact for downstream code.
    detect_hosts()
    is_host_present("codex")
