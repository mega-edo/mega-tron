"""Gemini CLI ``BeforeAgent`` hook entry point.

Mirrors :mod:`mega_tron.hosts.claude_code.hook`, but consumes and
emits the Gemini CLI hook wire format.

Stdin JSON (from Gemini CLI's BeforeAgent event — see
https://geminicli.com/docs/hooks/reference/):

    {
      "session_id": "...",
      "transcript_path": "...",
      "cwd": "...",
      "hook_event_name": "BeforeAgent",
      "timestamp": "...",
      "prompt": "<the user's actual prompt>"
    }

Stdout JSON (Gemini's BeforeAgent envelope; ``additionalContext`` is
appended to the prompt for this turn only):

    {
      "hookSpecificOutput": {
        "hookEventName": "BeforeAgent",
        "additionalContext": "..."
      }
    }

Behaviour:
- First BeforeAgent of a session (per-session marker under
  ``$XDG_RUNTIME_DIR/mega-tron/seen-gemini-<session_id>``) → run the
  router and inject top-K context via ``additionalContext``.
- Subsequent turns → emit empty output. The persistent guidance lives in
  ``~/.gemini/GEMINI.md`` (planted by ``mega-tron install --target
  gemini`` in Stage 3); Gemini renders that into its system prompt every
  turn, so the hook only needs to fire once per session.
- Mode-A (``MEGA_GEMINI_MODE=active``, or unset/default): rewrite
  ``~/.gemini/settings.json``'s ``skills.disabled`` so non-top-K skills
  are hidden from Gemini's native catalog. Stage 1 calls the stub in
  :mod:`.skill_overrides`; Stage 3 wires it to the real writer.
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

    Routing is host-agnostic: a skill that lives in ``~/.claude/skills``
    or ``~/.codex/skills`` should still rank when the user is in a
    Gemini session, and a HELPFUL verdict accumulated under another
    host should keep boosting that skill on Gemini turns. We therefore
    pass the entire union down to :class:`Router`, which already
    handles multi-dir warmup (first-dir-wins on name collision)."""
    from mega_tron.config import discover_skill_dirs

    dirs = discover_skill_dirs()
    if dirs:
        return dirs
    return [Path.home() / ".gemini" / "skills"]


def _first_fire_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or os.environ.get("TMPDIR") or "/tmp"
    return Path(base) / "mega-tron"


def _first_fire_marker(session_id: str | None) -> Path | None:
    """Per-session marker. Prefixed ``gemini-`` so Codex / Claude / Gemini
    sessions with the same id (unlikely but possible) don't collide."""
    if not session_id or not isinstance(session_id, str):
        return None
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_") or "anon"
    return _first_fire_dir() / f"seen-gemini-{safe}"


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
            "hookEventName": "BeforeAgent",
            "additionalContext": additional_context,
        }
    }
    json.dump(out, sys.stdout)
    return 0


def _native_mode() -> str:
    """Return ``"active"`` or ``"passive"`` based on ``MEGA_GEMINI_MODE``.

    - ``active`` (default): rewrite ``~/.gemini/settings.json``'s
      ``skills.disabled`` each turn so non-top-K skills are hidden from
      Gemini's native catalog. Router fully owns description visibility.
    - ``passive``: leave ``skills.disabled`` alone. Native catalog
      shows all installed skill descriptions; we overlay the top-K via
      ``additionalContext``. Two catalogs coexist; the model picks.

    Note: Stage 1 ships Mode-A as a stub (no-op writer); Stage 3 wires
    it to the real ``apply_mode_a`` that mutates settings.json.
    """
    val = os.environ.get("MEGA_GEMINI_MODE", "").strip().lower()
    return "passive" if val == "passive" else "active"


def _apply_native_mode_a(top_picks: list, skills_dirs: list[Path]) -> None:
    """Update ``skills.disabled`` to hide non-top-K skills.

    Best-effort: any failure (read-only fs, malformed JSON, missing
    settings.json) is logged and swallowed — Mode A is an optimization,
    never a hard requirement.
    """
    try:
        from mega_tron.hosts.gemini_cli.skill_overrides import apply_mode_a
        from mega_tron.router import load_skills

        all_skills = load_skills(skills_dirs)
        all_names = [s.name for s in all_skills]
        top_names = [rs.skill.name for rs in top_picks]
        n = apply_mode_a(top_names, all_skill_names=all_names)
        if n > 0:
            print(
                f"[mega-tron gemini-hook] mode A: disabled {n} non-top-K "
                "skills in settings.json",
                file=sys.stderr,
            )
    except Exception as e:  # noqa: BLE001
        print(
            f"[mega-tron gemini-hook] mode A skip: {e}",
            file=sys.stderr,
        )


def cmd_gemini_hook(args: argparse.Namespace) -> int:
    """Read Gemini hook JSON from stdin, write hook-output JSON to stdout."""
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as e:
        print(f"[mega-tron gemini-hook] invalid input JSON: {e}", file=sys.stderr)
        return 0

    event_name = data.get("hook_event_name") or data.get("hookEventName")
    if event_name and event_name != "BeforeAgent":
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
            f"[mega-tron gemini-hook] skills dir not found "
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
                # Gemini-flavored output: daemon may not honor this yet
                # (Stage 1 still calls the Codex prepender server-side);
                # we re-render on the client side below if needed.
                "target": "gemini",
            }
        )
    if daemon_response and daemon_response.get("ok"):
        # If the daemon already rendered Gemini-shaped context, use it.
        # Otherwise re-render client-side from the returned ranked names.
        ctx = daemon_response.get("additional_context") or ""
        if ctx.strip() and daemon_response.get("target") == "gemini":
            chosen_ctx = ctx
        else:
            # Re-render client-side. We only have names from the daemon,
            # not full RankedSkill objects — fall back to a minimal
            # rendering. Stage 2+ may teach the daemon to emit
            # Gemini-shaped context directly.
            top_names = daemon_response.get("skills") or []
            if not top_names:
                return _emit_empty()
            from mega_tron.prepender import build_gemini_hook_context
            from mega_tron.router import RankedSkill, load_skills

            by_name = {s.name: s for s in load_skills(skills_dirs)}
            pseudo = [
                RankedSkill(skill=by_name[n], score=0.0)
                for n in top_names
                if n in by_name
            ]
            chosen_ctx = build_gemini_hook_context(pseudo, k=args.prepend_k)
        if not chosen_ctx.strip():
            return _emit_empty()
        # Mode A: daemon already returned top-K names — apply the disabled
        # list against the full skill pool.
        if _native_mode() == "active":
            try:
                from mega_tron.hosts.gemini_cli.skill_overrides import apply_mode_a
                from mega_tron.router import load_skills

                all_names = [s.name for s in load_skills(skills_dirs)]
                top_names = daemon_response.get("skills") or []
                if top_names:
                    n = apply_mode_a(top_names, all_skill_names=all_names)
                    if n > 0:
                        print(
                            f"[mega-tron gemini-hook] mode A (daemon): "
                            f"disabled {n} non-top-K skills",
                            file=sys.stderr,
                        )
            except Exception as e:  # noqa: BLE001
                print(
                    f"[mega-tron gemini-hook] mode A (daemon) skip: {e}",
                    file=sys.stderr,
                )
        return _emit_additional_context(chosen_ctx.rstrip())

    if not daemon_mod.daemon_disabled() and not daemon_mod.is_running():
        # Tell the user what's happening so a slow first call (embedder
        # cold-load + skill embedding sync, typically 5-30s on a cold
        # cache) isn't mistaken for the 60-second Gemini hook timeout.
        # The daemon spawns in the background; the next session will be
        # fast.
        if not os.environ.get("MEGA_QUIET"):
            print(
                "[mega-tron gemini-hook] router daemon not running — this "
                "turn pays the embedder cold-load (~5-30s on first call). "
                "Spawning daemon in background so the next session routes "
                "in ~50ms.",
                file=sys.stderr,
            )
        daemon_mod.spawn_detached()

    from mega_tron.cache import Cache
    from mega_tron.prepender import build_gemini_hook_context
    from mega_tron.router import Router

    embedder = _make_embedder()
    cache = Cache(path=cache_path)
    router = Router(skills_dir=skills_dirs, embedder=embedder, cache=cache)

    try:
        router.warmup_if_stale()
    except Exception as e:  # noqa: BLE001
        print(
            f"[mega-tron gemini-hook] warmup_if_stale failed: {e}",
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
                f"[mega-tron gemini-hook] agentic init failed, falling "
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
        print(f"[mega-tron gemini-hook] rank failed: {e}", file=sys.stderr)
        return _emit_empty()

    # Best-effort route log (Phase 2: dashboard measurement). Errors
    # here MUST NOT affect routing.
    try:
        _log_route_gemini(prompt, ranked, router, session_id=session_id)
    except Exception:  # noqa: BLE001
        pass

    if not ranked:
        return _emit_empty()

    # Mode A: shrink the native catalog to just our top-K before emitting.
    if _native_mode() == "active":
        _apply_native_mode_a(ranked[: args.prepend_k], skills_dirs)

    ctx = build_gemini_hook_context(ranked, k=args.prepend_k)
    if not ctx.strip():
        return _emit_empty()

    return _emit_additional_context(ctx.rstrip())


def _log_route_gemini(prompt, ranked, router, *, session_id) -> None:
    """Write one row to the ``routes`` analytics table for a Gemini turn."""
    import hashlib

    from mega_tron.config import store_path
    from mega_tron.verdicts.store import Store

    k, k_reason = router.last_dynamic or (len(ranked), "manual")
    total_tok = sum(r.skill.desc_tok for r in ranked)
    qhash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
    Store(path=store_path()).record_route(
        session_id=session_id,
        host="gemini_cli",
        query_hash=qhash,
        picked_names=[r.skill.name for r in ranked],
        total_tok=total_tok,
        k=k,
        k_reason=k_reason,
    )


__all__ = ["cmd_gemini_hook"]
