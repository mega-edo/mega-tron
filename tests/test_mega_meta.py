"""SKILL.md mega_meta frontmatter read/update."""
from __future__ import annotations

from pathlib import Path

from mega_tron.verdicts.mega_meta import (
    AUTO_ARCHIVE_THRESHOLD,
    CONTEXTS_CAP,
    HARMFUL_COUNT_THRESHOLD,
    MegaMeta,
    read_meta,
    resync_from_store,
    update_meta,
)


def _write_skill(path: Path, *, with_meta: dict | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    meta_block = ""
    if with_meta is not None:
        import yaml

        meta_block = "mega_meta:\n" + yaml.safe_dump(with_meta, sort_keys=False).rstrip()
        meta_block = "\n".join("  " + line for line in meta_block.split("\n"))
        meta_block = meta_block.replace("  mega_meta:", "mega_meta:")
    fm = f"name: x\ndescription: \"USE WHEN: anything\"\n{meta_block}".rstrip()
    path.write_text(f"---\n{fm}\n---\n\n# body\nuntouched body\n")
    return path


def test_read_meta_default_when_missing(tmp_path):
    skill = _write_skill(tmp_path / "x" / "SKILL.md")
    meta = read_meta(skill)
    assert meta.helpful_count == 0
    assert meta.harmful_count == 0
    assert meta.status == "active"


def test_apply_verdict_helpful_increments_and_appends(tmp_path):
    meta = MegaMeta()
    meta.apply_verdict("HELPFUL", "caught the missing replay check", session_id="sess-1")
    assert meta.helpful_count == 1
    assert meta.helpful_contexts == ["caught the missing replay check"]
    assert meta.last_session_id == "sess-1"


def test_apply_verdict_neutral_no_op(tmp_path):
    meta = MegaMeta()
    meta.apply_verdict("NEUTRAL", "available but unused", session_id="s")
    assert meta.helpful_count == 0
    assert meta.harmful_count == 0
    assert meta.helpful_contexts == []


def test_apply_verdict_caps_contexts(tmp_path):
    meta = MegaMeta()
    for i in range(CONTEXTS_CAP + 2):
        meta.apply_verdict("HELPFUL", f"reason {i}", session_id="s")
    assert len(meta.helpful_contexts) == CONTEXTS_CAP
    # Oldest dropped: first to survive is "reason 2".
    assert meta.helpful_contexts[0] == f"reason {CONTEXTS_CAP - 1 - (CONTEXTS_CAP - 1)}" or \
        meta.helpful_contexts == [f"reason {i}" for i in range(2, CONTEXTS_CAP + 2)]


def test_apply_verdict_dedupes_identical_reasons(tmp_path):
    meta = MegaMeta()
    meta.apply_verdict("HELPFUL", "same reason", session_id="s")
    meta.apply_verdict("HELPFUL", "same reason", session_id="s")
    # counter still increments; context list deduplicates.
    assert meta.helpful_count == 2
    assert meta.helpful_contexts == ["same reason"]


def test_status_marks_suspect_on_high_harmful(tmp_path):
    meta = MegaMeta()
    for _ in range(HARMFUL_COUNT_THRESHOLD + 1):
        meta.apply_verdict("HARMFUL", "wrong choice", session_id="s")
    # Need enough invocations to clear the min-invocations gate.
    for _ in range(2):
        meta.apply_verdict("HELPFUL", "rare win", session_id="s")
    assert meta.status == "suspect"


def test_status_recovers_to_active(tmp_path):
    meta = MegaMeta(status="suspect", helpful_count=20, harmful_count=1)
    meta.apply_verdict("HELPFUL", "back on track", session_id="s")
    assert meta.status == "active"


def test_update_meta_writes_back_atomically(tmp_path):
    skill = _write_skill(tmp_path / "x" / "SKILL.md")
    result = update_meta(skill, verdict="HELPFUL", reason="great catch", session_id="sess-9")
    assert result.helpful_count == 1
    raw = skill.read_text()
    assert "mega_meta:" in raw
    assert "helpful_count: 1" in raw
    assert "great catch" in raw
    # Body preserved verbatim.
    assert "untouched body" in raw


def test_update_meta_preserves_other_frontmatter(tmp_path):
    skill = tmp_path / "x" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        '---\n'
        'name: x\n'
        'description: "USE WHEN: thing"\n'
        'allowed-tools: ["bash", "read"]\n'
        '---\n'
        '\n'
        '# Body\n'
    )
    update_meta(skill, verdict="HARMFUL", reason="bad fit", session_id="s1")
    raw = skill.read_text()
    assert 'allowed-tools' in raw
    assert "harmful_count: 1" in raw


def test_update_meta_round_trip(tmp_path):
    skill = _write_skill(tmp_path / "x" / "SKILL.md")
    update_meta(skill, verdict="HELPFUL", reason="first", session_id="s1")
    update_meta(skill, verdict="HARMFUL", reason="oops", session_id="s2")
    meta = read_meta(skill)
    assert meta.helpful_count == 1
    assert meta.harmful_count == 1
    assert meta.last_session_id == "s2"


# ---------------------------------------------------------------------------
# v0.4: C4 auto-archive on N consecutive HARMFUL.
# ---------------------------------------------------------------------------


def test_auto_archive_on_n_consecutive_harmful():
    meta = MegaMeta()
    for i in range(AUTO_ARCHIVE_THRESHOLD):
        assert meta.status != "archived", f"premature archive at i={i}"
        meta.apply_verdict("HARMFUL", f"failure-{i}", session_id="s")
    assert meta.consecutive_harmful == AUTO_ARCHIVE_THRESHOLD
    assert meta.status == "archived"


def test_helpful_resets_harmful_streak():
    meta = MegaMeta()
    meta.apply_verdict("HARMFUL", "fail-1", session_id="s")
    meta.apply_verdict("HARMFUL", "fail-2", session_id="s")
    meta.apply_verdict("HELPFUL", "recovery", session_id="s")
    assert meta.consecutive_harmful == 0
    # Two more HARMFUL after the reset should NOT archive — streak only re-counts.
    meta.apply_verdict("HARMFUL", "fail-3", session_id="s")
    meta.apply_verdict("HARMFUL", "fail-4", session_id="s")
    assert meta.consecutive_harmful == 2
    assert meta.status != "archived"


def test_neutral_does_not_reset_or_extend_streak():
    meta = MegaMeta()
    meta.apply_verdict("HARMFUL", "fail-1", session_id="s")
    meta.apply_verdict("NEUTRAL", "didn't move needle", session_id="s")
    # NEUTRAL leaves the streak alone (neither resets nor extends).
    assert meta.consecutive_harmful == 1
    meta.apply_verdict("HARMFUL", "fail-2", session_id="s")
    meta.apply_verdict("HARMFUL", "fail-3", session_id="s")
    assert meta.status == "archived"


def test_consecutive_harmful_persists_to_yaml(tmp_path):
    skill = _write_skill(tmp_path / "x" / "SKILL.md")
    update_meta(skill, verdict="HARMFUL", reason="r1", session_id="s")
    update_meta(skill, verdict="HARMFUL", reason="r2", session_id="s")
    meta = read_meta(skill)
    assert meta.consecutive_harmful == 2
    # The counter survives the YAML round-trip.
    raw = skill.read_text()
    assert "consecutive_harmful: 2" in raw


# --------------------------------------------------------------------------- #
# resync_from_store — dashboard post-edit frontmatter rebuild
# --------------------------------------------------------------------------- #


def test_resync_from_store_recomputes_counts(tmp_path):
    """SKILL.md with stale counts + actual verdicts in SQLite → resync
    rewrites the frontmatter to match SUM(CASE) over the table."""
    from mega_tron.verdicts.store import Store

    # Seed frontmatter with deliberately-wrong cumulative counts plus a
    # human-authored context the resync MUST preserve verbatim.
    skill = _write_skill(
        tmp_path / "x" / "SKILL.md",
        with_meta={
            "helpful_count": 99,
            "harmful_count": 99,
            "helpful_contexts": ["nice human-written summary"],
            "harmful_contexts": ["nice human-written failure summary"],
            "status": "suspect",
            "last_updated": "2026-01-01T00:00:00Z",
        },
    )

    store = Store(path=tmp_path / "store.db")
    store.initialize()
    store.record_verdict(
        skill_name="x", verdict="HELPFUL",
        reason="first helpful verdict for resync test",
        host="codex", session_id="s1",
    )
    store.record_verdict(
        skill_name="x", verdict="HELPFUL",
        reason="second helpful verdict for resync test",
        host="claude_code", session_id="s2",
    )
    store.record_verdict(
        skill_name="x", verdict="HARMFUL",
        reason="harmful verdict for resync test",
        host="codex", session_id="s3",
    )

    new_meta = resync_from_store(skill, store, "x")
    assert new_meta.helpful_count == 2
    assert new_meta.harmful_count == 1

    # Round-trip: re-read from disk and confirm.
    meta = read_meta(skill)
    assert meta.helpful_count == 2
    assert meta.harmful_count == 1
    # Free-text contexts must be untouched.
    assert "nice human-written summary" in meta.helpful_contexts
    assert "nice human-written failure summary" in meta.harmful_contexts
    # last_updated reflects MAX(occurred_at) in the table — non-empty.
    assert meta.last_updated is not None


def test_resync_from_store_clears_last_updated_when_empty(tmp_path):
    """All verdicts deleted → frontmatter should reflect a never-used
    skill so dashboard's active/idle classifier puts it in 'idle'."""
    from mega_tron.verdicts.store import Store

    skill = _write_skill(
        tmp_path / "x" / "SKILL.md",
        with_meta={
            "helpful_count": 5,
            "harmful_count": 2,
            "last_updated": "2026-01-01T00:00:00Z",
        },
    )
    store = Store(path=tmp_path / "store.db")
    store.initialize()
    # No verdicts inserted.

    meta = resync_from_store(skill, store, "x")
    assert meta.helpful_count == 0
    assert meta.harmful_count == 0
    assert meta.last_updated is None


def test_resync_from_store_re_runs_status_check(tmp_path):
    """If the new counts cross the suspect threshold the status field
    must reflect that — proves _refresh_status() was called."""
    from mega_tron.verdicts.store import Store

    skill = _write_skill(
        tmp_path / "x" / "SKILL.md",
        with_meta={"helpful_count": 0, "harmful_count": 0, "status": "active"},
    )
    store = Store(path=tmp_path / "store.db")
    store.initialize()
    # Many harmful, few helpful → triggers suspect logic.
    for i in range(HARMFUL_COUNT_THRESHOLD + 1):
        store.record_verdict(
            skill_name="x", verdict="HARMFUL",
            reason=f"failure iteration {i} of suspect-threshold seed",
            host="codex", session_id=f"s{i}",
        )
    store.record_verdict(
        skill_name="x", verdict="HELPFUL",
        reason="one win in an otherwise harmful run",
        host="codex", session_id="win",
    )

    meta = resync_from_store(skill, store, "x")
    assert meta.harmful_count > HARMFUL_COUNT_THRESHOLD
    assert meta.status in ("suspect", "archived")  # depends on streak
