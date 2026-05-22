"""Codex `UserPromptSubmit` hook entry point.

Codex invokes this on every interactive turn. We read the prompt from stdin
JSON, semantic-route via our embedding cache, and emit an `additionalContext`
that gets prepended to the model prompt. The context lists candidate skills
(with their descriptions and SKILL.md paths) and leaves the decision to the
model — we deliberately do not use codex's ``$SkillName`` MUST-use trigger
so the router proposes, the model disposes.

Wire schema (codex-rs/hooks/src/schema/UserPromptSubmitCommandInput):

    {
      "session_id": "...",
      "turn_id": "...",
      "transcript_path": "..." | null,
      "cwd": "...",
      "hook_event_name": "UserPromptSubmit",
      "model": "...",
      "permission_mode": "...",
      "prompt": "<the user's actual prompt>"
    }

We respond with:

    {
      "hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": "## Skills (selected for this turn ...)\n..."
      }
    }

Exit codes:
- 0 with stdout JSON: success, additionalContext injected
- 0 with empty stdout: hook ran but added nothing (silent passthrough)
- non-zero: codex logs the failure but continues — wrapper never breaks user

Behaviour:
- First UserPromptSubmit of a session (no marker file under
  ``$XDG_RUNTIME_DIR/mega-tron/seen-<session_id>``) → run the
  router end-to-end and inject the resulting candidate-skills block
  as the turn's ``additionalContext``.
- Subsequent turns → emit empty stdout (true noop). The search-CLI
  guidance lives permanently in ``~/.codex/AGENTS.md`` (planted by
  ``mega-tron install``), so codex carries it forward on every
  turn via its own system-prompt rendering — no per-turn prepend
  needed from us.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _default_cache_path() -> Path:
    """Embedder-keyed cache path under ``~/.cache/mega-tron``.

    The cache filename includes the embedder model-id slug
    (``ThakiCloud_SKILLRET-Embedding-0.6B.npz`` by default), so swapping
    models never accidentally reuses stale embeddings.
    """
    from mega_tron.config import Config

    model_id = Config.load().embedder_model
    slug = model_id.replace("/", "_")
    return Path.home() / ".cache" / "mega-tron" / f"{slug}.npz"


def _default_skills_dirs() -> list[Path]:
    """Return the full union of registered skill roots.

    Routing is host-agnostic: a skill that lives in ``~/.claude/skills``
    or ``~/.agents/skills`` should still rank when the user is in a
    Codex session, and a HELPFUL verdict accumulated under Claude
    should keep boosting that skill on Codex turns. We therefore pass
    the entire union down to :class:`Router`, which already handles
    multi-dir warmup (first-dir-wins on name collision)."""
    from mega_tron.config import discover_skill_dirs

    dirs = discover_skill_dirs()
    if dirs:
        return dirs
    return [Path.home() / ".codex" / "skills"]


def _first_fire_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or os.environ.get("TMPDIR") or "/tmp"
    return Path(base) / "mega-tron"


def _first_fire_marker(session_id: str | None) -> Path | None:
    if not session_id or not isinstance(session_id, str):
        return None
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_") or "anon"
    return _first_fire_dir() / f"seen-{safe}"


def _is_first_fire(session_id: str | None) -> bool:
    """Return True on the very first hook invocation for this session_id.

    Side effect: creates the marker so future calls return False. If we can't
    write the marker (read-only fs), we conservatively report True every time
    — the LLM cost is bounded by ``MEGA_MODE=semantic`` anyway.
    """
    marker = _first_fire_marker(session_id)
    if marker is None:
        return True
    if marker.exists():
        return False
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
    except OSError:
        return True
    return True


def _agentic_enabled() -> bool:
    """``MEGA_MODE`` selects the search mode; default is ``semantic``."""
    from mega_tron._env import resolve_mode
    return resolve_mode(None) == "agentic"


def _dispatch_wisdom_ignite(prompt: str) -> None:
    """Fire wisdom-curator in the background. No-op when ``MEGA_WITH_WISDOM``
    is unset or the curator path is unconfigured.

    Daemon-first: if the warm daemon is alive, send a ``wisdom_ignite``
    request and let the daemon manage the in-flight pool + dedup. The
    daemon replies immediately; we don't poll. If the daemon is dead or
    disabled, fall back to a direct in-process ignite (subprocess is
    detached via ``start_new_session=True`` so it survives hook exit).

    Designed for fire-and-forget at the *start* of first-fire processing
    so the wisdom-curator (~80 s typical) gets maximum lead time before
    the next session benefits from the freshly-downloaded skills. All
    failures swallow into stderr — wisdom must never break a turn.
    """
    from mega_tron import wisdom as wisdom_mod

    if not wisdom_mod.is_enabled():
        return

    # Daemon-first path: forward to the long-lived process, which holds
    # the in-flight pool and the dedup TTL state across hook invocations.
    from mega_tron import daemon as daemon_mod

    if not daemon_mod.daemon_disabled() and daemon_mod.is_running():
        resp = daemon_mod.client_query({"op": "wisdom_ignite", "prompt": prompt})
        if resp is not None and resp.get("ok"):
            status = resp.get("status", "?")
            if status == "fired":
                print(
                    f"[mega-tron hook] wisdom ignite fired (pid={resp.get('pid')})",
                    file=sys.stderr,
                )
            elif status in ("queued", "cached"):
                print(
                    f"[mega-tron hook] wisdom ignite {status} "
                    f"(age={resp.get('age_s') or resp.get('elapsed_s')}s)",
                    file=sys.stderr,
                )
            return

    # Daemon miss or disabled — fire directly. No dedup in this path; we
    # accept the cost of occasional duplicate fires when the daemon is
    # absent, since the alternative is a per-hook disk-backed marker file
    # and the daemon-first path is the common case.
    try:
        handle = wisdom_mod.ignite(prompt, silent=True)
    except RuntimeError as e:
        print(f"[mega-tron hook] wisdom ignite skipped: {e}", file=sys.stderr)
        return
    print(
        f"[mega-tron hook] wisdom ignite fired direct (pid={handle.proc.pid})",
        file=sys.stderr,
    )


def _emit_empty() -> int:
    """Return successfully without injecting anything. Codex treats this as no-op."""
    return 0


def _make_embedder() -> object:
    """Build the hook-time embedder honoring (in priority):

    1. ``MEGA_HOOK_EMBEDDER=openai|voyage`` — third-party adapters.
    2. ``MEGA_EMBEDDER_MODEL`` env / ``[embedder] model`` config — any
       HuggingFace sentence-transformers model.
    3. Default: SkillRet-Embedding-0.6B per
       :data:`mega_tron.config.DEFAULT_EMBEDDER_MODEL`.
    """
    kind = os.environ.get("MEGA_HOOK_EMBEDDER", "").lower().strip()
    if kind == "openai":
        from mega_tron.embedders.openai import OpenAIEmbedder

        return OpenAIEmbedder()
    if kind == "voyage":
        from mega_tron.embedders.voyage import VoyageEmbedder

        return VoyageEmbedder()
    from mega_tron.embedder import make_embedder

    return make_embedder()


def _emit_additional_context(additional_context: str) -> int:
    """Write the codex hookSpecificOutput envelope to stdout and return 0.

    ``suppressOutput: true`` hides the
    ``• UserPromptSubmit hook (completed)`` cell from the TUI without
    affecting the injected context — codex still feeds
    ``hookSpecificOutput.additionalContext`` to the model on every turn.
    See codex-rs/hooks/schema/generated/user-prompt-submit.command.output.schema.json.
    """
    out = {
        "suppressOutput": True,
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": additional_context,
        },
    }
    json.dump(out, sys.stdout)
    return 0


def cmd_hook(args: argparse.Namespace) -> int:
    """Read codex hook JSON from stdin, write hook-output JSON to stdout."""
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as e:
        print(f"[mega-tron hook] invalid input JSON: {e}", file=sys.stderr)
        return 0

    event_name = data.get("hook_event_name") or data.get("hookEventName")
    if event_name and event_name != "UserPromptSubmit":
        return _emit_empty()

    prompt = data.get("prompt", "")
    if not isinstance(prompt, str) or not prompt.strip():
        return _emit_empty()

    session_id = data.get("session_id") or data.get("sessionId")
    first_fire = _is_first_fire(session_id)

    # Only the first hook of a session auto-fires routing. Subsequent
    # turns are a true noop — the search-CLI guidance lives in
    # ~/.codex/AGENTS.md, which codex bakes into the system prompt itself.
    if not first_fire:
        return _emit_empty()

    # Wisdom ignition runs FIRST so the (~80s) MEGA-Code curator call gets
    # maximum lead time. It's fire-and-forget; this turn still routes off
    # whatever is already on disk. Next-session prompts benefit when the
    # new SKILL.md files land in the auto-discovered wisdom dir.
    _dispatch_wisdom_ignite(prompt)

    if args.skills_dir:
        skills_dirs = [Path(args.skills_dir)]
    elif os.environ.get("MEGA_SKILLS_DIR"):
        skills_dirs = [
            Path(p) for p in os.environ["MEGA_SKILLS_DIR"].split(os.pathsep) if p
        ]
    else:
        skills_dirs = _default_skills_dirs()
    cache_path = Path(args.cache_path or _default_cache_path())

    existing_dirs = [d for d in skills_dirs if d.exists()]
    if not existing_dirs:
        checked = ", ".join(str(d) for d in skills_dirs)
        print(
            f"[mega-tron hook] skills dir not found "
            f"(checked: {checked}), skipping route",
            file=sys.stderr,
        )
        return _emit_empty()
    skills_dirs = existing_dirs

    # ---- Daemon-first path -------------------------------------------------
    # Daemon protocol takes a single ``skills_dir`` per query; the
    # in-process fallback below consumes the full union.
    from mega_tron import daemon as daemon_mod

    daemon_op = "agentic_rank" if _agentic_enabled() else "rank"
    daemon_response = None
    if not daemon_mod.daemon_disabled():
        daemon_response = daemon_mod.client_query(
            {
                "op": daemon_op,
                "prompt": prompt,
                "skills_dir": str(skills_dirs[0]),
                "cache_path": str(cache_path),
                "top_k": args.top_k,
                "prepend_k": args.prepend_k,
            }
        )
    if daemon_response and daemon_response.get("ok"):
        ctx = daemon_response.get("additional_context") or ""
        if not ctx.strip():
            return _emit_empty()
        return _emit_additional_context(ctx)

    # Daemon miss — fall back to in-process and lazy-spawn for next turn.
    if not daemon_mod.daemon_disabled() and not daemon_mod.is_running():
        # Tell the user what's happening so a slow first call (embedder
        # cold-load + skill embedding sync, typically 5-30s on a cold
        # cache) isn't mistaken for a timeout. The daemon spawns in the
        # background; the next session will be fast.
        if not os.environ.get("MEGA_QUIET"):
            print(
                "[mega-tron] router daemon not running — this turn pays "
                "the embedder cold-load (~5-30s on first call). Spawning "
                "daemon in background so the next session routes in ~50ms.",
                file=sys.stderr,
            )
        daemon_mod.spawn_detached()

    from mega_tron.cache import Cache
    from mega_tron.prepender import build_hook_context
    from mega_tron.router import Router

    embedder = _make_embedder()
    cache = Cache(path=cache_path)
    router = Router(skills_dir=skills_dirs, embedder=embedder, cache=cache)

    try:
        router.warmup_if_stale()
    except Exception as e:  # noqa: BLE001
        print(f"[mega-tron hook] warmup_if_stale failed: {e}", file=sys.stderr)
        return _emit_empty()

    agentic = None
    if _agentic_enabled():
        try:
            from mega_tron.agentic import AgenticSearch
            from mega_tron.llm_backends import make_llm_backend

            agentic = AgenticSearch(backend=make_llm_backend())
        except Exception as e:  # noqa: BLE001 - backend init can blow up in many ways
            print(
                f"[mega-tron hook] agentic init failed, falling back to cosine: {e}",
                file=sys.stderr,
            )
            agentic = None

    try:
        ranked = router.rank(
            prompt,
            top_k=args.top_k,
            agentic=agentic,
            dynamic=getattr(args, "dynamic_k", False),
        )
    except Exception as e:  # noqa: BLE001
        print(f"[mega-tron hook] rank failed: {e}", file=sys.stderr)
        return _emit_empty()

    # Best-effort analytics log: record this routing decision so the
    # Context Savings dashboard can show the user's measured per-turn
    # token cost. Failures here MUST NOT affect routing — the try/except
    # in the Store method already swallows DB issues, but we wrap again
    # in case the import path itself blows up.
    try:
        _log_route(prompt, ranked, router, session_id=session_id, host="codex")
    except Exception:  # noqa: BLE001
        pass

    if not ranked:
        return _emit_empty()

    ctx = build_hook_context(ranked, k=args.prepend_k)
    if not ctx.strip():
        return _emit_empty()

    return _emit_additional_context(ctx.rstrip())


def _log_route(
    prompt: str,
    ranked,
    router,
    *,
    session_id,
    host: str,
) -> None:
    """Write one row to the ``routes`` analytics table.

    Pulls ``(K, k_reason)`` from ``router.last_dynamic`` (set by the
    most recent rank call) and the total injected token count from
    the sum of ``rs.skill.desc_tok`` over the ranked list. The query
    text itself isn't stored — only a SHA-256 prefix — so analytics
    can group identical queries without retaining user content.
    """
    import hashlib

    from mega_tron.config import store_path
    from mega_tron.verdicts.store import Store

    k, k_reason = router.last_dynamic or (len(ranked), "manual")
    total_tok = sum(r.skill.desc_tok for r in ranked)
    qhash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
    store = Store(path=store_path())
    store.record_route(
        session_id=session_id,
        host=host,
        query_hash=qhash,
        picked_names=[r.skill.name for r in ranked],
        total_tok=total_tok,
        k=k,
        k_reason=k_reason,
    )
