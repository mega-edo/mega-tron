"""Time-windowed regression detection over the verdict store.

Surfaces skills whose helpful/harmful trend recently flipped — a signal
Hermes's own Curator structurally cannot produce because it tracks only
cumulative usage frequency, not per-event timestamps. The classifier is
a pure function over :class:`mega_tron.verdicts.store.Store`: one SQL pass
computes recent / baseline / last-event aggregates per skill, then a
short threshold ladder produces ``broken | regressed | unused | stable``
labels.

Classification (first match wins, ordered by severity):

- ``unused``    — too few recent invocations to judge. **False-positive
                  guard.** A skill nobody calls is not "regressed", it's
                  just dormant. Without this rung we'd flood the output
                  with skills that simply fell out of use.
- ``broken``    — was working in the baseline window
                  (``helpful_baseline >= min_invocations``) and is now
                  mostly harmful (``harm_ratio >= 0.5``). The strongest
                  signal — usually an API change or a dependency
                  regression underneath the skill.
- ``regressed`` (hard) — was helpful, now zero helpful and at least one
                  harmful in the recent window.
- ``regressed`` (soft) — sharp helpful drop-off (``helpful_recent <=
                  helpful_baseline * 0.2``) with no harm signal. Usually
                  means the agent stopped picking it — could be benign,
                  could be a description that no longer matches user
                  intent.
- ``stable``    — nothing actionable.

Default CLI output emits only ``broken`` and ``regressed`` (the actionable
classes). ``--include-all`` shows ``unused`` and ``stable`` too — useful
for "why isn't this skill flagged?" debugging.

The thresholds are intentionally conservative. They can be overridden via
the function's keyword args; the CLI exposes ``--window-days`` and
``--min-invocations`` because those are the two knobs that materially
change classification, while the ratio cuts are stable defaults.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mega_tron.core import Regression
    from mega_tron.verdicts.store import Store


# Severe-regression cuts. Conservative — easy to tighten, hard to loosen
# once users start trusting the output.
BROKEN_HARM_RATIO = 0.5
"""``broken``: at least half of recent invocations are HARMFUL. Strong
signal of a real failure underneath the skill."""

SOFT_DROPOFF_RATIO = 0.2
"""``regressed`` (soft): helpful_recent has fallen to <= 20% of
helpful_baseline, with no harmful events. Surfaces "agent stopped picking
this skill" patterns."""

DEFAULT_WINDOW_DAYS = 30
"""Recent window. The baseline is the equal-length window immediately
preceding it (``window_days * 2 → window_days`` ago).
"""

DEFAULT_MIN_INVOCATIONS = 5
"""Minimum recent verdicts required before a skill can be classified as
anything other than ``unused``. The false-positive guard."""


@dataclass(frozen=True)
class _Counts:
    """Single skill's recent + baseline aggregates from one SQL pass."""

    skill_name: str
    helpful_recent: int
    harmful_recent: int
    recent_invocations: int  # helpful + harmful + neutral
    helpful_baseline: int
    harmful_baseline: int
    last_helpful_at: datetime | None
    last_harmful_at: datetime | None


_AGGREGATE_SQL = """
WITH bounds AS (
  SELECT
    datetime('now', :w_start_offset)  AS w_start,
    datetime('now', :b_start_offset)  AS b_start
)
SELECT
  v.skill_name,
  SUM(CASE WHEN v.occurred_at >= bounds.w_start
            AND v.verdict='HELPFUL' THEN 1 ELSE 0 END)              AS h_recent,
  SUM(CASE WHEN v.occurred_at >= bounds.w_start
            AND v.verdict='HARMFUL' THEN 1 ELSE 0 END)              AS x_recent,
  SUM(CASE WHEN v.occurred_at >= bounds.w_start
            THEN 1 ELSE 0 END)                                       AS n_recent,
  SUM(CASE WHEN v.occurred_at <  bounds.w_start
            AND v.occurred_at >= bounds.b_start
            AND v.verdict='HELPFUL' THEN 1 ELSE 0 END)              AS h_baseline,
  SUM(CASE WHEN v.occurred_at <  bounds.w_start
            AND v.occurred_at >= bounds.b_start
            AND v.verdict='HARMFUL' THEN 1 ELSE 0 END)              AS x_baseline,
  MAX(CASE WHEN v.verdict='HELPFUL' THEN v.occurred_at END)          AS last_h,
  MAX(CASE WHEN v.verdict='HARMFUL' THEN v.occurred_at END)          AS last_x
FROM verdicts v, bounds
WHERE (:host IS NULL OR v.host = :host)
GROUP BY v.skill_name
"""


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.rstrip("Z"))
    except (TypeError, ValueError):
        return None


def _format_detail(c: _Counts, window_days: int) -> str:
    """Human-readable one-liner. Lands in ``Regression.detail`` and the
    CLI table; designed to read like a Curator review hint."""
    parts = [
        f"helpful {c.helpful_baseline} → {c.helpful_recent} over {window_days}d"
    ]
    if c.harmful_recent:
        parts.append(f"{c.harmful_recent} harmful events recent")
    if c.harmful_baseline and not c.harmful_recent:
        parts.append(f"{c.harmful_baseline} harmful in baseline")
    return ", ".join(parts)


def _classify(c: _Counts, *, min_invocations: int) -> str:
    """Apply the classification ladder. Pure function over counts.

    The false-positive guard uses ``recent_invocations`` (all verdicts,
    including NEUTRAL) so an actively-used-but-inconclusive skill isn't
    misread as "unused". The broken / regressed thresholds use only
    helpful + harmful — NEUTRAL carries no quality signal.
    """
    if c.recent_invocations < min_invocations:
        return "unused"
    polar_recent = c.helpful_recent + c.harmful_recent
    if (
        c.helpful_baseline >= min_invocations
        and polar_recent > 0
        and (c.harmful_recent / polar_recent) >= BROKEN_HARM_RATIO
    ):
        return "broken"
    if (
        c.helpful_baseline >= min_invocations
        and c.helpful_recent == 0
        and c.harmful_recent >= 1
    ):
        return "regressed"
    if (
        c.helpful_baseline >= min_invocations
        and c.helpful_recent <= c.helpful_baseline * SOFT_DROPOFF_RATIO
        and c.harmful_recent == 0
    ):
        return "regressed"
    return "stable"


def compute(
    store: "Store",
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    min_invocations: int = DEFAULT_MIN_INVOCATIONS,
    host: str | None = None,
    include_all: bool = False,
) -> "list[Regression]":
    """Run the regression classifier against a populated :class:`Store`.

    Args:
        store: the store to query. Must be initialised; an unmigrated
            store (no verdict rows) returns ``[]``.
        window_days: width of the recent window in days. The baseline
            window is the equal-length stretch immediately preceding it.
        min_invocations: minimum recent verdicts required before a skill
            can be classified as anything other than ``unused``. Lower
            values give more sensitivity at the cost of noise.
        host: when set, only verdicts tagged with this host count. Use
            ``"codex"`` / ``"claude_code"`` / ``"hermes"`` to scope the
            analysis to one host's signal.
        include_all: when ``False`` (default) only actionable classes
            (``broken`` / ``regressed``) are returned. ``True`` includes
            ``unused`` and ``stable`` rows for debugging.

    Returns:
        Descending-severity list of :class:`Regression` rows.
    """
    from mega_tron.core import Regression  # local — avoid import cycle

    store.initialize()

    with store._connect() as conn:
        cur = conn.execute(
            _AGGREGATE_SQL,
            {
                "w_start_offset": f"-{window_days} days",
                "b_start_offset": f"-{window_days * 2} days",
                "host": host,
            },
        )
        rows = cur.fetchall()

    results: list[Regression] = []
    for row in rows:
        c = _Counts(
            skill_name=row[0],
            helpful_recent=int(row[1] or 0),
            harmful_recent=int(row[2] or 0),
            recent_invocations=int(row[3] or 0),
            helpful_baseline=int(row[4] or 0),
            harmful_baseline=int(row[5] or 0),
            last_helpful_at=_parse_iso(row[6]),
            last_harmful_at=_parse_iso(row[7]),
        )
        classification = _classify(c, min_invocations=min_invocations)
        if not include_all and classification not in ("broken", "regressed"):
            continue
        results.append(
            Regression(
                skill_name=c.skill_name,
                classification=classification,
                helpful_recent=c.helpful_recent,
                helpful_baseline=c.helpful_baseline,
                harmful_recent=c.harmful_recent,
                harmful_baseline=c.harmful_baseline,
                window_days=window_days,
                last_helpful_at=c.last_helpful_at,
                last_harmful_at=c.last_harmful_at,
                detail=_format_detail(c, window_days),
            )
        )

    # Stable order: severity (broken first, then regressed, ...) then
    # by skill_name for reproducibility.
    severity_order = {"broken": 0, "regressed": 1, "unused": 2, "stable": 3}
    results.sort(
        key=lambda r: (severity_order.get(r.classification, 99), r.skill_name)
    )
    return results


__all__ = [
    "BROKEN_HARM_RATIO",
    "DEFAULT_MIN_INVOCATIONS",
    "DEFAULT_WINDOW_DAYS",
    "SOFT_DROPOFF_RATIO",
    "compute",
]
