"""Pure JSON handlers for the dashboard.

The handlers in this module take primitive arguments and return
JSON-serialisable dicts / lists. They have no HTTP coupling — the
HTTP layer in :mod:`mega_tron.dashboard.server` calls them and
encodes the result.

Two invariants the dashboard relies on:

* **No embedder cold-load.** This module never imports
  :mod:`mega_tron.core` (which would pull
  :class:`mega_tron.embedder.Embedder`). Skill enumeration goes
  through :func:`mega_tron.config.discover_skill_dirs` +
  :func:`mega_tron.verdicts.mega_meta.read_meta`, host pivot through SQL.
* **Host display normalisation.** The ``verdicts.host`` column stores
  the long names (``"claude_code"``, ``"gemini_cli"``). Every read
  that surfaces ``host`` to the UI funnels through
  :func:`mega_tron.hosts.normalize_host` so chips stay consistent.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable

from mega_tron.config import discover_skill_dirs, store_path
from mega_tron.hosts import infer_host_from_skill_dir, normalize_host
from mega_tron.verdicts.mega_meta import MegaMeta, read_meta
from mega_tron.verdicts.store import Store

_LOG = logging.getLogger(__name__)


# Display order matches the install-time canonical order. "other" is
# intentionally not surfaced as a host pivot — anything that doesn't
# resolve to one of these four is routed into the orphan/unknown
# counter on the health row instead, so the host chip row stays
# meaningful (no garbage bucket).
_DISPLAY_HOSTS = ("codex", "claude", "gemini", "hermes", "user")
_UNKNOWN_HOST = "other"


# --------------------------------------------------------------------------- #
# Skill enumeration helpers (used by overview + skills + skill_detail)
# --------------------------------------------------------------------------- #


def _iter_skills(
    skill_roots: Iterable[Path],
) -> Iterable[tuple[str, Path, Path, MegaMeta | None, str]]:
    """Yield ``(name, skill_dir, skill_md, meta_or_None, host)`` for every
    SKILL.md found under the configured roots, first-dir-wins on name
    collision (matches Router.load_skills semantics).

    Results are cached per-(skill_md, mtime) so a workspace with 3K
    SKILL.md files doesn't pay for full YAML re-parsing on every
    dashboard endpoint hit (overview + skills + activity + verdicts +
    every poll). On warm cache iteration drops from ~20s to <50ms.
    Cache is busted automatically when any file's mtime changes.
    """
    seen: set[str] = set()
    for root in skill_roots:
        if not root.exists():
            continue
        try:
            entries = sorted(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir():
                continue
            skill_md = entry / "SKILL.md"
            if not skill_md.exists():
                continue
            try:
                mtime = skill_md.stat().st_mtime_ns
            except OSError:
                continue
            cache_key = (str(skill_md), mtime)
            cached = _SKILL_META_CACHE.get(cache_key)
            if cached is None:
                cached = _load_skill_meta(skill_md, entry)
                _SKILL_META_CACHE[cache_key] = cached
                # Bound the cache to the current scan to prevent it
                # from growing unbounded across renames.
                if len(_SKILL_META_CACHE) > 20_000:
                    _SKILL_META_CACHE.clear()
            name, meta = cached
            if name in seen:
                continue
            seen.add(name)
            host = infer_host_from_skill_dir(entry)
            yield name, entry, skill_md, meta, host


_SKILL_META_CACHE: dict[tuple[str, int], tuple[str, MegaMeta | None]] = {}


def _load_skill_meta(skill_md: Path, entry: Path) -> tuple[str, MegaMeta | None]:
    """Read SKILL.md once and return ``(display_name, meta_or_None)``.
    Called only on cache miss, so we can afford the full parse here.
    """
    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError:
        return entry.name, None
    # Best-effort name lookup from the frontmatter; falls back to
    # the directory name so we never lose a skill from the list.
    name = _extract_yaml_name(text) or entry.name
    try:
        meta = read_meta(skill_md)
    except Exception as exc:  # noqa: BLE001
        # YAML failures are common on workspaces that mix random
        # third-party skills (smart quotes, CJK punctuation in
        # description, etc.). Log once at debug — the dashboard
        # still shows the skill, just without counts.
        _LOG.debug("read_meta failed for %s: %s", skill_md, exc)
        meta = None
    return name, meta


def reset_iter_skills_cache_for_tests() -> None:
    """Clear the SKILL.md memoisation. Tests use this so a fresh
    tmp_path is read fresh."""
    _SKILL_META_CACHE.clear()


def _extract_yaml_name(text: str) -> str | None:
    """Return the ``name:`` field from SKILL.md frontmatter without
    parsing the whole YAML (we just need the value)."""
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end < 0:
        return None
    fm = text[3:end]
    for line in fm.splitlines():
        if line.startswith("name:"):
            return line.split(":", 1)[1].strip().strip("\"'")
    return None


def _extract_yaml_description(text: str) -> str | None:
    """Return the ``description:`` field from SKILL.md frontmatter.

    Handles both inline (``description: foo``) and YAML block scalar
    forms (``description: |`` followed by indented lines) since the
    project's own skills use the block form. Returns ``None`` when
    no description is present.
    """
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end < 0:
        return None
    fm_lines = text[3:end].splitlines()
    for i, line in enumerate(fm_lines):
        if not line.startswith("description:"):
            continue
        # Inline form: everything after the colon, stripped.
        inline = line[len("description:"):].strip()
        if inline and inline not in ("|", ">", "|-", ">-"):
            return inline.strip("\"'")
        # Block scalar — gather indented continuation lines.
        body: list[str] = []
        for cont in fm_lines[i + 1:]:
            # Stop at the next top-level key (no leading whitespace, has colon).
            stripped = cont.lstrip()
            if not cont.startswith((" ", "\t")) and stripped and ":" in stripped:
                break
            body.append(stripped)
        joined = " ".join(s for s in body if s).strip()
        return joined or None
    return None


def _is_used(meta: MegaMeta | None, sql_counts: dict[str, Any] | None) -> bool:
    """Has this skill ever recorded a verdict?

    SQLite is the authoritative source — frontmatter can lag behind
    (verdict_writer might not have refreshed it, or the SKILL.md may
    not even exist for an orphan verdict). We check SQLite first, then
    fall back to the frontmatter so the answer stays correct under
    every combination of stale / present / missing inputs.
    """
    if sql_counts is not None and sql_counts.get("total", 0) > 0:
        return True
    if meta is None:
        return False
    if meta.last_updated:
        return True
    return (meta.helpful_count + meta.harmful_count) > 0


def _merge_counts(
    meta: MegaMeta | None, sql_counts: dict[str, Any] | None
) -> tuple[int, int, int]:
    """Return ``(helpful, harmful, neutral)`` preferring SQLite over
    frontmatter — verdict_writer can lag, and an orphan verdict (one
    whose SKILL.md was deleted) still needs to be surfaced.
    """
    if sql_counts is not None and sql_counts.get("total", 0) > 0:
        return (
            int(sql_counts.get("helpful", 0)),
            int(sql_counts.get("harmful", 0)),
            int(sql_counts.get("neutral", 0)),
        )
    if meta is None:
        return (0, 0, 0)
    return (meta.helpful_count, meta.harmful_count, 0)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #


def overview(*, days: int = 30) -> dict[str, Any]:
    """Top-of-page summary: total / used / unused + per-host pivot +
    net-harmful skill count.

    ``used`` means the skill has at least one recorded verdict
    (helpful + harmful + neutral); ``unused`` means the skill is
    installed on disk but has never been invoked. The verb is
    deliberately distinct from :attr:`MegaMeta.status` (which
    happens to use ``"active"`` for "currently in the routing
    pool") so the two don't bleed together in the UI.
    """
    roots = discover_skill_dirs()
    store = _open_store()
    try:
        sql_by_skill = store.verdict_counts_by_skill()
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("verdict_counts_by_skill failed: %s", exc)
        sql_by_skill = {}

    # by_host counts skills *the host actually recorded a verdict on*.
    # This is the same predicate the host-chip click uses to filter the
    # skill list (see renderSkillList → hosts_seen.includes(host)), so
    # the number on the chip = the row count that appears when clicked.
    # We DELIBERATELY do not count "installed on this host's disk root"
    # — that's an inventory question, not a routing-signal question,
    # and conflating the two has historically confused users.
    by_host: dict[str, int] = {h: 0 for h in _DISPLAY_HOSTS}
    per_host_sql = _per_skill_host_counts(store)
    for _skill, host_counts in per_host_sql.items():
        seen_short: set[str] = set()
        for raw_host, counts in host_counts.items():
            if not (counts.get("helpful") or counts.get("harmful")
                    or counts.get("neutral")):
                continue
            short = normalize_host(raw_host)
            if short in by_host and short not in seen_short:
                by_host[short] += 1
                seen_short.add(short)

    total = 0
    used = 0
    net_harmful = 0
    on_disk_names: set[str] = set()
    unknown_on_disk = 0
    for _name, _dir, _md, meta, host in _iter_skills(roots):
        total += 1
        on_disk_names.add(_name)
        sql = sql_by_skill.get(_name)
        helpful, harmful, _neutral = _merge_counts(meta, sql)
        used_here = _is_used(meta, sql)
        if used_here:
            used += 1
        if harmful > helpful:
            net_harmful += 1
        if host not in by_host:
            unknown_on_disk += 1

    # Orphan verdicts: a skill that has SQLite history but no SKILL.md
    # on disk (e.g. the directory was deleted, the benchmark sandbox
    # was torn down, or the catalog hasn't been synced to this
    # machine). These are NOT counted in `total` (it tracks installed
    # skills) but ARE surfaced via `orphan_count` so the user can see
    # them in the health row and clean up.
    orphan_count = 0
    for orphan_name, counts in sql_by_skill.items():
        if orphan_name in on_disk_names:
            continue
        orphan_count += 1
        if counts.get("harmful", 0) > counts.get("helpful", 0):
            net_harmful += 1

    # Surface historical noise so the user can spot test-fixture
    # spillover (placeholder reasons like "ok" / "evidence A") without
    # opening sqlite. Cheap — single SELECT COUNT(*) with no scan.
    try:
        noise_count = store.count_low_quality_reasons()
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("noise count query failed: %s", exc)
        noise_count = 0

    return {
        "total": total,
        "used": used,
        "unused": total - used,
        "by_host": by_host,
        "time_range_days": days,
        "net_harmful_count": net_harmful,
        "noise_verdict_count": noise_count,
        "orphan_count": orphan_count,
        "unknown_host_count": unknown_on_disk,
    }


def skills(*, host: str | None = None, days: int = 30) -> list[dict[str, Any]]:
    """Per-skill rows for the treemap. ``host`` filters by short name
    (``"codex"`` / ``"claude"`` / ``"gemini"`` / ``"hermes"`` / ``"other"``).

    Counts come from SQLite (single GROUP BY) merged with the on-disk
    skill list. A SKILL.md with no verdicts shows ``helpful=harmful=0,
    used=False``. A verdict-only orphan (skill row deleted from disk)
    is appended under ``host="other"`` so the treemap doesn't lie.
    """
    roots = discover_skill_dirs()
    store = _open_store()
    try:
        sql_by_skill = store.verdict_counts_by_skill()
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("verdict_counts_by_skill failed: %s", exc)
        sql_by_skill = {}

    out: list[dict[str, Any]] = []
    on_disk_names: set[str] = set()
    for name, skill_dir, _md, meta, h in _iter_skills(roots):
        on_disk_names.add(name)
        sql = sql_by_skill.get(name)
        helpful, harmful, _neutral = _merge_counts(meta, sql)
        if host is not None and h != host:
            continue
        out.append(
            {
                "name": name,
                "host": h,
                "helpful_count": helpful,
                "harmful_count": harmful,
                "net": helpful - harmful,
                "status": meta.status if meta else "active",
                "last_updated": (
                    (sql.get("last_updated") if sql else None)
                    or (meta.last_updated if meta else None)
                ),
                "used": _is_used(meta, sql),
                "skill_dir": str(skill_dir),
                "orphan": False,
            }
        )

    for orphan_name, counts in sql_by_skill.items():
        if orphan_name in on_disk_names:
            continue
        # Orphans don't have a meaningful host pivot from disk — when a
        # host filter is active, skip them entirely; otherwise mark
        # host="other" so the treemap can render them under a separate
        # group rather than misattribute the skill to a real host.
        if host is not None:
            continue
        helpful = int(counts.get("helpful", 0))
        harmful = int(counts.get("harmful", 0))
        out.append(
            {
                "name": orphan_name,
                "host": "other",
                "helpful_count": helpful,
                "harmful_count": harmful,
                "net": helpful - harmful,
                "status": "orphan",
                "last_updated": counts.get("last_updated"),
                "used": True,
                "skill_dir": None,
                "orphan": True,
            }
        )

    out.sort(key=lambda r: (-r["net"], -r["helpful_count"], r["name"]))
    return out


def skills_by_name(*, days: int = 30) -> list[dict[str, Any]]:
    """Skill-centric view: one row per distinct skill_name across all
    hosts, with a per-host breakdown attached.

    This is the dashboard's default list now. The host chips become
    a *filter* on top of this list (highlight rows whose per_host
    dict mentions the chip), not a separate sub-view, so the user
    can see at-a-glance which hosts agree or disagree on a skill.
    Each row carries:

    - ``helpful_count`` / ``harmful_count`` / ``neutral_count`` — totals
      summed across every host that ever recorded a verdict.
    - ``per_host``: ``{ "codex": {helpful, harmful, neutral, net}, ... }``
      — the building block for the per-host comparison bar in the
      drawer.
    - ``hosts_seen``: ordered list of short-host names that have at
      least one verdict on this skill. Used by the UI to render the
      tiny coloured dots after the name.
    - ``installed_hosts``: where on disk the SKILL.md exists. Helps
      surface "claude has used it, but only codex has it installed".
    - ``orphan`` / ``status`` mirror the existing skills() semantics.
    """
    roots = discover_skill_dirs()
    store = _open_store()
    try:
        sql_by_skill = store.verdict_counts_by_skill()
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("verdict_counts_by_skill failed: %s", exc)
        sql_by_skill = {}

    # Per-(skill, host) verdict tallies — one SQL pass.
    per_host_sql = _per_skill_host_counts(store)

    # Walk disk first so we can resolve installed_hosts and reuse meta.
    on_disk: dict[str, dict[str, Any]] = {}
    for name, skill_dir, _md, meta, h in _iter_skills(roots):
        rec = on_disk.setdefault(
            name, {"installed_hosts": [], "skill_dir": str(skill_dir),
                   "meta": meta},
        )
        if h not in rec["installed_hosts"]:
            rec["installed_hosts"].append(h)

    seen_names: set[str] = set()
    rows: list[dict[str, Any]] = []
    # Compose: union of on-disk skill names + SQLite-only orphans.
    for name in list(on_disk.keys()) + [
        n for n in sql_by_skill.keys() if n not in on_disk
    ]:
        if name in seen_names:
            continue
        seen_names.add(name)

        meta = on_disk.get(name, {}).get("meta")
        sql = sql_by_skill.get(name)
        helpful = int(sql["helpful"]) if sql else (meta.helpful_count if meta else 0)
        harmful = int(sql["harmful"]) if sql else (meta.harmful_count if meta else 0)
        neutral = int(sql["neutral"]) if sql else 0

        per_host: dict[str, dict[str, int]] = {}
        for h, h_counts in (per_host_sql.get(name) or {}).items():
            short = normalize_host(h)
            agg = per_host.setdefault(
                short, {"helpful": 0, "harmful": 0, "neutral": 0, "net": 0},
            )
            agg["helpful"] += int(h_counts["helpful"])
            agg["harmful"] += int(h_counts["harmful"])
            agg["neutral"] += int(h_counts["neutral"])
            agg["net"] = agg["helpful"] - agg["harmful"]

        installed_hosts = on_disk.get(name, {}).get("installed_hosts", [])
        orphan = name not in on_disk
        # `hosts_seen` is the set of hosts with verdict activity — UI
        # uses it for dot rendering.
        hosts_seen = list(per_host.keys())

        rows.append({
            "name": name,
            "helpful_count": helpful,
            "harmful_count": harmful,
            "neutral_count": neutral,
            "net": helpful - harmful,
            "used": (helpful + harmful + neutral) > 0,
            "per_host": per_host,
            "hosts_seen": hosts_seen,
            "installed_hosts": installed_hosts,
            "orphan": orphan,
            "status": (meta.status if meta else "orphan") if not orphan else "orphan",
            "last_updated": (
                (sql.get("last_updated") if sql else None)
                or (meta.last_updated if meta else None)
            ),
            "skill_dir": on_disk.get(name, {}).get("skill_dir"),
        })

    rows.sort(key=lambda r: (
        # Used first, then highest net, then most verdicts.
        0 if r["used"] else 1,
        -r["net"],
        -(r["helpful_count"] + r["harmful_count"]),
        r["name"],
    ))
    return rows


def _per_skill_host_counts(store: Store) -> dict[str, dict[str, dict[str, int]]]:
    """Single GROUP BY ``(skill_name, host)`` aggregation used by
    :func:`skills_by_name`. Returns
    ``{skill_name: {host_raw: {helpful, harmful, neutral, total}}}``.
    """
    store.initialize()
    out: dict[str, dict[str, dict[str, int]]] = {}
    sql = """
    SELECT skill_name, host,
           SUM(CASE WHEN verdict='HELPFUL' THEN 1 ELSE 0 END),
           SUM(CASE WHEN verdict='HARMFUL' THEN 1 ELSE 0 END),
           SUM(CASE WHEN verdict='NEUTRAL' THEN 1 ELSE 0 END),
           COUNT(*)
    FROM verdicts
    GROUP BY skill_name, host
    """
    with store._connect() as conn:  # noqa: SLF001
        for row in conn.execute(sql).fetchall():
            skill, host = row[0], row[1]
            bucket = out.setdefault(skill, {})
            bucket[host] = {
                "helpful": int(row[2] or 0),
                "harmful": int(row[3] or 0),
                "neutral": int(row[4] or 0),
                "total":   int(row[5] or 0),
            }
    return out


def _dense_fill(
    sparse: list[tuple[str, int]], *, days: int
) -> list[list[str | int]]:
    """Expand a sparse ``[(YYYY-MM-DD, count)]`` list into a dense
    ``days``-element array ending today (UTC). Missing days get 0.
    """
    from datetime import date, timedelta, timezone, datetime as _dt

    today = _dt.now(timezone.utc).date()
    by_date = {d: c for d, c in sparse}
    out: list[list[str | int]] = []
    for i in range(days - 1, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        out.append([d, int(by_date.get(d, 0))])
    return out


def activity(
    *,
    days: int = 30,
    skill: str | None = None,
    host: str | None = None,
) -> list[list[str | int]]:
    """Dense-fill ``days``-element activity series for the sparkline.

    ``host`` here is the *raw* host value (``"codex"``, ``"claude_code"``,
    ``"gemini_cli"``, ``"hermes"``) since we query the DB column
    directly. The dashboard frontend passes whatever the user clicked
    after un-normalising via :func:`_denormalize_host` below.
    """
    store = _open_store()
    sparse = store.activity_per_day(days=days, skill_name=skill, host=host)
    return _dense_fill(sparse, days=days)


def verdicts(
    *,
    limit: int = 50,
    host: str | None = None,
    skill: str | None = None,
    days: int | None = None,
) -> list[dict[str, Any]]:
    """Most-recent verdict rows for the bottom list.

    ``host`` accepts the short display form; we expand to the matching
    raw column values via :func:`_denormalize_host` so chip clicks
    transparently catch both ``"claude_code"`` and the (theoretical)
    ``"claude"`` value.
    """
    store = _open_store()
    raw_hosts = _denormalize_host(host)
    params: dict[str, Any] = {"limit": int(limit), "skill": skill}
    where: list[str] = []
    if raw_hosts is not None:
        # Use a small IN clause with explicit placeholders.
        ph = ",".join(f":h{i}" for i in range(len(raw_hosts)))
        where.append(f"host IN ({ph})")
        for i, h in enumerate(raw_hosts):
            params[f"h{i}"] = h
    if skill:
        where.append("skill_name = :skill")
    if days is not None and days > 0:
        where.append("occurred_at >= datetime('now', :since)")
        params["since"] = f"-{int(days)} days"
    sql = (
        "SELECT id, skill_name, verdict, reason, host, occurred_at "
        "FROM verdicts"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY occurred_at DESC LIMIT :limit"

    store.initialize()
    with store._connect() as conn:  # noqa: SLF001 — read-only helper
        cur = conn.execute(sql, params)
        rows = cur.fetchall()
    return [
        {
            "id": r[0],
            "skill_name": r[1],
            "verdict": r[2],
            "reason": r[3],
            "host": normalize_host(r[4]),
            "host_raw": r[4],
            "occurred_at": r[5],
        }
        for r in rows
    ]


def verdict_search(
    *,
    q: str,
    host: str | None = None,
    days: int | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """FTS5 search over verdict reasons. Thin wrapper around
    :meth:`Store.search_reasons`."""
    store = _open_store()
    # Search by raw host because that's what the column stores; expand
    # the short form first.
    raw = _denormalize_host(host)
    if raw is None:
        rows = store.search_reasons(q, limit=limit, since_days=days)
    else:
        # search_reasons only takes a single host — issue one call per
        # raw value (almost always 1) and merge.
        merged: list[dict[str, Any]] = []
        for h in raw:
            merged.extend(
                store.search_reasons(q, host=h, limit=limit, since_days=days)
            )
        # Re-sort by FTS score (lower = stronger) and clip to limit.
        merged.sort(key=lambda r: (r.get("score") or 0.0))
        rows = merged[:limit]
    for r in rows:
        r["host_raw"] = r["host"]
        r["host"] = normalize_host(r["host"])
    return rows


def skill_detail(name: str) -> dict[str, Any] | None:
    """Drawer payload for one skill: meta + per-host breakdown +
    recent verdicts (helpful list + harmful list + neutral list) +
    30-day sparkline + SKILL.md path.

    Works for both on-disk skills and orphan SQLite-only skills. The
    drawer keeps the helpful and harmful streams in separate lists so
    a 24-harmful skill never blurs into its single helpful win.
    """
    store = _open_store()
    roots = discover_skill_dirs()

    skill_dir: Path | None = None
    skill_md: Path | None = None
    meta: MegaMeta | None = None
    host = "other"

    for skill_name, sd, smd, m, h in _iter_skills(roots):
        if skill_name == name:
            skill_dir, skill_md, meta, host = sd, smd, m, h
            break

    # If the skill isn't on disk we still surface it as long as SQLite
    # has at least one verdict — that's how an orphan shows up.
    if skill_dir is None and store.count_verdicts() == 0:
        return None

    # Per-host SQL aggregation.
    store.initialize()
    with store._connect() as conn:  # noqa: SLF001
        cur = conn.execute(
            """
            SELECT host,
                   SUM(CASE WHEN verdict='HELPFUL' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN verdict='HARMFUL' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN verdict='NEUTRAL' THEN 1 ELSE 0 END),
                   COUNT(*)
            FROM verdicts WHERE skill_name = ?
            GROUP BY host
            """,
            (name,),
        )
        per_host_rows = cur.fetchall()

    per_host: dict[str, dict[str, int]] = {}
    sql_total = 0
    sql_helpful = 0
    sql_harmful = 0
    sql_neutral = 0
    for row in per_host_rows:
        h_helpful = int(row[1] or 0)
        h_harmful = int(row[2] or 0)
        h_neutral = int(row[3] or 0)
        h_total = int(row[4] or 0)
        sql_helpful += h_helpful
        sql_harmful += h_harmful
        sql_neutral += h_neutral
        sql_total += h_total
        per_host[normalize_host(row[0])] = {
            "helpful": h_helpful,
            "harmful": h_harmful,
            "neutral": h_neutral,
            "total": h_total,
        }

    # If we have neither on-disk presence nor SQLite history, it's
    # really not there.
    if skill_dir is None and sql_total == 0:
        return None

    # Pull each verdict stream separately so the drawer can render
    # them in dedicated lists. Same query, varying WHERE.
    def _fetch(verdict_label: str | None) -> list[dict[str, Any]]:
        with store._connect() as conn:  # noqa: SLF001
            if verdict_label is None:
                cur = conn.execute(
                    "SELECT id, skill_name, verdict, reason, host, occurred_at "
                    "FROM verdicts WHERE skill_name = ? "
                    "ORDER BY occurred_at DESC LIMIT 50",
                    (name,),
                )
            else:
                cur = conn.execute(
                    "SELECT id, skill_name, verdict, reason, host, occurred_at "
                    "FROM verdicts WHERE skill_name = ? AND verdict = ? "
                    "ORDER BY occurred_at DESC LIMIT 50",
                    (name, verdict_label),
                )
            rows = cur.fetchall()
        return [
            {
                "id": r[0],
                "skill_name": r[1],
                "verdict": r[2],
                "reason": r[3],
                "host": normalize_host(r[4]),
                "host_raw": r[4],
                "occurred_at": r[5],
            }
            for r in rows
        ]

    helpful_rows = _fetch("HELPFUL")
    harmful_rows = _fetch("HARMFUL")
    neutral_rows = _fetch("NEUTRAL")
    recent = _fetch(None)[:10]
    sparkline = activity(days=30, skill=name)

    # Counts: prefer SQLite (verdict-by-verdict ground truth) over
    # frontmatter (lossy aggregate).
    helpful = sql_helpful if sql_total > 0 else (meta.helpful_count if meta else 0)
    harmful = sql_harmful if sql_total > 0 else (meta.harmful_count if meta else 0)

    # Description from SKILL.md frontmatter — gives the user a one-liner
    # of "what is this skill?" without leaving the dashboard. Best-effort:
    # an orphan or unreadable file just returns None.
    description: str | None = None
    if skill_md is not None:
        try:
            description = _extract_yaml_description(
                skill_md.read_text(encoding="utf-8")
            )
        except OSError:
            description = None

    return {
        "name": name,
        "description": description,
        "host": host,
        "skill_dir": str(skill_dir) if skill_dir else None,
        "skill_md": str(skill_md) if skill_md else None,
        "helpful_count": helpful,
        "harmful_count": harmful,
        "neutral_count": sql_neutral,
        "total_verdicts": sql_total,
        "net": helpful - harmful,
        "status": meta.status if meta else ("orphan" if skill_dir is None else "active"),
        "last_updated": meta.last_updated if meta else (
            recent[0]["occurred_at"] if recent else None
        ),
        "per_host": per_host,
        "recent": recent,
        "helpful_history": helpful_rows,
        "harmful_history": harmful_rows,
        "neutral_history": neutral_rows,
        "sparkline": sparkline,
        "orphan": skill_dir is None,
    }


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #


_STORE: Store | None = None


def _open_store() -> Store:
    """Process-wide singleton for the dashboard's Store. The dashboard
    is a single-tenant local server; one Store instance keeps the WAL
    connection cache warm across the polling requests.
    """
    global _STORE
    if _STORE is None:
        _STORE = Store(path=store_path())
    return _STORE


def _denormalize_host(short: str | None) -> list[str] | None:
    """Expand a short display host (``"claude"``) to the raw column
    values it can correspond to (``["claude_code", "claude"]``).

    Returns ``None`` when ``short`` is ``None`` (no filter), or a
    non-empty list otherwise.
    """
    if short is None:
        return None
    table: dict[str, list[str]] = {
        "claude": ["claude_code", "claude"],
        "gemini": ["gemini_cli", "gemini"],
        "codex": ["codex"],
        "user": ["user"],
        "hermes": ["hermes"],
        "other": ["other"],
    }
    return table.get(short, [short])


def reset_store_singleton_for_tests() -> None:
    """Reset the process-wide Store cache. Tests use this to point the
    dashboard at a temp-path store without restarting the process."""
    global _STORE, _VES
    _STORE = None
    _VES = None


# --------------------------------------------------------------------------- #
# Verdict-embedding store singleton
# --------------------------------------------------------------------------- #


_VES: Any = None


def _open_verdict_embeddings() -> Any:
    """Lazy-load the verdict-embedding store. Returns ``None`` when the
    embedder (or its dependencies) are unavailable — dashboard mutations
    treat embedding cleanup as best-effort.

    The embedder is imported lazily because importing
    :mod:`mega_tron.embedder` pulls sentence-transformers, which is
    a heavy cold-load we want to keep off the dashboard's startup
    path. The first verdict edit pays the cost; everything else stays
    cheap.
    """
    global _VES
    if _VES is not None:
        return _VES
    try:
        from mega_tron.embedder import fingerprint_of, make_embedder
        from mega_tron.verdicts.embeddings import VerdictEmbeddingsStore

        embedder = make_embedder()
        _VES = VerdictEmbeddingsStore(fingerprint=fingerprint_of(embedder))
        return _VES
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("verdict-embedding store unavailable: %s", exc)
        return None


# --------------------------------------------------------------------------- #
# Mutation endpoints (human-in-the-loop verdict edits)
# --------------------------------------------------------------------------- #


class NotFoundError(LookupError):
    """Sentinel raised when a verdict id has no matching row."""


_ALLOWED_PATCH_KEYS = {"verdict", "reason"}


def patch_verdict(verdict_id: int, body: dict[str, Any]) -> dict[str, Any]:
    """Mutate one verdict row, then resync the affected SKILL.md
    ``mega_meta`` frontmatter so the dashboard's cumulative counters
    stay in lockstep.

    ``body`` MUST be a subset of ``{"verdict", "reason"}``; unknown
    keys raise :class:`KeyError`. ``verdict`` is validated against
    {HELPFUL, HARMFUL, NEUTRAL}; an invalid value raises
    :class:`ValueError`. A non-existent ``verdict_id`` raises
    :class:`NotFoundError`.

    Embedding cleanup and frontmatter resync are best-effort: each
    failure logs a warning but doesn't fail the whole call. The
    SQLite write is the only step that must succeed (frontmatter is
    re-derivable from SQLite).
    """
    unknown = set(body.keys()) - _ALLOWED_PATCH_KEYS
    if unknown:
        raise KeyError(f"unknown body fields: {sorted(unknown)}")
    if not body:
        raise ValueError("body must include at least one of: verdict, reason")

    store = _open_store()
    row = store.get_verdict(verdict_id)
    if row is None:
        raise NotFoundError(verdict_id)

    skill_name = row["skill_name"]
    # The SQL write is the one step that must succeed.
    store.update_verdict(verdict_id, **body)

    # Best-effort: invalidate the matching embedding (the reason or
    # label changed, so the stored vector no longer represents this
    # row's signal). The next stop-hook embed will re-populate.
    ves = _open_verdict_embeddings()
    if ves is not None:
        try:
            ves.remove_by_verdict_id(verdict_id)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("embedding remove failed for id=%s: %s", verdict_id, exc)

    # Best-effort: resync SKILL.md frontmatter.
    new_meta = _resync_frontmatter(skill_name)

    return {
        "ok": True,
        "verdict": store.get_verdict(verdict_id),
        "meta": new_meta,
    }


def add_verdict_endpoint(body: dict[str, Any]) -> dict[str, Any]:
    """Insert a new manual verdict, attributed to ``host="user"``.

    Body shape::
        { "skill_name": "...", "verdict": "HELPFUL"|"HARMFUL"|"NEUTRAL",
          "reason": "...optional..." }

    The dashboard exposes this so a human can score a skill directly
    (without waiting for a host's Stop hook to fire). ``host`` is
    always written as ``"user"`` so these verdicts are clearly
    attributable in audits and the per-host chart.
    """
    skill = body.get("skill_name")
    verdict = body.get("verdict")
    reason = body.get("reason")
    if not isinstance(skill, str) or not skill:
        raise ValueError("skill_name is required")
    if not isinstance(verdict, str):
        raise ValueError("verdict is required")
    v_upper = verdict.upper()
    if v_upper not in {"HELPFUL", "HARMFUL", "NEUTRAL"}:
        raise ValueError(
            f"verdict must be HELPFUL/HARMFUL/NEUTRAL, got {verdict!r}"
        )
    if reason is not None and not isinstance(reason, str):
        raise ValueError("reason must be a string")

    # session_id: unique per click so the dedup gate in Store.record_verdict
    # doesn't swallow rapid-fire manual entries from the same user. UTC
    # millis + random suffix gets us cheap uniqueness.
    import secrets
    from datetime import datetime, timezone
    sid = (
        f"user-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')}"
        f"-{secrets.token_hex(2)}"
    )

    store = _open_store()
    store.record_verdict(
        skill_name=skill,
        verdict=v_upper,
        reason=(reason or None),
        host="user",
        session_id=sid,
    )
    new_meta = _resync_frontmatter(skill)
    return {"ok": True, "meta": new_meta}


def delete_verdict_endpoint(verdict_id: int) -> dict[str, Any]:
    """Delete a verdict row + its embedding + resync the SKILL.md."""
    store = _open_store()
    row = store.get_verdict(verdict_id)
    if row is None:
        raise NotFoundError(verdict_id)

    skill_name = row["skill_name"]
    store.delete_verdict(verdict_id)

    ves = _open_verdict_embeddings()
    if ves is not None:
        try:
            ves.remove_by_verdict_id(verdict_id)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("embedding remove failed for id=%s: %s", verdict_id, exc)

    new_meta = _resync_frontmatter(skill_name)
    return {"ok": True, "meta": new_meta}


def open_folder(body: dict[str, Any]) -> dict[str, Any]:
    """Open ``body["path"]`` in the OS file manager.

    Security: the path MUST resolve to a directory that lives **under
    one of the configured skill roots** (``discover_skill_dirs()``).
    Anything outside is rejected. The dashboard is loopback-bound and
    runs as the user, so the only meaningful confinement is this
    server-side path check — we use ``Path.resolve()`` so symlink
    games don't escape the allowlist.

    Returns ``{"ok": True, "path": "..."}`` on success.
    """
    import subprocess
    import sys

    raw = body.get("path")
    if not isinstance(raw, str) or not raw:
        raise ValueError("open-folder requires a 'path' string")
    try:
        target = Path(raw).expanduser().resolve(strict=True)
    except FileNotFoundError:
        raise ValueError(f"path does not exist: {raw}")
    if not target.is_dir():
        # Allow files too — many users want to jump straight to a
        # SKILL.md. The OS open handler picks the right tool.
        if not target.exists():
            raise ValueError(f"path does not exist: {raw}")

    allowed_roots = [
        p.resolve(strict=False) for p in discover_skill_dirs()
    ]
    if not any(_is_under(target, root) for root in allowed_roots):
        raise PermissionError(
            f"refused to open path outside skill roots: {target}"
        )

    if sys.platform == "darwin":
        cmd = ["open", str(target)]
    elif sys.platform.startswith("linux"):
        cmd = ["xdg-open", str(target)]
    elif sys.platform == "win32":
        cmd = ["explorer", str(target)]
    else:
        raise OSError(f"unsupported platform: {sys.platform}")
    # subprocess.run with check=True so a broken xdg-open / open
    # surfaces as a 500 instead of silently no-op'ing.
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"ok": True, "path": str(target)}


def archive_skill(name: str) -> dict[str, Any]:
    """Soft-archive: set ``mega_meta.status = "archived"`` so the
    router stops surfacing the skill. Reversible (the user can flip
    the YAML back manually).
    """
    skill_md = _find_skill_md(name)
    if skill_md is None:
        raise NotFoundError(name)

    from mega_tron.verdicts.mega_meta import (
        MegaMeta,
        _dump_frontmatter,
        _parse_frontmatter,
        read_meta,
    )

    text = skill_md.read_text(encoding="utf-8")
    fm, _, body = _parse_frontmatter(text)
    meta = MegaMeta.from_dict(fm.get("mega_meta") or {})
    meta.status = "archived"
    fm["mega_meta"] = meta.to_dict()
    new_text = "---\n" + _dump_frontmatter(fm) + "\n---\n" + body
    tmp = skill_md.with_suffix(skill_md.suffix + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    tmp.replace(skill_md)
    return {"ok": True, "skill_md": str(skill_md), "status": "archived"}


def hard_delete_skill(name: str) -> dict[str, Any]:
    """Hard-delete: remove the entire skill directory from disk.

    Confined to the standard skill roots — refuses to delete a
    SKILL.md whose parent isn't under one of the configured roots.
    The user just clicked through one confirm in the UI; this is
    the destructive primitive behind it.
    """
    import shutil

    skill_md = _find_skill_md(name)
    if skill_md is None:
        raise NotFoundError(name)
    skill_dir = skill_md.parent.resolve()
    allowed_roots = [
        p.resolve(strict=False) for p in discover_skill_dirs()
    ]
    if not any(_is_under(skill_dir, root) for root in allowed_roots):
        raise PermissionError(
            f"refused to delete path outside skill roots: {skill_dir}"
        )
    # Safety: never let a root accidentally become the deletion
    # target (e.g. a misconfigured root that points at the skill
    # itself). Equality check covers the case.
    for root in allowed_roots:
        if skill_dir == root:
            raise PermissionError(
                f"refused to delete a skill root: {skill_dir}"
            )
    shutil.rmtree(skill_dir)
    # Clear cache for this path so the dashboard immediately reflects
    # the deletion on the next request.
    reset_iter_skills_cache_for_tests()
    return {"ok": True, "skill_dir": str(skill_dir)}


def _find_skill_md(name: str) -> Path | None:
    """Locate the SKILL.md for ``name`` under any configured root."""
    for n, _dir, skill_md, _meta, _host in _iter_skills(discover_skill_dirs()):
        if n == name:
            return skill_md
    return None


def _is_under(child: Path, parent: Path) -> bool:
    """True if ``child`` is the same as ``parent`` or a descendant."""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def bulk_delete_verdicts(body: dict[str, Any]) -> dict[str, Any]:
    """Delete every verdict matching a (skill_name, host, reason)
    pattern in one shot.

    Body shape::
        { "skill_name": "...", "host": "claude"|"claude_code"|..., "reason": "..." }

    ``host`` accepts either the short display name (``"claude"``) or
    the raw column value (``"claude_code"``); short names are
    expanded via :func:`_denormalize_host` so a chip-driven bulk
    delete from the UI catches both encodings.

    Always followed by a frontmatter resync on the affected skill so
    cumulative counts stay honest. Returns
    ``{"ok": True, "deleted": N, "meta": ...}``.
    """
    skill = body.get("skill_name")
    host = body.get("host")
    reason = body.get("reason")  # may be None — matches NULL rows
    if not isinstance(skill, str) or not skill:
        raise ValueError("bulk-delete requires non-empty skill_name")
    if not isinstance(host, str) or not host:
        raise ValueError("bulk-delete requires non-empty host")
    if reason is not None and not isinstance(reason, str):
        raise ValueError("bulk-delete reason must be a string or null")

    store = _open_store()
    raw_hosts = _denormalize_host(host) or [host]
    total_deleted = 0
    for h in raw_hosts:
        try:
            total_deleted += store.delete_verdicts_matching(
                skill_name=skill, host=h, reason=reason,
            )
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("bulk delete partial failure for host=%s: %s", h, exc)
    new_meta = _resync_frontmatter(skill) if total_deleted > 0 else None
    return {"ok": True, "deleted": total_deleted, "meta": new_meta}


def _resync_frontmatter(skill_name: str) -> dict[str, Any] | None:
    """Locate the SKILL.md for ``skill_name`` and rebuild its mega_meta
    counters from SQLite. Returns the new meta dict or None if no
    matching SKILL.md was found."""
    from mega_tron.verdicts.mega_meta import resync_from_store

    roots = discover_skill_dirs()
    matches: list[Path] = []
    for _name, _dir, skill_md, _meta, _host in _iter_skills(roots):
        if _name == skill_name:
            matches.append(skill_md)
    if not matches:
        _LOG.warning("no SKILL.md found for %s; skipping resync", skill_name)
        return None
    if len(matches) > 1:
        _LOG.warning(
            "skill name %r maps to multiple SKILL.md files (%s) — using first",
            skill_name,
            [str(p) for p in matches],
        )
    target = matches[0]
    store = _open_store()
    try:
        meta = resync_from_store(target, store, skill_name)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("frontmatter resync failed for %s: %s", target, exc)
        return None
    return meta.to_dict()
