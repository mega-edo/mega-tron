"""HostAdapter ABC — the contract every host implementation satisfies.

Host adapters live under :mod:`mega_tron.hosts.<name>`. Each one wires
:class:`mega_tron.core.MegaCore` into a specific agent runtime
(Codex CLI, Claude Code, Hermes Agent, …) by translating between the
host's transcript / hook formats and the core's :class:`Verdict` /
:class:`RankedSkill` objects.

The ABC is intentionally minimal in Phase 1 — its concrete methods are
filled in by each host as the integration matures across phases. Today
its main role is documenting the *shape* of a host integration so
contributors can add new hosts without reading every existing adapter
end-to-end.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterable

from mega_tron.core import RankedSkill, Verdict


class HostAdapter(ABC):
    """Abstract host integration contract.

    A concrete adapter (e.g. ``CodexAdapter``, ``ClaudeCodeAdapter``,
    ``HermesAdapter``) implements:

    - :attr:`name`: the host slug used in ``Verdict.host`` and
      ``mega-tron install --target <name>``.
    - :meth:`install` / :meth:`uninstall`: idempotent configuration of
      the host so prompts and stop events reach :class:`MegaCore`.
    - :meth:`format_route`: render a top-K :class:`RankedSkill` list
      into the host's expected prompt-injection shape.
    - :meth:`parse_verdicts`: extract :class:`Verdict` objects from the
      host's stop-hook payload (transcript JSON, sentinel-wrapped JSON,
      etc.).

    Subclasses are free to add host-specific public methods on top of
    these — e.g. the Claude Code adapter exposes a Mode-A
    ``apply_skill_overrides`` helper that other hosts don't have. The
    ABC is the *minimum* shape, not the maximum.
    """

    #: Slug used in ``Verdict.host`` and ``--target`` CLI options.
    name: str

    @abstractmethod
    def install(self, **kwargs) -> int:
        """Wire mega-tron into this host's configuration. Idempotent."""

    @abstractmethod
    def uninstall(self, **kwargs) -> int:
        """Undo :meth:`install` cleanly. Idempotent."""

    @abstractmethod
    def format_route(self, ranked: list[RankedSkill], *, k: int = 3) -> str:
        """Render the routed top-K into the host's prompt-injection shape."""

    @abstractmethod
    def parse_verdicts(self, payload: dict) -> Iterable[Verdict]:
        """Extract :class:`Verdict` objects from the host's stop-hook payload."""
