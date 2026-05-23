"""`mega-tron search` — pick skills for a task.

Single command with multiple output forms (``--output {meta|names|
table|bodies|stage}``) and two ranking modes (``--mode {semantic|
agentic}``). Semantic mode is pure cosine + optional eval-blend
re-rank; agentic mode adds 1-2 LLM calls on top of the cosine prefilter.

The five ``_emit_*`` helpers below shape the final output. They are
private to this subcommand — nothing else needs them.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from mega_tron.cli._common import (
    _build_agentic,
    _make_router,
    _resolve_cache_path,
    _resolve_mode,
)


def _resolve_session_id(args: argparse.Namespace) -> str | None:
    """Resolve session_id from (1) --session-id, (2) env, (3) None.

    Models invoked from a host (codex / claude / gemini) call
    ``mega-tron search`` as a shell command. The per-turn skill
    block injected by the host hook tells the model to pass the
    host's session id via ``--session-id``; the env var fallback
    exists for scripted callers that prefer to set it once and
    forget. None is the right default for headless / SDK use.
    """
    explicit = getattr(args, "session_id", None)
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    env = os.environ.get("MEGA_SESSION_ID", "").strip()
    return env or None
from mega_tron.config import DEFAULT_PREFILTER
from mega_tron.hosts.codex.compat import detect_version, warn_if_untested
from mega_tron.prepender import build_prefix
from mega_tron.stager import Stager


def cmd_search(args: argparse.Namespace) -> int:
    """Pick skills for a task. One command, ``--output`` selects the
    emission form (``meta`` / ``names`` / ``bodies`` / ``table`` /
    ``stage``); ``--mode {semantic|agentic}`` selects whether the LLM
    rerank runs."""
    mode = _resolve_mode(args)
    if args.output == "stage" and not args.target:
        print("[search] --output stage requires --target <CODEX_HOME>", file=sys.stderr)
        return 2

    # ---- Daemon-first path (same pattern as host hooks) -------------------
    # CLI search is the same kind of routing decision a hook makes — only
    # the caller differs. Using the daemon when it's up keeps a CLI call
    # at sub-second latency instead of paying the embedder cold-load
    # (~5-30s) every time. Output forms that need full RankedSkill
    # objects (bodies, stage, table-with-scores) fall through to the
    # in-process path; meta/names land on the fast path because they
    # only need the picked names + dirs.
    #
    # Agentic mode also falls through — the LLM rerank lives in the
    # client process today, the daemon doesn't run it.
    # Eager spawn — regardless of whether THIS call can use the daemon
    # fast-path, we want a running daemon for the NEXT call (and for
    # every host hook on the system). Output modes like ``stage`` and
    # ``bodies`` need full RankedSkill objects the daemon doesn't
    # return, so they fall through to the in-process path — but we
    # still kick off ``spawn_detached`` so the daemon is up by the
    # time the user (or codex's shell wrapper) calls again.
    try:
        from mega_tron import daemon as daemon_mod

        if not daemon_mod.daemon_disabled() and not daemon_mod.is_running():
            daemon_mod.spawn_detached()
    except Exception:  # noqa: BLE001
        pass

    daemon_eligible = (
        mode == "semantic"
        and args.output in ("meta", "names")
        and not getattr(args, "json", False)
    )
    if daemon_eligible:
        rc = _try_daemon_path(args)
        if rc is not None:
            return rc

    router = _make_router(args)
    router.warmup_if_stale()
    agentic = _build_agentic(args, mode)

    # In semantic mode, --prefilter caps the cosine candidates that the
    # eval-blend reranker sees. (In agentic mode, AgenticSearch.top is the
    # equivalent knob and Router.rank ignores `prefilter`.)
    semantic_prefilter = None
    if mode == "semantic":
        semantic_prefilter = (
            args.prefilter if args.prefilter is not None else DEFAULT_PREFILTER
        )

    # Dynamic-K is the whole point of mega-tron's routing — let the
    # distribution decide K instead of hard-coding the user's --top-k.
    # The flag stays as the *cap* (so users who really want exactly 5
    # picks still get them), but the default behaviour is dynamic.
    # ``--no-dynamic-k`` reverts to manual mode for users who explicitly
    # want a fixed K, matching the hook subcommands.
    use_dynamic = getattr(args, "dynamic_k", True)

    ranked = router.rank(
        args.task,
        top_k=args.top_k,
        prefilter=semantic_prefilter,
        agentic=agentic,
        dynamic=use_dynamic,
    )

    # Best-effort route log. session_id is resolved from --session-id
    # arg → env MEGA_SESSION_ID → None. When a host model invokes the
    # CLI on its behalf, the per-turn prepender block tells it to pass
    # the host's session id; that's what lets the stop hook's verdict
    # gate credit `<skill-used>` tags against the right session catalog.
    try:
        _log_route_cli(args.task, ranked, router, session_id=_resolve_session_id(args))
    except Exception:  # noqa: BLE001
        pass

    if not ranked:
        print(f"[search] no matching skill for {args.task!r}", file=sys.stderr)
        return 1

    if args.output == "meta":
        return _emit_meta(ranked, args)
    if args.output == "names":
        return _emit_names(ranked, args)
    if args.output == "table":
        return _emit_table(ranked, args)
    if args.output == "bodies":
        return _emit_bodies(ranked, args)
    if args.output == "stage":
        return _emit_stage(ranked, args)
    print(f"[search] unknown --output {args.output!r}", file=sys.stderr)
    return 2


def _emit_meta(ranked, args: argparse.Namespace) -> int:
    """Default output — name + skill_dir + description per pick.

    Compact enough for an agent or human to survey several skills at once;
    informative enough that the reader knows *what* each pick is and *where*
    to dig deeper (run ``--output bodies`` for the full SKILL.md, or read
    the file directly at ``skill_dir``).
    """
    if args.json:
        out = [
            {
                "name": rs.skill.name,
                "skill_dir": str(rs.skill.skill_dir),
                "description": rs.skill.description,
            }
            for rs in ranked
        ]
        json.dump(out, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    for i, rs in enumerate(ranked):
        if i > 0:
            sys.stdout.write("\n")
        sys.stdout.write(f"- {rs.skill.name}\n")
        sys.stdout.write(f"    dir:  {rs.skill.skill_dir}\n")
        desc = (rs.skill.description or "").strip().replace("\n", " ")
        sys.stdout.write(f"    desc: {desc}\n")
    return 0


def _emit_names(ranked, args: argparse.Namespace) -> int:
    if args.json:
        json.dump([r.skill.name for r in ranked], sys.stdout)
        sys.stdout.write("\n")
        return 0
    for r in ranked:
        sys.stdout.write(f"{r.skill.name}\n")
    return 0


def _emit_table(ranked, args: argparse.Namespace) -> int:
    if args.json:
        out = [
            {
                "name": rs.skill.name,
                "score": round(rs.score, 4),
                "desc_tok": rs.skill.desc_tok,
                "skill_dir": str(rs.skill.skill_dir),
            }
            for rs in ranked
        ]
        json.dump(out, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    width = max((len(rs.skill.name) for rs in ranked), default=4)
    for rs in ranked:
        print(f"  {rs.score:.4f}  {rs.skill.name.ljust(width)}  ({rs.skill.desc_tok} tok)")
    return 0


def _emit_bodies(ranked, args: argparse.Namespace) -> int:
    if args.json:
        out = []
        for r in ranked:
            md = r.skill.skill_dir / "SKILL.md"
            try:
                body = md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            out.append({"name": r.skill.name, "content": body})
        json.dump(out, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    printed_any = False
    for r in ranked:
        md = r.skill.skill_dir / "SKILL.md"
        try:
            body = md.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            print(f"[search] could not read {md}: {e}", file=sys.stderr)
            continue
        if printed_any:
            sys.stdout.write("\n\n")
        sys.stdout.write(f"===== {r.skill.name} =====\n")
        sys.stdout.write(body)
        if not body.endswith("\n"):
            sys.stdout.write("\n")
        printed_any = True
    return 0 if printed_any else 1


def _emit_stage(ranked, args: argparse.Namespace) -> int:
    warn_if_untested(detect_version())
    stager = Stager(target=Path(args.target), budget_tok=args.budget_tok)
    manifest = stager.stage(ranked)

    print(
        f"[stage] staged={len(manifest.staged)} dropped={len(manifest.dropped)} "
        f"tok={manifest.total_staged_tok}/{manifest.budget_tok} target={manifest.target}",
        file=sys.stderr,
    )
    if args.print_manifest:
        print(manifest.to_json())
    elif args.no_prepend:
        pass
    else:
        sys.stdout.write(build_prefix(ranked, k=args.prepend_k))
    return 0


def _try_daemon_path(args: argparse.Namespace) -> int | None:
    """Attempt to serve the search via the running router daemon.

    Returns ``None`` when the daemon is unavailable / disabled / refused
    — the caller falls through to the in-process path. Otherwise emits
    the same output a hot in-process run would and returns the exit
    code directly.

    We also fire-and-forget ``spawn_detached()`` when the daemon is
    down so the *next* CLI call lands on the fast path; this mirrors
    the eager-spawn behaviour the host hooks rely on.
    """
    from mega_tron import daemon as daemon_mod
    from mega_tron.config import discover_skill_dirs

    if daemon_mod.daemon_disabled():
        return None

    if not daemon_mod.is_running():
        # Same eager-spawn rule the hooks use: kick off a detached
        # daemon startup before we fall back to the cold in-process
        # path, so the *next* CLI invocation is fast even if this one
        # has to pay the cold-load.
        daemon_mod.spawn_detached()
        return None

    skills_dirs = discover_skill_dirs()
    if not skills_dirs:
        return None

    # Daemon protocol still takes a single skills_dir per query — same
    # constraint the hooks live with. Send the highest-priority root
    # and the daemon's in-memory union covers the rest.
    cache_path = _resolve_cache_path(args)
    use_dynamic = getattr(args, "dynamic_k", True)
    resp = daemon_mod.client_query(
        {
            "op": "rank",
            "prompt": args.task,
            "skills_dir": str(skills_dirs[0]),
            "cache_path": str(cache_path),
            "top_k": args.top_k,
            "prepend_k": getattr(args, "prepend_k", 0),
            "dynamic": use_dynamic,
        }
    )
    if not resp or not resp.get("ok"):
        return None

    picked_names = resp.get("skills") or []
    if not picked_names:
        print(f"[search] no matching skill for {args.task!r}", file=sys.stderr)
        return 1

    # The daemon returns names only — resolve dirs + descriptions
    # client-side. This stays fast (no embedder load) because we only
    # touch the on-disk SKILL.md frontmatter for the picked names, not
    # the full pool.
    from mega_tron.router import load_skills

    by_name = {s.name: s for s in load_skills(skills_dirs)}
    chosen = [by_name[n] for n in picked_names if n in by_name]
    if not chosen:
        return None  # daemon returned names we can't resolve; cold-path

    if args.output == "names":
        for s in chosen:
            sys.stdout.write(f"{s.name}\n")
    else:  # meta — the default
        for i, s in enumerate(chosen):
            if i > 0:
                sys.stdout.write("\n")
            sys.stdout.write(f"- {s.name}\n")
            sys.stdout.write(f"    dir:  {s.skill_dir}\n")
            desc = (s.description or "").strip().replace("\n", " ")
            sys.stdout.write(f"    desc: {desc}\n")

    # Daemon-served calls still log to routes. The daemon already wrote
    # the row on its side (when implemented) — but until that lands,
    # the CLI mirrors the hook's pattern and logs client-side. Cheap
    # since we have the picked names + token count in hand.
    try:
        extras = resp.get("extras") or {}
        from mega_tron.config import store_path
        from mega_tron.verdicts.store import Store
        import hashlib

        total_tok = int(extras.get("total_tok") or sum(getattr(s, "desc_tok", 0) for s in chosen))
        k = int(extras.get("k") or len(chosen))
        k_reason = extras.get("k_reason") or ("dynamic" if use_dynamic else "manual")
        qhash = hashlib.sha256(args.task.encode("utf-8")).hexdigest()[:16]
        Store(path=store_path()).record_route(
            session_id=_resolve_session_id(args),
            host="cli",
            query_hash=qhash,
            picked_names=[s.name for s in chosen],
            total_tok=total_tok,
            k=k,
            k_reason=k_reason,
        )
    except Exception:  # noqa: BLE001
        pass

    return 0


def _log_route_cli(
    query: str,
    ranked,
    router,
    *,
    session_id: str | None = None,
) -> None:
    """Write one row to the ``routes`` analytics table for a CLI rank.

    ``session_id`` is supplied by the caller (resolved from
    ``--session-id`` or ``MEGA_SESSION_ID`` env); when None the row is
    written headless (no host conversation to credit). The host's stop
    hook later uses this column to admit the model's `<skill-used>`
    tags against the session's routed catalog.
    """
    import hashlib

    from mega_tron.config import store_path
    from mega_tron.verdicts.store import Store

    # Pull (K, reason) from the router if dynamic ran; otherwise this
    # call used a fixed --top-k so we tag it accordingly.
    if router.last_dynamic is not None:
        k, k_reason = router.last_dynamic
    else:
        k, k_reason = len(ranked), "manual"
    total_tok = sum(r.skill.desc_tok for r in ranked)
    qhash = hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]
    Store(path=store_path()).record_route(
        session_id=session_id,
        host="cli",
        query_hash=qhash,
        picked_names=[r.skill.name for r in ranked],
        total_tok=total_tok,
        k=k,
        k_reason=k_reason,
    )
