"""Claude Code SKILL.md frontmatter compatibility (Stage 0 scaffold).

Claude Code's SKILL.md frontmatter ([skills doc](https://code.claude.com/docs/en/skills))
overlaps with Codex/Anthropic's Agent Skills standard but adds a few
fields the router should be aware of:

  - ``disable-model-invocation: true`` — skill is user-invocable only.
    The router should *not* surface these in additionalContext, since
    Claude won't auto-load them anyway.
  - ``user-invocable: false`` — only Claude invokes; harmless to route.
  - ``allowed-tools`` — pre-approved tool grants. Router ignores.
  - ``paths`` — glob filter for auto-activation. Router could honor
    this to skip skills not matching the current cwd's file types,
    but Stage 1 keeps it simple and ignores.
  - ``context: fork`` — runs in a subagent. Router treats as normal.

Stage 0 status: stub helpers. Returns False / no-op until Stage 1+ wires
filters into ``Router.rank``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Mapping


def is_user_only_skill(frontmatter: Mapping[str, object]) -> bool:
    """Skills with ``disable-model-invocation: true`` should not be routed.

    Stage 0: always returns False (route everything). Stage 1+ should
    actually inspect the frontmatter.
    """
    return False


def skill_path_matches_cwd(
    frontmatter: Mapping[str, object], cwd: Path
) -> bool:
    """If frontmatter has ``paths:`` globs, honor them.

    Stage 0: always returns True.
    """
    return True


__all__ = ["is_user_only_skill", "skill_path_matches_cwd"]
