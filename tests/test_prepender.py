"""Candidate-skills prefix builder."""
from __future__ import annotations

from pathlib import Path

from mega_tron.prepender import build_hook_context, build_prefix
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


def test_build_hook_context_empty():
    assert build_hook_context([]) == ""


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
    # 3-label verdict vocabulary (HELPFUL / HARMFUL / NEUTRAL); the old
    # 4th label INCONCLUSIVE was dropped because silence already encodes
    # "no signal" — emitting INCONCLUSIVE was redundant with omitting
    # the tag, and the writer no-ops it either way.
    assert "<skill-used" in ctx
    assert "verdict=" in ctx
    assert "HELPFUL" in ctx and "HARMFUL" in ctx and "NEUTRAL" in ctx
    assert "INCONCLUSIVE" not in ctx


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
        # 3-label vocabulary, INCONCLUSIVE dropped.
        assert "HELPFUL" in ctx and "HARMFUL" in ctx and "NEUTRAL" in ctx
        assert "INCONCLUSIVE" not in ctx, builder.__name__
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
