"""Stop-hook verdict-writer catalog-membership filter.

``persist_verdicts`` is the shared write path every host Stop hook
ultimately calls. Until the catalog filter landed, it would happily
INSERT any ``<skill-used name="X"/>`` tag the transcript contained,
even when no SKILL.md with that name existed under any registered
root — that hallucinated tag would land in the verdicts table with a
non-existent ``skill_dir`` and instantly manifest as an "orphan" row
in the dashboard.

The fix is a single membership check against the union of
``discover_skill_dirs()`` before anything is written. These tests
pin both halves of the contract:

  - Known names: write proceeds (SQLite row + frontmatter both
    updated; the rest of the write paths get the same ``items``).
  - Unknown names: skipped silently, one stderr line summarising
    what was dropped.

The fixture monkeypatches ``$HOME`` + ``$XDG_DATA_HOME`` +
``$MEGA_TRON_STORE`` so the writer's automatic store-creation lands
in the tmp tree, never touching the user's real store.
"""
from __future__ import annotations

from io import StringIO
from pathlib import Path
from contextlib import redirect_stderr

import pytest

from mega_tron.verdicts.store import Store
from mega_tron.verdicts.writer import persist_verdicts


def _write_skill(skills_dir: Path, name: str) -> None:
    d = skills_dir / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f'---\nname: {name}\ndescription: "USE WHEN: x"\n---\n\nbody\n',
        encoding="utf-8",
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("MEGA_SKILL_DIRS", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("MEGA_TRON_STORE", str(tmp_path / "store.db"))
    monkeypatch.setenv("MEGA_VERDICT_EMBED", "0")  # skip torch cold-load
    monkeypatch.delenv("MEGA_WITH_WISDOM", raising=False)

    claude_skills = tmp_path / ".claude" / "skills"
    claude_skills.mkdir(parents=True)
    return tmp_path, claude_skills


def test_known_skill_writes_through(env, tmp_path):
    _home, claude_skills = env
    _write_skill(claude_skills, "webhook-signer")

    outcome = persist_verdicts(
        skills_dir=claude_skills,
        verdicts=[{
            "skill": "webhook-signer",
            "verdict": "HELPFUL",
            "reason": "signed the request with the right HMAC",
        }],
        host="claude_code",
        session_id="s1",
    )
    store = Store(path=tmp_path / "store.db")
    counts = store.verdict_counts_by_skill()
    assert "webhook-signer" in counts
    assert counts["webhook-signer"]["helpful"] == 1
    assert outcome.updated >= 1


def test_unknown_skill_dropped_silently(env, tmp_path):
    """A hallucinated name must not produce a verdicts row and must
    not raise — only a one-line stderr summary."""
    _home, claude_skills = env
    # No SKILL.md is created for this name under any root.

    err = StringIO()
    with redirect_stderr(err):
        outcome = persist_verdicts(
            skills_dir=claude_skills,
            verdicts=[{
                "skill": "domain-glossary-explainer",
                "verdict": "NEUTRAL",
                "reason": "model invented this name in a tag",
            }],
            host="claude_code",
            session_id="s1",
        )

    store = Store(path=tmp_path / "store.db")
    assert "domain-glossary-explainer" not in store.verdict_counts_by_skill()
    assert outcome.updated == 0
    stderr_text = err.getvalue()
    assert "dropped 1 verdict tag(s)" in stderr_text
    assert "domain-glossary-explainer" in stderr_text
    assert "hallucination" in stderr_text.lower()


def test_mixed_batch_keeps_known_and_drops_unknown(env, tmp_path):
    """A realistic transcript carries multiple tags in one Stop fire.
    Known names must still be written even when the batch also
    contains hallucinated ones."""
    _home, claude_skills = env
    _write_skill(claude_skills, "jwt-verifier")

    err = StringIO()
    with redirect_stderr(err):
        persist_verdicts(
            skills_dir=claude_skills,
            verdicts=[
                {"skill": "jwt-verifier", "verdict": "HELPFUL",
                 "reason": "validated the audience claim correctly"},
                {"skill": "fake-skill-A", "verdict": "HARMFUL",
                 "reason": "hallucinated name one"},
                {"skill": "fake-skill-B", "verdict": "NEUTRAL",
                 "reason": "hallucinated name two"},
            ],
            host="claude_code",
            session_id="s2",
        )

    store = Store(path=tmp_path / "store.db")
    counts = store.verdict_counts_by_skill()
    assert counts["jwt-verifier"]["helpful"] == 1
    assert "fake-skill-A" not in counts
    assert "fake-skill-B" not in counts
    stderr_text = err.getvalue()
    assert "dropped 2 verdict tag(s)" in stderr_text


def test_name_found_under_a_different_root_still_accepted(env, tmp_path):
    """Stop hook for Codex passes skills_dir=~/.codex/skills, but a
    Claude-rooted skill (~/.claude/skills/gsd-debug) is still valid
    catalog membership because mega-tron unions the discovery roots.
    The catalog check must reflect that."""
    home, claude_skills = env
    codex_skills = home / ".codex" / "skills"
    codex_skills.mkdir(parents=True)
    # Skill is only present under ~/.claude/skills.
    _write_skill(claude_skills, "gsd-debug")

    persist_verdicts(
        skills_dir=codex_skills,
        verdicts=[{
            "skill": "gsd-debug",
            "verdict": "HELPFUL",
            "reason": "scientific method debugging led to the root cause",
        }],
        host="codex",
        session_id="s3",
    )

    store = Store(path=tmp_path / "store.db")
    counts = store.verdict_counts_by_skill()
    assert counts.get("gsd-debug", {}).get("helpful") == 1


def test_empty_batch_is_noop(env):
    _home, claude_skills = env
    outcome = persist_verdicts(
        skills_dir=claude_skills,
        verdicts=[],
        host="claude_code",
        session_id="s4",
    )
    assert outcome.updated == 0
