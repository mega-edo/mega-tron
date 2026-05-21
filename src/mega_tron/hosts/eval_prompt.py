"""Shared evaluation prompt builder for the four host Stop/AfterAgent
hooks. All hosts emit the same evidence-grounded grading contract;
only the trigger-token form ("$name", "/name", "tool: name") differs.

Centralising the prompt here keeps the four hooks aligned and gives
the Store's reason-quality gate (:func:`_reason_passes_quality`) and
the dashboard's noise counter a single text source they all conform
to.
"""
from __future__ import annotations


def build_eval_prompt(
    *,
    invoked: list[str],
    trigger_token: str,
    sentinel_start: str,
    sentinel_end: str,
) -> str:
    """Render the in-session self-evaluation prompt.

    Args:
        invoked: skill names tagged in the model's last reply via
            ``<skill-used name="..."/>``.
        trigger_token: a template-style prefix used to render each
            skill name in the bullet list. ``"$"`` for Codex's
            mention syntax, ``"/"`` for Claude's slash command,
            ``""`` for Gemini's tool-call form (the bullet just
            shows the name).
        sentinel_start: opening fence for the JSON block. Both
            sentinels are usually produced by
            :func:`mega_tron.host_sentinels.make_sentinels`.
        sentinel_end: closing fence.

    The returned text is appended verbatim to the host's "continue
    with this prompt" payload. Keep it stable — the reason-quality
    rules ("≥ one sentence, citing concrete evidence") are baked in
    here and any drift breaks the new dashboard noise filter.
    """
    bullets = "\n".join(f"  - {trigger_token}{name}" for name in invoked)
    return f"""Before this session ends, evaluate the skills you used in your work.

Skills invoked this session:
{bullets}

Before assigning a verdict, gather concrete evidence using your tools:
  - Run `git diff --stat` and `git status` to see what actually changed.
  - For each invoked skill, locate the part of the diff or transcript that
    shows it was used and what it produced.
  - If a test was added or modified, run it. If a script ran, check its exit
    code in the transcript.
  - If you cannot produce a one-sentence evidence citation for a skill, mark
    it INCONCLUSIVE — do NOT default to HELPFUL.

A verdict without a useful reason is worse than no verdict at all — it
pollutes the rank-blend signal that future routing depends on. Bad reasons
are rejected automatically and counted as noise on the dashboard.

VERDICT LABELS
  - HELPFUL      = evidence shows the skill clearly contributed
  - HARMFUL      = evidence shows the skill led you astray or was wrong
  - NEUTRAL      = the skill ran and the evidence shows it didn't move the needle
  - INCONCLUSIVE = no evidence either way (treated as no signal — no counter update)

REASON FIELD RULES (strict)
  - Minimum one full sentence (≥ ~15 words), grounded in concrete evidence.
  - Cite the file path / test name / command / API response that proves
    the verdict. Generic praise or blame is rejected.
  - Explain WHY the skill helped or failed in language a future router run
    can learn from. Imagine your reason will appear in a recommendation
    list — make it useful out of context.

  Good HELPFUL reason:
    "Used webhook-signer/scripts/verify.py to validate the HMAC-SHA256
     header on the inbound payload; signature matched and the new
     test in tests/webhooks.py::test_constant_time_compare now passes."
  Good HARMFUL reason:
    "Followed jwt-verifier's default check but it skipped the `aud`
     claim — caller in src/auth/middleware.py accepted a token minted
     for a different service. Reverted to manual jwt.decode with
     audience= argument."
  REJECTED reasons (these will be dropped, not stored):
    "ok"     "test"    "good"    "evidence A"    "wrong audience"
    "fine"   "works"   "fix"     "first try"      "r1"

Reply with a single JSON object between the sentinels (no other commentary
between the sentinels — they're parsed by an automated tracker):

{sentinel_start}
{{
  "evaluations": [
    {{"skill": "<name>", "verdict": "HELPFUL|HARMFUL|NEUTRAL|INCONCLUSIVE",
      "reason": "<one full sentence citing concrete evidence>"}},
    ...
  ]
}}
{sentinel_end}
"""


__all__ = ["build_eval_prompt"]
