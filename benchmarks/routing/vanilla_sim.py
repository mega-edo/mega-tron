"""Vanilla host catalog simulators (DESIGN.md §4.2).

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
reproduced verbatim from the public sources cited in DESIGN.md §1.1.
"""
from __future__ import annotations

from dataclasses import dataclass

import tiktoken


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

_ENCODER = None


def _enc() -> "tiktoken.Encoding":
    global _ENCODER
    if _ENCODER is None:
        _ENCODER = tiktoken.get_encoding("o200k_base")
    return _ENCODER


def _token_count(text: str) -> int:
    return len(_enc().encode(text))


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
        # ---- Phase 2: every skill is included; distribute description chars
        # one at a time across skills in round-robin order. ----
        # Per-skill remaining description length, indexed by sorted position.
        desc_remaining = [len(s.description) for s in sorted_skills]
        # Characters allocated to each skill's description so far.
        chars_allocated = [0] * len(sorted_skills)
        # Track current line lengths so we don't have to re-render every loop.
        current_line_len = [len(l) for l in minimum_lines]
        # Remaining budget after Phase 1's minimum-cost reservation.
        remaining = char_budget - minimum_cost

        # Cost model: adding the *first* description char swaps the line
        # from "- name: (file: path)" to "- name: c (file: path)" — that
        # is, the literal ": " between name and (file:) becomes ": c " ,
        # so the delta from 0→1 char is +1 char (the new char) +1 char
        # (the space inserted before "(file:"). After the first char each
        # additional char is +1 char. See _codex_truncated_line vs
        # _codex_minimum_line.
        # We implement this exactly by re-rendering on each grant.

        # Round-robin: in each pass, try to grant +1 char to every skill
        # that still has description left. Stop when no skill could be
        # granted a char in a full pass.
        while remaining > 0:
            changed = False
            for i, skill in enumerate(sorted_skills):
                if chars_allocated[i] >= desc_remaining[i]:
                    continue
                # Cost of next character
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
                else:
                    # Can't fit any more for this skill; try others
                    pass
            if not changed:
                break

        lines: list[str] = []
        predicted: set[str] = set()
        all_names: set[str] = set()
        for i, skill in enumerate(sorted_skills):
            all_names.add(skill.name)  # every skill emits at least its name
            n = chars_allocated[i]
            if n == 0:
                lines.append(_codex_minimum_line(skill.name, paths[skill.name]))
            else:
                lines.append(_codex_truncated_line(
                    skill.name, skill.description, n, paths[skill.name]
                ))
                if n == len(skill.description):
                    # description survived intact
                    predicted.add(skill.name)

    else:
        # ---- Phase 3: minimum cost overflows budget. Pack minimum
        # lines greedily; drop overflow. This is the only path where
        # a skill is completely omitted. ----
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
            # else: skill is dropped entirely

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
    """Simulate Claude Code's flat skill catalog.

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
    """
    token_budget = int(ctx_window * CLAUDE_BUDGET_FRAC)
    sorted_skills = sorted(skills, key=lambda s: s.name)

    # Pre-step: apply per-entry description cap (maxSkillDescriptionChars).
    capped_descs = [
        s.description[:CLAUDE_MAX_DESC_CHARS] if len(s.description) > CLAUDE_MAX_DESC_CHARS
        else s.description
        for s in sorted_skills
    ]

    # Phase 1: emit every name with no description. This bytes-anyway
    # cost is what Claude pays for "names are always included."
    name_lines: list[str] = [f"- {s.name}\n" for s in sorted_skills]
    name_block = "".join(name_lines)
    name_tokens = _token_count(name_block)

    # Phase 2: greedily append descriptions to those name entries
    # in alphabetical order until the token budget runs out. The
    # description used here is the pre-capped version.
    predicted: set[str] = set()
    used_tokens = name_tokens
    rendered_lines = list(name_lines)  # mutable copy we upgrade in place

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
            # "predicted" means the description was attached. Since the
            # cap was applied before this step, a capped description
            # still counts — the model still sees the keyword surface.
            predicted.add(skill.name)
        # else: skill stays name-only, no break — later (smaller)
        # descriptions might still fit. This matches "skills you invoke
        # least are dropped first" — under fresh-user, the description
        # that didn't fit just gets dropped while we keep trying others.

    text = "".join(rendered_lines)
    return CatalogBlock(
        text=text,
        tokens=_token_count(text),
        predicted_skills=frozenset(predicted),
        all_emitted_names=frozenset(s.name for s in sorted_skills),
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
    "simulate_gemini_catalog",
    "DEFAULT_CTX_WINDOW",
    "CODEX_CHAR_BUDGET_CAP",
    "CODEX_BUDGET_PCT",
    "CLAUDE_BUDGET_FRAC",
]
