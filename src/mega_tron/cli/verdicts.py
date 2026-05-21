"""`mega-tron` verdict-store readers.

Two subcommands that read from the SQLite verdict store:

- ``search-verdicts``: BM25 full-text search over verdict reasons.
- ``regressions``: skills whose helpful/harmful trend recently flipped.
"""
from __future__ import annotations

import argparse
import json
import sys


def cmd_search_verdicts(args: argparse.Namespace) -> int:
    """Full-text search over verdict reasons via FTS5.

    Surfaces "what did sessions say about this skill / topic"
    historically — e.g. ``mega-tron search-verdicts "webhook
    signature"`` shows every HELPFUL/HARMFUL reason mentioning that
    phrase, ranked by BM25 relevance.

    Query syntax follows SQLite FTS5: bare words are tokenised,
    quoted phrases match literally, ``AND`` / ``OR`` / ``NOT`` /
    prefix ``*`` are supported.
    """
    from mega_tron.config import store_path
    from mega_tron.verdicts.store import Store

    path = store_path()
    if not path.exists():
        print(
            f"[search-verdicts] no SQLite store at {path}. "
            f"Accumulate verdicts through a session with the Stop hook "
            f"active, or run `mega-tron migrate-to-sqlite` to seed "
            f"from existing SKILL.md frontmatter.",
            file=sys.stderr,
        )
        return 1

    store = Store(path)
    rows = store.search_reasons(
        args.query,
        host=args.host,
        verdict=args.verdict,
        limit=args.limit,
        since_days=args.since_days,
    )

    if args.json:
        json.dump(rows, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    if not rows:
        print(
            f"(no verdict reasons matched {args.query!r}; "
            f"check FTS5 MATCH syntax if the query looks complex)"
        )
        return 0

    skill_w = max(len(r["skill_name"]) for r in rows)
    label_w = max(len(r["verdict"]) for r in rows)
    host_w = max(len(r["host"]) for r in rows)
    for r in rows:
        print(
            f"  {r['occurred_at']}  "
            f"{r['skill_name'].ljust(skill_w)}  "
            f"{r['verdict'].ljust(label_w)}  "
            f"{r['host'].ljust(host_w)}  "
            f"{r['reason']}"
        )
    return 0


def cmd_regressions(args: argparse.Namespace) -> int:
    """Surface skills whose helpful/harmful trend recently flipped.

    Reads the SQLite verdict store (errors out with a helpful pointer
    when the store doesn't exist) and runs
    :func:`mega_tron.verdicts.regressions.compute`. Default output lists only
    actionable classes (``broken``, ``regressed``); ``--include-all``
    adds ``unused`` and ``stable`` for debugging "why isn't this
    flagged?"
    """
    from mega_tron.config import store_path
    from mega_tron.verdicts.regressions import compute
    from mega_tron.verdicts.store import Store

    path = store_path()
    if not path.exists():
        print(
            f"[regressions] no SQLite store at {path}. "
            f"Run `mega-tron migrate-to-sqlite` first, or accumulate "
            f"verdicts through a session with the Stop hook active.",
            file=sys.stderr,
        )
        return 1

    store = Store(path)
    rows = compute(
        store,
        window_days=args.window_days,
        min_invocations=args.min_invocations,
        host=args.host,
        include_all=args.include_all,
    )

    if args.json:
        out = [
            {
                "skill_name": r.skill_name,
                "class": r.classification,
                "helpful_recent": r.helpful_recent,
                "helpful_baseline": r.helpful_baseline,
                "harmful_recent": r.harmful_recent,
                "harmful_baseline": r.harmful_baseline,
                "window_days": r.window_days,
                "last_helpful_at": (
                    r.last_helpful_at.strftime("%Y-%m-%dT%H:%M:%SZ")
                    if r.last_helpful_at
                    else None
                ),
                "last_harmful_at": (
                    r.last_harmful_at.strftime("%Y-%m-%dT%H:%M:%SZ")
                    if r.last_harmful_at
                    else None
                ),
                "detail": r.detail,
            }
            for r in rows
        ]
        json.dump(out, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    if not rows:
        print(
            "(no regressions detected — "
            "the library is currently stable across the window)"
        )
        return 0

    # Plain text — one row per regression, severity-grouped.
    width = max(len(r.skill_name) for r in rows)
    class_w = max(len(r.classification) for r in rows)
    print(
        f"  {'class'.ljust(class_w)}  {'skill'.ljust(width)}  detail"
    )
    print(
        f"  {'-' * class_w}  {'-' * width}  ------"
    )
    for r in rows:
        print(
            f"  {r.classification.ljust(class_w)}  "
            f"{r.skill_name.ljust(width)}  {r.detail}"
        )
    return 0
