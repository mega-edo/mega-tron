"""End-to-end Stage 3 test for Claude target: verdict accumulation
shifts subsequent ranking.

This is the "adapt" half of the route → observe → adapt loop:

1. Run claude_hook against a fixture skills pool → record top-3 ranking
2. Inject a synthetic HARMFUL verdict for the top-1 pick (via the same
   apply_evaluations path the Stop hook uses)
3. Re-run claude_hook with the same prompt → top-1 should de-rank (or
   the harmed skill should pick up `harmful_count`)

Lightweight — uses the bundled fixture skills and a tmp cache so the
test is hermetic and fast (single embedder warm).
"""
from __future__ import annotations

import argparse
import io
import json
import shutil
import uuid
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest

from mega_tron.hosts.claude_code.hook import cmd_claude_hook
from mega_tron.verdicts.mega_meta import apply_evaluations, read_meta


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    runtime = tmp_path / "xdg"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("MEGA_MODE", "semantic")
    monkeypatch.setenv("MEGA_DAEMON", "0")
    monkeypatch.setenv("MEGA_QUIET", "1")
    # Ensure the eval-blend is active (it's the default but make it explicit).
    monkeypatch.delenv("MEGA_EVAL_BLEND", raising=False)
    yield


@pytest.fixture
def skills(tmp_path):
    src = Path(__file__).parent / "fixtures" / "skills"
    dst = tmp_path / "skills"
    shutil.copytree(src, dst)
    return dst


def _args(skills_dir: Path, cache: Path) -> argparse.Namespace:
    return argparse.Namespace(
        skills_dir=str(skills_dir),
        top_k=5,
        prepend_k=3,
        cache_path=str(cache),
    )


def _run_hook(prompt: str, skills_dir: Path, cache: Path) -> dict:
    payload = {
        "hook_event_name": "UserPromptSubmit",
        "prompt": prompt,
        "session_id": f"e2e-{uuid.uuid4()}",
        "cwd": "/tmp",
        "transcript_path": "/tmp/fake.jsonl",
    }
    raw = json.dumps(payload)
    stdout = io.StringIO()
    stderr = io.StringIO()
    with patch("sys.stdin", io.StringIO(raw)):
        with redirect_stdout(stdout), redirect_stderr(stderr):
            cmd_claude_hook(_args(skills_dir, cache))
    out = stdout.getvalue()
    if not out:
        return {}
    return json.loads(out)


def _extract_slash_names(ctx: str) -> list[str]:
    """Pull `/skill-name` bullets from the meta block in order."""
    names = []
    for line in ctx.splitlines():
        line = line.strip()
        if line.startswith("- /"):
            names.append(line[3:].split()[0])
    return names


def test_harmful_verdict_shifts_subsequent_ranking(skills, tmp_path):
    """End-to-end: routing → observe → adapt produces a measurable
    de-ranking of the harmed skill on a re-run with the same prompt."""
    cache = tmp_path / "cache.npz"
    prompt = "validate this incoming webhook"

    # ----- Round 1: baseline ranking -----
    out1 = _run_hook(prompt, skills, cache)
    ctx1 = out1["hookSpecificOutput"]["additionalContext"]
    picks1 = _extract_slash_names(ctx1)
    assert picks1, "expected non-empty pick list"
    target = picks1[0]  # the top-1 we'll mark HARMFUL

    # Confirm starting mega_meta is empty for this skill
    meta_before = read_meta(skills / target / "SKILL.md")
    assert meta_before.harmful_count == 0

    # ----- Inject HARMFUL verdicts (3 of them → triggers auto-archive) -----
    # The ranker promotes to `suspect` at harmful_ratio > 0.3 (50% penalty)
    # and to `archived` at 3 consecutive HARMFULs. A single HARMFUL won't
    # always be enough to bump it out of the top-3, so we hammer it.
    for i in range(3):
        outcome = apply_evaluations(
            skills_dir=skills,
            evaluations=[
                {
                    "skill": target,
                    "verdict": "HARMFUL",
                    "reason": f"e2e synthetic harm #{i}",
                }
            ],
            session_id=f"e2e-{i}",
        )
        assert outcome.updated == 1

    # Confirm the SKILL.md picked up the counters
    meta_after = read_meta(skills / target / "SKILL.md")
    assert meta_after.harmful_count == 3
    # Status should have escalated (suspect or archived)
    assert meta_after.status in ("suspect", "archived"), (
        f"expected suspect/archived, got {meta_after.status!r}"
    )

    # ----- Round 2: re-rank with the same prompt -----
    # Clear first-fire markers so the hook actually re-runs the router
    # (the marker is keyed on session_id which is unique per call, so
    # we're fine, but be defensive).
    out2 = _run_hook(prompt, skills, cache)
    ctx2 = out2["hookSpecificOutput"]["additionalContext"]
    picks2 = _extract_slash_names(ctx2)
    assert picks2, "expected non-empty pick list on re-run"

    # The harmed skill should either drop out of top-3 OR fall to rank 3
    # (it was rank 1). If still rank 1, the eval blend isn't biting.
    if target in picks2:
        new_rank = picks2.index(target)
        assert new_rank > 0, (
            f"expected {target!r} to de-rank from #1 after 3 HARMFULs, "
            f"still at #{new_rank+1}. picks: {picks2}"
        )
    # Otherwise it was dropped entirely — the strongest possible signal.
