"""Shared best-effort route-log helpers for host hooks.

Every host hook (codex / claude / gemini) writes one row to the
``routes`` analytics table after a successful rank — this is the data
the dashboard's Context Savings tab consumes as the "measured median"
once it has accumulated enough samples. The two helpers in this module
exist because each hook now has two code paths that can succeed:

* **Cold path** — in-process ``Router.rank`` returns a list of
  :class:`RankedSkill` objects. ``log_route_from_ranked`` writes the
  row directly from those objects.

* **Daemon fast-path** — ``daemon_mod.client_query`` returns a dict
  with ``"skills"`` (names only) and an optional ``"extras"`` block
  carrying ``total_tok``/``k``/``k_reason``. ``log_route_from_daemon``
  resolves picked names back to on-disk skill rows so it can compute
  ``total_tok`` even when the daemon didn't pre-compute it.

Both helpers are best-effort: any exception is swallowed by the
caller. They share a single ``Store.record_route`` call shape so the
schema stays consistent regardless of which path produced the row.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from mega_tron.config import store_path
from mega_tron.verdicts.store import Store


def _qhash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]


def log_route_from_ranked(
    prompt: str,
    ranked,
    router,
    *,
    session_id: str | None,
    host: str,
) -> None:
    """Write a routes row from a list of :class:`RankedSkill` objects.

    Called from the cold-path branch of each host hook (and from the
    CLI ``search`` subcommand when it lands on the in-process path).
    Errors are silenced by the caller — the rank itself must not be
    blocked by a logging miss.
    """
    if router.last_dynamic is not None:
        k, k_reason = router.last_dynamic
    else:
        k, k_reason = len(ranked), "manual"
    total_tok = sum(getattr(r.skill, "desc_tok", 0) for r in ranked)
    Store(path=store_path()).record_route(
        session_id=session_id,
        host=host,
        query_hash=_qhash(prompt),
        picked_names=[r.skill.name for r in ranked],
        total_tok=total_tok,
        k=k,
        k_reason=k_reason,
    )


def log_route_from_daemon(
    prompt: str,
    daemon_response: dict,
    skills_dirs: list[Path],
    *,
    session_id: str | None,
    host: str,
) -> None:
    """Write a routes row from a daemon ``rank`` / ``agentic_rank`` response.

    ``daemon_response`` carries ``"skills"`` (list of picked names) and
    optionally an ``"extras"`` dict with ``total_tok`` / ``k`` /
    ``k_reason``. When extras is missing we resolve the picked names
    against the on-disk skill pool to compute ``total_tok`` from the
    description tokens, and tag the row as ``"dynamic"`` (the daemon's
    default rank path always uses dynamic-K).

    Empty ``picked`` is still logged — a no-match turn is a real routing
    event the dashboard's measured-median analytics needs to count.
    Without this, sessions that never surface a skill look identical to
    sessions where the hook never fired, and per-session multi-turn QA
    cannot distinguish "hook ran, no match" from "hook didn't run".
    """
    picked = list(daemon_response.get("skills") or [])

    extras = daemon_response.get("extras") or {}
    total_tok = extras.get("total_tok")
    k = extras.get("k")
    k_reason = extras.get("k_reason")

    if total_tok is None:
        if picked:
            from mega_tron.router import load_skills

            by_name = {s.name: s for s in load_skills(skills_dirs)}
            total_tok = sum(
                getattr(by_name.get(n), "desc_tok", 0) or 0 for n in picked
            )
        else:
            total_tok = 0

    if k is None:
        k = len(picked)
    if not k_reason:
        k_reason = "dynamic" if picked else "no-match"

    Store(path=store_path()).record_route(
        session_id=session_id,
        host=host,
        query_hash=_qhash(prompt),
        picked_names=picked,
        total_tok=int(total_tok or 0),
        k=int(k),
        k_reason=str(k_reason),
    )
