"""Long-lived router daemon — Unix-domain JSON-RPC over a single socket.

Interactive codex hooks otherwise pay a ~2-3 second tax per turn
because every call cold-loads PyTorch + the embedder. The daemon keeps
Router + Embedder + Cache warm in a single resident process, dropping
the per-call latency to ~50 ms.

Wire protocol (line-delimited JSON, one request per connection):

    request:  {"op": "rank", "prompt": "...", "skills_dir": "...",
               "top_k": 5, "prepend_k": 3}
    response: {"ok": true, "additional_context": "...", "skills": ["a","b"]}

    request:  {"op": "ping"}
    response: {"ok": true}

    request:  {"op": "shutdown"}
    response: {"ok": true}   # daemon then exits

The daemon stays alive until it receives an explicit shutdown
(``{"op": "shutdown"}`` or SIGTERM). Tests can pass an explicit
``idle_timeout_s`` to :func:`serve` for bounded runs, but production
never does — the user expectation is "once spawned, it lives until
the machine shuts down."

Failure semantics: clients fall back to the in-process path.
:func:`client_query` returns ``None`` for any of:

- socket file missing,
- connect timeout / refused,
- response timeout,
- protocol error (non-JSON, missing fields).

The hook always has an in-process fallback so a broken daemon never
breaks a codex turn.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

DEFAULT_IDLE_TIMEOUT_S = 1800  # 30 minutes
_CONNECT_TIMEOUT_S = 0.2
# The first rank call after spawn pays the router warmup (embedder
# load + skill embedding sync) inside the daemon process. 2 s wasn't
# enough — the client would time out and the caller would think the
# daemon was dead, falling back to its own cold-path. 30 s is well
# above the worst-case warmup we've measured (~20 s on a 3 K-skill
# pool with cold OS-page-cache) and still safely under any host
# hook timeout. Subsequent rank calls return in <50 ms regardless.
_RESPONSE_TIMEOUT_S = 30.0
_SOCKET_BACKLOG = 16

# How long a completed wisdom ignite stays "cached" for dedup. A second
# ignite of the same query within this window is a no-op: the iteration
# JSON is on disk, the wisdom skills are in the auto-discovered dir,
# nothing else to do. Configurable via MEGA_WISDOM_TTL_S (clamped to a
# 60s-1h range so a fat-finger env value can't disable dedup entirely
# or stall the loop forever).
_WISDOM_INFLIGHT_TTL_S_DEFAULT = 600
_WISDOM_INFLIGHT_TTL_MIN_S = 60
_WISDOM_INFLIGHT_TTL_MAX_S = 3600


def _wisdom_inflight_ttl_s() -> float:
    """Resolve the wisdom dedup TTL from env, clamped to a safe range."""
    raw = os.environ.get("MEGA_WISDOM_TTL_S", "").strip()
    if not raw:
        return float(_WISDOM_INFLIGHT_TTL_S_DEFAULT)
    try:
        v = float(raw)
    except ValueError:
        return float(_WISDOM_INFLIGHT_TTL_S_DEFAULT)
    return max(_WISDOM_INFLIGHT_TTL_MIN_S, min(_WISDOM_INFLIGHT_TTL_MAX_S, v))


def _wisdom_query_key(query: str) -> str:
    """Stable, short dedup key for the in-flight pool. Not security-sensitive."""
    return hashlib.sha1(query.encode("utf-8")).hexdigest()[:16]


def default_socket_path() -> Path:
    """Per-UID AF_UNIX path. XDG_RUNTIME_DIR preferred, falls back to TMPDIR."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime) if runtime else Path(os.environ.get("TMPDIR", "/tmp"))
    return base / f"mega-tron-{os.getuid()}.sock"


def default_pid_path() -> Path:
    return Path.home() / ".cache" / "mega-tron" / "daemon.pid"


def daemon_disabled() -> bool:
    """``MEGA_DAEMON=0`` disables the daemon path entirely (hook stays in-proc)."""
    raw = os.environ.get("MEGA_DAEMON", "").strip().lower()
    return raw in ("0", "false", "no", "off")


# ---------------------------------------------------------------------------
# Client (the hook's read side)
# ---------------------------------------------------------------------------


def client_query(
    request: dict,
    socket_path: Path | None = None,
    connect_timeout_s: float = _CONNECT_TIMEOUT_S,
    response_timeout_s: float = _RESPONSE_TIMEOUT_S,
) -> dict | None:
    """Send one request, read one response. Returns ``None`` on any failure.

    Callers should treat ``None`` as "daemon unavailable — use in-process path".
    """
    path = Path(socket_path) if socket_path else default_socket_path()
    if not path.exists():
        return None
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(connect_timeout_s)
    try:
        s.connect(str(path))
    except (OSError, socket.timeout):
        s.close()
        return None
    try:
        s.settimeout(response_timeout_s)
        payload = (json.dumps(request) + "\n").encode("utf-8")
        s.sendall(payload)
        s.shutdown(socket.SHUT_WR)
        buf = bytearray()
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf.extend(chunk)
            if b"\n" in buf:
                break
        line = buf.split(b"\n", 1)[0].decode("utf-8", errors="replace")
        if not line:
            return None
        return json.loads(line)
    except (OSError, socket.timeout, json.JSONDecodeError):
        return None
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


@dataclass
class _DaemonState:
    """Lazy-initialized router holders so the first request also serializes
    on the model load — subsequent requests fly through.

    Also owns the wisdom-curator in-flight pool: maps a query hash to the
    ``(started_at, WisdomHandle)`` pair, so duplicate ignite requests for
    the same prompt within the TTL window collapse to a single subprocess.
    """
    last_request_ts: float = 0.0
    by_skills_dir: dict[str, object] = None  # type: ignore[assignment]
    wisdom_inflight: dict[str, tuple[float, object]] = field(default_factory=dict)
    wisdom_lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        if self.by_skills_dir is None:
            self.by_skills_dir = {}

    def get_router(self, skills_dir: Path, cache_path: Path, embedder_factory: Callable[[], object]):
        key = str(skills_dir.resolve()) + "|" + str(cache_path)
        router = self.by_skills_dir.get(key)
        if router is None:
            # Local import — keeps daemon module import-cheap so clients can
            # import this file without dragging PyTorch in.
            from mega_tron.cache import Cache
            from mega_tron.router import Router

            embedder = embedder_factory()
            cache = Cache(path=cache_path)
            router = Router(skills_dir=skills_dir, embedder=embedder, cache=cache)
            router.warmup()
            self.by_skills_dir[key] = router
        else:
            router.warmup_if_stale()
        return router

    def wisdom_check_and_fire(self, query: str) -> dict:
        """Dedup a wisdom-curator ignite against the in-flight pool, then fire if new.

        Returns a small status dict ({status, ...}). The hook treats this
        as fire-and-forget — it sends the request and ignores the reply
        beyond logging.

        Status values:
          * ``skipped``  — wisdom not enabled, or ``MEGA_WISDOM_CURATOR_PATH``
            unset. No fire.
          * ``cached``   — a recent ignite for this prompt already completed
            within the TTL window. The iteration JSON / wisdom skills are
            already on disk.
          * ``queued``   — a recent ignite for this prompt is still running.
            No re-fire; the existing subprocess will finish on its own.
          * ``fired``    — fresh subprocess spawned. Includes ``pid``.
          * ``error``    — ignite raised (config error). Includes ``error``.
        """
        # Lazy import — keeps the daemon module import cheap for clients.
        from .wisdom import ignite, is_enabled, resolve_curator_path

        if not is_enabled():
            return {"ok": True, "status": "skipped", "reason": "MEGA_WITH_WISDOM not set"}
        if resolve_curator_path() is None:
            return {"ok": True, "status": "skipped", "reason": "MEGA_WISDOM_CURATOR_PATH not set"}

        ttl = _wisdom_inflight_ttl_s()
        key = _wisdom_query_key(query)
        now = time.time()

        with self.wisdom_lock:
            # Garbage-collect stale entries (cheap; pool is small).
            stale = [k for k, (t, _h) in self.wisdom_inflight.items() if now - t > ttl]
            for k in stale:
                self.wisdom_inflight.pop(k, None)

            existing = self.wisdom_inflight.get(key)
            if existing is not None:
                started_at, handle = existing
                st = handle.status()  # type: ignore[attr-defined]
                if st == "in_flight":
                    return {
                        "ok": True,
                        "status": "queued",
                        "elapsed_s": round(now - started_at, 1),
                    }
                if st == "ready":
                    return {
                        "ok": True,
                        "status": "cached",
                        "age_s": round(now - started_at, 1),
                    }
                # "failed" / "no_result" — drop the dedup entry so we
                # can re-fire on the user's next attempt.
                self.wisdom_inflight.pop(key, None)

            try:
                handle = ignite(query, silent=True)
            except RuntimeError as e:
                return {"ok": False, "status": "error", "error": str(e)}
            self.wisdom_inflight[key] = (now, handle)
            return {"ok": True, "status": "fired", "pid": handle.proc.pid}  # type: ignore[attr-defined]


def _handle_request(
    request: dict,
    state: _DaemonState,
    embedder_factory: Callable[[], object],
) -> dict:
    """Dispatch a single request dict to a response dict."""
    op = request.get("op")
    if op == "ping":
        return {"ok": True, "version": _version()}
    if op == "shutdown":
        return {"ok": True, "shutdown": True}
    if op == "wisdom_ignite":
        # Wisdom is opt-in; dispatched by the hook on first-fire when
        # MEGA_WITH_WISDOM=1. Fire-and-forget from the client's perspective —
        # we still reply (the protocol is request/response) but the hook
        # doesn't depend on a "fired" status to continue.
        prompt = request.get("prompt") or ""
        if not isinstance(prompt, str) or not prompt.strip():
            return {"ok": False, "error": "empty prompt"}
        return state.wisdom_check_and_fire(prompt)
    if op not in ("rank", "agentic_rank", "find"):
        return {"ok": False, "error": f"unknown op {op!r}"}

    prompt = request.get("prompt") or ""
    if not isinstance(prompt, str) or not prompt.strip():
        return {"ok": False, "error": "empty prompt"}

    skills_dir = Path(request.get("skills_dir") or "")
    cache_path = Path(request.get("cache_path") or "")
    if not skills_dir.exists():
        return {"ok": False, "error": f"skills_dir {skills_dir} not found"}
    top_k = int(request.get("top_k", 5))
    prepend_k = int(request.get("prepend_k", 3))

    router = state.get_router(skills_dir, cache_path, embedder_factory)

    agentic = None
    if op in ("agentic_rank", "find"):
        try:
            from mega_tron.agentic import AgenticSearch
            from mega_tron.llm_backends import make_llm_backend

            agentic = AgenticSearch(backend=make_llm_backend())
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"agentic init failed: {e}"}

    try:
        ranked = router.rank(prompt, top_k=top_k, agentic=agentic)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"rank failed: {e}"}
    if not ranked:
        if op == "find":
            return {"ok": True, "skills": [], "bodies": []}
        return {"ok": True, "additional_context": "", "skills": []}

    if op == "find":
        bodies = []
        for r in ranked:
            md = r.skill.skill_dir / "SKILL.md"
            try:
                body = md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            bodies.append({"name": r.skill.name, "content": body})
        return {"ok": True, "skills": [b["name"] for b in bodies], "bodies": bodies}

    # Pick the prepender variant matching the target CLI. Claude Code
    # uses /skill-name slash invocation and a softer "candidate" directive
    # (no `$Name` must-use trigger). Codex (default) gets the must-use
    # line + meta block since Codex's catalog is replaced wholesale.
    target = request.get("target", "codex")
    if target == "claude":
        from mega_tron.prepender import build_claude_hook_context

        ctx = build_claude_hook_context(ranked, k=prepend_k)
    else:
        from mega_tron.prepender import build_hook_context

        ctx = build_hook_context(ranked, k=prepend_k)

    # The daemon backs the UserPromptSubmit hook, so we emit the full
    # hook-context shape. Persistent guidance lives in AGENTS.md / CLAUDE.md
    # (planted by `install`) and is not re-prepended every turn.
    additional_context = ctx.rstrip() if ctx.strip() else ""
    return {
        "ok": True,
        "additional_context": additional_context,
        "skills": [r.skill.name for r in ranked[:prepend_k]],
    }


def _version() -> str:
    from mega_tron import __version__

    return __version__


def _make_embedder() -> object:
    """Pick an embedder for the daemon. Mirrors :func:`hook._make_embedder`."""
    kind = os.environ.get("MEGA_HOOK_EMBEDDER", "").lower().strip()
    if kind == "openai":
        from mega_tron.embedders.openai import OpenAIEmbedder

        return OpenAIEmbedder()
    if kind == "voyage":
        from mega_tron.embedders.voyage import VoyageEmbedder

        return VoyageEmbedder()
    from mega_tron.embedder import make_embedder

    return make_embedder()


def serve(
    socket_path: Path | None = None,
    idle_timeout_s: float | None = None,
    embedder_factory: Callable[[], object] | None = None,
    log_prefix: str = "[mega-trond]",
    ready_event: "threading.Event | None" = None,
) -> int:
    """Run the daemon in the calling thread. Blocks until SIGTERM/shutdown.

    Args:
        socket_path: AF_UNIX path. Defaults to :func:`default_socket_path`.
        idle_timeout_s: ``None`` (default) means the daemon stays alive
            until an explicit shutdown signal. Pass an explicit float for
            tests that need a bounded run; production never sets this.
        embedder_factory: callable that returns an Embedder. Defaults to the
            same selection logic the hook uses (``MEGA_HOOK_EMBEDDER``).
        log_prefix: stderr prefix for daemon log lines.
        ready_event: optional :class:`threading.Event` that ``set()`` is
            called on once the socket is listening — lets tests wait without
            polling races between ``bind`` and ``listen``.

    Returns:
        Process exit code (0 = clean exit).
    """
    path = Path(socket_path) if socket_path else default_socket_path()
    factory = embedder_factory or _make_embedder
    state = _DaemonState()
    state.last_request_ts = time.monotonic()
    stop = threading.Event()

    # If an old socket exists we try to take it over: a quick ping decides
    # whether another daemon is alive, otherwise we unlink + bind.
    if path.exists():
        probe = client_query({"op": "ping"}, socket_path=path)
        if probe and probe.get("ok"):
            print(f"{log_prefix} another daemon is running at {path}", file=sys.stderr)
            return 1
        try:
            path.unlink()
        except OSError:
            pass

    path.parent.mkdir(parents=True, exist_ok=True)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    srv.listen(_SOCKET_BACKLOG)
    # With idle_timeout_s=None the daemon runs forever; we still want
    # the accept() loop to wake periodically so signal handlers (SIGTERM
    # etc.) actually fire — a blocking-forever socket wouldn't return
    # control to Python to process the signal. 30 s is a quiet poll
    # that costs effectively nothing.
    accept_poll_s = (
        30.0 if idle_timeout_s is None
        else min(30.0, max(1.0, idle_timeout_s / 4))
    )
    srv.settimeout(accept_poll_s)
    if ready_event is not None:
        ready_event.set()

    # Best-effort pid file.
    pid_path = default_pid_path()
    try:
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text(str(os.getpid()))
    except OSError:
        pid_path = None  # type: ignore[assignment]

    print(f"{log_prefix} listening on {path} (pid={os.getpid()})", file=sys.stderr)

    try:
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                # Idle check — only enforced when the caller passed an
                # explicit timeout. With idle_timeout_s=None (production
                # default) the daemon stays up until an explicit signal,
                # matching the user expectation that "once started, it
                # lives until the machine shuts down."
                if (
                    idle_timeout_s is not None
                    and time.monotonic() - state.last_request_ts > idle_timeout_s
                ):
                    print(
                        f"{log_prefix} idle {idle_timeout_s:.0f}s — exiting",
                        file=sys.stderr,
                    )
                    break
                continue
            except OSError:
                break
            try:
                _serve_one(conn, state, factory, stop, log_prefix)
            finally:
                conn.close()
    finally:
        try:
            srv.close()
        except OSError:
            pass
        try:
            path.unlink()
        except OSError:
            pass
        if pid_path is not None:
            try:
                pid_path.unlink()
            except OSError:
                pass
    return 0


def _serve_one(
    conn: socket.socket,
    state: _DaemonState,
    factory: Callable[[], object],
    stop: threading.Event,
    log_prefix: str,
) -> None:
    conn.settimeout(2.0)
    buf = bytearray()
    try:
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            buf.extend(chunk)
            if b"\n" in buf:
                break
        if not buf:
            return
        line = buf.split(b"\n", 1)[0].decode("utf-8", errors="replace")
        if not line.strip():
            return
        try:
            request = json.loads(line)
        except json.JSONDecodeError as e:
            response = {"ok": False, "error": f"invalid JSON: {e}"}
        else:
            try:
                response = _handle_request(request, state, factory)
            except Exception as e:  # noqa: BLE001
                response = {"ok": False, "error": f"server error: {e}"}
        conn.sendall((json.dumps(response) + "\n").encode("utf-8"))
        if response.get("shutdown"):
            print(f"{log_prefix} shutdown requested", file=sys.stderr)
            stop.set()
        state.last_request_ts = time.monotonic()
    except (OSError, socket.timeout):
        pass


# ---------------------------------------------------------------------------
# Spawn / status / stop helpers used by the hook + CLI
# ---------------------------------------------------------------------------


def spawn_detached() -> int | None:
    """Fork-exec ``mega-tron daemon serve`` as a detached process.

    Returns the child PID on success, or ``None`` if we can't spawn. The hook
    calls this on a cache MISS so the next turn lands on a warm daemon.

    The spawned daemon runs **without an idle timeout** — once warm it stays
    resident until the user reboots, stops it explicitly, or kills the
    process. The 30-minute idle exit that the CLI's `daemon serve` argparse
    default applies is the wrong behaviour for an auto-spawned daemon: when
    it fires, the *next* host turn pays the 20–30s embedder cold-load
    again, which on Gemini exceeds the host's 60s BeforeAgent hook timeout
    and silently drops the verdict for that session. Pinning the lifetime
    here keeps the router warm for the full uptime of the user's machine.
    """
    if daemon_disabled():
        return None
    try:
        import subprocess

        # `setsid` detaches from the controlling terminal; preexec_fn would
        # do the same but Python warns about thread safety. subprocess+start_new_session
        # is the modern equivalent.
        proc = subprocess.Popen(
            [
                sys.executable, "-m", "mega_tron.cli",
                "daemon", "serve", "--idle-timeout", "0",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        return proc.pid
    except (OSError, ImportError):
        return None


def is_running(socket_path: Path | None = None) -> bool:
    """Return True if a daemon answers a ping on the socket."""
    resp = client_query({"op": "ping"}, socket_path=socket_path)
    return bool(resp and resp.get("ok"))


def request_shutdown(socket_path: Path | None = None) -> bool:
    """Send a shutdown request. Returns whether the daemon acknowledged."""
    resp = client_query(
        {"op": "shutdown"},
        socket_path=socket_path,
        response_timeout_s=1.0,
    )
    return bool(resp and resp.get("ok"))
