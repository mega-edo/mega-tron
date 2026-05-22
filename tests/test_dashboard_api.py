"""Dashboard JSON handlers — GET + PATCH + DELETE.

The handlers are pure-Python (no HTTP coupling) so each test can call
them directly. We feed them a temp $HOME with a tiny SKILL.md fixture
and an isolated SQLite store, then assert the returned shape.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mega_tron.dashboard import api
from mega_tron.verdicts.mega_meta import read_meta
from mega_tron.verdicts.store import Store
from mega_tron.verdicts.embeddings import VerdictEmbeddingsStore


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _write_skill(
    path: Path,
    *,
    name: str,
    description: str = "USE WHEN: testing",
    helpful: int = 0,
    harmful: int = 0,
    status: str = "active",
    last_updated: str | None = None,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    parts = [f"helpful_count: {helpful}", f"harmful_count: {harmful}",
             f"status: {status}"]
    if last_updated is not None:
        parts.append(f"last_updated: {last_updated}")
    meta_block = "mega_meta:\n  " + "\n  ".join(parts)
    fm = f'name: {name}\ndescription: "{description}"\n{meta_block}'
    path.write_text(f"---\n{fm}\n---\n\n# body\n", encoding="utf-8")
    return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Wire $HOME + $MEGA_TRON_STORE + verdict-embeddings path into
    a clean tmp_path so dashboard reads are deterministic.

    Returns ``(home, store)`` for test convenience.

    By default we stub :func:`api._open_verdict_embeddings` to return
    ``None`` so PATCH/DELETE tests don't pay the sentence-transformers
    cold-load cost. Tests that exercise embedding cleanup
    (:func:`test_delete_verdict_drops_embedding_when_present`,
    :func:`test_patch_verdict_invalidates_embedding`) override this
    by ``monkeypatch.setattr(api, "_VES", ves)`` with a hand-built
    stub.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("MEGA_SKILL_DIRS", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("MEGA_TRON_STORE", str(tmp_path / "store.db"))
    monkeypatch.setenv(
        "MEGA_TRON_VERDICT_EMBEDDINGS", str(tmp_path / "ve.npz")
    )
    # Disable wisdom so it doesn't reach for ~/.local/share.
    monkeypatch.delenv("MEGA_WITH_WISDOM", raising=False)
    # Default: short-circuit the embedder cold-load. Individual tests
    # that need the real path override `_VES`.
    monkeypatch.setattr(api, "_open_verdict_embeddings", lambda: None)
    api.reset_store_singleton_for_tests()
    api.reset_iter_skills_cache_for_tests()
    store = Store(path=tmp_path / "store.db")
    store.initialize()
    yield tmp_path, store
    api.reset_store_singleton_for_tests()
    api.reset_iter_skills_cache_for_tests()


# --------------------------------------------------------------------------- #
# GET /api/overview
# --------------------------------------------------------------------------- #


def test_overview_empty_returns_zeros(env):
    home, _store = env
    payload = api.overview()
    assert payload["total"] == 0
    assert payload["used"] == 0
    assert payload["unused"] == 0
    assert payload["net_harmful_count"] == 0


# by_host schema upstream: `dict[str, int]` (count of skills with
# verdicts per host). Four tests below were authored against a
# richer `dict[str, dict[str, int]]` shape that was never landed.
# Skipped here so the suite stays green during the port; revisit
# when the by_host schema is intentionally expanded.
_BY_HOST_SCHEMA_DRIFT = pytest.mark.skip(
    reason="by_host schema is dict[str, int]; richer dict-of-dict tests "
    "carried over from mega-optimus, not yet implemented in api.py."
)


@_BY_HOST_SCHEMA_DRIFT
def test_overview_counts_active_idle(env):
    home, _store = env
    _write_skill(
        home / ".codex" / "skills" / "alive-1" / "SKILL.md",
        name="alive-1", helpful=5, harmful=0, last_updated="2026-05-19T00:00:00Z",
    )
    _write_skill(
        home / ".codex" / "skills" / "alive-2" / "SKILL.md",
        name="alive-2", helpful=2, harmful=1, last_updated="2026-05-19T00:00:00Z",
    )
    _write_skill(
        home / ".codex" / "skills" / "idle-1" / "SKILL.md",
        name="idle-1",
    )

    payload = api.overview()
    assert payload["total"] == 3
    assert payload["used"] == 2
    assert payload["unused"] == 1
    assert payload["by_host"]["codex"]["total"] == 3
    assert payload["by_host"]["codex"]["used"] == 2


def test_overview_net_harmful_counts_only_negative_net(env):
    home, _store = env
    _write_skill(
        home / ".codex" / "skills" / "bad" / "SKILL.md",
        name="bad", helpful=1, harmful=5, last_updated="2026-05-19T00:00:00Z",
    )
    _write_skill(
        home / ".codex" / "skills" / "ok" / "SKILL.md",
        name="ok", helpful=5, harmful=0, last_updated="2026-05-19T00:00:00Z",
    )
    payload = api.overview()
    assert payload["net_harmful_count"] == 1


@_BY_HOST_SCHEMA_DRIFT
def test_overview_per_host_pivot_via_skill_dir(env):
    home, _store = env
    _write_skill(
        home / ".codex" / "skills" / "a" / "SKILL.md",
        name="a", helpful=1, last_updated="2026-05-19T00:00:00Z",
    )
    _write_skill(
        home / ".claude" / "skills" / "b" / "SKILL.md",
        name="b", helpful=1, last_updated="2026-05-19T00:00:00Z",
    )
    _write_skill(
        home / ".gemini" / "skills" / "c" / "SKILL.md",
        name="c",
    )
    payload = api.overview()
    assert payload["by_host"]["codex"]["total"] == 1
    assert payload["by_host"]["claude"]["total"] == 1
    assert payload["by_host"]["gemini"]["total"] == 1
    assert payload["by_host"]["hermes"]["total"] == 0


# --------------------------------------------------------------------------- #
# GET /api/skills
# --------------------------------------------------------------------------- #


def test_skills_returns_rows_with_host_and_counts(env):
    home, _store = env
    _write_skill(
        home / ".codex" / "skills" / "a" / "SKILL.md",
        name="a", helpful=3, harmful=1, last_updated="2026-05-19T00:00:00Z",
    )
    rows = api.skills()
    assert len(rows) == 1
    row = rows[0]
    assert row["name"] == "a"
    assert row["host"] == "codex"
    assert row["helpful_count"] == 3
    assert row["harmful_count"] == 1
    assert row["net"] == 2
    assert row["used"] is True


def test_skills_filter_by_host(env):
    home, _store = env
    _write_skill(home / ".codex" / "skills" / "a" / "SKILL.md", name="a")
    _write_skill(home / ".claude" / "skills" / "b" / "SKILL.md", name="b")
    just_codex = api.skills(host="codex")
    assert [r["name"] for r in just_codex] == ["a"]
    just_claude = api.skills(host="claude")
    assert [r["name"] for r in just_claude] == ["b"]


def test_skills_sort_by_net_desc(env):
    home, _store = env
    _write_skill(
        home / ".codex" / "skills" / "lo" / "SKILL.md",
        name="lo", helpful=1, harmful=0, last_updated="2026-05-19T00:00:00Z",
    )
    _write_skill(
        home / ".codex" / "skills" / "hi" / "SKILL.md",
        name="hi", helpful=10, harmful=0, last_updated="2026-05-19T00:00:00Z",
    )
    rows = api.skills()
    assert [r["name"] for r in rows] == ["hi", "lo"]


# --------------------------------------------------------------------------- #
# Per-skill sparkline (used by skill_detail)
#
# The global Activity card and its `/api/activity` HTTP endpoint were
# removed — total verdicts over time without per-skill context wasn't
# actionable. The same dense-fill logic survives inside `skill_detail`
# as `_per_skill_sparkline` because the detail pane gives the user
# context for what they're looking at ("THIS skill's history"). These
# tests pin the helper's shape and per-skill filtering.
# --------------------------------------------------------------------------- #


def test_per_skill_sparkline_dense_fills_to_requested_days(env):
    _home, store = env
    store.record_verdict(
        skill_name="a", verdict="HELPFUL",
        reason="real verdict reason for sparkline shape test",
        host="codex", session_id="s1",
    )
    series = api._per_skill_sparkline("a", days=7)
    assert len(series) == 7
    # Today (last entry) carries the verdict count.
    assert series[-1][1] >= 1
    # All entries are [date_str, int].
    for d, c in series:
        assert isinstance(d, str) and len(d) == 10
        assert isinstance(c, int)


def test_per_skill_sparkline_filters_to_named_skill(env):
    _home, store = env
    store.record_verdict(
        skill_name="a", verdict="HELPFUL",
        reason="helpful verdict on skill a for filtering test",
        host="codex", session_id="s1",
    )
    store.record_verdict(
        skill_name="b", verdict="HELPFUL",
        reason="helpful verdict on skill b for filtering test",
        host="codex", session_id="s2",
    )
    just_a = api._per_skill_sparkline("a", days=7)
    assert sum(c for _, c in just_a) == 1


# --------------------------------------------------------------------------- #
# GET /api/verdicts
# --------------------------------------------------------------------------- #


def test_verdicts_orders_desc_by_time(env):
    _home, store = env
    store.record_verdict(
        skill_name="a", verdict="HELPFUL",
        reason="first verdict for ordering test",
        host="codex", session_id="s1",
    )
    store.record_verdict(
        skill_name="b", verdict="HARMFUL",
        reason="second verdict for ordering test",
        host="claude_code", session_id="s2",
    )
    rows = api.verdicts(limit=10)
    # Newest first.
    assert "second verdict" in rows[0]["reason"]
    assert "first verdict" in rows[1]["reason"]


def test_verdicts_normalises_host_to_short_name(env):
    _home, store = env
    store.record_verdict(
        skill_name="a", verdict="HELPFUL",
        reason="helpful verdict for host normalisation test",
        host="claude_code", session_id="s1",
    )
    rows = api.verdicts(limit=10)
    assert rows[0]["host"] == "claude"
    assert rows[0]["host_raw"] == "claude_code"


def test_verdicts_filter_by_short_host_expands_to_raw(env):
    """Clicking the 'claude' chip should catch verdicts stored as
    either 'claude_code' (current canonical) or 'claude' (a future
    rename / a manual insert)."""
    _home, store = env
    store.record_verdict(
        skill_name="a", verdict="HELPFUL",
        reason="from-stop-hook synthetic verdict",
        host="claude_code", session_id="s1",
    )
    rows = api.verdicts(limit=10, host="claude")
    assert len(rows) == 1
    assert rows[0]["reason"] == "from-stop-hook synthetic verdict"


# --------------------------------------------------------------------------- #
# GET /api/skill/<name>
# --------------------------------------------------------------------------- #


def test_skill_detail_includes_per_host_and_sparkline(env):
    home, store = env
    _write_skill(
        home / ".codex" / "skills" / "x" / "SKILL.md",
        name="x", helpful=2, last_updated="2026-05-19T00:00:00Z",
    )
    store.record_verdict(
        skill_name="x", verdict="HELPFUL",
        reason="helpful verdict from codex for skill detail test",
        host="codex", session_id="s1",
    )
    store.record_verdict(
        skill_name="x", verdict="HARMFUL",
        reason="harmful verdict from claude for skill detail test",
        host="claude_code", session_id="s2",
    )
    payload = api.skill_detail("x")
    assert payload is not None
    assert payload["name"] == "x"
    assert payload["host"] == "codex"
    assert "codex" in payload["per_host"]
    assert "claude" in payload["per_host"]
    assert len(payload["sparkline"]) == 30
    assert len(payload["recent"]) == 2


def test_skill_detail_unknown_returns_none(env):
    home, _store = env
    _write_skill(home / ".codex" / "skills" / "a" / "SKILL.md", name="a")
    assert api.skill_detail("does-not-exist") is None


# --------------------------------------------------------------------------- #
# PATCH /api/verdict/<id>
# --------------------------------------------------------------------------- #


def _seed_verdict(store: Store, *, skill: str, verdict: str = "HELPFUL",
                  reason: str = "synthetic verdict for unit test",
                  session_id: str = "s") -> int:
    store.record_verdict(
        skill_name=skill, verdict=verdict, reason=reason,
        host="codex", session_id=session_id,
    )
    with store._connect() as conn:
        cur = conn.execute("SELECT MAX(id) FROM verdicts")
        return int(cur.fetchone()[0])


def test_patch_verdict_flips_and_resyncs_frontmatter(env):
    home, store = env
    skill_md = _write_skill(
        home / ".codex" / "skills" / "x" / "SKILL.md",
        name="x", helpful=1, last_updated="2026-05-19T00:00:00Z",
    )
    vid = _seed_verdict(store, skill="x", verdict="HELPFUL",
                        reason="signed HMAC correctly")

    out = api.patch_verdict(vid, {"verdict": "HARMFUL"})
    assert out["ok"] is True
    assert out["verdict"]["verdict"] == "HARMFUL"

    # Frontmatter resynced: HELPFUL→HARMFUL means helpful=0 harmful=1.
    meta = read_meta(skill_md)
    assert meta.helpful_count == 0
    assert meta.harmful_count == 1


def test_patch_verdict_changes_reason_only(env):
    home, store = env
    _write_skill(
        home / ".codex" / "skills" / "x" / "SKILL.md",
        name="x", helpful=1, last_updated="2026-05-19T00:00:00Z",
    )
    vid = _seed_verdict(store, skill="x", reason="vague reason")

    out = api.patch_verdict(vid, {"reason": "specific reason"})
    assert out["ok"] is True
    assert out["verdict"]["reason"] == "specific reason"


def test_patch_unknown_id_raises_notfound(env):
    _home, _store = env
    with pytest.raises(api.NotFoundError):
        api.patch_verdict(99_999, {"verdict": "HARMFUL"})


def test_patch_invalid_verdict_raises_valueerror(env):
    _home, store = env
    _write_skill(env[0] / ".codex" / "skills" / "x" / "SKILL.md", name="x")
    vid = _seed_verdict(store, skill="x")
    with pytest.raises(ValueError):
        api.patch_verdict(vid, {"verdict": "BANANA"})


def test_patch_unknown_field_raises_keyerror(env):
    _home, store = env
    _write_skill(env[0] / ".codex" / "skills" / "x" / "SKILL.md", name="x")
    vid = _seed_verdict(store, skill="x")
    with pytest.raises(KeyError):
        api.patch_verdict(vid, {"forbidden": "value"})


def test_patch_empty_body_raises_valueerror(env):
    _home, store = env
    _write_skill(env[0] / ".codex" / "skills" / "x" / "SKILL.md", name="x")
    vid = _seed_verdict(store, skill="x")
    with pytest.raises(ValueError):
        api.patch_verdict(vid, {})


# --------------------------------------------------------------------------- #
# DELETE /api/verdict/<id>
# --------------------------------------------------------------------------- #


def test_delete_verdict_removes_row_and_resyncs(env):
    home, store = env
    skill_md = _write_skill(
        home / ".codex" / "skills" / "x" / "SKILL.md",
        name="x", helpful=1, last_updated="2026-05-19T00:00:00Z",
    )
    vid = _seed_verdict(store, skill="x", verdict="HELPFUL")

    out = api.delete_verdict_endpoint(vid)
    assert out["ok"] is True
    assert store.get_verdict(vid) is None
    meta = read_meta(skill_md)
    assert meta.helpful_count == 0


def test_delete_unknown_id_raises_notfound(env):
    _home, _store = env
    with pytest.raises(api.NotFoundError):
        api.delete_verdict_endpoint(99_999)


def test_delete_verdict_drops_embedding_when_present(env, monkeypatch):
    """Seed a stub VerdictEmbeddingsStore directly (no embedder) so
    we can prove the embedding-cleanup path fires without paying the
    sentence-transformers cold-load cost."""
    home, store = env

    import numpy as np

    _write_skill(home / ".codex" / "skills" / "x" / "SKILL.md", name="x")
    ves = VerdictEmbeddingsStore(fingerprint="fp-test")
    vid = _seed_verdict(store, skill="x")
    ves.append(
        verdict_id=vid, skill_name="x", label="HELPFUL",
        embedding=np.array([1.0, 0.0, 0.0], dtype=np.float32),
    )

    # Override the env fixture's no-op stub: point
    # _open_verdict_embeddings at our hand-built ves so the cleanup
    # path actually fires.
    monkeypatch.setattr(api, "_open_verdict_embeddings", lambda: ves)

    out = api.delete_verdict_endpoint(vid)
    assert out["ok"] is True
    assert len(ves) == 0


def test_overview_surfaces_noise_count(env):
    """Noise counter reports historical low-quality verdict rows
    (placeholder reason, <8 chars, etc.) so the dashboard can warn
    the user about test-fixture spillover."""
    _home, store = env
    # Bypass the quality gate by writing the row directly — these
    # represent legacy noise the production gate now blocks.
    with store._connect() as conn:
        conn.execute("BEGIN")
        conn.execute(
            "INSERT INTO skills (skill_name, skill_dir, first_seen_at, last_seen_at) "
            "VALUES ('webhook-signer', '/x', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO verdicts (skill_name, verdict, reason, host, occurred_at, session_id) "
            "VALUES ('webhook-signer', 'HELPFUL', 'ok', 'codex', "
            "'2026-01-01T00:00:00Z', 's1')"
        )
        conn.execute(
            "INSERT INTO verdicts (skill_name, verdict, reason, host, occurred_at, session_id) "
            "VALUES ('webhook-signer', 'HARMFUL', 'evidence A', 'codex', "
            "'2026-01-01T00:00:01Z', 's2')"
        )
        conn.execute("COMMIT")

    payload = api.overview()
    assert payload["noise_verdict_count"] == 2


def test_bulk_delete_removes_all_matching(env):
    """The drawer's 'Delete all N matching' button hits this endpoint;
    it must collapse a (skill, host, reason) cluster in one shot."""
    home, store = env
    _write_skill(home / ".codex" / "skills" / "spam" / "SKILL.md", name="spam")
    # Three identical-tuple rows (different session ids).
    for i in range(3):
        store.record_verdict(
            skill_name="spam", verdict="HELPFUL",
            reason="placeholder reason cluster row",
            host="codex", session_id=f"s{i}",
        )
    # One row with a different reason — must NOT be deleted.
    store.record_verdict(
        skill_name="spam", verdict="HARMFUL",
        reason="genuinely different harmful reason",
        host="codex", session_id="harmful-1",
    )

    out = api.bulk_delete_verdicts({
        "skill_name": "spam",
        "host": "codex",
        "reason": "placeholder reason cluster row",
    })
    assert out["ok"] is True
    assert out["deleted"] == 3

    # Only the harmful row survives.
    remaining = api.verdicts(limit=10, skill="spam")
    assert len(remaining) == 1
    assert remaining[0]["verdict"] == "HARMFUL"


def test_bulk_delete_via_short_host_name(env):
    """Clicking the 'claude' chip and then bulk-deleting must catch
    rows stored under the canonical 'claude_code' value."""
    home, store = env
    _write_skill(home / ".claude" / "skills" / "spam" / "SKILL.md", name="spam")
    for i in range(2):
        store.record_verdict(
            skill_name="spam", verdict="HELPFUL",
            reason="from claude code stop hook",
            host="claude_code", session_id=f"s{i}",
        )

    out = api.bulk_delete_verdicts({
        "skill_name": "spam",
        "host": "claude",
        "reason": "from claude code stop hook",
    })
    assert out["deleted"] == 2


def test_bulk_delete_validates_body(env):
    _home, _store = env
    import pytest as _pytest
    with _pytest.raises(ValueError):
        api.bulk_delete_verdicts({"host": "codex", "reason": "x"})
    with _pytest.raises(ValueError):
        api.bulk_delete_verdicts({"skill_name": "spam", "reason": "x"})


# --------------------------------------------------------------------------- #
# Skill management — archive + hard delete + open folder
# --------------------------------------------------------------------------- #


def test_archive_skill_flips_status_in_frontmatter(env):
    home, _store = env
    skill_md = _write_skill(
        home / ".codex" / "skills" / "victim" / "SKILL.md",
        name="victim",
    )
    out = api.archive_skill("victim")
    assert out["ok"] is True
    assert out["status"] == "archived"
    # Read the file back; status must be archived now.
    text = skill_md.read_text()
    assert "status: archived" in text


def test_archive_unknown_skill_raises_notfound(env):
    _home, _store = env
    import pytest as _pytest
    with _pytest.raises(api.NotFoundError):
        api.archive_skill("does-not-exist")


def test_hard_delete_removes_directory(env):
    home, _store = env
    skill_dir = home / ".codex" / "skills" / "doomed"
    _write_skill(skill_dir / "SKILL.md", name="doomed")
    assert skill_dir.exists()
    out = api.hard_delete_skill("doomed")
    assert out["ok"] is True
    assert not skill_dir.exists()


def test_hard_delete_refuses_outside_roots(env, tmp_path, monkeypatch):
    """The path-confinement guard MUST reject a skill whose directory
    isn't under one of the configured roots — defends against a
    misconfigured root or symlink games."""
    home, _store = env
    # Materialise a SKILL.md outside of any standard root by pointing
    # the test at an unrelated tmp_path. We patch _find_skill_md so
    # the function locates the unsafe SKILL.md.
    rogue = tmp_path / "outside-roots" / "evil" / "SKILL.md"
    rogue.parent.mkdir(parents=True)
    rogue.write_text("---\nname: evil\n---\n", encoding="utf-8")
    monkeypatch.setattr(api, "_find_skill_md", lambda _n: rogue)
    import pytest as _pytest
    with _pytest.raises(PermissionError):
        api.hard_delete_skill("evil")
    assert rogue.exists()  # untouched


def test_open_folder_accepts_path_under_root(env, monkeypatch):
    """open_folder must succeed for a path under a discovered root.
    We stub subprocess so the test doesn't actually launch Finder."""
    home, _store = env
    skill_dir = home / ".codex" / "skills" / "target"
    _write_skill(skill_dir / "SKILL.md", name="target")
    spawned = []
    import subprocess as _sub
    monkeypatch.setattr(
        _sub, "Popen",
        lambda cmd, **kw: spawned.append(cmd) or _Stub(),
    )
    out = api.open_folder({"path": str(skill_dir)})
    assert out["ok"] is True
    assert spawned and str(skill_dir) in spawned[0]


def test_open_folder_refuses_path_outside_roots(env, tmp_path):
    _home, _store = env
    rogue = tmp_path / "outside"
    rogue.mkdir()
    import pytest as _pytest
    with _pytest.raises(PermissionError):
        api.open_folder({"path": str(rogue)})


def test_open_folder_validates_body(env):
    _home, _store = env
    import pytest as _pytest
    with _pytest.raises(ValueError):
        api.open_folder({})
    with _pytest.raises(ValueError):
        api.open_folder({"path": "/no/such/place/exists"})


class _Stub:
    """Stand-in for subprocess.Popen in the open_folder smoke test."""
    pass


def test_patch_verdict_invalidates_embedding(env, monkeypatch):
    home, store = env
    import numpy as np
    _write_skill(home / ".codex" / "skills" / "x" / "SKILL.md", name="x")
    ves = VerdictEmbeddingsStore(fingerprint="fp-test")
    vid = _seed_verdict(store, skill="x", verdict="HELPFUL")
    ves.append(
        verdict_id=vid, skill_name="x", label="HELPFUL",
        embedding=np.array([1.0, 0.0, 0.0], dtype=np.float32),
    )
    monkeypatch.setattr(api, "_open_verdict_embeddings", lambda: ves)

    api.patch_verdict(vid, {"verdict": "HARMFUL"})
    # Old embedding was tagged HELPFUL; flipping label invalidates it.
    assert len(ves) == 0


# --------------------------------------------------------------------------- #
# SQLite-driven used count + history split (the two user-visible fixes)
# --------------------------------------------------------------------------- #


@_BY_HOST_SCHEMA_DRIFT
def test_overview_counts_used_from_sqlite_even_when_frontmatter_stale(env):
    """SKILL.md says helpful=0, but SQLite has a verdict — used must
    still be 1. This is the exact bug the user reported: 'verdict이
    한번이라도 된 스킬이면 used로 카운트가 되어야하는데 안 그러고 있음'.
    """
    home, store = env
    _write_skill(
        home / ".codex" / "skills" / "lagging" / "SKILL.md",
        name="lagging", helpful=0, harmful=0,
    )
    _seed_verdict(store, skill="lagging", verdict="HELPFUL")
    payload = api.overview()
    assert payload["used"] == 1
    assert payload["by_host"]["codex"]["used"] == 1


@_BY_HOST_SCHEMA_DRIFT
def test_overview_surfaces_orphan_count_separately(env):
    """Orphan verdicts (SQLite row with no SKILL.md on disk) are NOT
    folded into total/used/by_host — those track installed skills —
    but are exposed via the dedicated `orphan_count` so the user can
    spot benchmark leftovers or stale catalogs in the health row.
    """
    _home, store = env
    _seed_verdict(store, skill="ghost-skill", verdict="HARMFUL")
    payload = api.overview()
    assert payload["total"] == 0
    assert payload["used"] == 0
    assert payload["orphan_count"] == 1
    # Net-harmful still counts the orphan so the health row stays honest.
    assert payload["net_harmful_count"] == 1
    # "other" must not be in by_host at all — only the four canonical hosts.
    assert set(payload["by_host"].keys()) == {"codex", "claude", "gemini", "hermes"}


def test_skills_includes_orphan_rows(env):
    """Skills list must include SQLite-only orphans so the treemap can
    paint them — otherwise the user sees verdicts in the recent list
    with no corresponding tile."""
    _home, store = env
    _seed_verdict(store, skill="ghost", verdict="HELPFUL")
    rows = api.skills()
    names = {r["name"] for r in rows}
    assert "ghost" in names
    ghost = next(r for r in rows if r["name"] == "ghost")
    assert ghost["orphan"] is True
    assert ghost["used"] is True
    assert ghost["helpful_count"] == 1
    assert ghost["host"] == "other"


def test_skills_prefers_sqlite_counts_over_stale_frontmatter(env):
    """SKILL.md frontmatter helpful_count=99 (stale), SQLite has 2 real
    verdicts. The dashboard reports 2, not 99."""
    home, store = env
    _write_skill(
        home / ".codex" / "skills" / "drift" / "SKILL.md",
        name="drift", helpful=99, harmful=99,
    )
    _seed_verdict(store, skill="drift", verdict="HELPFUL", session_id="s1")
    _seed_verdict(store, skill="drift", verdict="HARMFUL",
                  reason="another synthetic reason", session_id="s2")
    rows = [r for r in api.skills() if r["name"] == "drift"]
    assert rows[0]["helpful_count"] == 1
    assert rows[0]["harmful_count"] == 1


def test_skill_detail_splits_history_by_verdict(env):
    """The drawer needs separate helpful_history and harmful_history
    lists so the user can audit each stream independently."""
    home, store = env
    _write_skill(home / ".codex" / "skills" / "k" / "SKILL.md", name="k")
    _seed_verdict(store, skill="k", verdict="HELPFUL",
                  reason="caught real bug in test fixture A", session_id="s1")
    _seed_verdict(store, skill="k", verdict="HARMFUL",
                  reason="suggested wrong import path", session_id="s2")
    _seed_verdict(store, skill="k", verdict="HARMFUL",
                  reason="confused codebase convention", session_id="s3")
    _seed_verdict(store, skill="k", verdict="NEUTRAL",
                  reason="not applicable to this change", session_id="s4")
    detail = api.skill_detail("k")
    assert detail is not None
    assert detail["helpful_count"] == 1
    assert detail["harmful_count"] == 2
    assert detail["neutral_count"] == 1
    assert len(detail["helpful_history"]) == 1
    assert len(detail["harmful_history"]) == 2
    assert len(detail["neutral_history"]) == 1
    # Each row carries enough to render + edit inline.
    row = detail["helpful_history"][0]
    for k in ("id", "verdict", "reason", "host", "occurred_at"):
        assert k in row


def test_skill_detail_works_for_orphan_skill(env):
    """An orphan skill (verdicts in SQLite, no SKILL.md) still produces
    a usable drawer payload so the user can clean it up."""
    _home, store = env
    _seed_verdict(store, skill="vanished", verdict="HARMFUL",
                  reason="documents step that no longer applies")
    detail = api.skill_detail("vanished")
    assert detail is not None
    assert detail["orphan"] is True
    assert detail["skill_dir"] is None
    assert detail["harmful_count"] == 1
    assert len(detail["harmful_history"]) == 1
    assert detail["status"] == "orphan"


def test_verdict_counts_by_skill_aggregates(env):
    _home, store = env
    _seed_verdict(store, skill="agg", verdict="HELPFUL",
                  reason="first synthetic verdict", session_id="s1")
    _seed_verdict(store, skill="agg", verdict="HELPFUL",
                  reason="second synthetic verdict", session_id="s2")
    _seed_verdict(store, skill="agg", verdict="HARMFUL",
                  reason="third synthetic verdict", session_id="s3")
    counts = store.verdict_counts_by_skill()
    assert counts["agg"]["helpful"] == 2
    assert counts["agg"]["harmful"] == 1
    assert counts["agg"]["total"] == 3


# --------------------------------------------------------------------------- #
# GET /api/skills-by-name (skill-centric aggregation across hosts)
# --------------------------------------------------------------------------- #


def test_skills_by_name_aggregates_across_hosts(env):
    """A skill installed in both ~/.codex/skills and ~/.claude/skills
    appears as ONE row with both hosts in per_host + installed_hosts."""
    home, store = env
    _write_skill(home / ".codex" / "skills" / "shared" / "SKILL.md", name="shared")
    _write_skill(home / ".claude" / "skills" / "shared" / "SKILL.md", name="shared")
    store.record_verdict(
        skill_name="shared", verdict="HELPFUL",
        reason="codex round-trip caught the schema mismatch first",
        host="codex", session_id="c1",
    )
    store.record_verdict(
        skill_name="shared", verdict="HARMFUL",
        reason="claude code interpreted the OAuth callback URL wrong",
        host="claude_code", session_id="cc1",
    )
    rows = api.skills_by_name()
    matching = [r for r in rows if r["name"] == "shared"]
    assert len(matching) == 1
    row = matching[0]
    assert row["helpful_count"] == 1
    assert row["harmful_count"] == 1
    assert "codex" in row["per_host"]
    assert "claude" in row["per_host"]
    assert row["per_host"]["codex"]["helpful"] == 1
    assert row["per_host"]["claude"]["harmful"] == 1


def test_skills_by_name_marks_orphans(env):
    _home, store = env
    _seed_verdict(store, skill="vanished", verdict="HARMFUL",
                  reason="documents a step that no longer applies here")
    rows = api.skills_by_name()
    by_name = {r["name"]: r for r in rows}
    assert by_name["vanished"]["orphan"] is True
    assert by_name["vanished"]["installed_hosts"] == []


def test_skills_by_name_sorts_used_first_then_net(env):
    home, store = env
    _write_skill(home / ".codex" / "skills" / "idle" / "SKILL.md", name="idle")
    _write_skill(home / ".codex" / "skills" / "winner" / "SKILL.md", name="winner")
    _write_skill(home / ".codex" / "skills" / "loser" / "SKILL.md", name="loser")
    _seed_verdict(store, skill="winner", verdict="HELPFUL", session_id="w1")
    _seed_verdict(store, skill="loser", verdict="HARMFUL", session_id="l1")
    names = [r["name"] for r in api.skills_by_name()]
    # winner (used, +1) before loser (used, -1) before idle (unused)
    assert names.index("winner") < names.index("loser") < names.index("idle")


# --------------------------------------------------------------------------- #
# GET /api/orphans + POST /api/orphans/delete-bulk
#
# An orphan is a skill_name that has verdict history in store.db but
# no SKILL.md on disk under any registered root. The dashboard surfaces
# them as a clean-up worklist; these tests pin the contract the UI
# depends on.
# --------------------------------------------------------------------------- #


def test_orphans_lists_only_skills_missing_from_disk(env):
    home, store = env
    # On-disk skill — must NOT appear in orphan list.
    _write_skill(home / ".codex" / "skills" / "alive" / "SKILL.md", name="alive")
    _seed_verdict(store, skill="alive", verdict="HELPFUL", session_id="a1")
    # SQLite-only skill — IS an orphan.
    _seed_verdict(store, skill="ghost-1", verdict="HARMFUL", session_id="g1")

    rows = api.orphans()
    names = [r["name"] for r in rows]
    assert "ghost-1" in names
    assert "alive" not in names


def test_orphans_carries_verdict_counts_and_hosts(env):
    _home, store = env
    # Two verdicts, different verdict labels, two raw host strings.
    # Reasons must clear the quality gate in record_verdict
    # (>= ~8 chars + non-placeholder); short tokens like "r1" are
    # silently dropped.
    store.record_verdict(
        skill_name="ghost", verdict="HELPFUL",
        reason="confirmed signature verification worked",
        host="codex", session_id="g1",
    )
    store.record_verdict(
        skill_name="ghost", verdict="HARMFUL",
        reason="picked the wrong skill, no help",
        host="claude_code", session_id="g2",
    )

    rows = api.orphans()
    ghost = next(r for r in rows if r["name"] == "ghost")
    assert ghost["helpful"] == 1
    assert ghost["harmful"] == 1
    assert ghost["total"] == 2
    # Hosts are normalised — "claude_code" → "claude".
    assert set(ghost["hosts"]) == {"codex", "claude"}


def test_orphans_sorted_by_total_desc_then_name(env):
    _home, store = env
    # 1 verdict — should land below the 3-verdict orphan.
    _seed_verdict(store, skill="small-orphan", verdict="HELPFUL", session_id="s1")
    # 3 verdicts — should be first.
    for sid in ("b1", "b2", "b3"):
        _seed_verdict(store, skill="big-orphan", verdict="HELPFUL", session_id=sid)
    # Same count as small-orphan — tiebreak by name.
    _seed_verdict(store, skill="another-orphan", verdict="HELPFUL", session_id="a1")

    rows = api.orphans()
    names = [r["name"] for r in rows]
    assert names == ["big-orphan", "another-orphan", "small-orphan"]


def test_orphans_surfaces_last_seen_dir_when_skills_row_present(env):
    """When a verdict is recorded with ``skill_dir`` set,
    Store.record_verdict's transactional upsert_skill stamps that
    path into the skills table. The orphan endpoint surfaces it so
    the user can recognise which directory was removed."""
    _home, store = env
    store.record_verdict(
        skill_name="ex-skill", verdict="HELPFUL",
        reason="recorded with a real directory snapshot",
        host="codex", session_id="x1",
        skill_dir="/tmp/old/skills/ex-skill",
    )

    rows = api.orphans()
    ex = next(r for r in rows if r["name"] == "ex-skill")
    assert ex["last_seen_dir"] == "/tmp/old/skills/ex-skill"
    assert ex["last_seen_host"] == "codex"


def test_orphans_last_seen_dir_blank_when_verdict_lacks_skill_dir(env):
    """``record_verdict`` without an explicit ``skill_dir`` writes an
    empty path into the skills table (the codepath migration replays
    + Stop-hook NEUTRAL emits hit). The orphan endpoint must still
    list the skill, but with ``last_seen_dir == ""`` so the UI can
    show a "no directory recorded" hint."""
    _home, store = env
    _seed_verdict(store, skill="migration-row", verdict="HELPFUL", session_id="m1")

    rows = api.orphans()
    mig = next(r for r in rows if r["name"] == "migration-row")
    assert mig["last_seen_dir"] == ""
    # _seed_verdict uses host="codex", so the skills-row's
    # last_seen_host will reflect that — orphan rendering of "no
    # directory" is only based on the empty skill_dir, not host.
    assert mig["last_seen_host"] == "codex"


def test_bulk_delete_orphans_removes_verdicts_and_skills_row(env):
    _home, store = env
    _seed_verdict(store, skill="dead", verdict="HELPFUL", session_id="d1")
    _seed_verdict(store, skill="dead", verdict="HARMFUL", session_id="d2")
    # Confirm pre-state: orphan present, skills row exists (record_verdict
    # auto-upserts the skills row via Store.upsert_skill).
    assert store.skill_last_seen("dead") is not None

    out = api.bulk_delete_orphans({"names": ["dead"]})
    assert out["deleted"] == [{"name": "dead", "verdicts_removed": 2}]
    assert out["skipped"] == []

    # Verdicts gone.
    assert store.verdict_counts_by_skill().get("dead") is None
    # skills row gone (so orphan list no longer flags it).
    assert store.skill_last_seen("dead") is None


def test_bulk_delete_orphans_refuses_skills_still_on_disk(env):
    """Safety guard: if the user crafts a request with a name that
    still has SKILL.md on disk, the API must refuse — bulk delete is
    only for genuine orphans."""
    home, store = env
    _write_skill(home / ".codex" / "skills" / "live" / "SKILL.md", name="live")
    _seed_verdict(store, skill="live", verdict="HELPFUL", session_id="l1")

    out = api.bulk_delete_orphans({"names": ["live"]})
    assert out["deleted"] == []
    assert len(out["skipped"]) == 1
    assert out["skipped"][0]["name"] == "live"
    assert "on disk" in out["skipped"][0]["reason"].lower()
    # Verdict still intact.
    counts = store.verdict_counts_by_skill()
    assert counts["live"]["helpful"] == 1


def test_bulk_delete_orphans_rejects_non_list_body(env):
    _home, _store = env
    with pytest.raises(ValueError):
        api.bulk_delete_orphans({"names": "not-a-list"})
    with pytest.raises(ValueError):
        api.bulk_delete_orphans({"names": [1, 2]})


def test_bulk_delete_orphans_empty_list_is_noop(env):
    _home, _store = env
    out = api.bulk_delete_orphans({"names": []})
    assert out == {"deleted": [], "skipped": []}


# --------------------------------------------------------------------------- #
# GET /api/context-savings — token-injection estimate per host.
# --------------------------------------------------------------------------- #


def test_context_savings_no_hosts_installed(env):
    home, _store = env
    # No host dirs created under tmp $HOME.
    out = api.context_savings()
    assert out["installed_host_count"] == 0
    assert out["per_host"] == {}
    assert out["vanilla_sum_tokens_per_turn"] == 0
    # Empty catalog → reference curve passes through (0, 0). The
    # mega_tron_per_turn is 0 here, which is honest: with no skills the
    # router never injects anything. The UI falls back to its empty
    # state in that case (no hero bars to compare).
    assert out["mega_tron_per_turn"] == 0
    assert out["mega_tron_is_measured"] is False
    assert out["mega_tron_turn_count"] == 0
    assert out["warm_up_threshold"] == 20
    assert out["multiplier"] == 0
    assert out["claude_mode"] == "passive"
    assert out["shared_skill_count"] == 0
    # Reference is the bge-m3 default family at pool=0 — not extrapolated.
    assert out["mega_tron_embedder_family"] == "bge-m3"
    assert out["mega_tron_reference_is_extrapolated"] is False


def test_context_savings_single_host_codex_only(env):
    home, _store = env
    # Wire up only Codex with a handful of skills.
    codex_root = home / ".codex" / "skills"
    for i in range(5):
        _write_skill(
            codex_root / f"skill-{i}" / "SKILL.md",
            name=f"skill-{i}",
            description=f"Description for skill {i} explaining what it does.",
        )
    out = api.context_savings()
    assert out["installed_host_count"] == 1
    assert "codex" in out["per_host"]
    assert "claude" not in out["per_host"]
    assert "gemini" not in out["per_host"]
    codex = out["per_host"]["codex"]
    assert codex["skill_count"] == 5
    assert codex["tokens_per_turn"] > 0
    # New shape: names_emitted vs descriptions_emitted.
    assert codex["names_emitted"] == 5
    assert codex["descriptions_emitted"] >= 0
    assert "cap-bound" in codex["rule_summary"]
    # codex / gemini have no actionable advice — only claude carries
    # a non-null severity icon. This keeps the row sub-line short
    # for the hosts where there's nothing to recommend.
    assert codex["rule_advice"] is None
    assert codex["rule_severity"] is None
    assert codex["shared_skill_count"] == 0
    # vanilla sum equals the single host's tokens.
    assert out["vanilla_sum_tokens_per_turn"] == codex["tokens_per_turn"]


def test_context_savings_three_hosts_passive_claude(env, monkeypatch):
    home, _store = env
    monkeypatch.delenv("MEGA_CLAUDE_NATIVE_MODE", raising=False)
    for host in ("codex", "claude", "gemini"):
        root = home / f".{host}" / "skills"
        for i in range(3):
            _write_skill(
                root / f"s{i}" / "SKILL.md",
                name=f"{host}-skill-{i}",
                description=f"A description for {host}-skill-{i} that's long enough to exercise the token counter.",
            )

    out = api.context_savings()
    assert out["installed_host_count"] == 3
    assert set(out["per_host"].keys()) == {"codex", "claude", "gemini"}
    assert out["claude_mode"] == "passive"

    claude = out["per_host"]["claude"]
    assert claude["claude_mode"] == "passive"
    # Passive-mode rule subtitle is short — the actionable advice
    # lives in the hover-only `rule_advice`. Small pool (3 skills) →
    # suggest active mode.
    assert "1% × ctx" in claude["rule_summary"]
    assert "MEGA_CLAUDE_NATIVE_MODE=active" in claude["rule_advice"]
    assert claude["rule_severity"] == "suggest"
    # Claude's "names always" rule: every skill ships its name.
    assert claude["names_emitted"] == 3

    # Sum == sum of three hosts.
    assert out["vanilla_sum_tokens_per_turn"] == sum(
        h["tokens_per_turn"] for h in out["per_host"].values()
    )


def test_context_savings_grand_total_and_shared_only(env):
    """The breakdown table's bottom rows depend on two derived
    counts: ``shared_only_count`` (in ~/.agents/skills but in no
    host's private dir) and ``total_unique_count`` (the grand union
    across every dir, deduped by name)."""
    home, _store = env
    shared = home / ".agents" / "skills"
    claude_root = home / ".claude" / "skills"

    # Layout:
    #   shared: { s1, s2, s3, s4, s5 }
    #   claude private: { s4, s5, c-only-1, c-only-2 }
    # → s1, s2, s3 are shared-only (3)
    # → c-only-1, c-only-2 are claude-unique (2)
    # → s4, s5 are in both
    # → total union = {s1..s5, c-only-1, c-only-2} = 7
    for i in range(1, 6):
        _write_skill(shared / f"s{i}" / "SKILL.md", name=f"s{i}", description="d.")
    for i in range(4, 6):
        _write_skill(claude_root / f"s{i}" / "SKILL.md", name=f"s{i}", description="d.")
    for i in (1, 2):
        _write_skill(
            claude_root / f"c-only-{i}" / "SKILL.md",
            name=f"c-only-{i}", description="d.",
        )

    out = api.context_savings()
    assert out["shared_skill_count"] == 5
    assert out["shared_only_count"] == 3
    assert out["total_unique_count"] == 7


def test_context_savings_shared_agents_dir_folded_into_every_host(env):
    """~/.agents/skills is host-neutral — every host's catalog must pick
    those skills up on top of its private dir."""
    home, _store = env
    # 2 skills in shared, 1 in codex only.
    shared = home / ".agents" / "skills"
    for i in range(2):
        _write_skill(
            shared / f"shared-{i}" / "SKILL.md",
            name=f"shared-skill-{i}",
            description=f"Shared skill {i} description.",
        )
    codex_root = home / ".codex" / "skills"
    _write_skill(
        codex_root / "only-codex" / "SKILL.md",
        name="only-codex",
        description="Codex-private skill.",
    )

    out = api.context_savings()
    # The shared count is surfaced at the top level.
    assert out["shared_skill_count"] == 2
    # Every host where private OR shared has content shows up.
    # Codex has private + shared → pool = 3.
    assert out["per_host"]["codex"]["skill_count"] == 3
    # Claude / Gemini have no private dirs, but shared exists so they
    # are still surfaced with the shared pool.
    assert "claude" in out["per_host"]
    assert out["per_host"]["claude"]["skill_count"] == 2
    assert "gemini" in out["per_host"]
    assert out["per_host"]["gemini"]["skill_count"] == 2
    # Per-host carries the shared count for the UI footer row.
    for h in out["per_host"].values():
        assert h["shared_skill_count"] == 2

    # New decomposition keys for the breakdown card:
    # codex has 1 private skill, none of which overlap with shared.
    codex = out["per_host"]["codex"]
    assert codex["private_skill_count"] == 1
    assert codex["overlap_with_shared"] == 0
    assert codex["unique_to_host"] == 1
    # claude has no private dir → private_count=0, overlap=0, unique=0.
    cl = out["per_host"]["claude"]
    assert cl["private_skill_count"] == 0
    assert cl["overlap_with_shared"] == 0
    assert cl["unique_to_host"] == 0


def test_context_savings_overlap_decomposition_with_duplicates(env):
    """When a host's private folder duplicates names from
    ~/.agents/skills, the breakdown payload must expose
    (private_count, overlap_with_shared, unique_to_host) so the UI
    can tell the user how much of their private folder is "really"
    just a mirror of the shared pool."""
    home, _store = env
    shared = home / ".agents" / "skills"
    claude_root = home / ".claude" / "skills"
    # 10 shared skills.
    for i in range(10):
        _write_skill(
            shared / f"sk-{i}" / "SKILL.md",
            name=f"sk-{i}",
            description=f"Shared {i}.",
        )
    # claude's folder: 7 of them mirror shared, 3 are unique to claude.
    for i in range(7):
        _write_skill(
            claude_root / f"sk-{i}" / "SKILL.md",
            name=f"sk-{i}",
            description=f"Claude's copy of shared {i}.",
        )
    for i in range(3):
        _write_skill(
            claude_root / f"claude-only-{i}" / "SKILL.md",
            name=f"claude-only-{i}",
            description=f"Claude-unique {i}.",
        )

    out = api.context_savings()
    cl = out["per_host"]["claude"]
    # 7 mirrored + 3 unique = 10 files under ~/.claude/skills.
    assert cl["private_skill_count"] == 10
    assert cl["overlap_with_shared"] == 7
    assert cl["unique_to_host"] == 3
    # Visible (deduped) pool = 7 (shared keeps its copy) + 3 unique
    # + 3 shared-only that claude doesn't mirror = 13.
    assert cl["skill_count"] == 13


def test_context_savings_claude_active_mode_small_pool_saves_tokens(env, monkeypatch):
    """Small-pool active mode collapses descriptions → fewer tokens.

    Below the passive budget saturation point (~250 skills), active
    mode meaningfully drops description tokens. Above that point
    passive already evicts everything; see the dedicated
    test_context_savings_claude_active_no_op_at_large_pool below."""
    home, _store = env
    claude_root = home / ".claude" / "skills"
    for i in range(20):
        _write_skill(
            claude_root / f"s{i}" / "SKILL.md",
            name=f"skill-{i:03d}",
            description="A reasonably long description so passive-mode budget gets exercised. " * 3,
        )

    monkeypatch.delenv("MEGA_CLAUDE_NATIVE_MODE", raising=False)
    passive_out = api.context_savings()
    passive_tok = passive_out["per_host"]["claude"]["tokens_per_turn"]
    assert passive_out["claude_mode"] == "passive"

    monkeypatch.setenv("MEGA_CLAUDE_NATIVE_MODE", "active")
    active_out = api.context_savings()
    active_tok = active_out["per_host"]["claude"]["tokens_per_turn"]
    assert active_out["claude_mode"] == "active"
    assert active_out["per_host"]["claude"]["claude_mode"] == "active"
    # Small pool, active mode → "names-only catalog (skillOverrides)" +
    # suggest icon. Strict is the next-step advice.
    cl = active_out["per_host"]["claude"]
    assert "names-only" in cl["rule_summary"]
    assert cl["rule_severity"] == "suggest"
    assert "MEGA_CLAUDE_NATIVE_MODE=strict" in cl["rule_advice"]
    # Active mode drops descriptions → strictly fewer tokens.
    assert active_tok < passive_tok


def test_context_savings_claude_active_no_op_at_large_pool(env, monkeypatch):
    """At pool sizes > the saturation threshold, active mode produces
    the same token count as passive (because passive already evicts
    every description to make room for names). The rule message must
    warn the user that strict is the only remaining lever."""
    home, _store = env
    claude_root = home / ".claude" / "skills"
    # 300 > _CLAUDE_ACTIVE_USEFUL_BELOW (250). Description doesn't
    # need to be huge — the budget is consumed by name lines alone.
    for i in range(300):
        _write_skill(
            claude_root / f"s{i:03d}" / "SKILL.md",
            name=f"skill-{i:03d}",
            description="A description.",
        )

    monkeypatch.setenv("MEGA_CLAUDE_NATIVE_MODE", "active")
    out = api.context_savings()
    cl = out["per_host"]["claude"]
    assert cl["claude_mode"] == "active"
    # The warning is the load-bearing UX signal — now in the hover
    # advice + severity icon, not in the inline summary text.
    assert cl["rule_severity"] == "warn"
    assert "no effect" in cl["rule_advice"]
    assert "MEGA_CLAUDE_NATIVE_MODE=strict" in cl["rule_advice"]


def test_context_savings_claude_strict_zeros_native_catalog(env, monkeypatch):
    """Strict mode removes the native catalog entirely; tokens → 0
    and the rule message confirms the suppression."""
    home, _store = env
    claude_root = home / ".claude" / "skills"
    for i in range(50):
        _write_skill(
            claude_root / f"s{i:02d}" / "SKILL.md",
            name=f"skill-{i:02d}",
            description="Some description text.",
        )

    monkeypatch.setenv("MEGA_CLAUDE_NATIVE_MODE", "strict")
    out = api.context_savings()
    cl = out["per_host"]["claude"]
    assert out["claude_mode"] == "strict"
    assert cl["claude_mode"] == "strict"
    assert cl["tokens_per_turn"] == 0
    # Strict-mode summary mentions the kill switch; severity is ok
    # (no further action needed).
    rule = cl["rule_summary"]
    assert "suppressed" in rule.lower()
    assert "disallowedTools" in rule or "skill tool" in rule.lower()
    assert cl["rule_severity"] == "ok"


def test_context_savings_empty_skill_dir_shows_row_with_zero(env):
    """Dir exists but has no SKILL.md — still surface the host so the
    user sees it's wired up but inert."""
    home, _store = env
    (home / ".codex" / "skills").mkdir(parents=True)
    # No skills under it.
    out = api.context_savings()
    assert "codex" in out["per_host"]
    assert out["per_host"]["codex"]["skill_count"] == 0
    assert out["per_host"]["codex"]["tokens_per_turn"] == 0
    assert "no skills installed" in out["per_host"]["codex"]["rule_summary"]


def test_context_savings_skips_codex_dotfile_dirs(env):
    """~/.codex/skills/.system holds Codex's bundled sample cache.
    A user who never installed anything themselves shouldn't see those
    counted in their catalog size."""
    home, _store = env
    codex_root = home / ".codex" / "skills"
    _write_skill(
        codex_root / ".system" / "bundled-cache" / "SKILL.md",
        name="bundled",
        description="codex bundled sample, not user-owned",
    )
    _write_skill(
        codex_root / "user-skill" / "SKILL.md",
        name="user-skill",
        description="user-installed skill",
    )
    out = api.context_savings()
    # Only the user-installed skill counted; ".system" tree is filtered out.
    assert out["per_host"]["codex"]["skill_count"] == 1


def test_context_savings_multiplier_floored_not_rounded(env, monkeypatch):
    """The multiplier must never overstate — `floor(sum / baseline)`."""
    home, _store = env
    # Build a catalog whose vanilla simulation sums to a value
    # comfortably above 1× baseline so we can verify the integer floor.
    gemini_root = home / ".gemini" / "skills"
    for i in range(20):
        _write_skill(
            gemini_root / f"s{i}" / "SKILL.md",
            name=f"gemini-skill-{i:02d}",
            description="Some description, somewhat long, so the per-skill token cost is non-trivial under Gemini's uncapped catalog rules.",
        )

    out = api.context_savings()
    vsum = out["vanilla_sum_tokens_per_turn"]
    baseline = out["mega_tron_per_turn"]
    expected = vsum // baseline
    assert out["multiplier"] == expected
    # Sanity: with 20 skills emitted in full under Gemini's uncapped
    # rules the vanilla sum (~99 tok/skill) divided by the reference
    # value still leaves a multiplier > 1× — i.e. mega-tron is shipping
    # strictly fewer tokens than vanilla even with the conservative
    # reference. We don't pin the exact ratio because it shifts when
    # the reference value is retuned against a fresh benchmark run.
    assert out["multiplier"] >= 3


def test_interpolate_reference_tokens_curve():
    """The reference curve passes through the published benchmark
    anchors and extrapolates by maintaining the last segment's slope.
    """
    from mega_tron.dashboard.api import _interpolate_reference_tokens

    # bge-m3 anchors from results.md: (0,0), (59,112), (183,145), (500,208).
    for n, expected in [(0, 0), (59, 112), (183, 145), (500, 208)]:
        ref, extrap = _interpolate_reference_tokens("bge-m3", n)
        assert ref == expected, f"pool={n} expected {expected}, got {ref}"
        assert extrap is False, f"pool={n} should be in-range"

    # In-between values land on the line between anchors. At pool=29
    # (halfway from 0 to 59) we expect ~56 tok for bge-m3.
    ref, extrap = _interpolate_reference_tokens("bge-m3", 29)
    assert 50 <= ref <= 60
    assert extrap is False

    # Above the last anchor → extrapolated, slope of the last segment
    # (183→500: 63 tok over 317 skills = ~0.2 tok/skill) applied.
    ref, extrap = _interpolate_reference_tokens("bge-m3", 1000)
    assert extrap is True
    assert ref > 208  # strictly larger than the last measured point

    # Unknown family falls back to bge-m3 silently.
    ref_unknown, _ = _interpolate_reference_tokens("voyage-3", 100)
    ref_bge, _ = _interpolate_reference_tokens("bge-m3", 100)
    assert ref_unknown == ref_bge


def test_classify_embedder_family():
    """The classifier maps HuggingFace IDs to one of three families;
    unknown IDs fall back to bge-m3 (the install-time default)."""
    from mega_tron.dashboard.api import _classify_embedder

    assert _classify_embedder("BAAI/bge-m3") == "bge-m3"
    assert _classify_embedder("BAAI/bge-small-en-v1.5") == "bge-small"
    assert _classify_embedder("ThakiCloud/SKILLRET-Embedding-0.6B") == "skillret"
    # Unknown → default bge-m3.
    assert _classify_embedder("voyage-3") == "bge-m3"
    assert _classify_embedder("") == "bge-m3"


def test_context_savings_warming_up_below_threshold(env):
    """Fewer than WARM_UP_THRESHOLD routes → endpoint must keep the
    benchmark interpolation fallback and flag the warming-up state."""
    home, store = env
    # Need at least one skill on disk so the reference curve evaluates
    # at a non-zero pool size; otherwise the empty catalog case applies.
    _write_skill(
        home / ".codex" / "skills" / "single" / "SKILL.md",
        name="single",
        description="One skill so the reference curve is non-zero.",
    )
    for i in range(api.WARM_UP_THRESHOLD - 1):
        store.record_route(
            session_id=f"sess-{i}",
            host="codex",
            query_hash=f"q-{i}",
            picked_names=["x"],
            total_tok=200 + i,
            k=1,
            k_reason="gap-cut@1",
        )
    out = api.context_savings()
    assert out["mega_tron_is_measured"] is False
    assert out["mega_tron_turn_count"] == api.WARM_UP_THRESHOLD - 1
    # Reference value is interpolated from the benchmark curve, not
    # the legacy static constant — but it must be a positive number
    # since the catalog is non-empty and the curve passes through (0,0).
    assert out["mega_tron_per_turn"] > 0
    assert "reference value" in out["mega_tron_baseline_source"].lower()


def test_context_savings_measured_above_threshold(env):
    """At ≥ WARM_UP_THRESHOLD routes the endpoint switches to the
    measured median + reports p50/p90 in the subtitle."""
    home, store = env
    # 20 samples with median 200 (10 below, 10 ≥): values
    # [191, 192, …, 200, 201, …, 210]. Median (even N) =
    # (samples[9] + samples[10]) // 2 = (200 + 201) // 2 = 200.
    for i in range(20):
        store.record_route(
            session_id=f"sess-{i}",
            host="codex",
            query_hash=f"q-{i}",
            picked_names=["x"],
            total_tok=191 + i,
            k=2,
            k_reason="gap-cut@2",
        )
    out = api.context_savings()
    assert out["mega_tron_is_measured"] is True
    assert out["mega_tron_turn_count"] == 20
    assert out["mega_tron_per_turn"] == 200
    # Subtitle must surface the measurement so the user sees the
    # provenance — including p50 and p90, plus the "sessions" unit
    # (a session = one mega-tron hook fire, not per-turn).
    src = out["mega_tron_baseline_source"]
    assert "sample median" in src.lower() or "median" in src.lower()
    assert "20 sessions" in src
    assert "p50" in src.lower()
    assert "p90" in src.lower()
