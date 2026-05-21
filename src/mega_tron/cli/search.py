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
import sys
from pathlib import Path

from mega_tron.cli._common import _build_agentic, _make_router, _resolve_mode
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

    ranked = router.rank(
        args.task,
        top_k=args.top_k,
        prefilter=semantic_prefilter,
        agentic=agentic,
    )
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
