"""`mega-tron install` — dispatcher for per-host installers.

Routes the `--target` flag to one of the host installer entry points
(`mega_tron.hosts.<host>.install`). Before doing so, scans the user's
home directory for sentinel blocks left by an earlier installer
generation and refuses to run if any conflicting blocks are found
(unless `--force` is passed), since side-by-side installs would
double-fire every hook.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

# ---- Legacy sentinel detection ----
# Sentinels left by earlier installer generations may still live in the
# user's home (AGENTS.md, CLAUDE.md, shell rc, codex config.toml). We
# detect them so a new install never lands on top of a stale hook
# block. Inactive-generation markers trigger an auto-cleanup pass;
# the immediately-prior generation's markers refuse install unless
# --force is given, because that binary may still be on the user's
# PATH and would double-fire.

_LEGACY_MEGA_OPTIMUS_MARKERS: tuple[str, ...] = (
    # Shell rc / config.toml sentinel
    "# >>> mega-optimus (managed; edit between sentinels at your own risk) >>>",
    # AGENTS.md / CLAUDE.md sentinel
    "<!-- >>> mega-optimus (managed; edit between sentinels at your own risk) >>> -->",
    # Codex hook-trust sentinel (separate block in config.toml)
    "# >>> mega-optimus hook-trust (managed) >>>",
    # JSON managed-key marker stamped into hook entries
    "_mega_optimus_managed",
    # Bare PyPI/CLI name in hook command strings
    "mega-optimus claude-hook",
    "mega-optimus claude-stop-hook",
    "mega-optimus hook",
    "mega-optimus stop-hook",
    "mega-optimus gemini-hook",
    "mega-optimus gemini-stop-hook",
)

# mega-skill-router markers. The binary itself is long gone from
# systems we've seen; the leftovers are dead AGENTS.md guidance blocks
# pointing at a `mega-skill-router search ...` command that resolves
# to "command not found". Auto-stripped on install — no manual
# remediation needed.
_LEGACY_MEGA_SKILL_ROUTER_BLOCKS: tuple[tuple[str, str], ...] = (
    # (start_sentinel, end_sentinel) pairs. Stripping is inclusive of
    # both sentinels and everything between them.
    (
        "<!-- >>> mega-skill-router (managed; edit between sentinels at your own risk) >>> -->",
        "<!-- <<< mega-skill-router <<< -->",
    ),
    (
        "# >>> mega-skill-router (managed; edit between sentinels at your own risk) >>>",
        "# <<< mega-skill-router <<<",
    ),
)

# Backwards-compat alias so any test or extension importing
# `_LEGACY_MARKERS` still works.
_LEGACY_MARKERS = _LEGACY_MEGA_OPTIMUS_MARKERS


def _candidate_legacy_files() -> list[Path]:
    """Files we scan for any prior-generation sentinel."""
    home = Path.home()
    return [
        home / ".claude" / "settings.json",
        home / ".claude" / "CLAUDE.md",
        home / ".codex" / "config.toml",
        home / ".codex" / "AGENTS.md",
        home / ".codex" / "hooks.json",
        home / ".gemini" / "settings.json",
        home / ".gemini" / "GEMINI.md",
        home / ".zshrc",
        home / ".bashrc",
        home / ".bash_profile",
        home / ".profile",
    ]


def _legacy_megaoptimus_files() -> list[Path]:
    """Return list of user-home files that still contain mega-optimus
    sentinels / hook commands planted by an earlier install."""
    hits: list[Path] = []
    for path in _candidate_legacy_files():
        if not path.exists() or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(marker in text for marker in _LEGACY_MEGA_OPTIMUS_MARKERS):
            hits.append(path)
    return hits


def _strip_legacy_mega_skill_router_blocks() -> list[Path]:
    """Auto-remove mega-skill-router managed blocks from user configs.
    The binary is no longer installed on systems we've seen; the
    leftover blocks point at a command that resolves to ``not found``
    and just confuse the model when AGENTS.md is rendered into the
    system prompt.

    Returns the list of files we actually edited so the caller can report
    the cleanup.
    """
    edited: list[Path] = []
    for path in _candidate_legacy_files():
        if not path.exists() or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        new_text = text
        changed = False
        for start, end in _LEGACY_MEGA_SKILL_ROUTER_BLOCKS:
            while start in new_text and end in new_text:
                s = new_text.index(start)
                e = new_text.index(end, s) + len(end)
                # Also swallow the trailing newline immediately after the
                # closing sentinel so we don't leave a stray blank line.
                if e < len(new_text) and new_text[e] == "\n":
                    e += 1
                new_text = new_text[:s] + new_text[e:]
                changed = True
        if changed:
            try:
                path.write_text(new_text, encoding="utf-8")
                edited.append(path)
            except OSError:
                # Best-effort cleanup — never block the install on this.
                pass
    return edited


def _apply_profile_choice(cli_profile: str | None) -> None:
    """Resolve the embedder profile from `--profile` + TTY prompt and
    persist the model id to ``config.toml`` so the per-host warmup
    that runs immediately after picks it up.

    No-op when the user has already set a non-default embedder via
    ``mega-tron embedder set``; that choice always wins on a re-run
    of ``mega-tron setup``.
    """
    from mega_tron.cli.profile_picker import resolve_profile
    from mega_tron.config import (
        Config,
        DEFAULT_EMBEDDER_MODEL,
        set_embedder_model,
    )

    current = Config.load().embedder_model
    config_is_default = current == DEFAULT_EMBEDDER_MODEL

    chosen = resolve_profile(
        cli_profile,
        config_is_default=config_is_default,
    )
    if chosen is None:
        # Either the user skipped, no TTY, or they already have a
        # custom embedder. Leave config alone.
        if not config_is_default:
            print(
                f"[setup] keeping current embedder: {current}",
                file=sys.stderr,
            )
        return

    if chosen.model_id == current:
        print(
            f"[setup] embedder profile = {chosen.key} ({chosen.model_id}) — "
            "already configured.",
            file=sys.stderr,
        )
        return

    set_embedder_model(chosen.model_id)
    print(
        f"[setup] embedder profile = {chosen.key} → {chosen.model_id}",
        file=sys.stderr,
    )


def cmd_install(args: argparse.Namespace) -> int:
    """Install dispatcher: routes to the appropriate installer based on --target.

    Targets:
      - auto   (default): install for every host detected on this
                machine (``~/.codex/``, ``~/.claude/``, ``~/.gemini/``
                directories or the matching CLI binaries on ``PATH``).
                Falls back to ``all`` if nothing is detected so the
                install still leaves a usable system.
      - codex: Codex CLI shell wrapper + hooks.json + AGENTS.md.
      - claude: Claude Code settings.json hooks + CLAUDE.md.
      - gemini: Gemini CLI settings.json hooks + GEMINI.md.
      - both:   codex + claude (legacy two-host install, preserved for
                back-compat).
      - all:    codex + claude + gemini (force all three).

    Each target is independent — installing one doesn't disturb another,
    and ``--uninstall`` only undoes the chosen target.

    Refuses to run when a legacy mega-optimus installation is detected,
    unless ``--force`` is passed — side-by-side installs would
    double-fire every hook.
    """
    # Step 0 — PATH auto-update. `uv tool install mega-tron` drops the
    # binary into ~/.local/bin, which isn't on $PATH by default on
    # macOS / Linux. Without this step the very next host install would
    # spawn a hook subprocess that can't resolve `mega-tron`. We do this
    # first, before anything else, so even hook-trust stamping (Codex)
    # which embeds the absolute hook command path is stable across
    # future shells.
    from mega_tron.cli.path_setup import ensure_on_path, remove_from_path

    if getattr(args, "uninstall", False):
        # Strip on uninstall (best-effort; never blocks the per-host
        # uninstall dispatch below).
        try:
            remove_from_path()
        except Exception as e:  # noqa: BLE001
            print(
                f"[install] PATH cleanup skipped: {e}",
                file=sys.stderr,
            )
    else:
        try:
            ensure_on_path()
        except Exception as e:  # noqa: BLE001
            print(
                f"[install] PATH auto-update skipped ({e}); add the "
                "directory containing `mega-tron` to your shell PATH "
                "manually so hook subprocesses can find it.",
                file=sys.stderr,
            )

    # Step 1 — auto-strip mega-skill-router leftovers. The binary is
    # gone on every system we've seen; the blocks just confuse the
    # model. Safe to do unconditionally — never blocks the install.
    if not getattr(args, "uninstall", False):
        stripped = _strip_legacy_mega_skill_router_blocks()
        for path in stripped:
            print(
                f"[install] removed stale mega-skill-router block from {path}",
                file=sys.stderr,
            )

    if not getattr(args, "uninstall", False) and not getattr(args, "force", False):
        legacy = _legacy_megaoptimus_files()
        if legacy:
            paths = "\n  ".join(str(p) for p in legacy)
            print(
                "error: legacy mega-optimus installation detected.\n\n"
                "  These files still contain mega-optimus hook blocks "
                "or sentinels:\n  "
                f"{paths}\n\n"
                "  mega-tron will not install alongside mega-optimus — "
                "every prompt would fire two hook implementations in "
                "parallel. Remove the legacy install first:\n\n"
                "    pip install mega-optimus\n"
                "    mega-optimus install --uninstall\n"
                "    pip uninstall mega-optimus\n\n"
                "  Then re-run `mega-tron install`. Pass --force to "
                "this command to bypass this check (advanced; expect "
                "duplicate hook fires).",
                file=sys.stderr,
            )
            return 2

    # Step 2 — embedder profile picker. Runs before any host installer
    # dispatches so the per-host warmup loads the user-chosen model. The
    # picker is a no-op in three cases: --uninstall (we're removing,
    # not configuring), --print-only (read-only mode), and when the
    # user has already picked a non-default embedder via
    # `mega-tron embedder set` (we never stomp on explicit choices).
    if (
        not getattr(args, "uninstall", False)
        and not getattr(args, "print_only", False)
    ):
        try:
            _apply_profile_choice(getattr(args, "profile", "auto"))
        except Exception as e:  # noqa: BLE001
            # Profile selection is a convenience layer — never block install.
            print(
                f"[install] profile picker skipped: {e}",
                file=sys.stderr,
            )

    target = getattr(args, "target", "auto")
    wired_hosts: list[str] = []
    if target == "auto":
        from mega_tron.hosts import detect_hosts

        detected = detect_hosts()
        if not detected:
            print(
                "[install] no hosts detected on this machine "
                "(no ~/.codex, ~/.claude, ~/.gemini and "
                "none of `codex`/`claude`/`gemini` on PATH); "
                "falling back to --target all so nothing is silently "
                "skipped. Pass an explicit --target to override.",
                file=sys.stderr,
            )
            detected = ["codex", "claude", "gemini"]
        print(
            f"[install] detected hosts: {', '.join(detected)}",
            file=sys.stderr,
        )
        rc = _install_targets(detected, args)
        wired_hosts = detected
    elif target == "claude":
        from mega_tron.hosts.claude_code.install import run_install_claude

        rc = run_install_claude(args)
        wired_hosts = ["claude"]
    elif target == "gemini":
        from mega_tron.hosts.gemini_cli.install import run_install_gemini

        rc = run_install_gemini(args)
        wired_hosts = ["gemini"]
    elif target == "both":
        rc = _install_targets(["codex", "claude"], args)
        wired_hosts = ["codex", "claude"]
    elif target == "all":
        rc = _install_targets(["codex", "claude", "gemini"], args)
        wired_hosts = ["codex", "claude", "gemini"]
    else:
        # Explicit "codex".
        from mega_tron.hosts.codex.install import run_install

        rc = run_install(args)
        wired_hosts = ["codex"]

    # On a successful install, drop a marker so the "you haven't run
    # setup" nudge in `parser._warn_if_unwired` stops firing.
    # `--uninstall` removes the marker so a future re-install nudge
    # re-arms cleanly.
    try:
        from mega_tron.config import data_dir
        marker = data_dir() / ".setup-done"
        if getattr(args, "uninstall", False):
            if marker.exists():
                marker.unlink()
        elif rc == 0:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("ok\n", encoding="utf-8")
    except Exception:
        # Best-effort; never block install on the marker write.
        pass

    # Show the user a skill inventory + closing summary at the end of a
    # successful setup so they immediately see how many skills mega-tron
    # is now routing across (typically far more than they expect once
    # host bundles + ~/.agents + ~/.hermes are counted together) and
    # exactly which host CLIs were wired this run.
    if rc == 0 and not getattr(args, "uninstall", False):
        if not os.environ.get("MEGA_QUIET"):
            try:
                _print_skill_inventory(wired_hosts=wired_hosts)
            except Exception:
                # Best-effort; never break setup on inventory failure.
                pass
        # Warm the daemon in the background so the user's very first
        # host session lands on the fast path (~50ms) instead of paying
        # the embedder cold-load on first hook fire. The daemon detaches
        # via ``setsid`` so setup returns immediately; if the spawn
        # fails for any reason the lazy-spawn path in each host hook
        # still kicks in on first miss — this is purely an optimisation.
        _warm_daemon_on_setup()

        # Optional end-to-end self-check. Plants a ``_mega-tron-check``
        # skill into each wired host, drives one non-interactive call
        # per host, verifies the inline verdict tag reaches SQLite, and
        # launches the dashboard in the background. Strictly opt-in
        # behind ``--qa-live`` because it spends provider quota and
        # assumes the user has already auth'd each host CLI.
        if getattr(args, "qa_live", False) and wired_hosts:
            try:
                from mega_tron.cli.qa_live import run_qa_live

                run_qa_live(wired_hosts)
            except Exception as e:  # noqa: BLE001
                if not os.environ.get("MEGA_QUIET"):
                    print(
                        f"[setup] --qa-live skipped ({e})",
                        file=sys.stderr,
                    )

    return rc


def _warm_daemon_on_setup() -> None:
    """Spawn the daemon AND force its embedder cold-load to finish before
    setup returns.

    Why synchronous: ``spawn_detached`` only opens the AF_UNIX socket —
    the embedder + skill embeddings still cold-load lazily on the first
    rank request. On Gemini that first request comes from the
    BeforeAgent hook, which has a hard **60-second timeout**. A 130–570
    MB embedder cold-load routinely exceeds 60 s on a fresh laptop, so
    the hook times out, no route row is logged for that session, and
    the AfterAgent Stop hook then can't admit the verdict (no routed
    catalog to gate on → legacy fallback → claimed_use reject → no
    SQLite row). Codex / Claude have looser timeouts so they limp
    through; Gemini doesn't.

    Pre-warming the embedder once at setup time costs the user the
    same 20–30 s they'd otherwise pay on their first turn, but in a
    place where they're already waiting on `setup` to finish. After
    this returns, every host's first turn routes in ~50 ms.

    Skipped when:
      - ``MEGA_DAEMON=0`` (user explicitly disabled the daemon path)
      - A daemon is already running on the per-UID socket
      - ``spawn_detached`` returns ``None`` (sandboxed CI etc.)
      - The warm-up rank call fails for any reason — non-fatal,
        first hook fire will pay the cold-load instead.
    """
    try:
        from mega_tron import daemon as daemon_mod
    except Exception:  # noqa: BLE001
        return

    try:
        if daemon_mod.daemon_disabled():
            return
        if daemon_mod.is_running():
            if not os.environ.get("MEGA_QUIET"):
                print(
                    "[setup] daemon already running — skipped warm-up.",
                    file=sys.stderr,
                )
            return
        pid = daemon_mod.spawn_detached()
    except Exception as e:  # noqa: BLE001
        # Spawn failure is non-fatal — the first hook fire will retry
        # via the lazy-spawn path that's been in place all along.
        if not os.environ.get("MEGA_QUIET"):
            print(
                f"[setup] daemon warm-up skipped ({e}); first host "
                "session will lazy-spawn it on demand.",
                file=sys.stderr,
            )
        return

    if pid is None:
        return

    quiet = bool(os.environ.get("MEGA_QUIET"))
    if not quiet:
        print(
            f"[setup] router daemon spawned (pid {pid}); pre-warming "
            "embedder so the first host turn doesn't pay the 60s "
            "Gemini-hook timeout window for the cold-load...",
            file=sys.stderr,
        )

    # Force the embedder cold-load by sending one real rank request.
    # Pick the first skill root we can find — Router needs a valid
    # skills_dir on the wire even though the prompt itself is generic.
    # cache_path MUST be passed; daemon's Cache.__init__ calls
    # ``with_suffix`` on it and an empty path crashes the worker.
    try:
        from mega_tron.config import discover_skill_dirs

        dirs = discover_skill_dirs()
        skills_dir = str(dirs[0]) if dirs else str(Path.home() / ".codex" / "skills")
    except Exception:  # noqa: BLE001
        skills_dir = str(Path.home() / ".codex" / "skills")

    try:
        from mega_tron.config import Config

        model_id = Config.load().embedder_model
        slug = model_id.replace("/", "_")
        cache_path = str(Path.home() / ".cache" / "mega-tron" / f"{slug}.npz")
    except Exception:  # noqa: BLE001
        cache_path = str(Path.home() / ".cache" / "mega-tron" / "default.npz")

    # Wait briefly for the socket to appear — spawn_detached returns
    # before the child has bound the AF_UNIX path. 2 s ceiling matches
    # the daemon's own startup grace.
    socket_path = daemon_mod.default_socket_path()
    deadline = time.monotonic() + 2.0
    while not socket_path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)

    started = time.monotonic()
    # response_timeout_s widened to 120s: cold loading bge-m3 (570 MB)
    # plus embedding a 2,975-skill catalog routinely takes 30–90 s on a
    # cold cache; the default 30s on client_query would race the
    # embedder. setup itself is allowed to be slow — that's the whole
    # point of doing this work here instead of inside a 60s host hook.
    response = daemon_mod.client_query(
        {
            "op": "rank",
            "prompt": "mega-tron setup warmup probe",
            "skills_dir": skills_dir,
            "cache_path": cache_path,
            "top_k": 1,
            "prepend_k": 1,
        },
        response_timeout_s=120.0,
    )
    elapsed = time.monotonic() - started

    if response and response.get("ok"):
        if not quiet:
            print(
                f"[setup] embedder warmed up in {elapsed:.1f}s; first "
                "host turn now routes in ~50ms.",
                file=sys.stderr,
            )
    else:
        # Warm-up didn't complete (timeout, fresh embedder still
        # downloading, etc.). Non-fatal — the first hook fire will
        # finish the cold-load, just at the cost of the first turn.
        if not quiet:
            print(
                f"[setup] embedder warm-up didn't finish in {elapsed:.1f}s "
                "(model may still be downloading); your first host turn "
                "will pay the cold-load. Re-run `mega-tron qa-live` once "
                "the model is on disk.",
                file=sys.stderr,
            )


def _install_targets(hosts: list[str], args: argparse.Namespace) -> int:
    """Sequentially install for the given hosts, short-circuiting on
    the first non-zero rc. Shared by ``--target auto/all/both``."""
    for host in hosts:
        if host == "codex":
            from mega_tron.hosts.codex.install import run_install as fn
        elif host == "claude":
            from mega_tron.hosts.claude_code.install import (
                run_install_claude as fn,
            )
        elif host == "gemini":
            from mega_tron.hosts.gemini_cli.install import (
                run_install_gemini as fn,
            )
        else:
            print(f"[install] unknown host {host!r}", file=sys.stderr)
            return 2
        rc = fn(args)
        if rc != 0:
            return rc
    return 0


_HOST_LABELS: dict[str, str] = {
    "codex": "Codex",
    "claude": "Claude Code",
    "gemini": "Gemini CLI",
}


def _print_skill_inventory(*, wired_hosts: list[str] | None = None) -> None:
    """Print a per-directory skill count summary at the end of setup.

    Walks every directory that :func:`discover_skill_dirs` returns
    (Codex, Claude, Gemini, Hermes, ~/.agents, $CODEX_HOME, user-added
    extra_dirs, MEGA_SKILL_DIRS) and reports two numbers per row:

      - the raw count of valid ``SKILL.md`` files in that directory
      - the count contributed to the deduped union (first-dir-wins)

    The total at the bottom is the deduped union size — the number of
    distinct skills mega-tron will actually route across. Dedup count
    is shown separately so users see why simple summation doesn't
    match the total.

    When ``wired_hosts`` is provided, a closing line names every host
    CLI mega-tron now routes for, so the user finishes setup with a
    clear single-sentence statement of what just happened.
    """
    from mega_tron.config import discover_skill_dirs
    from mega_tron.router import load_skills

    dirs = [Path(p) for p in discover_skill_dirs()]
    if not dirs:
        return

    seen: set[str] = set()
    rows: list[tuple[str, int, int]] = []  # (label, raw_count, unique_added)
    for d in dirs:
        if not d.exists():
            continue
        try:
            skills = load_skills([d])
        except Exception:
            continue
        raw = len(skills)
        added = 0
        for s in skills:
            if s.name not in seen:
                seen.add(s.name)
                added += 1
        # Render path with ~ replacement for brevity.
        home = str(Path.home())
        label = str(d)
        if label.startswith(home):
            label = "~" + label[len(home):]
        rows.append((label, raw, added))

    if not rows:
        return

    total_raw = sum(r[1] for r in rows)
    total_unique = len(seen)
    duplicates = total_raw - total_unique

    label_width = max(len(r[0]) for r in rows)
    label_width = max(label_width, len("total unique"))

    print("", file=sys.stderr)
    print("[setup] skill inventory:", file=sys.stderr)
    for label, raw, added in rows:
        # `added` shows how many *new* skills this dir contributed
        # after first-dir-wins dedup. Only print it when it differs
        # from raw to keep the common case (no collisions) uncluttered.
        if added == raw:
            suffix = ""
        else:
            shadowed = raw - added
            suffix = f"  ({shadowed} shadowed by earlier dirs)"
        print(
            f"  {label:<{label_width}}  {raw:>4} skills{suffix}",
            file=sys.stderr,
        )
    print(f"  {'─' * label_width}  ─────────────", file=sys.stderr)
    if duplicates > 0:
        print(
            f"  {'total unique':<{label_width}}  {total_unique:>4} skills"
            f"  ({duplicates} duplicate name(s) deduped across dirs)",
            file=sys.stderr,
        )
    else:
        print(
            f"  {'total unique':<{label_width}}  {total_unique:>4} skills",
            file=sys.stderr,
        )
    print("", file=sys.stderr)

    # Closing summary: what mega-tron will do from this point on.
    if wired_hosts:
        labels = [_HOST_LABELS.get(h, h) for h in wired_hosts]
        if len(labels) == 1:
            hosts_phrase = labels[0]
        elif len(labels) == 2:
            hosts_phrase = f"{labels[0]} and {labels[1]}"
        else:
            hosts_phrase = ", ".join(labels[:-1]) + f", and {labels[-1]}"
        print(
            f"[setup] mega-tron will now unify skill routing and evaluation "
            f"across {len(wired_hosts)} host CLI"
            f"{'s' if len(wired_hosts) != 1 else ''} "
            f"({hosts_phrase}) over {total_unique} skill"
            f"{'s' if total_unique != 1 else ''}.",
            file=sys.stderr,
        )
        print("", file=sys.stderr)
