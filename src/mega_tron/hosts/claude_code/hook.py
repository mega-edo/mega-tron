"""Claude Code `UserPromptSubmit` hook entry point.

Mirrors :mod:`mega_tron.hook` (the Codex hook), but consumes/emits the
Claude Code hook wire format instead of Codex's.

Stdin JSON (from Claude Code's hooks pipeline):

    {
      "session_id": "...",
      "transcript_path": "...",
      "cwd": "...",
      "permission_mode": "...",
      "hook_event_name": "UserPromptSubmit",
      "prompt": "<the user's actual prompt>"
    }

Stdout JSON (envelope identical to Codex's — Claude Code supports the
same ``hookSpecificOutput.additionalContext`` mechanism, see
https://code.claude.com/docs/en/hooks):

    {
      "hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": "..."
      }
    }

Behaviour mirrors the Codex hook:
- First UserPromptSubmit of a session (per-session marker file under
  ``$XDG_RUNTIME_DIR/mega-tron/seen-claude-<session_id>``) → run the
  router end-to-end and inject the top-K context.
- Subsequent turns → emit empty stdout. The persistent guidance lives in
  ``~/.claude/CLAUDE.md`` (planted by ``mega-tron install --target
  claude``), so Claude Code carries it forward on every turn via its own
  memory rendering.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _default_cache_path() -> Path:
    """Embedder-keyed cache path under ``~/.cache/mega-tron``."""
    from mega_tron.config import Config

    model_id = Config.load().embedder_model
    slug = model_id.replace("/", "_")
    return Path.home() / ".cache" / "mega-tron" / f"{slug}.npz"


def _default_skills_dirs() -> list[Path]:
    """Return the full union of registered skill roots.

    Routing is host-agnostic: a skill that lives in ``~/.codex/skills``
    or ``~/.agents/skills`` should still rank when the user is in a
    Claude Code session, and a HELPFUL verdict accumulated under Codex
    should keep boosting that skill on Claude turns. We therefore pass
    the entire union down to :class:`Router`, which already handles
    multi-dir warmup (first-dir-wins on name collision)."""
    from mega_tron.config import discover_skill_dirs

    dirs = discover_skill_dirs()
    if dirs:
        return dirs
    return [Path.home() / ".claude" / "skills"]


def _first_fire_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or os.environ.get("TMPDIR") or "/tmp"
    return Path(base) / "mega-tron"


def _first_fire_marker(session_id: str | None) -> Path | None:
    """Per-session marker. Prefixed ``claude-`` so Claude + Codex sessions
    with the same id (unlikely but possible) don't collide."""
    if not session_id or not isinstance(session_id, str):
        return None
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_") or "anon"
    return _first_fire_dir() / f"seen-claude-{safe}"


def _is_first_fire(session_id: str | None) -> bool:
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
    from mega_tron._env import resolve_mode

    return resolve_mode(None) == "agentic"


def _make_embedder() -> object:
    kind = os.environ.get("MEGA_HOOK_EMBEDDER", "").lower().strip()
    if kind == "openai":
        from mega_tron.embedders.openai import OpenAIEmbedder

        return OpenAIEmbedder()
    if kind == "voyage":
        from mega_tron.embedders.voyage import VoyageEmbedder

        return VoyageEmbedder()
    from mega_tron.embedder import make_embedder

    return make_embedder()


def _emit_empty() -> int:
    return 0


def _emit_additional_context(additional_context: str) -> int:
    out = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": additional_context,
        }
    }
    json.dump(out, sys.stdout)
    return 0


def _native_mode() -> str:
    """Return ``"strict"``, ``"active"``, or ``"passive"`` based on
    ``MEGA_CLAUDE_NATIVE_MODE``.

    Three escalating levels of native-catalog suppression:

    - ``passive`` (default): leave ``skillOverrides`` alone. Native catalog
      shows all installed skill descriptions; we overlay the top-K via
      ``additionalContext``. Two catalogs coexist; the model picks.
    - ``active``: rewrite ``~/.claude/settings.local.json`` ``skillOverrides``
      each turn so non-top-K skills become ``"name-only"`` in the native
      catalog. Router owns description visibility; ``/skillname`` slash
      invocation still works for every skill (the Skill tool is alive).
    - ``strict``: same per-turn ``skillOverrides`` rewrite as ``active``
      *plus* a Claude shell wrapper that adds ``--disallowedTools Skill``
      to every ``claude`` invocation. The native catalog and the Skill
      tool both disappear; only mega-tron's ``additionalContext`` block
      surfaces skills. Maximum token saving; minimum fallback for routing
      misses. ``setup`` installs the wrapper into the user's shell rc
      when the user picks this level — see
      :func:`mega_tron.hosts.claude_code.install._install_claude_wrapper`.

    The hook treats ``active`` and ``strict`` the same way (both trigger
    the same ``apply_mode_a`` write); the difference between them is
    install-time only (the shell wrapper).
    """
    val = os.environ.get("MEGA_CLAUDE_NATIVE_MODE", "").strip().lower()
    if val == "strict":
        return "strict"
    if val == "active":
        return "active"
    return "passive"


def _apply_native_mode_a(
    top_picks: list, skills_dirs: list[Path]
) -> None:
    """Update ``skillOverrides`` to downgrade non-top-K skills.

    Best-effort: any failure (read-only fs, malformed JSON) is logged
    and swallowed — Mode A is an optimization, never a hard requirement.
    """
    try:
        from mega_tron.hosts.claude_code.skill_overrides import apply_mode_a
        from mega_tron.router import load_skills

        all_skills = load_skills(skills_dirs)
        all_names = [s.name for s in all_skills]
        top_names = [rs.skill.name for rs in top_picks]
        n = apply_mode_a(top_names, all_skill_names=all_names)
        print(
            f"[mega-tron claude-hook] mode A: set {n} non-top-K skills "
            "to name-only in settings.local.json",
            file=sys.stderr,
        )
    except Exception as e:  # noqa: BLE001
        print(
            f"[mega-tron claude-hook] mode A skip: {e}",
            file=sys.stderr,
        )


def cmd_claude_hook(args: argparse.Namespace) -> int:
    """Read Claude Code hook JSON from stdin, write hook-output JSON to stdout."""
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as e:
        print(f"[mega-tron claude-hook] invalid input JSON: {e}", file=sys.stderr)
        return 0

    event_name = data.get("hook_event_name") or data.get("hookEventName")
    if event_name and event_name != "UserPromptSubmit":
        return _emit_empty()

    prompt = data.get("prompt", "")
    if not isinstance(prompt, str) or not prompt.strip():
        return _emit_empty()

    session_id = data.get("session_id") or data.get("sessionId")
    if not _is_first_fire(session_id):
        return _emit_empty()

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
            f"[mega-tron claude-hook] skills dir not found "
            f"(checked: {checked}), skipping route",
            file=sys.stderr,
        )
        return _emit_empty()
    skills_dirs = existing_dirs

    # ---- Daemon-first path -------------------------------------------------
    # The daemon protocol still takes a single ``skills_dir`` per query,
    # so we send the top-priority root. The in-process fallback below
    # consumes the full union; daemon multi-dir support is tracked
    # separately.
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
                # Claude-flavored output: daemon may not honor this yet
                # (Stage 1 still calls the Codex prepender server-side);
                # we re-render on the client side below if needed.
                "target": "claude",
            }
        )
    if daemon_response and daemon_response.get("ok"):
        ctx = daemon_response.get("additional_context") or ""
        if not ctx.strip():
            return _emit_empty()
        # Mode A (active/strict): daemon already returned top-K names —
        # apply skillOverrides downgrade against the full skill pool.
        # strict adds the shell wrapper on top (install-time concern;
        # the per-turn skillOverrides write is identical).
        if _native_mode() in ("active", "strict"):
            try:
                from mega_tron.hosts.claude_code.skill_overrides import apply_mode_a
                from mega_tron.router import load_skills

                all_names = [s.name for s in load_skills(skills_dirs)]
                top_names = daemon_response.get("skills") or []
                if top_names:
                    n = apply_mode_a(top_names, all_skill_names=all_names)
                    print(
                        f"[mega-tron claude-hook] mode A (daemon): "
                        f"set {n} non-top-K skills to name-only",
                        file=sys.stderr,
                    )
            except Exception as e:  # noqa: BLE001
                print(
                    f"[mega-tron claude-hook] mode A (daemon) skip: {e}",
                    file=sys.stderr,
                )
        return _emit_additional_context(ctx)

    if not daemon_mod.daemon_disabled() and not daemon_mod.is_running():
        # Tell the user what's happening so a slow first call (embedder
        # cold-load + skill embedding sync, typically 5-30s on a cold
        # cache) isn't mistaken for a hang. Daemon spawns in the
        # background; the next session will be fast.
        if not os.environ.get("MEGA_QUIET"):
            print(
                "[mega-tron claude-hook] router daemon not running — this "
                "turn pays the embedder cold-load (~5-30s on first call). "
                "Spawning daemon in background so the next session routes "
                "in ~50ms.",
                file=sys.stderr,
            )
        daemon_mod.spawn_detached()

    from mega_tron.cache import Cache
    from mega_tron.prepender import build_claude_hook_context
    from mega_tron.router import Router

    embedder = _make_embedder()
    cache = Cache(path=cache_path)
    router = Router(skills_dir=skills_dirs, embedder=embedder, cache=cache)

    try:
        router.warmup_if_stale()
    except Exception as e:  # noqa: BLE001
        print(
            f"[mega-tron claude-hook] warmup_if_stale failed: {e}",
            file=sys.stderr,
        )
        return _emit_empty()

    agentic = None
    if _agentic_enabled():
        try:
            from mega_tron.agentic import AgenticSearch
            from mega_tron.llm_backends import make_llm_backend

            agentic = AgenticSearch(backend=make_llm_backend())
        except Exception as e:  # noqa: BLE001
            print(
                f"[mega-tron claude-hook] agentic init failed, falling "
                f"back to cosine: {e}",
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
        print(f"[mega-tron claude-hook] rank failed: {e}", file=sys.stderr)
        return _emit_empty()

    # Best-effort route log (Phase 2: dashboard measurement). Logging
    # the empty/dropped case is fine — the Context Savings tab uses the
    # distribution including zero-K turns. Errors here MUST NOT affect
    # routing.
    try:
        _log_route_claude(prompt, ranked, router, session_id=session_id)
    except Exception:  # noqa: BLE001
        pass

    if not ranked:
        return _emit_empty()

    # Mode A (active/strict): shrink the native catalog to just our top-K
    # before emitting. strict additionally relies on a shell wrapper —
    # installed by `mega-tron setup --claude-native-mode strict` — that
    # adds `--disallowedTools Skill` on every `claude` invocation, but
    # the per-turn skillOverrides rewrite is the same.
    if _native_mode() in ("active", "strict"):
        _apply_native_mode_a(ranked[: args.prepend_k], skills_dirs)

    ctx = build_claude_hook_context(ranked, k=args.prepend_k)
    if not ctx.strip():
        return _emit_empty()

    return _emit_additional_context(ctx.rstrip())


def _log_route_claude(prompt, ranked, router, *, session_id) -> None:
    """Write one row to the ``routes`` analytics table for a Claude turn.

    See ``mega_tron.hosts.codex.hook._log_route`` for the rationale —
    we keep these tiny helpers per-host so each hook stays self-contained,
    even though the body is mechanical. The host name is the long
    convention ``"claude_code"`` so it lines up with the verdict-host
    column already in the store.
    """
    import hashlib

    from mega_tron.config import store_path
    from mega_tron.verdicts.store import Store

    k, k_reason = router.last_dynamic or (len(ranked), "manual")
    total_tok = sum(r.skill.desc_tok for r in ranked)
    qhash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
    Store(path=store_path()).record_route(
        session_id=session_id,
        host="claude_code",
        query_hash=qhash,
        picked_names=[r.skill.name for r in ranked],
        total_tok=total_tok,
        k=k,
        k_reason=k_reason,
    )
