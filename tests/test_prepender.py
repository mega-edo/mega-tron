"""Candidate-skills prefix builder."""
from __future__ import annotations

from pathlib import Path

from mega_tron.prepender import (
    build_claude_hook_context,
    build_gemini_hook_context,
    build_hook_context,
    build_prefix,
)
from mega_tron.router import RankedSkill, Skill


def _rs(
    name: str,
    score: float = 0.5,
    skill_dir: Path = Path("/tmp"),
    description: str = "d",
) -> RankedSkill:
    return RankedSkill(
        skill=Skill(
            name=name,
            skill_dir=skill_dir,
            description=description,
            desc_tok=1,
            sha="x" * 16,
        ),
        score=score,
    )


def test_build_prefix_empty():
    assert build_prefix([]) == ""


def test_build_prefix_one_skill():
    prefix = build_prefix([_rs("alpha")])
    assert prefix == (
        "Candidate skills for this task: alpha. "
        "Use whichever fit; ignore the rest.\n\n"
    )


def test_build_prefix_multiple():
    prefix = build_prefix([_rs("alpha"), _rs("beta"), _rs("gamma")])
    assert prefix == (
        "Candidate skills for this task: alpha, beta, gamma. "
        "Use whichever fit; ignore the rest.\n\n"
    )


def test_build_prefix_caps_at_k():
    ranked = [_rs(f"s{i}") for i in range(10)]
    prefix = build_prefix(ranked, k=3)
    assert "s0" in prefix
    assert "s2" in prefix
    assert "s3" not in prefix
    # No `$` token — codex's must-use rule must not be triggered.
    assert "$" not in prefix


def test_build_prefix_no_must_use_token():
    """`$` would trigger codex's built-in MUST-use rule. We must not emit it."""
    prefix = build_prefix([_rs("alpha"), _rs("beta")])
    assert "$" not in prefix


def test_build_prefix_custom_template():
    prefix = build_prefix([_rs("alpha")], template="Prefer {names}.\n")
    assert prefix == "Prefer alpha.\n"


# --- build_hook_context -------------------------------------------------------


def _assert_no_match_block(out: str) -> None:
    """Shared assertion for the three hook contexts' zero-match path.

    The block must (a) name itself as the catalog block so the model
    pattern-matches it against the same header it sees on populated
    turns, (b) explicitly tell the model to emit zero tags, and (c)
    name the failure mode it's preventing (stale names from earlier
    turns) so the instruction sticks even under attention pressure.
    """
    assert out != ""
    assert "## Skills (selected for this turn by mega-tron)" in out
    assert "(none" in out
    assert "emit zero" in out
    assert "`<skill-used>`" in out
    assert "earlier turns" in out


def test_build_hook_context_empty():
    """Zero-match turn must inject an explicit "emit zero tags"
    instruction rather than returning an empty string. Empty injection
    silently lets the model fall back on Skills blocks from earlier
    turns of the same conversation, which is the dominant origin of
    hallucinated `<skill-used>` tag names."""
    _assert_no_match_block(build_hook_context([]))


def test_build_claude_hook_context_empty():
    """Claude variant must emit the same zero-match guard. The three
    hook contexts share one ``_no_match_context`` helper, so this is a
    contract test against drift if anyone ever inlines the empty
    branch in just one of them."""
    _assert_no_match_block(build_claude_hook_context([]))


def test_build_gemini_hook_context_empty():
    """Gemini variant must emit the same zero-match guard."""
    _assert_no_match_block(build_gemini_hook_context([]))


def test_build_hook_context_includes_candidates_and_meta_block():
    ranked = [
        _rs(
            "webhook-signer",
            skill_dir=Path("/Users/alice/.codex/skills/webhook-signer"),
            description="HMAC-SHA256 webhook signature verification middleware.",
        ),
        _rs(
            "hmac-verify",
            skill_dir=Path("/Users/alice/.claude/skills/hmac-verify"),
            description="Constant-time HMAC comparison utility.",
        ),
    ]
    ctx = build_hook_context(ranked, k=2)
    # Top-level header so the model recognises this as a skills section.
    assert ctx.startswith("## Skills (selected for this turn by mega-tron)")
    # Candidates are named bare (no `$` — that would trigger codex's
    # built-in must-use rule, which we deliberately avoid so the model
    # judges fit instead of being forced).
    assert "Candidate skills for this task: webhook-signer, hmac-verify." in ctx
    # Each skill name appears at least twice — candidate line + meta entry.
    assert ctx.count("webhook-signer") >= 2
    assert ctx.count("hmac-verify") >= 2
    # SKILL.md absolute paths so the model can open them directly.
    assert "/Users/alice/.codex/skills/webhook-signer/SKILL.md" in ctx
    assert "/Users/alice/.claude/skills/hmac-verify/SKILL.md" in ctx
    # Descriptions are emitted alongside each pick.
    assert "HMAC-SHA256 webhook signature verification middleware." in ctx
    assert "Constant-time HMAC comparison utility." in ctx
    # No `$` token anywhere, and no MUST-use rule block — those are the
    # two things that turn "candidate" into "forced". Their absence is
    # the contract this builder owes the system.
    assert "$" not in ctx
    assert "MUST use" not in ctx
    assert "Trigger rules" not in ctx
    # Self-evaluation contract is previewed inline. The contract uses a
    # 3-label verdict vocabulary (HELPFUL / HARMFUL / NEUTRAL); silence
    # encodes "no signal" rather than a fourth label.
    assert "<skill-used" in ctx
    assert "verdict=" in ctx
    assert "HELPFUL" in ctx and "HARMFUL" in ctx and "NEUTRAL" in ctx


def test_build_hook_context_caps_at_k():
    ranked = [_rs(f"s{i}", skill_dir=Path(f"/tmp/s{i}"), description=f"desc {i}") for i in range(5)]
    ctx = build_hook_context(ranked, k=3)
    for i in range(3):
        assert f"s{i}" in ctx
        assert f"/tmp/s{i}/SKILL.md" in ctx
    for i in range(3, 5):
        # Use a unique substring that can't be a prefix of an in-range name.
        assert f"/tmp/s{i}/SKILL.md" not in ctx


def test_build_hook_context_handles_blank_description():
    ranked = [_rs("nameonly", description="")]
    ctx = build_hook_context(ranked)
    assert "nameonly" in ctx
    # Blank description should not emit a stranded "    desc: " line.
    assert "    desc: \n" not in ctx


# --- Gemini workspace-trust regression -----------------------------------


def test_build_gemini_hook_context_inlines_skill_body(tmp_path):
    """Regression: Gemini's workspace-trust sandbox refuses ``read_file``
    on paths outside the active project, so the hook must embed the
    SKILL.md body directly. Verify the body lands and the prose tells
    the model not to call ``read_file``.
    """
    from mega_tron.prepender import build_gemini_hook_context

    skill_dir = tmp_path / "demo-skill"
    skill_dir.mkdir()
    skill_md = skill_dir / "SKILL.md"
    skill_body = (
        "---\nname: demo-skill\n---\n\n"
        "# Demo Skill\n\nFollow these exact steps when invoked:\n\n"
        "1. Step one\n2. Step two\n"
    )
    skill_md.write_text(skill_body)

    ranked = [_rs("demo-skill", skill_dir=skill_dir, description="A demo")]
    ctx = build_gemini_hook_context(ranked, k=1)

    assert "demo-skill" in ctx
    assert "body: |" in ctx
    # Each line of the body should appear with leading-space indent for
    # the YAML-ish block format.
    assert "      # Demo Skill" in ctx
    assert "      1. Step one" in ctx
    # Workspace-trust guard text present.
    assert "Do not call" in ctx
    assert "workspace-trust" in ctx


def test_build_gemini_hook_context_handles_missing_body(tmp_path):
    """Missing SKILL.md must not crash — desc-only entry is still useful."""
    from mega_tron.prepender import build_gemini_hook_context

    nonexistent = tmp_path / "no-such-skill"
    nonexistent.mkdir()
    ranked = [_rs("ghost", skill_dir=nonexistent, description="phantom")]
    ctx = build_gemini_hook_context(ranked, k=1)

    assert "ghost" in ctx
    assert "phantom" in ctx
    # No body block emitted when SKILL.md is unreadable.
    assert "body: |" not in ctx


def test_build_gemini_hook_context_per_skill_cap(tmp_path):
    """Very large SKILL.md bodies must be truncated, not blown into the
    additionalContext blob."""
    from mega_tron.prepender import (
        _GEMINI_PER_SKILL_BODY_CAP,
        build_gemini_hook_context,
    )

    skill_dir = tmp_path / "fat-skill"
    skill_dir.mkdir()
    fat_body = "line\n" * 5000  # ~25000 chars, well over the per-skill cap
    (skill_dir / "SKILL.md").write_text(fat_body)

    ranked = [_rs("fat-skill", skill_dir=skill_dir, description="huge")]
    ctx = build_gemini_hook_context(ranked, k=1)

    # Truncation marker present.
    assert "[truncated" in ctx
    # The total output is bounded — body excerpt is capped in raw chars,
    # then each line gets a 6-char indent ("      ") and a newline, so
    # worst-case the indented form can be up to ~8× the raw cap (1-char
    # lines all get the same overhead). Add a prose-overhead budget on
    # top. The point of this assertion is "didn't dump all 25000 chars
    # of the body in", not a tight numeric bound.
    assert len(ctx) < (_GEMINI_PER_SKILL_BODY_CAP * 9) + 4000


# --- Inline-verdict contract regression -----------------------------------


def test_self_eval_contract_uses_inline_verdict_attribute():
    """The Stop-hook UX bug was that {"decision":"block","reason":...}
    surfaces the eval prompt to the user on Claude/Codex. We fixed it
    by asking the model to emit the verdict inline in the same
    `<skill-used>` tag it already writes. This test pins the contract
    to the inline form so future edits don't regress to the old
    'continuation-turn' prompt.
    """
    from mega_tron.prepender import (
        build_claude_hook_context,
        build_gemini_hook_context,
        build_hook_context,
    )

    for builder in (build_hook_context, build_claude_hook_context, build_gemini_hook_context):
        ctx = builder([_rs("alpha", description="d")], k=1)
        # The inline tag form is documented in the contract.
        assert "verdict=" in ctx, builder.__name__
        # 3-label vocabulary.
        assert "HELPFUL" in ctx and "HARMFUL" in ctx and "NEUTRAL" in ctx
        # The contract explicitly tells the model that silence == no
        # signal, so the model doesn't feel forced to emit a verdict.
        lower = ctx.lower()
        assert "silence" in lower or "omit the tag" in lower, builder.__name__
        # And no continuation-turn prompt — the eval is captured from
        # the same final reply.
        assert "no follow-up" in lower or "no continuation" in lower, builder.__name__
        # Verdict `reason` text must be English even when the rest of
        # the reply is not. Reasons get persisted to SKILL.md + the
        # verdict-embedding store, where the embedder is tuned on
        # English; mixing languages silently degrades future routing.
        assert "must be written in english" in lower, builder.__name__


# --- session_id propagation ---------------------------------------------------
#
# When the host hook supplies a session id, the prepender must stamp it
# into the injected block so the model can pass it through to its
# `mega-tron search` shell calls. This is the load-bearing piece of the
# verdict-capture fix: cli-host routes rows written with the real session
# id let the stop hook's verdict gate admit the model's `<skill-used>`
# tags. Without these tests it's easy to silently drop the session-id
# stamp during a future refactor and re-break verdict capture.


def test_session_block_omitted_when_session_id_none():
    """Backward compat: passing None (or no session_id) keeps the
    output identical to the legacy block — no Session header."""
    for builder in (
        build_hook_context,
        build_claude_hook_context,
        build_gemini_hook_context,
    ):
        ctx = builder([_rs("alpha", description="d")], k=1)
        assert "### Session" not in ctx, builder.__name__
        assert "--session-id" not in ctx, builder.__name__
        ctx_none = builder([_rs("alpha", description="d")], k=1, session_id=None)
        assert ctx == ctx_none, builder.__name__


def test_session_block_stamps_literal_id_when_provided():
    """The literal session id appears verbatim; `--session-id <id>` is
    spelled out so the model can copy it into a shell call."""
    sid = "abc-123-deadbeef"
    for builder in (
        build_hook_context,
        build_claude_hook_context,
        build_gemini_hook_context,
    ):
        ctx = builder([_rs("alpha", description="d")], k=1, session_id=sid)
        assert "### Session" in ctx, builder.__name__
        assert sid in ctx, builder.__name__
        assert f"--session-id {sid}" in ctx, builder.__name__
        # The instruction that prevents the model from echoing the id
        # in its reply must be present too — otherwise the UUID leaks
        # into user-visible output.
        assert "do not echo" in ctx.lower(), builder.__name__


def test_session_block_present_on_no_match_too():
    """A no-match turn can still cause the model to call `mega-tron
    search` (the install block tells it to). The session block must
    therefore appear even when the router returns zero skills."""
    sid = "no-match-sid"
    for builder in (
        build_hook_context,
        build_claude_hook_context,
        build_gemini_hook_context,
    ):
        ctx = builder([], k=3, session_id=sid)
        assert "### Session" in ctx, builder.__name__
        assert sid in ctx, builder.__name__


def test_append_session_block_idempotent_when_empty():
    """append_session_block (used by daemon fast-paths) must no-op
    when ctx is empty or session_id is None."""
    from mega_tron.prepender import append_session_block

    assert append_session_block("", "any-sid") == ""
    assert append_session_block("some ctx", None) == "some ctx"
    out = append_session_block("some ctx", "")  # empty string -> no-op
    assert out == "some ctx"


def test_append_session_block_adds_block_for_nonempty_ctx():
    from mega_tron.prepender import append_session_block

    sid = "daemon-fast-path-sid"
    out = append_session_block("existing ctx\n", sid)
    assert out.startswith("existing ctx\n")
    assert "### Session" in out
    assert sid in out
    assert f"--session-id {sid}" in out


# --- follow_up=True slim block ----------------------------------------------
#
# The slim variant is what enables Codex multi-turn verdict capture
# (the model sees a fresh catalog every turn without paying the full
# ~1800-tok first-fire cost). These tests pin the contract: catalog
# header + entries + session block stay; "How to use" prose +
# self-eval contract drop.


def test_follow_up_drops_how_to_use_and_contract():
    """All three builders strip the "How to use these skills" prose
    and the inline self-eval contract on follow-up turns. Catalog
    header + candidate list + Available skills entries stay."""
    for builder in (
        build_hook_context,
        build_claude_hook_context,
        build_gemini_hook_context,
    ):
        full = builder([_rs("alpha", description="d")], k=1)
        slim = builder(
            [_rs("alpha", description="d")], k=1, follow_up=True
        )
        # Both still emit the catalog block.
        assert slim.startswith("## Skills (selected for this turn"), builder.__name__
        assert "### Available skills" in slim, builder.__name__
        assert "alpha" in slim, builder.__name__
        # But the prose / contract is gone in slim.
        assert "### How to use these skills" in full, builder.__name__
        assert "### How to use these skills" not in slim, builder.__name__
        assert "verdict=" in full, builder.__name__
        assert "verdict=" not in slim, builder.__name__
        # Slim should be meaningfully smaller.
        assert len(slim) < len(full) * 0.5, (
            f"{builder.__name__}: slim={len(slim)} full={len(full)}"
        )


def test_follow_up_preserves_session_block():
    """The session-id stamp is load-bearing on follow-up too — the
    model uses it to pass `--session-id <id>` to shell `mega-tron
    search` calls. Slim variant must still carry it."""
    sid = "abc-followup-123"
    for builder in (
        build_hook_context,
        build_claude_hook_context,
        build_gemini_hook_context,
    ):
        slim = builder(
            [_rs("alpha", description="d")], k=1, session_id=sid, follow_up=True
        )
        assert "### Session" in slim, builder.__name__
        assert sid in slim, builder.__name__
        assert f"--session-id {sid}" in slim, builder.__name__


def test_follow_up_no_match_returns_session_block_only():
    """On a no-match follow-up turn (router returned empty), don't
    spam the "emit zero tags" notice every turn — just stamp the
    session block so any shell `mega-tron search` call this turn
    gets attributed correctly. When session_id is also None, true
    noop (empty string)."""
    sid = "no-match-followup"
    for builder in (
        build_hook_context,
        build_claude_hook_context,
        build_gemini_hook_context,
    ):
        # No session_id + no ranked = true noop.
        out = builder([], k=3, follow_up=True)
        assert out == "", builder.__name__

        # With session_id, the session block alone is emitted.
        out_sess = builder([], k=3, session_id=sid, follow_up=True)
        assert "### Session" in out_sess, builder.__name__
        assert sid in out_sess, builder.__name__
        # The "emit zero tags" no-match prose should NOT appear on
        # follow-up turns — only the session block.
        assert "## Skills (selected for this turn" not in out_sess, builder.__name__
