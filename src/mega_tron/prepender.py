"""Build the candidate-skills prompt prefix(es) for codex.

The router presents top-K picks as candidates; the model decides whether
to use them based on the task. We deliberately avoid the ``$SkillName``
token because that triggers codex's built-in "MUST use" rule
(``codex-rs/core-skills/src/render.rs``), which would force invocation
even when the router's pick doesn't fit the user's actual intent.

Two emission shapes:

- :func:`build_prefix` — bare ``Candidate skills: a, b, c. ...`` line.
  Used by the shell wrapper's stage path, where the top-K SKILL.md
  files are symlinked into ``$CODEX_HOME/skills/`` *before* codex
  starts, so codex has already inlined each description into its own
  system prompt — the model sees those descriptions and picks the
  applicable ones.

- :func:`build_hook_context` — candidate line *plus* a meta block
  listing ``name / skill_dir / description`` for each pick. Used by the
  codex ``UserPromptSubmit`` hook, where skills may live in roots codex
  itself doesn't know about (e.g. ``~/.claude/skills``). The meta block
  gives the model the description and path so it can open the SKILL.md
  directly when it decides a skill applies.
"""
from __future__ import annotations

from mega_tron.router import RankedSkill
from mega_tron.self_eval_contract import render_inline_self_eval_contract


def _session_block(session_id: str | None) -> str:
    """Emit the per-turn session-id stamp instruction, or empty string.

    When the host hook knows its session id, we stamp it into the
    prepended block and instruct the model to pass it via
    ``--session-id`` on any ``mega-tron search`` shell call it makes
    for this turn. The shell example uses the absolute mega-tron
    binary path (same convention as the AGENTS.md / CLAUDE.md /
    GEMINI.md install blocks) so the call works under minimal-PATH
    subshells. Without this stamp the model's shell calls write
    routes rows with ``session_id=NULL``, and the stop hook's verdict
    gate later drops the model's `<skill-used>` tags as "not in this
    session's routed catalog". The instruction is omitted when
    ``session_id`` is None (CLI direct, MegaCore, etc.) so headless
    use stays clean.
    """
    if not session_id:
        return ""
    from mega_tron.cli.path_setup import resolve_bin_path

    mega_tron_bin = resolve_bin_path()
    return (
        "### Session\n"
        "\n"
        f"This turn's session id is `{session_id}`. When you call "
        f"`{mega_tron_bin} search \"<query>\"` from a shell for THIS "
        f"session, pass `--session-id {session_id}` so the routing "
        "log credits the call against this conversation. Example:\n"
        "\n"
        f"    {mega_tron_bin} search \"jwt bearer middleware\" --session-id {session_id}\n"
        "\n"
        "Do NOT echo the session id in your reply text — it is a "
        "routing identifier, not output. Skipping the flag still "
        "lets the call succeed, but mega-tron's verdict gate cannot "
        f"credit any `<skill-used>` tag you emit against this session's "
        "catalog, and your routing signal for this turn is lost.\n"
    )


def append_session_block(ctx: str, session_id: str | None) -> str:
    """Append the per-turn session-id stamp to an already-built ctx.

    Used by host hooks on the daemon fast-path, where the
    ``additional_context`` comes back from the daemon process
    (which doesn't know the host's session id). When ``ctx`` is
    empty or ``session_id`` is None this is a no-op — the caller
    keeps its original empty-ctx semantics.
    """
    if not session_id or not ctx:
        return ctx
    sess = _session_block(session_id)
    if not sess:
        return ctx
    sep = "" if ctx.endswith("\n") else "\n"
    return ctx + sep + "\n" + sess


def _no_match_context(
    session_id: str | None = None, *, follow_up: bool = False
) -> str:
    """Hook injection for turns where the router returned zero matches.

    Without this, the three ``build_*_hook_context`` functions return
    "" on an empty ranking — i.e. the model gets no mega-tron guidance
    at all for that turn. The model's long context still contains the
    Skills blocks from earlier turns of the same conversation, so
    "no inject" is silently read as "use whatever names you remember
    from previous turns" — which is the dominant origin of the
    hallucinated ``<skill-used name="..."/>`` tag pattern observed in
    real transcripts.

    An explicit one-block injection — *"emit zero tags"* — fixes that
    by replacing the silence with a positive instruction the model
    can't pattern-match against a stale prior block.

    When the host supplies a session id we still tack the session-id
    instruction on the end: even a no-match turn may produce a shell
    `mega-tron search` call from the model (it's instructed by
    AGENTS.md/CLAUDE.md/GEMINI.md to do so for ambiguous prompts),
    and we want that call attributed to the right session.

    On follow-up turns (``follow_up=True``) the "emit zero tags"
    paragraph is omitted — the model has already seen the persistent
    self-eval contract from earlier turns / from AGENTS.md, and
    spamming it the no-match notice on every chatty turn is just
    context bloat. The session block (if any) is returned alone so
    a shell ``mega-tron search`` call still gets attributed.
    """
    sess = _session_block(session_id)
    if follow_up:
        return sess  # may be "" when session_id is None — true noop
    base = (
        "## Skills (selected for this turn by mega-tron)\n"
        "\n"
        "(none — no surfaced skill applied; emit zero "
        "`<skill-used>` tags for this turn. Silence is the correct "
        "signal that the top-K missed; do not substitute names from "
        "earlier turns of this conversation.)\n"
    )
    return base + ("\n" + sess if sess else "")


def build_prefix(
    ranked: list[RankedSkill],
    k: int = 3,
    template: str = (
        "Candidate skills for this task: {names}. "
        "Use whichever fit; ignore the rest.\n\n"
    ),
) -> str:
    """Generate a non-forcing candidate-skills prefix.

    The names are emitted as bare identifiers (no ``$`` prefix), so codex's
    built-in must-use trigger does not fire. The model decides which
    candidates apply based on each skill's description (which codex inlines
    natively for skills staged under ``$CODEX_HOME/skills/``).

    Args:
        ranked: descending-score skill list (from Router.rank).
        k: cap the prefix at the top-K skill names. Default 3 — beyond this,
            the candidate list gets too long for the model to weigh
            efficiently.
        template: format string with `{names}` placeholder. Override if you
            prefer different wording.

    Returns the prefix string (with trailing ``\n\n`` by default) or an
    empty string when there are no ranked skills.
    """
    if not ranked:
        return ""
    top = ranked[:k]
    names = ", ".join(rs.skill.name for rs in top)
    return template.format(names=names)


def build_hook_context(
    ranked: list[RankedSkill],
    k: int = 3,
    *,
    session_id: str | None = None,
    follow_up: bool = False,
) -> str:
    """Build the codex ``UserPromptSubmit`` hook's ``additionalContext``.

    Replaces codex's built-in skill catalog (disabled via
    ``skills.include_instructions = false`` at install time) with a
    semantically ranked top-K candidate list. Differences vs. codex's
    native catalog block:

    - Names are emitted bare (no ``$`` prefix), so codex's built-in
      must-use trigger rule does **not** fire. The model decides whether
      each candidate applies based on its description and the user's
      actual intent — the router proposes, the model disposes.
    - Each pick carries an absolute ``path`` to its SKILL.md so the
      model can open it directly, including skills under
      ``~/.claude/skills`` or other roots codex does not natively
      enumerate.
    - The Stop-hook self-evaluation loop is previewed so the model
      gathers evidence as it works, not at termination only.

    Use :func:`build_prefix` instead when staging into ``$CODEX_HOME``
    via the shell wrapper (codex inlines each staged skill's
    description natively, so the bare candidate line is enough).

    ``follow_up=True`` emits a *slim* block (catalog header + entries
    + session block only) for follow-up turns of a multi-turn
    conversation. The "How to use" prose and the inline self-eval
    contract are intentionally skipped — they live persistently in
    AGENTS.md / CLAUDE.md / GEMINI.md, so re-injecting them every
    turn is pure context bloat. Saves ~1700 tok/turn vs the full
    block while keeping the catalog the model needs to choose which
    skill applies and emit `<skill-used>` tags.
    """
    if not ranked:
        return _no_match_context(session_id=session_id, follow_up=follow_up)
    top = ranked[:k]
    names = ", ".join(rs.skill.name for rs in top)
    lines: list[str] = [
        "## Skills (selected for this turn by mega-tron)",
        "",
        f"Candidate skills for this task: {names}. "
        f"Apply the ones that fit the user's intent; ignore the rest.",
        "",
    ]
    if not follow_up:
        lines.append(
            "These skills were semantically ranked as the most relevant for "
            "your prompt. Each entry lists the skill name, the absolute path "
            "to its SKILL.md, and a one-line description — open the SKILL.md "
            "at that path for full instructions when you decide to apply a "
            "skill."
        )
        lines.append("")
    lines.append("### Available skills")
    for rs in top:
        desc = (rs.skill.description or "").strip().replace("\n", " ")
        lines.append(f"- {rs.skill.name}")
        skill_md = rs.skill.skill_dir / "SKILL.md"
        lines.append(f"    path: {skill_md}")
        if desc:
            lines.append(f"    desc: {desc}")
    if not follow_up:
        lines.append("")
        lines.append("### How to use these skills")
        lines.append(
            "- Treat the list above as candidates, not commands. If a skill's "
            "description clearly matches the user's task, apply it. If none "
            "fit, proceed without them and say so briefly."
        )
        lines.append(
            "- Open the SKILL.md at the listed `path` to load full instructions. "
            "When SKILL.md references relative paths (e.g. `scripts/foo.py`), "
            "resolve them relative to that SKILL.md's directory first."
        )
        lines.append(
            "- If `scripts/` exist, prefer running or patching them instead of "
            "retyping large code blocks. If `assets/` or templates exist, reuse "
            "them instead of recreating from scratch."
        )
        lines.append(
            "- If a skill can't be applied cleanly (missing files, unclear "
            "instructions), state the issue, pick the next-best approach, and "
            "continue."
        )
        lines.append("")
        lines.append(render_inline_self_eval_contract())
    sess = _session_block(session_id)
    if sess:
        lines.append("")
        lines.append(sess.rstrip("\n"))
    return "\n".join(lines) + "\n"


_GEMINI_INLINE_BUDGET_CHARS = 12_000
_GEMINI_PER_SKILL_BODY_CAP = 6_000


def _read_skill_body(skill_md_path) -> str:
    """Best-effort read of a SKILL.md body. Returns empty string on any error.

    Gemini's workspace-trust sandbox refuses ``read_file`` calls outside
    the active project directory, so the model can't reliably open the
    SKILL.md at runtime even when given its absolute path. The hook
    therefore inlines the body here, in trusted code that reads from
    disk before the JSON envelope is emitted.
    """
    try:
        return skill_md_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def build_gemini_hook_context(
    ranked: list[RankedSkill],
    k: int = 3,
    *,
    session_id: str | None = None,
    follow_up: bool = False,
) -> str:
    """Build the Gemini CLI ``BeforeAgent`` hook's ``additionalContext``.

    Differences from :func:`build_claude_hook_context` (the Claude variant):

    - Gemini CLI surfaces Agent Skills via an ``activate_skill`` tool
      call, not a slash command. The must-use line therefore names the
      tool ("activate the `activate_skill` tool with name=...") instead
      of Claude's ``/skill-name`` form.
    - The persistent self-eval contract lives in ``~/.gemini/GEMINI.md``
      (planted by ``install --target gemini``), so this overlay just
      summarizes the tagging convention without restating the full rule.
    - The grading hook is ``AfterAgent`` (not Claude Code's ``Stop``);
      the prose reflects that.
    - **Bodies are inlined.** Gemini's workspace-trust sandbox refuses
      ``read_file`` calls outside the project directory, so the model
      cannot open the SKILL.md path we hand it. We therefore embed the
      full SKILL.md body (capped per skill, total-capped) directly so
      the model has everything it needs without a follow-up tool call.

    ``follow_up=True`` produces a slim variant for multi-turn
    conversations: catalog header + names+desc only (NO inlined
    bodies — the bodies are large and the model has already seen
    them on first fire) + session block. Saves ~12k chars/turn vs
    the full Gemini block.
    """
    if not ranked:
        return _no_match_context(session_id=session_id, follow_up=follow_up)
    top = ranked[:k]
    names = ", ".join(f"`{rs.skill.name}`" for rs in top)
    lines: list[str] = [
        "## Skills (selected for this turn by mega-tron)",
        "",
        (
            "Strongly prefer activating one of these skills via the "
            f"`activate_skill` tool: {names}."
        ),
        "",
    ]
    if not follow_up:
        lines.append(
            "These skills were semantically ranked as the most relevant for "
            "your prompt. Each entry lists the skill name, a one-line "
            "description, and the full SKILL.md body (inlined because "
            "Gemini's workspace-trust sandbox can't read files outside "
            "the active project)."
        )
        lines.append("")
    lines.append("### Available skills")
    remaining = _GEMINI_INLINE_BUDGET_CHARS
    for rs in top:
        desc = (rs.skill.description or "").strip().replace("\n", " ")
        lines.append(f"- {rs.skill.name}")
        skill_md = rs.skill.skill_dir / "SKILL.md"
        lines.append(f"    path: {skill_md}")
        if desc:
            lines.append(f"    desc: {desc}")
        if follow_up:
            # Slim variant: skip the inlined SKILL.md body entirely.
            # The model has already seen it on first fire; re-injecting
            # it every turn dwarfs the rest of the block.
            continue
        body = _read_skill_body(skill_md)
        if body:
            per_skill_cap = min(_GEMINI_PER_SKILL_BODY_CAP, max(0, remaining))
            if per_skill_cap > 0:
                excerpt = body[:per_skill_cap]
                truncated = len(body) > per_skill_cap
                lines.append("    body: |")
                for ln in excerpt.splitlines():
                    lines.append(f"      {ln}")
                if truncated:
                    lines.append(
                        f"      ... [truncated; full body at {skill_md}]"
                    )
                remaining -= len(excerpt)
    if not follow_up:
        lines.append("")
        lines.append("### How to use these skills")
        lines.append(
            "- If the task clearly matches one of the skills above, call "
            "`activate_skill` with the corresponding `name`. Multiple matches "
            "mean activate them all."
        )
        lines.append(
            "- The full SKILL.md body is inlined under each `body:` block "
            "above — use it directly. Do not call `read_file` on the `path:` "
            "line; Gemini's workspace-trust sandbox will refuse it."
        )
        lines.append(
            "- If `scripts/` or `assets/` ship with the skill, prefer using "
            "them over retyping equivalent code from scratch."
        )
        lines.append(
            "- If a skill can't be applied cleanly (missing files, unclear "
            "instructions), state the issue, pick the next-best approach, and "
            "continue."
        )
        lines.append("")
        lines.append(render_inline_self_eval_contract())
    sess = _session_block(session_id)
    if sess:
        lines.append("")
        lines.append(sess.rstrip("\n"))
    return "\n".join(lines) + "\n"


def build_claude_hook_context(
    ranked: list[RankedSkill],
    k: int = 3,
    *,
    session_id: str | None = None,
    follow_up: bool = False,
) -> str:
    """Build the Claude Code ``UserPromptSubmit`` hook's ``additionalContext``.

    Differences from :func:`build_hook_context` (the Codex variant):

    - Claude Code does **not** have Codex's ``$SkillName`` must-use
      trigger rule baked into its system prompt. Skills are invoked via
      ``/skill-name`` (the Claude Code slash-command form). We use the
      slash form in the must-use line and a strongly-worded natural
      language directive ("strongly prefer using one of these") in
      place of Codex's hard rule.
    - The persistent self-eval contract lives in ``~/.claude/CLAUDE.md``
      (planted by ``install --target claude``), so we preview it here
      briefly rather than reproducing the full rule.
    - Each pick still lists ``name / path / desc`` so Claude can open
      the SKILL.md directly even if it lives outside the natively-loaded
      ``~/.claude/skills`` root (e.g. user-registered ``extra_dirs``).

    ``follow_up=True`` emits the slim variant for multi-turn
    conversations — catalog header + name/path/desc + session block
    only. The self-eval contract is omitted (it lives persistently in
    ~/.claude/CLAUDE.md anyway).
    """
    if not ranked:
        return _no_match_context(session_id=session_id, follow_up=follow_up)
    top = ranked[:k]
    slash_names = ", ".join(f"/{rs.skill.name}" for rs in top)
    lines: list[str] = [
        "## Skills (selected for this turn by mega-tron)",
        "",
        f"Strongly prefer using one of these skills: {slash_names}.",
        "",
    ]
    if not follow_up:
        lines.append(
            "These skills were semantically ranked as the most relevant for "
            "your prompt. Each entry lists the skill name, the absolute path "
            "to its SKILL.md, and a one-line description."
        )
        lines.append("")
    lines.append("### Available skills")
    for rs in top:
        desc = (rs.skill.description or "").strip().replace("\n", " ")
        lines.append(f"- /{rs.skill.name}")
        skill_md = rs.skill.skill_dir / "SKILL.md"
        lines.append(f"    path: {skill_md}")
        if desc:
            lines.append(f"    desc: {desc}")
    if not follow_up:
        lines.append("")
        lines.append("### How to use these skills")
        lines.append(
            "- If the task clearly matches one of the skills above, invoke it "
            "with `/skill-name` (or let it auto-load — both work). Multiple "
            "matches mean use them all."
        )
        lines.append(
            "- The skill's full SKILL.md content loads only when the skill is "
            "actually invoked; the listing above only carries the description. "
            "If you need to peek before invoking, open the file at the listed "
            "`path`."
        )
        lines.append(
            "- If `scripts/` or `assets/` ship with the skill, prefer using "
            "them over retyping equivalent code from scratch."
        )
        lines.append(
            "- If a skill can't be applied cleanly (missing files, unclear "
            "instructions), state the issue, pick the next-best approach, and "
            "continue."
        )
        lines.append("")
        lines.append(render_inline_self_eval_contract())
    sess = _session_block(session_id)
    if sess:
        lines.append("")
        lines.append(sess.rstrip("\n"))
    return "\n".join(lines) + "\n"
