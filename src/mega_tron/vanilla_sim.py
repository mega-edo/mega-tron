"""Vanilla host catalog simulators.

Three pure functions reproduce each host's *documented* catalog-build
policy. Each returns a ``CatalogBlock`` containing both the rendered
catalog text (for token counting) and the set of skills whose
description survived intact (for F1 — name-only entries are excluded
from the predicted set because a bare name rarely lets a model
disambiguate close neighbours like ``password-hash-argon2`` vs
``password-hash-bcrypt``).

Sources for each policy:

- Codex CLI ``codex-rs/core-skills/src/render.rs``:
  ``DEFAULT_SKILL_METADATA_CHAR_BUDGET = 8_000``,
  ``SKILL_METADATA_CONTEXT_WINDOW_PERCENT = 2``;
  budget = ``min(2% * ctx_window, 8_000)`` characters;
  order = SYSTEM-bundled first then alphabetical;
  overflow = truncate descriptions mid-sentence, then omit further
  skills past the cap (their name still appears with no description).
- Claude Code skills doc (code.claude.com/docs/en/skills):
  budget = ``1% * ctx_window`` tokens (default
  ``skillListingBudgetFraction = 0.01``);
  ALL skill names always emitted (no skill dropped entirely);
  descriptions appended in invocation-frequency LRU order — under the
  fresh-user assumption (all counts = 0), ties are broken
  alphabetically; past budget the skill gets a name-only entry.
- Gemini CLI docs (geminicli.com/docs/cli/skills/):
  no cap; every enabled skill's name+description in
  filesystem-iteration order.

These are simulations, not real CLI invocations — but the policies are
reproduced verbatim from the public sources.

Originally lived under ``benchmarks/routing/`` (where the routing
benchmark calls these to produce the README's vanilla-baseline
numbers). Promoted to ``mega_tron.vanilla_sim`` so the dashboard's
Context Savings tab can call the same code paths against the user's
real catalog. ``benchmarks/routing/vanilla_sim.py`` is now a thin
shim that re-exports from this module.
"""
from __future__ import annotations

from dataclasses import dataclass

from mega_tron.tokens import count_tokens


# Default context window we model against (Codex / Claude both ship
# 200K-token model variants by default; benchmark uses this).
DEFAULT_CTX_WINDOW = 200_000

CODEX_CHAR_BUDGET_CAP = 8_000
CODEX_BUDGET_PCT = 0.02
CLAUDE_BUDGET_FRAC = 0.01  # `skillListingBudgetFraction` default
# Per-entry hard cap from code.claude.com/docs/en/skills "Skill descriptions
# are cut short": each description (+ when_to_use) is truncated to 1,536
# chars before the budget step. Configurable via `maxSkillDescriptionChars`.
CLAUDE_MAX_DESC_CHARS = 1_536


def _token_count(text: str) -> int:
    return count_tokens(text)


@dataclass(frozen=True)
class CatalogBlock:
    """Result of one vanilla simulation against a pool."""
    text: str
    tokens: int
    # Names of skills whose *description* survived intact (used for F1).
    # Name-only entries are excluded.
    predicted_skills: frozenset[str]
    # All names emitted in the block (for diagnostics — Claude in
    # particular emits names always, even when description dropped).
    all_emitted_names: frozenset[str]


@dataclass(frozen=True)
class PoolSkill:
    """Minimal skill record the simulators consume."""
    name: str
    description: str


# --- Codex --------------------------------------------------------------

def _codex_minimum_line(name: str, path: str) -> str:
    """Codex's ``render_minimum`` form: ``- name: (file: path)``."""
    return f"- {name}: (file: {path})\n"


def _codex_full_line(name: str, description: str, path: str) -> str:
    """Codex's ``render_with_description`` form with a non-empty description."""
    return f"- {name}: {description} (file: {path})\n"


def _codex_truncated_line(name: str, description: str, n_chars: int, path: str) -> str:
    """Render the skill with the first ``n_chars`` characters of description."""
    if n_chars <= 0:
        return _codex_minimum_line(name, path)
    return _codex_full_line(name, description[:n_chars], path)


def simulate_codex_catalog(
    skills: list[PoolSkill],
    *,
    ctx_window: int = DEFAULT_CTX_WINDOW,
) -> CatalogBlock:
    """Simulate Codex's ``<skills_instructions>`` block.

    Reproduces the algorithm in
    ``codex-rs/core-skills/src/render.rs`` (verified against the public
    source at openai/codex):

      Budget = ``min(ctx_window * 0.02, 8_000)`` characters.

      Phase 1 — Compute the minimum cost: every skill rendered with
      *no* description, i.e. ``- name: (file: path)``. If the sum of
      minimum-cost lines fits the budget, run Phase 2; otherwise run
      Phase 3.

      Phase 2 (``render_lines_with_description_budget``) — Every skill
      keeps its minimum line; the remaining budget is distributed
      *one character at a time* across all descriptions in
      round-robin order until budget is exhausted. Skills whose
      description is short finish early and their slack flows to
      longer descriptions. Skills with description-chars=full are
      "predicted" (description survived intact); skills with
      description-chars in (0, full) are name-with-truncated-desc.

      Phase 3 (``render_minimum_skill_lines_until_budget``) — If even
      the minimum-cost render overflows, append minimum lines one by
      one until budget runs out; remaining skills are omitted entirely.
      This is the only path on which a skill is fully dropped.

    Sort order: ``SkillScope::System (0) → Admin (1) → Repo (2) →
    User (3)``, ties broken alphabetically. Our pool has no scope
    metadata so every skill is User-scoped → pure alphabetical order.

    ``path`` is required by Codex's wire format but we don't have a
    real on-disk path; we substitute ``skills/<name>/SKILL.md`` which
    is the canonical install layout. The path's *length* affects the
    budget; its content does not affect ``all_emitted_names``.
    """
    char_budget = min(int(ctx_window * CODEX_BUDGET_PCT), CODEX_CHAR_BUDGET_CAP)
    sorted_skills = sorted(skills, key=lambda s: s.name)

    # Synthesise per-skill path so the line format matches Codex's wire
    # format. Length of the path matters for budget calculations.
    paths = {s.name: f"skills/{s.name}/SKILL.md" for s in sorted_skills}

    # Compute the cost of rendering every skill at minimum form.
    minimum_lines = [_codex_minimum_line(s.name, paths[s.name]) for s in sorted_skills]
    minimum_cost = sum(len(l) for l in minimum_lines)

    if minimum_cost <= char_budget:
        # Phase 2: every skill is included; distribute description chars
        # one at a time across skills in round-robin order.
        desc_remaining = [len(s.description) for s in sorted_skills]
        chars_allocated = [0] * len(sorted_skills)
        current_line_len = [len(l) for l in minimum_lines]
        remaining = char_budget - minimum_cost

        while remaining > 0:
            changed = False
            for i, skill in enumerate(sorted_skills):
                if chars_allocated[i] >= desc_remaining[i]:
                    continue
                new_chars = chars_allocated[i] + 1
                new_line_len = len(_codex_truncated_line(
                    skill.name, skill.description, new_chars, paths[skill.name]
                ))
                delta = new_line_len - current_line_len[i]
                if delta <= remaining:
                    chars_allocated[i] = new_chars
                    current_line_len[i] = new_line_len
                    remaining -= delta
                    changed = True
            if not changed:
                break

        lines: list[str] = []
        predicted: set[str] = set()
        all_names: set[str] = set()
        for i, skill in enumerate(sorted_skills):
            all_names.add(skill.name)
            n = chars_allocated[i]
            if n == 0:
                lines.append(_codex_minimum_line(skill.name, paths[skill.name]))
            else:
                lines.append(_codex_truncated_line(
                    skill.name, skill.description, n, paths[skill.name]
                ))
                if n == len(skill.description):
                    predicted.add(skill.name)

    else:
        # Phase 3: minimum cost overflows budget. Pack minimum
        # lines greedily; drop overflow.
        lines = []
        predicted = set()
        all_names = set()
        used = 0
        for i, skill in enumerate(sorted_skills):
            line = minimum_lines[i]
            if used + len(line) <= char_budget:
                lines.append(line)
                all_names.add(skill.name)
                used += len(line)

    text = "<skills_instructions>\n" + "".join(lines) + "</skills_instructions>\n"
    return CatalogBlock(
        text=text,
        tokens=_token_count(text),
        predicted_skills=frozenset(predicted),
        all_emitted_names=frozenset(all_names),
    )


# --- Claude Code --------------------------------------------------------

def simulate_claude_catalog(
    skills: list[PoolSkill],
    *,
    ctx_window: int = DEFAULT_CTX_WINDOW,
) -> CatalogBlock:
    """Simulate Claude Code's flat skill catalog (passive mode).

    Verified against ``code.claude.com/docs/en/skills`` "Skill
    descriptions are cut short" section:

      1. Per-entry char cap: each ``description`` (+ ``when_to_use``)
         is truncated to ``maxSkillDescriptionChars`` (default 1 536)
         before the budget step. The cap is applied at the source.
      2. Budget = ``ctx_window * skillListingBudgetFraction`` *tokens*
         (default fraction 0.01 → 2 000 tokens for a 200K window).
      3. ALL skill names are always emitted (Claude never drops a
         skill entirely under budget pressure).
      4. Under budget pressure, "descriptions for the skills you
         invoke least are dropped first." Under the fresh-user
         assumption (every invocation count = 0), we fall back to
         alphabetical for ties — that's the policy a freshly
         installed Claude Code session would actually run.

    This is what ships when ``MEGA_CLAUDE_NATIVE_MODE=passive`` (the
    default). To see the active-mode downgrade, call
    :func:`simulate_claude_active_downgrade`.
    """
    token_budget = int(ctx_window * CLAUDE_BUDGET_FRAC)
    sorted_skills = sorted(skills, key=lambda s: s.name)

    capped_descs = [
        s.description[:CLAUDE_MAX_DESC_CHARS] if len(s.description) > CLAUDE_MAX_DESC_CHARS
        else s.description
        for s in sorted_skills
    ]

    name_lines: list[str] = [f"- {s.name}\n" for s in sorted_skills]
    name_block = "".join(name_lines)
    name_tokens = _token_count(name_block)

    predicted: set[str] = set()
    used_tokens = name_tokens
    rendered_lines = list(name_lines)

    for idx, skill in enumerate(sorted_skills):
        desc = capped_descs[idx]
        if not desc:
            continue
        candidate_full = f"- {skill.name}: {desc}\n"
        candidate_tokens = _token_count(candidate_full)
        existing_tokens = _token_count(rendered_lines[idx])
        delta = candidate_tokens - existing_tokens
        if used_tokens + delta <= token_budget:
            rendered_lines[idx] = candidate_full
            used_tokens += delta
            predicted.add(skill.name)

    text = "".join(rendered_lines)
    return CatalogBlock(
        text=text,
        tokens=_token_count(text),
        predicted_skills=frozenset(predicted),
        all_emitted_names=frozenset(s.name for s in sorted_skills),
    )


def simulate_claude_active_downgrade(
    skills: list[PoolSkill],
    *,
    ctx_window: int = DEFAULT_CTX_WINDOW,
) -> CatalogBlock:
    """Simulate Claude Code under ``MEGA_CLAUDE_NATIVE_MODE=active``.

    When mega-tron rewrites ``~/.claude/settings.local.json``'s
    ``skillOverrides`` to downgrade non-top-K skills to name-only,
    Claude's catalog drops descriptions for non-top-K entries.

    Subtlety: the *passive* catalog is itself constrained by the same
    1% × ctx_window token budget — so at large catalog sizes Claude
    already drops every description even WITHOUT active mode (budget
    runs out filling the always-emitted name lines). Active mode
    therefore only saves tokens when the passive catalog would have
    been ABLE to fit descriptions in the first place — i.e. catalog
    size ≲ 300 skills on a 200K-token window.

    To reflect this accurately, this simulator runs the passive
    pipeline and then takes the SMALLER of (passive output, name-only
    listing). For large catalogs the passive output is already the
    name-only listing, so the two coincide.
    """
    sorted_skills = sorted(skills, key=lambda s: s.name)
    name_only_text = "".join(f"- {s.name}\n" for s in sorted_skills)
    name_only_tokens = _token_count(name_only_text)

    passive = simulate_claude_catalog(skills, ctx_window=ctx_window)
    if name_only_tokens <= passive.tokens:
        return CatalogBlock(
            text=name_only_text,
            tokens=name_only_tokens,
            predicted_skills=frozenset(),
            all_emitted_names=frozenset(s.name for s in sorted_skills),
        )
    # Pathological case (very few short skills): passive already smaller
    # than name-only. Surface passive — active can never make it worse.
    return passive


def simulate_claude_strict_suppression(
    skills: list[PoolSkill],
    *,
    ctx_window: int = DEFAULT_CTX_WINDOW,  # unused — empty catalog has no budget
) -> CatalogBlock:
    """Simulate Claude Code under ``MEGA_CLAUDE_NATIVE_MODE=strict``.

    Strict mode adds ``--disallowedTools Skill`` to every ``claude``
    invocation via a shell wrapper installed by ``mega-tron setup``.
    With the ``Skill`` tool disallowed, Claude doesn't render the
    native catalog at all — the entire block (names + descriptions +
    tool schema) disappears from the system prompt.

    We model it as zero tokens. In reality there's a tiny system-
    prompt overhead for tool schemas that survive (~50-100 tok), but
    that's the prompt scaffolding and not "catalog cost" — counting it
    here would mislead the user about how much the Skill tool was
    costing them.

    ``predicted_skills`` and ``all_emitted_names`` are both empty —
    the model has no way to know what skills exist via native channels.
    """
    return CatalogBlock(
        text="",
        tokens=0,
        predicted_skills=frozenset(),
        all_emitted_names=frozenset(),
    )


# --- Gemini -------------------------------------------------------------

def simulate_gemini_catalog(
    skills: list[PoolSkill],
    *,
    ctx_window: int = DEFAULT_CTX_WINDOW,  # unused — Gemini has no cap
) -> CatalogBlock:
    """Simulate Gemini's catalog injection.

    Reproduces the algorithm in
    ``packages/core/src/prompts/snippets.ts`` (function
    ``renderAgentSkills``) of ``google-gemini/gemini-cli``:

    No budget, no cap. Every enabled skill is emitted as an XML
    ``<skill>`` element with three children: ``<name>``,
    ``<description>``, ``<location>``. The XML is wrapped in a
    ``<available_skills>`` block under an "# Available Agent Skills"
    markdown header.

    Order is filesystem-iteration; ``predicted_skills`` and
    ``all_emitted_names`` are both the entire pool because every
    description always survives intact.

    ``location`` is required by Gemini's wire format but we don't
    have a real on-disk path; we substitute
    ``<cwd>/.gemini/skills/<name>/SKILL.md`` which is the canonical
    workspace install layout.
    """
    skill_xmls = []
    for s in skills:
        location = f".gemini/skills/{s.name}/SKILL.md"
        skill_xmls.append(
            f"  <skill>\n"
            f"    <name>{s.name}</name>\n"
            f"    <description>{s.description}</description>\n"
            f"    <location>{location}</location>\n"
            f"  </skill>"
        )
    skills_xml = "\n".join(skill_xmls)
    header = (
        "# Available Agent Skills\n\n"
        "You have access to the following specialized skills. To activate a "
        "skill and receive its detailed instructions, call the activate_skill "
        "tool with the skill's name.\n\n"
    )
    text = f"{header}<available_skills>\n{skills_xml}\n</available_skills>"
    return CatalogBlock(
        text=text,
        tokens=_token_count(text),
        predicted_skills=frozenset(s.name for s in skills),
        all_emitted_names=frozenset(s.name for s in skills),
    )


__all__ = [
    "CatalogBlock",
    "PoolSkill",
    "simulate_codex_catalog",
    "simulate_claude_catalog",
    "simulate_claude_active_downgrade",
    "simulate_claude_strict_suppression",
    "simulate_gemini_catalog",
    "DEFAULT_CTX_WINDOW",
    "CODEX_CHAR_BUDGET_CAP",
    "CODEX_BUDGET_PCT",
    "CLAUDE_BUDGET_FRAC",
    "CLAUDE_MAX_DESC_CHARS",
]
