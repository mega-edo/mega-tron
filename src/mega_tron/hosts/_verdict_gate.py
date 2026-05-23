"""Shared verdict-admission gate for host Stop hooks.

The old gate (`scripts/<name>/` substring echo in the transcript) silently
dropped verdict tags for the vast majority of skills, because most skills
have no ``scripts/`` directory at all — the assistant just reads SKILL.md
and follows it. The result was a verdict store that filled with 0 rows on
some hosts (gemini_cli at 0 / 15 routes) and almost-0 rows on others.

The new gate is `routes-table membership`: a verdict tag is admitted when
the named skill appears in *some* ``routes`` row for this turn's session.
That row was written by the host's hook the moment the router surfaced
the catalog — so it is a precise record of what the model actually saw
this session, with no reliance on whether the model happened to echo a
filesystem path in its reply.

Behaviour:

* When a session_id is known and we can read the routes table → admit
  any tag whose name is in the surfaced catalog, regardless of the
  tracker's ``label`` (``claimed_use`` / ``informed_use`` / ``silent_use``).
* When session_id is missing OR routes lookup fails OR returns empty
  (e.g. fresh install with an empty store) → fall back to the legacy
  tracker label: admit only tags that already had an operational trace.
  This preserves the old behaviour for environments where the routes
  table never got populated.
* The downstream ``verdicts.writer.persist_verdicts`` filter still drops
  any name whose ``SKILL.md`` doesn't exist on disk, so a permissive
  admit here can't write garbage to the store.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class GateOutcome:
    """Result of running the gate over a single Stop hook's invocations.

    * ``admitted`` — `[{"skill","verdict","reason"}, ...]` ready for
      ``persist_verdicts``.
    * ``skipped_no_verdict`` — names that had no ``verdict=`` attribute
      (the upstream rule, unchanged).
    * ``skipped_not_in_catalog`` — names that were tagged but *not*
      surfaced by the router this session. Likely hallucinations; the
      Stop hook logs these and the writer's on-disk catalog filter
      would drop them again, but rejecting here saves a round-trip.
    * ``via`` — ``"routes"`` when routes-table lookup gave us a non-
      empty set, ``"legacy"`` when we fell back to the tracker's label.
      Stop hooks log this so we can tell the two paths apart.
    """

    admitted: list[dict]
    skipped_no_verdict: list[str]
    skipped_not_in_catalog: list[str]
    via: str


def filter_invocations(
    *,
    invocations,
    session_id: str | None,
    host: str,
) -> GateOutcome:
    """Apply the routes-membership gate to a tracker scan's
    ``invocations`` dict (``{name: SelfReport}``).

    Returns a :class:`GateOutcome`. Never raises — every failure path
    silently falls back to the legacy label-based gate so a broken
    SQLite read can't kill verdict capture entirely.
    """
    picked: set[str] = set()
    via = "legacy"
    if session_id and isinstance(session_id, str):
        try:
            from mega_tron.config import store_path
            from mega_tron.verdicts.store import Store

            store = Store(store_path())
            # First: host-narrowed lookup (what the host's own hook
            # surfaced this session).
            picked = store.session_picked_names(
                session_id=session_id, host=host
            )
            # Then: widen to host-agnostic union for the SAME session.
            # This catches `mega-tron search` shell calls the model made
            # mid-session (those land under host="cli"). The host-narrow
            # filter was too tight: a JWT-related cli routes row written
            # by the model's own shell call should be admitted against
            # the host session's verdict gate, since it provably came
            # from this same conversation (same session_id).
            cli_picked = store.session_picked_names(
                session_id=session_id, host="cli"
            )
            picked |= cli_picked
            if picked:
                via = "routes"
        except Exception:
            picked = set()

    admitted: list[dict] = []
    skipped_no_verdict: list[str] = []
    skipped_not_in_catalog: list[str] = []

    for name, inv in invocations.items():
        if not inv.verdicts:
            skipped_no_verdict.append(name)
            continue

        if via == "routes":
            # New gate: admit any tag whose name was surfaced this session.
            # The tracker's `claimed_use` label is no longer a reason to
            # drop the verdict — most skills don't ship scripts/ so a
            # `scripts/` echo is the wrong proxy for "was actually used".
            if name not in picked:
                skipped_not_in_catalog.append(name)
                continue
        else:
            # Legacy fallback (no routes data available): keep the old
            # gate so fresh installs / dev environments don't regress to
            # accepting every hallucinated tag.
            if inv.label == "claimed_use":
                skipped_not_in_catalog.append(name)
                continue

        verdict_label = inv.verdicts[-1]
        reason = inv.reasons[-1] if inv.reasons else ""
        admitted.append(
            {"skill": name, "verdict": verdict_label, "reason": reason}
        )

    return GateOutcome(
        admitted=admitted,
        skipped_no_verdict=skipped_no_verdict,
        skipped_not_in_catalog=skipped_not_in_catalog,
        via=via,
    )
