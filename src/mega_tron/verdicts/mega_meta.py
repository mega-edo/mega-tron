"""Per-skill ROI bookkeeping inside SKILL.md's YAML frontmatter.

Each skill's SKILL.md carries its own evidence under a ``mega_meta:``
block, updated in place when Codex evaluates skill usage at session
end.

Frontmatter layout:

    ---
    name: webhook-signer
    description: |
      USE WHEN: ...
    mega_meta:
      helpful_count: 12
      harmful_count: 2
      helpful_contexts:
        - "Caught the missing replay-protection check on cycle 4"
        - ...
      harmful_contexts: []
      status: active             # active | suspect | archived
      last_session_id: "0193..."
      last_updated: "2026-05-17T07:54:33Z"
      version_history:
        - {v: 1, ts: "...", change: "first invocation"}
    ---

Semantics:
- counters monotonically increase (one per Stop-hook persist phase per
  skill).
- contexts are natural-language one-liners, capped at 3 — oldest is
  dropped when the cap is reached.
- status is set heuristically: ``harmful_count > 3`` OR
  ``harmful_ratio > 0.3`` over ≥5 invocations marks ``suspect``.
  ``consecutive_harmful >= AUTO_ARCHIVE_THRESHOLD`` flips ``status`` to
  ``archived`` (one-way; recovery is a manual YAML edit).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

CONTEXTS_CAP = 3
HARMFUL_RATIO_THRESHOLD = 0.3
HARMFUL_COUNT_THRESHOLD = 3
MIN_INVOCATIONS_FOR_STATUS = 5

# N consecutive HARMFUL verdicts (with no HELPFUL in between) flips a
# skill's status to "archived" — one-way; manual recovery only.
AUTO_ARCHIVE_THRESHOLD = 3


@dataclass
class MegaMeta:
    helpful_count: int = 0
    harmful_count: int = 0
    helpful_contexts: list[str] = field(default_factory=list)
    harmful_contexts: list[str] = field(default_factory=list)
    status: str = "active"
    # Tracks the current run of HARMFUL verdicts since the last HELPFUL
    # one. Resets on HELPFUL; auto-archives at AUTO_ARCHIVE_THRESHOLD.
    consecutive_harmful: int = 0
    last_session_id: str | None = None
    last_updated: str | None = None
    version_history: list[dict] = field(default_factory=list)
    # Preserve any unknown fields the user (or another tool) may have set.
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "MegaMeta":
        if not isinstance(d, dict):
            return cls()
        known = {
            "helpful_count",
            "harmful_count",
            "helpful_contexts",
            "harmful_contexts",
            "status",
            "consecutive_harmful",
            "last_session_id",
            "last_updated",
            "version_history",
        }
        return cls(
            helpful_count=int(d.get("helpful_count", 0) or 0),
            harmful_count=int(d.get("harmful_count", 0) or 0),
            helpful_contexts=list(d.get("helpful_contexts") or []),
            harmful_contexts=list(d.get("harmful_contexts") or []),
            status=str(d.get("status") or "active"),
            consecutive_harmful=int(d.get("consecutive_harmful", 0) or 0),
            last_session_id=d.get("last_session_id"),
            last_updated=d.get("last_updated"),
            version_history=list(d.get("version_history") or []),
            extra={k: v for k, v in d.items() if k not in known},
        )

    def to_dict(self) -> dict:
        out: dict[str, Any] = {
            "helpful_count": self.helpful_count,
            "harmful_count": self.harmful_count,
            "helpful_contexts": self.helpful_contexts,
            "harmful_contexts": self.harmful_contexts,
            "status": self.status,
        }
        if self.consecutive_harmful:
            out["consecutive_harmful"] = self.consecutive_harmful
        if self.last_session_id:
            out["last_session_id"] = self.last_session_id
        if self.last_updated:
            out["last_updated"] = self.last_updated
        if self.version_history:
            out["version_history"] = self.version_history
        out.update(self.extra)
        return out

    def apply_verdict(self, verdict: str, reason: str | None, *, session_id: str | None) -> None:
        """Update counters + contexts based on codex's per-skill verdict.

        verdict ∈ {"HELPFUL", "HARMFUL", "NEUTRAL"} (case-insensitive).
        - HELPFUL/HARMFUL: increment counter and append context.
        - NEUTRAL: timestamp only (skill ran but didn't move the needle).
        - Unknown verdicts: silently ignored, never crash.
        """
        v = (verdict or "").upper()
        if v == "HELPFUL":
            self.helpful_count += 1
            self.consecutive_harmful = 0
            if reason:
                _append_capped(self.helpful_contexts, reason, CONTEXTS_CAP)
        elif v == "HARMFUL":
            self.harmful_count += 1
            self.consecutive_harmful += 1
            if reason:
                _append_capped(self.harmful_contexts, reason, CONTEXTS_CAP)
        elif v == "NEUTRAL":
            # Neither counter changes; harmful streak survives but isn't extended.
            pass
        else:
            return  # ignore unknown verdicts; never crash

        self.last_session_id = session_id
        self.last_updated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._refresh_status()

    def _refresh_status(self) -> None:
        # C4: a sustained HARMFUL streak archives the skill (one-way). Checked
        # first because archived dominates over suspect/active.
        if self.consecutive_harmful >= AUTO_ARCHIVE_THRESHOLD:
            self.status = "archived"
            return
        total = self.helpful_count + self.harmful_count
        if total < MIN_INVOCATIONS_FOR_STATUS:
            return  # not enough data
        ratio = self.harmful_count / total if total else 0
        if self.harmful_count > HARMFUL_COUNT_THRESHOLD or ratio > HARMFUL_RATIO_THRESHOLD:
            self.status = "suspect"
        elif self.status == "suspect" and ratio <= 0.15 and self.harmful_count <= 1:
            # Recover: a previously-suspect skill earned its way back.
            self.status = "active"


def _append_capped(items: list[str], item: str, cap: int) -> None:
    """Append one item; oldest is dropped when cap is reached."""
    s = item.strip()
    if not s:
        return
    if s in items:
        return  # dedupe simple repeats
    items.append(s)
    while len(items) > cap:
        items.pop(0)


# --------------------------------------------------------------------------
# Frontmatter I/O — manual parse so we can preserve the body verbatim.
# --------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?", re.DOTALL)


def _parse_frontmatter(text: str) -> tuple[dict, str, str]:
    """Return (frontmatter_dict, fm_yaml_text, body_text).

    Raises ValueError on malformed YAML or missing fences. We can't safely
    update a SKILL.md that codex itself might reject.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        raise ValueError("SKILL.md missing `---` frontmatter fences")
    fm_text = m.group(1)
    body = text[m.end():]
    loaded = yaml.safe_load(fm_text)
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise ValueError(f"frontmatter must be a mapping, got {type(loaded).__name__}")
    return loaded, fm_text, body


def _dump_frontmatter(fm: dict) -> str:
    """Dump frontmatter with stable key order — known fields first, then alphabetical."""
    preferred_order = ["name", "description", "mega_meta"]
    ordered: dict = {}
    for k in preferred_order:
        if k in fm:
            ordered[k] = fm[k]
    for k in sorted(fm.keys()):
        if k not in ordered:
            ordered[k] = fm[k]
    return yaml.safe_dump(
        ordered,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    ).rstrip("\n")


def read_meta(skill_md: Path) -> MegaMeta:
    """Return the MegaMeta block from skill_md, or a default if absent."""
    fm, _, _ = _parse_frontmatter(skill_md.read_text(encoding="utf-8"))
    return MegaMeta.from_dict(fm.get("mega_meta") or {})


def resync_from_store(
    skill_md: Path,
    store: "Any",
    skill_name: str,
) -> MegaMeta:
    """Rebuild SKILL.md ``mega_meta`` counters from the verdicts table.

    Used after a dashboard verdict edit / delete so the frontmatter
    (the canonical source of cumulative counts after the P0 refactor)
    stays consistent with the underlying SQLite time-series. The
    derivation is::

        helpful_count := SUM(CASE WHEN verdict='HELPFUL' THEN 1 ELSE 0 END)
        harmful_count := SUM(CASE WHEN verdict='HARMFUL' THEN 1 ELSE 0 END)
        last_updated  := MAX(occurred_at)            -- only when > 0 rows

    `helpful_contexts` / `harmful_contexts` are **preserved verbatim**.
    They're natural-language summaries (capped via _append_capped) that
    don't map 1:1 to verdict rows, so recomputing them from scratch
    would lose the human-readable phrasing the contexts carry.

    Atomic write via the same tmp+rename pattern :func:`update_meta`
    uses (no half-written SKILL.md on crash). After rewriting counters
    we re-run :meth:`MegaMeta._refresh_status` so the status / suspect /
    archived flags re-converge for the new totals.

    `store` is typed as Any to avoid a circular import; in practice it
    is :class:`mega_tron.verdicts.store.Store`.
    """
    # Aggregate the three counters in a single round-trip. occurred_at
    # is ISO-8601; MAX(text) is lex-sorted but the format is
    # zero-padded, so lex == chrono here.
    with store._connect() as conn:  # noqa: SLF001 — internal helper, intentional
        cur = conn.execute(
            """
            SELECT
                SUM(CASE WHEN verdict='HELPFUL' THEN 1 ELSE 0 END),
                SUM(CASE WHEN verdict='HARMFUL' THEN 1 ELSE 0 END),
                MAX(occurred_at)
            FROM verdicts WHERE skill_name = ?
            """,
            (skill_name,),
        )
        row = cur.fetchone()
    helpful = int(row[0] or 0)
    harmful = int(row[1] or 0)
    latest_ts = row[2]  # already ISO-8601 with Z suffix; preserve verbatim

    text = skill_md.read_text(encoding="utf-8")
    fm, _, body = _parse_frontmatter(text)
    meta = MegaMeta.from_dict(fm.get("mega_meta") or {})
    meta.helpful_count = helpful
    meta.harmful_count = harmful
    if latest_ts is not None:
        meta.last_updated = latest_ts
    elif helpful == 0 and harmful == 0:
        # No surviving verdicts at all — clear last_updated so the
        # dashboard's "active vs idle" signal correctly classifies this
        # skill as never-used.
        meta.last_updated = None
    meta._refresh_status()  # noqa: SLF001 — same module, intentional
    fm["mega_meta"] = meta.to_dict()

    new_text = "---\n" + _dump_frontmatter(fm) + "\n---\n" + body
    tmp = skill_md.with_suffix(skill_md.suffix + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    tmp.replace(skill_md)
    return meta


def update_meta(
    skill_md: Path,
    *,
    verdict: str,
    reason: str | None,
    session_id: str | None,
) -> MegaMeta:
    """Read-modify-write the SKILL.md mega_meta block.

    Atomic: writes to `<skill_md>.tmp` then renames. Body is preserved verbatim;
    only the frontmatter YAML is re-emitted (with mega_meta updated).
    """
    text = skill_md.read_text(encoding="utf-8")
    fm, _, body = _parse_frontmatter(text)
    meta = MegaMeta.from_dict(fm.get("mega_meta") or {})
    meta.apply_verdict(verdict, reason, session_id=session_id)
    fm["mega_meta"] = meta.to_dict()

    new_text = "---\n" + _dump_frontmatter(fm) + "\n---\n" + body
    tmp = skill_md.with_suffix(skill_md.suffix + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    tmp.replace(skill_md)
    return meta


@dataclass(frozen=True)
class EvaluationOutcome:
    """Aggregate result of :func:`apply_evaluations`.

    Counters add up to ``len(evaluations)`` exactly so callers can sanity-check
    they didn't lose entries to silent skips.
    """

    updated: int
    skipped_missing: int
    skipped_invalid: int
    errors: list[tuple[str, str]]  # (skill_name, error_message)
    applied: list[str]  # skill names that received a mutation (or would, dry-run)


def apply_evaluations(
    skills_dir: Path,
    evaluations: list[dict],
    *,
    session_id: str | None = None,
    dry_run: bool = False,
) -> EvaluationOutcome:
    """Apply a list of verdicts to per-skill SKILL.md ``mega_meta`` blocks.

    Each ``evaluations`` entry must be a dict with at minimum a ``skill``
    name and a ``verdict`` string; an optional ``reason`` is preserved as a
    NL context.

    Missing skill directories and malformed entries are tallied separately.

    With ``dry_run=True`` no files are mutated; the returned ``applied``
    list still reports which names would have been updated.

    Used by both the ``mega-tron evaluate`` CLI and the
    Stop-hook persist phase — keeping the mutation logic in one place
    makes JSON-in/file-out evaluation testable independently of codex.
    """
    updated = 0
    skipped_missing = 0
    skipped_invalid = 0
    errors: list[tuple[str, str]] = []
    applied: list[str] = []

    for v in evaluations:
        if not isinstance(v, dict):
            skipped_invalid += 1
            continue
        name = v.get("skill")
        verdict = v.get("verdict")
        reason = v.get("reason")
        if not isinstance(name, str) or not name:
            skipped_invalid += 1
            continue
        skill_md = skills_dir / name / "SKILL.md"
        if not skill_md.exists():
            skipped_missing += 1
            continue
        if dry_run:
            applied.append(name)
            updated += 1
            continue
        try:
            update_meta(
                skill_md,
                verdict=verdict or "",
                reason=reason if isinstance(reason, str) else None,
                session_id=session_id,
            )
            applied.append(name)
            updated += 1
        except Exception as e:  # noqa: BLE001
            errors.append((name, str(e)))

    return EvaluationOutcome(
        updated=updated,
        skipped_missing=skipped_missing,
        skipped_invalid=skipped_invalid,
        errors=errors,
        applied=applied,
    )
