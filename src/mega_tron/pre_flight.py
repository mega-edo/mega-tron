"""SKILL.md validation — router-level admission control.

mega-tron's router only needs two things from a SKILL.md to do its
job: a YAML frontmatter that parses as a mapping, and a non-empty
description string to embed. Anything beyond that is a host-stage
concern, not an admission gate.

Earlier revisions rejected every SKILL.md whose description exceeded
Codex's 1024-character cap, because the original sibling
(``stage_codex_home.py``) was a Codex-only stager. That cap exists
because Codex inlines *every* description into the system prompt
unconditionally — without a router, truncation is the only escape
hatch. mega-tron has a router: it ships at most ~10 descriptions per
turn, so the cap is irrelevant to admission. Host-specific shaping
(Codex's 1024-char trim, Claude's slash form, Gemini's body inlining)
happens at stage time in :mod:`mega_tron.stager` and the host
prependers, not here.

Net behaviour:
- ``parse_frontmatter`` rejects malformed YAML / non-mapping frontmatter.
- ``validate`` rejects SKILL.md files missing a usable description string.
- Description length is *not* an admission check — long descriptions
  flow through to the embedder (BGE-M3 handles >8K context fine) and
  are truncated per-host at stage time.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

# Codex's own SKILL.md loader hard-trims descriptions to this character
# count at stage time. Surfaced as a constant so the Codex stager can
# import it without bringing back the admission gate that used it.
CODEX_DESC_MAX = 1024


@dataclass(frozen=True)
class ValidationError:
    """Describes why a SKILL.md cannot be admitted to the router."""

    skill_md: Path
    reason: str

    def __str__(self) -> str:  # pragma: no cover — trivial
        return f"{self.skill_md}: {self.reason}"


def parse_frontmatter(skill_md: Path) -> tuple[dict, str | None]:
    """Parse YAML frontmatter from a SKILL.md.

    Returns:
        (frontmatter_dict, error). error is None on success.
    """
    text = skill_md.read_text(encoding="utf-8")
    m = re.match(r"---\n(.*?)\n---", text, re.DOTALL)
    if not m:
        return {}, "no YAML frontmatter"
    try:
        loaded = yaml.safe_load(m.group(1)) or {}
        if not isinstance(loaded, dict):
            return {}, f"frontmatter must be a mapping, got {type(loaded).__name__}"
        return loaded, None
    except yaml.YAMLError as e:
        return {}, f"invalid YAML: {e}"


def description_error(fm: dict) -> str | None:
    """Return a human-readable reason if the frontmatter lacks a usable
    description. We accept any non-empty string regardless of length —
    host-specific limits are applied at stage time, not at admission.
    """
    raw_desc = fm.get("description")
    if raw_desc is None:
        return "missing description"
    # Lists are tolerated (joined at normalize_desc time).
    if not isinstance(raw_desc, (str, list)):
        return f"description must be string or list, got {type(raw_desc).__name__}"
    if isinstance(raw_desc, str) and not raw_desc.strip():
        return "description is empty"
    if isinstance(raw_desc, list) and not any(
        isinstance(x, str) and x.strip() for x in raw_desc
    ):
        return "description list has no usable string entries"
    return None


# Back-compat shim: external callers (mega-symphony, older hosts) used to
# import ``codex_compat_error``. Keep the symbol pointing at the new
# admission check so they keep working without code changes.
codex_compat_error = description_error


def normalize_desc(desc: object) -> str:
    """Coerce a description value into a clean single-line string.

    Tolerant of list values (joined with spaces) and collapses whitespace.
    """
    if desc is None:
        return ""
    if isinstance(desc, list):
        desc = " ".join(str(x) for x in desc)
    return re.sub(r"\s+", " ", str(desc)).strip()


def validate(skill_md: Path) -> ValidationError | None:
    """One-shot router admission. Returns ValidationError on failure,
    None on pass. The router admits any SKILL.md with valid frontmatter
    and a non-empty description string — length and host-specific
    shaping are applied later, at stage time."""
    fm, parse_err = parse_frontmatter(skill_md)
    if parse_err:
        return ValidationError(skill_md=skill_md, reason=parse_err)
    desc_err = description_error(fm)
    if desc_err:
        return ValidationError(skill_md=skill_md, reason=desc_err)
    return None
