"""Wisdom-curator integration — env gates, result parsing.

We do NOT exercise ``ignite()`` here because it spawns the real
``wisdom_curator.py`` subprocess against the live MEGA-Code gateway.
Daemon-level wiring + dedup is covered in ``test_daemon.py``; this
file covers the pure helpers.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from mega_tron import wisdom as wisdom_mod
from mega_tron.wisdom import (
    ENV_CURATOR_PATH,
    ENV_ENABLED,
    WisdomResult,
    is_enabled,
    resolve_curator_path,
)


# ---------------------------------------------------------------------------
# is_enabled — env truthiness gate
# ---------------------------------------------------------------------------


def test_is_enabled_default_off(monkeypatch):
    monkeypatch.delenv(ENV_ENABLED, raising=False)
    assert is_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "True", "yes", "YES", "on"])
def test_is_enabled_truthy_values(monkeypatch, val):
    monkeypatch.setenv(ENV_ENABLED, val)
    assert is_enabled() is True


@pytest.mark.parametrize("val", ["", "0", "false", "no", "off", "junk"])
def test_is_enabled_falsy_values(monkeypatch, val):
    monkeypatch.setenv(ENV_ENABLED, val)
    assert is_enabled() is False


def test_is_enabled_strips_whitespace(monkeypatch):
    monkeypatch.setenv(ENV_ENABLED, "  1  ")
    assert is_enabled() is True


# ---------------------------------------------------------------------------
# resolve_curator_path — env path resolution
# ---------------------------------------------------------------------------


def test_resolve_curator_path_unset(monkeypatch):
    monkeypatch.delenv(ENV_CURATOR_PATH, raising=False)
    assert resolve_curator_path() is None


def test_resolve_curator_path_missing_file(monkeypatch, tmp_path):
    monkeypatch.setenv(ENV_CURATOR_PATH, str(tmp_path / "does_not_exist.py"))
    assert resolve_curator_path() is None


def test_resolve_curator_path_existing_file(monkeypatch, tmp_path):
    p = tmp_path / "curator.py"
    p.write_text("# fake")
    monkeypatch.setenv(ENV_CURATOR_PATH, str(p))
    resolved = resolve_curator_path()
    assert resolved is not None
    assert resolved == p


def test_resolve_curator_path_expands_user(monkeypatch, tmp_path):
    """``~`` in the env var must be expanded to HOME."""
    p = tmp_path / "curator.py"
    p.write_text("")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(ENV_CURATOR_PATH, "~/curator.py")
    resolved = resolve_curator_path()
    assert resolved == p


# ---------------------------------------------------------------------------
# WisdomResult.from_iteration_json — parsing
# ---------------------------------------------------------------------------


def _write_iteration(tmp_path: Path, payload: dict) -> Path:
    p = tmp_path / "iter.json"
    p.write_text(json.dumps(payload))
    return p


def test_wisdom_result_parses_happy_path(tmp_path):
    payload = {
        "session_id": "msr-router-20260101T000000000",
        "skills_root": str(tmp_path / "skills"),
        "skills": [
            {"name": "webhook-signer", "installed": True, "status": "installed", "path": ""},
            {"name": "broken-skill", "installed": False, "status": "failed:HTTPError", "path": ""},
        ],
        "wisdoms": [
            {"wisdom_id": "w-1", "name": "Webhook validation", "score": 0.82,
             "references": ["webhook-signer/SKILL.md#main L1-50"]},
        ],
        "usage": {"token_count": 1234, "cost_usd": 0.0042},
    }
    iter_path = _write_iteration(tmp_path, payload)
    res = WisdomResult.from_iteration_json(iter_path)

    assert res.session_id == "msr-router-20260101T000000000"
    assert res.token_count == 1234
    assert res.cost_usd == pytest.approx(0.0042)
    assert res.skills_root == tmp_path / "skills"
    assert res.iteration_path == iter_path
    assert len(res.skills) == 2

    [s_ok, s_fail] = res.skills
    assert s_ok.name == "webhook-signer" and s_ok.installed is True
    assert s_fail.name == "broken-skill" and s_fail.installed is False
    assert "failed:" in s_fail.status

    # installed_skill_dirs excludes failed installs and resolves under skills_root.
    dirs = res.installed_skill_dirs
    assert dirs == [tmp_path / "skills" / "webhook-signer"]


def test_wisdom_result_handles_missing_optional_fields(tmp_path):
    """Iteration JSON without usage / skills_root must default cleanly."""
    payload = {
        "session_id": "s1",
        "skills": [],
        "wisdoms": [],
    }
    iter_path = _write_iteration(tmp_path, payload)
    res = WisdomResult.from_iteration_json(iter_path)

    assert res.session_id == "s1"
    assert res.skills == []
    assert res.wisdoms == []
    assert res.token_count == 0
    assert res.cost_usd == 0.0
    # When skills_root is absent, falls back to the module-level default.
    assert res.skills_root == wisdom_mod.WISDOM_SKILLS_DIR


def test_wisdom_result_handles_null_usage_fields(tmp_path):
    """Defensive: gateway may emit ``token_count: null`` instead of omitting it."""
    payload = {
        "session_id": "s1",
        "skills_root": str(tmp_path),
        "skills": [],
        "wisdoms": [],
        "usage": {"token_count": None, "cost_usd": None},
    }
    iter_path = _write_iteration(tmp_path, payload)
    res = WisdomResult.from_iteration_json(iter_path)
    assert res.token_count == 0
    assert res.cost_usd == 0.0
