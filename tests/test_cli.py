"""CLI smoke tests — exercise subcommand wiring without the real BGE model.

We patch :func:`mega_tron.cli._make_router` so the real
:class:`BGESmallEmbedder` is never instantiated; the rest of the CLI surface
(argument parsing, table rendering, JSON emission) runs end-to-end.
"""
from __future__ import annotations

import argparse
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from mega_tron.cache import Cache
from mega_tron.cli import cmd_why, main
from mega_tron.router import Router


@pytest.fixture
def cli_skills_dir(tmp_path: Path) -> Path:
    skills = tmp_path / "skills"
    skills.mkdir()
    # Two skills, one with mega_meta evidence relevant to "webhook" queries.
    (skills / "webhook-signer").mkdir()
    (skills / "webhook-signer" / "SKILL.md").write_text(
        '---\n'
        'name: webhook-signer\n'
        'description: "USE WHEN: validate incoming HMAC webhook signature with replay window"\n'
        'mega_meta:\n'
        '  helpful_count: 8\n'
        '  harmful_count: 0\n'
        '  helpful_contexts:\n'
        '    - "validated HMAC payload on test vector"\n'
        '  harmful_contexts: []\n'
        '  status: active\n'
        '---\n'
    )
    (skills / "git-amend-staged").mkdir()
    (skills / "git-amend-staged" / "SKILL.md").write_text(
        '---\n'
        'name: git-amend-staged\n'
        'description: "USE WHEN: amend the staged commit with the message edits"\n'
        '---\n'
    )
    return skills


@pytest.fixture
def patched_make_router(monkeypatch, fake_embedder, tmp_cache_path):
    """Force every CLI command to use a FakeEmbedder + tmp cache."""

    def _factory(args: argparse.Namespace) -> Router:
        cache = Cache(path=tmp_cache_path)
        use_eval = None
        if getattr(args, "no_eval_blend", False):
            use_eval = False
        return Router(
            skills_dir=Path(args.skills_dir),
            embedder=fake_embedder,
            cache=cache,
            use_eval=use_eval,
        )

    monkeypatch.setattr("mega_tron.cli._make_router", _factory)
    return _factory


def _run_why(args_ns: argparse.Namespace) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        rc = cmd_why(args_ns)
    return rc, stdout.getvalue(), stderr.getvalue()


def test_why_single_skill_renders_breakdown(patched_make_router, cli_skills_dir):
    args = argparse.Namespace(
        ticket="validate incoming HMAC webhook signature",
        skill="webhook-signer",
        skills_dir=str(cli_skills_dir),
        top_skills=5,
        json=False,
        no_eval_blend=False,
        sub_cache_path=None,
        cache_path=None,
    )
    rc, out, _ = _run_why(args)
    assert rc == 0
    # Each row of the breakdown table must be present.
    for needle in (
        "skill:  webhook-signer",
        "semantic",
        "count_bonus",
        "context_match",
        "status_mult",
        "final",
    ):
        assert needle in out, f"missing {needle!r} in:\n{out}"


def test_why_top_skills_lists_n_when_skill_omitted(patched_make_router, cli_skills_dir):
    args = argparse.Namespace(
        ticket="HMAC webhook",
        skill=None,
        skills_dir=str(cli_skills_dir),
        top_skills=2,
        json=True,
        no_eval_blend=False,
        sub_cache_path=None,
        cache_path=None,
    )
    rc, out, _ = _run_why(args)
    assert rc == 0
    payload = json.loads(out)
    assert isinstance(payload, list)
    assert 1 <= len(payload) <= 2
    # Webhook skill should be in there because the fake embedder hits its keywords.
    assert any(bd["name"] == "webhook-signer" for bd in payload)


def test_why_helpful_context_match_contributes_positive(
    patched_make_router, cli_skills_dir
):
    """The evidence-aware boost should be non-zero when the query matches a
    helpful_context — the whole reason `why` exists is to make that visible."""
    args = argparse.Namespace(
        ticket="validate webhook hmac signature",
        skill="webhook-signer",
        skills_dir=str(cli_skills_dir),
        top_skills=5,
        json=True,
        no_eval_blend=False,
        sub_cache_path=None,
        cache_path=None,
    )
    rc, out, _ = _run_why(args)
    assert rc == 0
    bd = json.loads(out)[0]
    # Helpful context "validated HMAC payload on test vector" matches the query.
    assert bd["helpful_match"] > 0.0
    assert bd["count_bonus_contribution"] > 0.0
    assert bd["final"] > bd["semantic"]


def test_why_unknown_skill_returns_error(patched_make_router, cli_skills_dir):
    args = argparse.Namespace(
        ticket="anything",
        skill="ghost-skill",
        skills_dir=str(cli_skills_dir),
        top_skills=5,
        json=False,
        no_eval_blend=False,
        sub_cache_path=None,
        cache_path=None,
    )
    rc, _, err = _run_why(args)
    assert rc == 1
    assert "ghost-skill" in err


def test_why_subcommand_wired_into_main(patched_make_router, cli_skills_dir, capsys):
    rc = main([
        "why",
        "HMAC webhook",
        "webhook-signer",
        "--skills-dir", str(cli_skills_dir),
        "--json",
    ])
    assert rc == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload[0]["name"] == "webhook-signer"


# ---------------------------------------------------------------------------
# search — v1.0 unified entry point
# ---------------------------------------------------------------------------

def test_search_default_output_is_meta(
    patched_make_router, cli_skills_dir, capsys
):
    """v1.0 default --output: meta (name + dir + description per pick)."""
    rc = main([
        "search",
        "validate HMAC webhook signature",
        "--skills-dir", str(cli_skills_dir),
        "--mode", "semantic",
        "--top-k", "2",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "- webhook-signer" in out
    assert "dir:" in out
    assert "desc:" in out
    # Description text from the fixture's SKILL.md must be inline.
    assert "USE WHEN: validate incoming HMAC webhook signature" in out


def test_search_output_meta_json(patched_make_router, cli_skills_dir, capsys):
    """--output meta --json emits structured records with name/dir/desc."""
    rc = main([
        "search",
        "validate HMAC webhook",
        "--skills-dir", str(cli_skills_dir),
        "--mode", "semantic",
        "--output", "meta",
        "--top-k", "1",
        "--json",
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, list)
    rec = payload[0]
    assert rec["name"] == "webhook-signer"
    assert "skill_dir" in rec and rec["skill_dir"]
    assert "USE WHEN" in rec["description"]


def test_search_output_names_emits_one_per_line(
    patched_make_router, cli_skills_dir, capsys
):
    """`--output names`: bare skill names, newline-separated. Script-friendly."""
    rc = main([
        "search",
        "validate HMAC webhook signature",
        "--skills-dir", str(cli_skills_dir),
        "--mode", "semantic",
        "--output", "names",
        "--top-k", "2",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    names = [ln for ln in out.splitlines() if ln.strip()]
    assert "webhook-signer" in names
    # Plain text only — no header, no score column, no dir/desc.
    assert "0." not in out
    assert "=====" not in out
    assert "dir:" not in out


def test_search_output_names_json(patched_make_router, cli_skills_dir, capsys):
    rc = main([
        "search",
        "validate HMAC webhook",
        "--skills-dir", str(cli_skills_dir),
        "--mode", "semantic",
        "--output", "names",
        "--top-k", "2",
        "--json",
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, list)
    assert "webhook-signer" in payload


def test_search_output_table_matches_legacy_rank_shape(
    patched_make_router, cli_skills_dir, capsys
):
    """`--output table` reproduces the v0.5 `rank` table layout."""
    rc = main([
        "search",
        "validate HMAC webhook",
        "--skills-dir", str(cli_skills_dir),
        "--mode", "semantic",
        "--output", "table",
        "--top-k", "2",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    # Score column, skill name, token count parenthetical — same as `rank`.
    assert "webhook-signer" in out
    assert "tok)" in out
    # Score is a 4-decimal number.
    assert re_search_score_present(out), out


def re_search_score_present(text: str) -> bool:
    """Helper: at least one `\\d\\.\\d{4}` looking score column appears."""
    import re
    return re.search(r"\b\d+\.\d{4}\b", text) is not None


def test_search_output_bodies_emits_skill_md_content(
    patched_make_router, cli_skills_dir, capsys
):
    """`--output bodies` dumps each picked SKILL.md body, ===== headers in."""
    rc = main([
        "search",
        "validate HMAC webhook",
        "--skills-dir", str(cli_skills_dir),
        "--mode", "semantic",
        "--output", "bodies",
        "--top-k", "1",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "===== webhook-signer =====" in out
    # The SKILL.md description text is part of the body.
    assert "USE WHEN: validate incoming HMAC webhook signature" in out


def test_search_output_bodies_json(patched_make_router, cli_skills_dir, capsys):
    rc = main([
        "search",
        "validate HMAC webhook",
        "--skills-dir", str(cli_skills_dir),
        "--mode", "semantic",
        "--output", "bodies",
        "--top-k", "1",
        "--json",
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, list)
    assert payload[0]["name"] == "webhook-signer"
    assert "USE WHEN" in payload[0]["content"]


def test_search_output_stage_creates_symlinks_and_emits_prefix(
    patched_make_router, cli_skills_dir, tmp_codex_home, capsys
):
    """`--output stage` symlinks into target + emits the candidate prefix."""
    rc = main([
        "search",
        "validate HMAC webhook",
        "--skills-dir", str(cli_skills_dir),
        "--target", str(tmp_codex_home),
        "--mode", "semantic",
        "--output", "stage",
        "--top-k", "2",
        "--prepend-k", "1",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    # Non-forcing candidate-skills prefix on stdout (no `$` token — that
    # would trigger codex's MUST-use rule; we want the model to judge fit).
    assert "Candidate skills for this task: webhook-signer" in out
    assert "$webhook-signer" not in out
    # Symlink under target/skills/.
    staged = tmp_codex_home / "skills" / "webhook-signer"
    assert staged.is_symlink() or staged.exists(), (
        f"expected symlink under {tmp_codex_home/'skills'}"
    )


def test_search_output_stage_without_target_errors(
    patched_make_router, cli_skills_dir, capsys
):
    """`--output stage` without `--target` should fail fast, not crash."""
    rc = main([
        "search",
        "validate HMAC webhook",
        "--skills-dir", str(cli_skills_dir),
        "--mode", "semantic",
        "--output", "stage",
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "--target" in err


# ---------------------------------------------------------------------------
# evaluate — v1.0 JSON-in mutation CLI
# ---------------------------------------------------------------------------

def test_evaluate_applies_helpful_verdict(cli_skills_dir, capsys):
    """`evaluate` mutates SKILL.md mega_meta when given a HELPFUL verdict."""
    payload = json.dumps({
        "evaluations": [
            {
                "skill": "git-amend-staged",
                "verdict": "HELPFUL",
                "reason": "amended staged commit successfully",
            },
        ],
    })
    import sys as _sys
    import io as _io
    _sys.stdin = _io.StringIO(payload)
    try:
        rc = main([
            "evaluate",
            "--skills-dir", str(cli_skills_dir),
        ])
    finally:
        _sys.stdin = _sys.__stdin__
    assert rc == 0
    # Mutation landed.
    from mega_tron.verdicts.mega_meta import read_meta
    meta = read_meta(cli_skills_dir / "git-amend-staged" / "SKILL.md")
    assert meta.helpful_count == 1


def test_evaluate_dry_run_does_not_mutate(cli_skills_dir, tmp_path):
    """--dry-run reports what would change but leaves SKILL.md alone."""
    payload = tmp_path / "verdicts.json"
    payload.write_text(json.dumps({
        "evaluations": [
            {"skill": "git-amend-staged", "verdict": "HELPFUL", "reason": "ok"},
        ],
    }))
    rc = main([
        "evaluate",
        "--skills-dir", str(cli_skills_dir),
        "--file", str(payload),
        "--dry-run",
        "--json",
    ])
    assert rc == 0
    from mega_tron.verdicts.mega_meta import read_meta
    meta = read_meta(cli_skills_dir / "git-amend-staged" / "SKILL.md")
    assert meta.helpful_count == 0  # dry-run: nothing changed


def test_evaluate_json_payload_shape(cli_skills_dir, tmp_path, capsys):
    """--json emits the outcome counts + applied list."""
    payload = tmp_path / "verdicts.json"
    payload.write_text(json.dumps({
        "evaluations": [
            {"skill": "git-amend-staged", "verdict": "HARMFUL", "reason": "broke things"},
            {"skill": "nonexistent-skill", "verdict": "HELPFUL", "reason": "?"},
        ],
    }))
    rc = main([
        "evaluate",
        "--skills-dir", str(cli_skills_dir),
        "--file", str(payload),
        "--json",
    ])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["updated"] == 1
    assert out["skipped_missing"] == 1
    assert out["applied"] == ["git-amend-staged"]


def test_evaluate_invalid_json_returns_2(cli_skills_dir, tmp_path):
    """Malformed JSON exits 2 with a readable stderr."""
    payload = tmp_path / "broken.json"
    payload.write_text("{not json")
    rc = main([
        "evaluate",
        "--skills-dir", str(cli_skills_dir),
        "--file", str(payload),
    ])
    assert rc == 2


def test_search_no_match_returns_nonzero(
    patched_make_router, cli_skills_dir, capsys
):
    """Empty rank → rc=1 with explanatory stderr (same shape as `find`)."""
    # Empty skills dir → router returns []
    rc = main([
        "search",
        "anything",
        "--skills-dir", str(cli_skills_dir.parent / "nonexistent_dir"),
        "--mode", "semantic",
    ])
    # Router warmup raises on missing dir? Or returns empty? Either way: not 0.
    assert rc != 0


def test_search_semantic_prefilter_passes_through_to_router(
    patched_make_router, cli_skills_dir, monkeypatch, capsys
):
    """--prefilter in semantic mode lands on Router.rank's `prefilter` kwarg."""
    captured = {}

    real_rank = Router.rank

    def spy_rank(self, query, top_k=5, *, prefilter=None, agentic=None):
        captured["prefilter"] = prefilter
        captured["agentic"] = agentic
        captured["top_k"] = top_k
        return real_rank(self, query, top_k=top_k, prefilter=prefilter, agentic=agentic)

    monkeypatch.setattr(Router, "rank", spy_rank)

    rc = main([
        "search",
        "validate HMAC webhook",
        "--skills-dir", str(cli_skills_dir),
        "--mode", "semantic",
        "--prefilter", "17",
        "--top-k", "1",
    ])
    assert rc == 0
    assert captured["prefilter"] == 17
    assert captured["agentic"] is None
    assert captured["top_k"] == 1


def test_search_semantic_default_prefilter_is_50(
    patched_make_router, cli_skills_dir, monkeypatch, capsys
):
    """When --prefilter is omitted in semantic mode, CLI passes default 50."""
    captured = {}
    real_rank = Router.rank

    def spy_rank(self, query, top_k=5, *, prefilter=None, agentic=None):
        captured["prefilter"] = prefilter
        return real_rank(self, query, top_k=top_k, prefilter=prefilter, agentic=agentic)

    monkeypatch.setattr(Router, "rank", spy_rank)
    rc = main([
        "search",
        "validate HMAC webhook",
        "--skills-dir", str(cli_skills_dir),
        "--mode", "semantic",
    ])
    assert rc == 0
    assert captured["prefilter"] == 50


def test_search_agentic_passes_cli_knobs_to_agentic_search(
    patched_make_router, cli_skills_dir, monkeypatch, capsys
):
    """--prefilter/--shortlist/--read-max/--timeout-s reach AgenticSearch.__init__."""
    captured = {}

    class FakeAgentic:
        def __init__(
            self,
            backend,
            *,
            top=None,
            shortlist=None,
            top_k=None,
            max_reads=None,
            timeout_s=None,
        ):
            captured["top"] = top
            captured["shortlist"] = shortlist
            captured["top_k"] = top_k
            captured["max_reads"] = max_reads
            captured["timeout_s"] = timeout_s
            self.backend = backend
            # Knobs the Router reads when deciding whether to short-circuit.
            self.multi_perspective = False
            self.always_read = False
            self.skip_when_confident = False
            self.confidence_gap = 0.0
            self.body_first = False
            self.pin_cosine_top1 = False

        def prefilter(self, cache, q_vec):
            return list(cache.entries())[:1]

        def pick(self, query, candidates):
            from mega_tron.agentic import AgenticResult
            return AgenticResult(
                picks=[candidates[0].name],
                calls=1,
                reads=[],
                fell_back=False,
            )

    monkeypatch.setattr("mega_tron.agentic.AgenticSearch", FakeAgentic)
    monkeypatch.setattr(
        "mega_tron.llm_backends.make_llm_backend",
        lambda: object(),
    )

    rc = main([
        "search",
        "validate HMAC webhook",
        "--skills-dir", str(cli_skills_dir),
        "--mode", "agentic",
        "--prefilter", "175",
        "--shortlist", "22",
        "--read-max", "4",
        "--timeout-s", "12",
        "--top-k", "1",
    ])
    assert rc == 0
    assert captured == {
        "top": 175,
        "shortlist": 22,
        "top_k": 1,
        "max_reads": 4,
        "timeout_s": 12,
    }


# --- --session-id / MEGA_SESSION_ID resolution -------------------------------
#
# `mega-tron search` writes a routes row on every invocation. When the
# call comes from a model inside a host CLI, the row must carry the
# host's session id so the stop hook's verdict gate can credit the
# model's `<skill-used>` tags. These tests pin the resolution order
# and the env-var fallback.


def test_resolve_session_id_uses_arg_first(monkeypatch):
    """Explicit --session-id wins over MEGA_SESSION_ID env."""
    import argparse

    from mega_tron.cli.search import _resolve_session_id

    monkeypatch.setenv("MEGA_SESSION_ID", "env-id")
    args = argparse.Namespace(session_id="arg-id")
    assert _resolve_session_id(args) == "arg-id"


def test_resolve_session_id_falls_back_to_env(monkeypatch):
    """Missing flag falls back to MEGA_SESSION_ID."""
    import argparse

    from mega_tron.cli.search import _resolve_session_id

    monkeypatch.setenv("MEGA_SESSION_ID", "env-id")
    args = argparse.Namespace(session_id=None)
    assert _resolve_session_id(args) == "env-id"


def test_resolve_session_id_returns_none_when_nothing_set(monkeypatch):
    """Neither flag nor env → headless (None)."""
    import argparse

    from mega_tron.cli.search import _resolve_session_id

    monkeypatch.delenv("MEGA_SESSION_ID", raising=False)
    args = argparse.Namespace(session_id=None)
    assert _resolve_session_id(args) is None


def test_resolve_session_id_strips_whitespace(monkeypatch):
    """Whitespace-only values count as "not set"."""
    import argparse

    from mega_tron.cli.search import _resolve_session_id

    monkeypatch.setenv("MEGA_SESSION_ID", "   ")
    args = argparse.Namespace(session_id="   ")
    assert _resolve_session_id(args) is None


def test_search_writes_route_with_session_id(
    patched_make_router, cli_skills_dir, monkeypatch, tmp_path
):
    """End-to-end: `mega-tron search --session-id X` writes a routes row
    with session_id=X. This is the load-bearing piece of the verdict-
    capture fix — without it the stop-hook gate cannot admit the
    model's `<skill-used>` tags."""
    from mega_tron.config import store_path
    from mega_tron.verdicts.store import Store

    # Redirect store to a fresh per-test DB.
    db = tmp_path / "store.db"
    monkeypatch.setattr("mega_tron.cli.search.store_path", lambda: db, raising=False)
    monkeypatch.setattr("mega_tron.config.store_path", lambda: db, raising=False)
    # The `_log_route_cli` helper resolves store_path() at call time via
    # `from mega_tron.config import store_path`, so we also patch the
    # symbol the helper imports.
    import mega_tron.cli.search as search_mod

    orig = search_mod._log_route_cli

    def _patched(query, ranked, router, *, session_id=None):
        # Force a deterministic Store path regardless of import quirks.
        import hashlib

        if router.last_dynamic is not None:
            k, k_reason = router.last_dynamic
        else:
            k, k_reason = len(ranked), "manual"
        total_tok = sum(r.skill.desc_tok for r in ranked)
        qhash = hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]
        Store(path=db).record_route(
            session_id=session_id,
            host="cli",
            query_hash=qhash,
            picked_names=[r.skill.name for r in ranked],
            total_tok=total_tok,
            k=k,
            k_reason=k_reason,
        )

    monkeypatch.setattr(search_mod, "_log_route_cli", _patched)

    rc = main([
        "search",
        "validate HMAC webhook",
        "--skills-dir", str(cli_skills_dir),
        "--mode", "semantic",
        "--output", "names",
        "--top-k", "1",
        "--session-id", "test-session-xyz",
    ])
    assert rc == 0

    s = Store(path=db)
    s.initialize()
    picked = s.session_picked_names(session_id="test-session-xyz", host="cli")
    assert picked, "no routes row written for the supplied session id"


