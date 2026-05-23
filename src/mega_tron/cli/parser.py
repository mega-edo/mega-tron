"""Argparse wiring for `mega-tron` CLI.

Contains the ``main()`` entry point and the entire argparse tree
(subparsers, flags, set_defaults binding cmd_* functions to each
subcommand). All actual cmd_* implementations live in their own
per-file modules under ``mega_tron.cli.*``; this file imports them
and connects them into the argparse plumbing.

The ``mega_tron.cli:main`` entry point declared in ``pyproject.toml``
resolves to ``cli/__init__.py:main``, which re-exports
:func:`main` from this module.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from mega_tron.cache import Cache
from mega_tron.hosts.codex.compat import detect_version, warn_if_untested
from mega_tron.config import (
    DEFAULT_PREFILTER,
    DEFAULT_TOP_K,
    Config,
    add_skill_dir,
    config_path,
    discover_skill_dirs,
    remove_skill_dir,
    set_embedder_model,
)
from mega_tron.embedder import make_embedder
from mega_tron.prepender import build_prefix
from mega_tron.router import Router
from mega_tron.stager import DEFAULT_BUDGET_TOK, Stager

from mega_tron.cli._common import (
    _add_cache_path,
    _build_agentic,
    _default_cache_path,
    _embedder_slug,
    _make_router,
    _maybe_warn_legacy_cache,
    _resolve_cache_path,
    _resolve_mode,
    _resolve_skills_dirs,
)
from mega_tron.cli.build_cache import cmd_build_cache
from mega_tron.cli.daemon import cmd_daemon
from mega_tron.cli.dashboard import cmd_dashboard
from mega_tron.cli.dirs import cmd_dirs
from mega_tron.cli.embedder_cmd import cmd_embedder
from mega_tron.cli.evaluate import cmd_evaluate
from mega_tron.cli.hooks import (
    cmd_claude_hook,
    cmd_claude_stop_hook,
    cmd_gemini_hook,
    cmd_gemini_stop_hook,
    cmd_hook,
    cmd_stop_hook,
)
from mega_tron.cli.install import (
    cmd_install,
)
from mega_tron.cli.maintenance import (
    cmd_compact_embeddings,
    cmd_compact_skills,
    cmd_export_frontmatter,
    cmd_migrate_to_sqlite,
    cmd_qa_live,
)
from mega_tron.cli.search import cmd_search
from mega_tron.cli.skills import cmd_skills
from mega_tron.cli.upgrade import cmd_upgrade
from mega_tron.cli.stats import cmd_stats
from mega_tron.cli.verdicts import cmd_regressions, cmd_search_verdicts
from mega_tron.cli.why import cmd_why


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mega-tron",
        description=(
            "Semantic skill routing for OpenAI Codex CLI.\n\n"
            "Public commands:\n"
            "  search    Pick skills for a task (semantic or agentic mode).\n"
            "  dirs      Manage the registered skill-root search path.\n"
            "  embedder  Show or set the default embedder model.\n"
            "  evaluate  Apply a JSON verdict batch to SKILL.md mega_meta.\n"
            "  why       Score-decomposition explainer for ranking.\n"
            "  stats     Per-skill helpful/harmful counters.\n"
            "  install   Inject codex hooks + shell wrapper.\n\n"
            "Internal commands `hook`, `stop-hook`, `daemon`, `build-cache` "
            "are invoked by codex / the shell wrapper — not by humans."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # ---- Internal: invoked by the install-time shell wrapper / hooks. ----
    p_build = sub.add_parser("build-cache", help=argparse.SUPPRESS)
    p_build.add_argument("--skills-dir", default=None, help="Directory containing skill subfolders.")
    _add_cache_path(p_build)
    p_build.set_defaults(func=cmd_build_cache)

    p_search = sub.add_parser(
        "search",
        help=(
            "Pick skills for a task. ``--output`` selects emission form "
            "(meta / names / bodies / table / stage). ``--mode`` selects "
            "between cosine-only semantic ranking (default) and an "
            "LLM-rerank agentic pipeline."
        ),
    )
    p_search.add_argument("task", help="Task description.")
    p_search.add_argument(
        "--skills-dir",
        default=None,
        help=(
            "Optional skill root override (comma-separated for multiple). "
            "Default: auto-discover ~/.claude/skills, ~/.codex/skills, "
            "$CODEX_HOME/skills, and any dirs registered via "
            "`mega-tron dirs add`."
        ),
    )
    p_search.add_argument(
        "--mode",
        choices=("agentic", "semantic"),
        default=None,
        help=(
            "Search mode. Default: env MEGA_MODE, else `semantic` "
            "(cosine top-K with optional eval-blend rerank). Opt into "
            "`agentic` to add 1-2 LLM calls on top of the cosine "
            "prefilter."
        ),
    )
    p_search.add_argument(
        "--prefilter",
        type=int,
        default=None,
        help=(
            "Cosine prefilter cut. Agentic default: env MEGA_PREFILTER "
            f"(or 200). Semantic default: {DEFAULT_PREFILTER}."
        ),
    )
    p_search.add_argument(
        "--shortlist",
        type=int,
        default=None,
        help=(
            "Agentic step B pick-list cap. Default: env MEGA_SHORTLIST "
            "(or 20). The LLM picks up to N skills from the prefiltered "
            "candidates; final top-k is applied on top."
        ),
    )
    p_search.add_argument(
        "--read-max",
        dest="read_max",
        type=int,
        default=None,
        help="Agentic step C SKILL.md body read cap. Default: env MEGA_READ_MAX (or 5).",
    )
    p_search.add_argument(
        "--top-k",
        type=int,
        default=DEFAULT_TOP_K,
        help=(
            f"Hard cap on returned skill count (default: {DEFAULT_TOP_K}). "
            "By default dynamic-K decides the actual count up to this cap "
            "based on the prompt's score distribution; pass --no-dynamic-k "
            "to always return exactly --top-k picks."
        ),
    )
    p_search.add_argument(
        "--no-dynamic-k",
        dest="dynamic_k",
        action="store_false",
        default=True,
        help=(
            "Disable the dynamic-K policy and always return exactly "
            "--top-k picks (manual mode). The hook subcommands offer the "
            "same flag; mirrored here so `mega-tron search` and the hooks "
            "behave identically."
        ),
    )
    p_search.add_argument(
        "--output",
        choices=("meta", "names", "bodies", "table", "stage"),
        default="meta",
        help=(
            "Output form. meta=name+dir+description per pick (default — what "
            "humans and agents survey with), names=bare names one per line "
            "(script-friendly), table=score+name+token count (debug), "
            "bodies=full SKILL.md content, stage=symlink into CODEX_HOME + "
            "must-use prefix."
        ),
    )
    p_search.add_argument(
        "--target",
        default=None,
        help="CODEX_HOME staging target. Required when --output=stage.",
    )
    p_search.add_argument(
        "--prepend-k",
        type=int,
        default=3,
        help="When --output=stage, how many skills to name in the must-use prefix.",
    )
    p_search.add_argument(
        "--budget-tok",
        type=int,
        default=DEFAULT_BUDGET_TOK,
        help="When --output=stage, SAFE_BUDGET_TOK cap.",
    )
    p_search.add_argument(
        "--no-eval-blend",
        action="store_true",
        help="Semantic mode: skip mega_meta eval blend (pure cosine).",
    )
    p_search.add_argument(
        "--timeout-s",
        dest="timeout_s",
        type=int,
        default=None,
        help="Agentic: per-LLM-call timeout seconds. Default: env MEGA_TIMEOUT_S (or 30).",
    )
    p_search.add_argument(
        "--print-manifest",
        action="store_true",
        help="When --output=stage, print stage manifest JSON instead of the must-use prefix.",
    )
    p_search.add_argument(
        "--no-prepend",
        action="store_true",
        help="When --output=stage, stage symlinks but emit empty stdout.",
    )
    p_search.add_argument("--json", action="store_true", help="Emit JSON instead of plain text.")
    p_search.add_argument(
        "--session-id",
        default=None,
        help=(
            "Host session id to credit this routing call against. Resolved "
            "from (1) this flag, (2) env MEGA_SESSION_ID, (3) None. When "
            "models invoke `mega-tron search` from an interactive host "
            "(codex / claude / gemini) the per-turn skill block tells them "
            "to pass the host's session id here so the verdict gate can "
            "credit their `<skill-used>` tags."
        ),
    )
    _add_cache_path(p_search)
    p_search.set_defaults(func=cmd_search)

    p_eval = sub.add_parser(
        "evaluate",
        help=(
            "Apply a JSON verdict batch to SKILL.md mega_meta blocks. "
            "Reads `{evaluations: [...]}` from stdin or --file."
        ),
    )
    p_eval.add_argument(
        "--skills-dir",
        default=None,
        help=(
            "Folder containing skill subdirectories. Default: "
            "auto-discover via `mega-tron dirs list`."
        ),
    )
    p_eval.add_argument(
        "--file",
        default=None,
        help="JSON file path (default: read from stdin).",
    )
    p_eval.add_argument(
        "--session-id",
        default=None,
        help="Optional session id stamped on each mega_meta version_history entry.",
    )
    p_eval.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse + report what would change; do not mutate any SKILL.md.",
    )
    p_eval.add_argument(
        "--json",
        action="store_true",
        help="Emit a JSON outcome summary instead of plain text.",
    )
    p_eval.set_defaults(func=cmd_evaluate)

    def _add_install_arguments(p) -> None:
        """Register the shared option set used by both `setup` and the
        `install` alias. Kept as a closure so both subparsers stay
        bit-identical — only the command name differs.
        """
        p.add_argument(
            "--target",
            choices=["auto", "codex", "claude", "gemini", "both", "all"],
            default="auto",
            help=(
                "Which CLI to install for. 'auto' (default) installs for "
                "every host detected on this machine (presence of "
                "~/.codex/, ~/.claude/, ~/.gemini/, or the matching CLI on "
                "PATH). 'codex' wires up the codex() shell wrapper + "
                "~/.codex/hooks.json + AGENTS.md. 'claude' wires up "
                "~/.claude/settings.json hooks + CLAUDE.md. 'gemini' wires "
                "up ~/.gemini/settings.json BeforeAgent/AfterAgent hooks + "
                "GEMINI.md. 'both' = codex+claude (legacy). 'all' = "
                "codex+claude+gemini (force all three, skipping detection)."
            ),
        )
        p.add_argument(
            "--profile",
            choices=["auto", "en-quality", "en-fast", "multilingual"],
            default="auto",
            help=(
                "Embedder profile to install. 'en-quality' = "
                "ThakiCloud/SKILLRET-Embedding-0.6B (best F1, English-only, "
                "slower). 'en-fast' = BAAI/bge-small-en-v1.5 (near-best F1, "
                "English-only, fastest). 'multilingual' = BAAI/bge-m3 "
                "(100+ languages, medium speed). 'auto' (default) prompts "
                "interactively on a TTY; on non-TTY installs it keeps the "
                "currently saved embedder (multilingual on fresh installs)."
            ),
        )
        p.add_argument("--shell", choices=["zsh", "bash", "auto"], default="auto")
        p.add_argument("--rc-file", default=None, help="Override the rc file path.")
        p.add_argument(
            "--claude-native-mode",
            choices=["passive", "active", "strict"],
            default="passive",
            help=(
                "How aggressively mega-tron suppresses Claude Code's "
                "native skill catalog. 'passive' (default): overlay-only, "
                "no token saving. 'active': per-turn skillOverrides rewrite "
                "so non-top-K skills become name-only. 'strict': active "
                "behaviour + a shell wrapper that adds "
                "`--disallowedTools Skill` to every `claude` invocation, "
                "removing the Skill tool entirely. Only meaningful when "
                "Claude Code is one of the install targets."
            ),
        )
        p.add_argument(
            "--skills-dir",
            default=None,
            help=(
                "Skills root the wrapper passes to the CLI. Default: empty "
                "(CLI auto-discovers ~/.claude/skills, ~/.codex/skills, "
                "$CODEX_HOME/skills, and any registered roots)."
            ),
        )
        p.add_argument(
            "--codex-home",
            default=str(Path.home() / ".local" / "share" / "mega-tron" / "codex-home"),
        )
        p.add_argument("--budget-tok", type=int, default=DEFAULT_BUDGET_TOK)
        p.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
        p.add_argument("--uninstall", action="store_true")
        p.add_argument(
            "--force",
            action="store_true",
            help=(
                "Skip the legacy mega-optimus sentinel check. Use only "
                "when you understand that mega-optimus hooks will keep "
                "firing alongside mega-tron's."
            ),
        )
        p.add_argument("--print-only", action="store_true", help="Dump the snippet to stdout; do not modify any file.")
        p.add_argument("--no-warmup", action="store_true", help="Skip the initial build-cache invocation.")
        p.add_argument(
            "--no-hook",
            action="store_true",
            help="Skip registering the codex UserPromptSubmit hook (interactive routing).",
        )
        p.add_argument(
            "--codex-hooks-file",
            default=None,
            help="Override the codex hooks.json path (default: ~/.codex/hooks.json).",
        )
        p.add_argument(
            "--hook-command",
            default=None,
            help="Shell command codex runs for the hook (default: `mega-tron hook`).",
        )
        p.add_argument(
            "--no-agents-md",
            action="store_true",
            help=(
                "Skip writing the persistent search-CLI guidance block into "
                "AGENTS.md. (Default: inject the block so codex sees it on "
                "every turn via its system prompt.)"
            ),
        )
        p.add_argument(
            "--agents-md-path",
            default=None,
            help="Override the AGENTS.md path (default: ~/.codex/AGENTS.md).",
        )
        p.add_argument(
            "--keep-codex-catalog",
            action="store_true",
            help=(
                "Skip patching ~/.codex/config.toml. Codex will keep emitting "
                "its native ~2%% / 8000-char skill catalog block on every turn "
                "alongside the router's dynamic top-K. Default: disable codex's "
                "catalog so the router owns skill routing end-to-end."
            ),
        )
        p.add_argument(
            "--codex-config-path",
            default=None,
            help="Override the codex config.toml path (default: ~/.codex/config.toml).",
        )
        p.set_defaults(func=cmd_install)

    # Primary, user-friendly name. "setup" reads as a one-time
    # environment-wiring step, distinct from `uv tool install mega-tron`
    # which only places the binary — so the two commands no longer share
    # the word "install" in the README.
    p_setup = sub.add_parser(
        "setup",
        help="Wire mega-tron into your host CLIs (Codex / Claude / Gemini) and your shell PATH.",
    )
    _add_install_arguments(p_setup)

    # Back-compat alias. Older docs, scripts, and the bootstrap installer
    # still call `mega-tron install`; keep it working forever.
    p_install = sub.add_parser(
        "install",
        help="Alias for `setup`. Wire mega-tron into your host CLIs.",
    )
    _add_install_arguments(p_install)

    # ----- upgrade ----- #
    # One-stop wheel refresh + running-process restart + setup re-run.
    # See mega_tron.cli.upgrade for the full rationale.
    p_upgrade = sub.add_parser(
        "upgrade",
        help=(
            "Refresh the mega-tron wheel AND restart every running "
            "mega-tron process (daemon + dashboard) so the new build "
            "actually serves on the same host:port the old one did."
        ),
    )
    p_upgrade.add_argument(
        "--from",
        dest="source",
        default=None,
        help=(
            "Local path to install from (a clone of mega-tron). "
            "If omitted, upgrade auto-discovers a clone in common "
            "locations and falls back to PyPI / a fresh /tmp clone."
        ),
    )
    p_upgrade.add_argument(
        "--from-pypi",
        action="store_true",
        help=(
            "Skip local-clone auto-discovery and install from PyPI. "
            "Useful on machines where the working clone is on a branch "
            "you don't want to publish."
        ),
    )
    p_upgrade.set_defaults(func=cmd_upgrade)

    # ----- dashboard ----- #
    p_dashboard = sub.add_parser(
        "dashboard",
        help="Launch local HTTP observability + verdict-edit UI.",
    )
    p_dashboard.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("MEGA_TRON_DASHBOARD_PORT", "7531")),
        help=(
            "TCP port (default: 7531 or $MEGA_TRON_DASHBOARD_PORT). "
            "Use 0 to let the OS pick a free port (mostly for tests)."
        ),
    )
    p_dashboard.add_argument(
        "--host",
        default="127.0.0.1",
        help=(
            "Bind address (default: 127.0.0.1 — loopback-only). The "
            "dashboard has no auth; only widen this with care."
        ),
    )
    p_dashboard.add_argument(
        "--no-open",
        action="store_true",
        help="Do not auto-open a browser tab on startup.",
    )
    p_dashboard.set_defaults(func=cmd_dashboard)

    p_hook = sub.add_parser("hook", help=argparse.SUPPRESS)
    p_hook.add_argument("--skills-dir", default=None)
    p_hook.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    p_hook.add_argument("--prepend-k", type=int, default=3)
    p_hook.add_argument(
        "--cache-path",
        dest="cache_path",
        default=None,
        help=f"Embedding cache file (default: {_default_cache_path()}).",
    )
    p_hook.add_argument(
        "--no-dynamic-k",
        dest="dynamic_k",
        action="store_false",
        default=True,
        help="Disable the dynamic-K policy (always return up to --top-k).",
    )
    p_hook.set_defaults(func=cmd_hook)

    p_stop = sub.add_parser("stop-hook", help=argparse.SUPPRESS)
    p_stop.add_argument("--skills-dir", default=None)
    p_stop.set_defaults(func=cmd_stop_hook)

    # Claude Code variants (Stage 0 scaffold; no-op until Stage 1+).
    p_chook = sub.add_parser("claude-hook", help=argparse.SUPPRESS)
    p_chook.add_argument("--skills-dir", default=None)
    p_chook.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    p_chook.add_argument("--prepend-k", type=int, default=3)
    p_chook.add_argument(
        "--cache-path",
        dest="cache_path",
        default=None,
        help=f"Embedding cache file (default: {_default_cache_path()}).",
    )
    p_chook.add_argument(
        "--no-dynamic-k",
        dest="dynamic_k",
        action="store_false",
        default=True,
        help="Disable the dynamic-K policy (always return up to --top-k).",
    )
    p_chook.set_defaults(func=cmd_claude_hook)

    p_cstop = sub.add_parser("claude-stop-hook", help=argparse.SUPPRESS)
    p_cstop.add_argument("--skills-dir", default=None)
    p_cstop.set_defaults(func=cmd_claude_stop_hook)

    # Gemini CLI variants (Stage 0 scaffold; no-op until Stage 1+).
    p_ghook = sub.add_parser("gemini-hook", help=argparse.SUPPRESS)
    p_ghook.add_argument("--skills-dir", default=None)
    p_ghook.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    p_ghook.add_argument("--prepend-k", type=int, default=3)
    p_ghook.add_argument(
        "--cache-path",
        dest="cache_path",
        default=None,
        help=f"Embedding cache file (default: {_default_cache_path()}).",
    )
    p_ghook.add_argument(
        "--no-dynamic-k",
        dest="dynamic_k",
        action="store_false",
        default=True,
        help="Disable the dynamic-K policy (always return up to --top-k).",
    )
    p_ghook.set_defaults(func=cmd_gemini_hook)

    p_gstop = sub.add_parser("gemini-stop-hook", help=argparse.SUPPRESS)
    p_gstop.add_argument("--skills-dir", default=None)
    p_gstop.set_defaults(func=cmd_gemini_stop_hook)

    p_daemon = sub.add_parser("daemon", help=argparse.SUPPRESS)
    daemon_sub = p_daemon.add_subparsers(dest="daemon_op", required=True)
    p_serve = daemon_sub.add_parser("serve", help="Run the daemon in the foreground.")
    p_serve.add_argument("--socket", default=None, help="Override AF_UNIX socket path.")
    p_serve.add_argument(
        "--idle-timeout",
        type=float,
        default=1800.0,
        help=(
            "Exit after this many idle seconds. Pass 0 (or any non-"
            "positive value) to run until the machine reboots or the "
            "daemon is stopped explicitly. Default: 1800 (30 min) for "
            "manual `daemon serve` runs; the auto-spawn path used by "
            "hooks pins it to 0."
        ),
    )
    daemon_sub.add_parser("status", help="Print whether a daemon is running.").add_argument(
        "--socket", default=None
    )
    daemon_sub.add_parser("stop", help="Ask the running daemon to exit.").add_argument(
        "--socket", default=None
    )
    p_daemon.set_defaults(func=cmd_daemon)

    p_why = sub.add_parser(
        "why",
        help=(
            "Show the score breakdown (semantic / count_bonus / context_match / "
            "status) for a ticket × skill — or the top-N ranked skills."
        ),
    )
    p_why.add_argument("ticket", help="Ticket spec / task description.")
    p_why.add_argument("skill", nargs="?", default=None, help="Skill name (optional; omit to see top-N).")
    p_why.add_argument(
        "--skills-dir",
        default=None,
        help=(
            "Optional override. Default: auto-discover via "
            "`mega-tron dirs list`."
        ),
    )
    p_why.add_argument("--top-skills", type=int, default=5, help="If no skill specified, show top-N.")
    p_why.add_argument("--json", action="store_true", help="Emit JSON instead of a human table.")
    p_why.add_argument(
        "--no-eval-blend",
        action="store_true",
        help="Render the breakdown without the eval blend (still shows zero contributions).",
    )
    _add_cache_path(p_why)
    p_why.set_defaults(func=cmd_why)

    p_dirs = sub.add_parser(
        "dirs",
        help=(
            "Manage skill-root directories. `dirs list` shows the effective "
            "search path (standard + registered); `dirs add <path>` adds a "
            "custom root; `dirs remove <path>` un-registers one. Standard "
            "dirs (~/.claude/skills, ~/.codex/skills, $CODEX_HOME/skills) "
            "are always auto-discovered and cannot be removed via this CLI."
        ),
    )
    dirs_sub = p_dirs.add_subparsers(dest="dirs_op", required=True)
    p_dirs_list = dirs_sub.add_parser("list", help="Show the effective skill-root search path.")
    p_dirs_list.add_argument("--json", action="store_true")
    p_dirs_add = dirs_sub.add_parser("add", help="Register a new skill root.")
    p_dirs_add.add_argument("path", help="Directory containing skill subfolders.")
    p_dirs_remove = dirs_sub.add_parser("remove", help="Un-register a skill root.")
    p_dirs_remove.add_argument("path", help="Directory previously registered.")
    p_dirs.set_defaults(func=cmd_dirs)

    p_skills = sub.add_parser(
        "skills",
        help=(
            "Manage the cross-host master skill pool. Promoted skills live "
            "at $XDG_DATA_HOME/mega-tron/pool/skills/ and are mirrored "
            "into each host via symlink so all three hosts (Codex, Claude "
            "Code, Hermes) see the same canonical copy."
        ),
    )
    skills_sub = p_skills.add_subparsers(dest="skills_op", required=True)

    p_skills_list = skills_sub.add_parser("list", help="Show every skill in the master pool.")
    p_skills_list.add_argument("--json", action="store_true")

    p_skills_promote = skills_sub.add_parser(
        "promote",
        help=(
            "Move a host-owned skill into the master pool, leaving a symlink "
            "behind. Pass a skill name (auto-resolved across known hosts) or "
            "an absolute path to a SKILL.md directory."
        ),
    )
    p_skills_promote.add_argument("target", help="Skill name or absolute path.")
    p_skills_promote.add_argument(
        "--from", dest="from_host",
        choices=("codex", "claude_code", "hermes", "gemini_cli"),
        default=None,
        help="Disambiguate when the same skill name exists in multiple hosts.",
    )
    p_skills_promote.add_argument(
        "--force", action="store_true",
        help="Overwrite an existing pool entry with a different SHA.",
    )

    p_skills_unpromote = skills_sub.add_parser(
        "unpromote",
        help="Reverse a promote: restore the canonical copy to its original host dir and drop all mirror symlinks.",
    )
    p_skills_unpromote.add_argument("name", help="Promoted skill name.")

    p_skills_mirror = skills_sub.add_parser(
        "mirror",
        help="Create symlinks in <target> host's skills/ pointing at the pool.",
    )
    p_skills_mirror.add_argument(
        "target",
        choices=("codex", "claude_code", "hermes", "gemini_cli"),
    )

    p_skills_unmirror = skills_sub.add_parser(
        "unmirror",
        help="Remove pool-pointing symlinks from <target> host's skills/. Non-symlink files are never touched.",
    )
    p_skills_unmirror.add_argument(
        "target",
        choices=("codex", "claude_code", "hermes", "gemini_cli"),
    )

    p_skills_sync = skills_sub.add_parser(
        "sync",
        help="Mirror the pool into every host whose skills/ dir exists.",
    )
    _ = p_skills_sync  # silence linter

    p_skills.set_defaults(func=cmd_skills)

    p_emb = sub.add_parser(
        "embedder",
        help=(
            "Show or set the default embedder model (any HuggingFace "
            "sentence-transformers id). Default: SkillRet-Embedding-0.6B. "
            "Changing it transparently switches the cache file too."
        ),
    )
    emb_sub = p_emb.add_subparsers(dest="embedder_op", required=True)
    p_emb_show = emb_sub.add_parser("show", help="Print the currently configured model.")
    p_emb_show.add_argument("--json", action="store_true")
    p_emb_set = emb_sub.add_parser("set", help="Persist a new default model id.")
    p_emb_set.add_argument("model", help="HuggingFace sentence-transformers model id.")
    p_emb.set_defaults(func=cmd_embedder)

    p_stats = sub.add_parser(
        "stats",
        help="Print per-skill helpful/harmful counters (SQLite or frontmatter).",
    )
    p_stats.add_argument(
        "--skills-dir",
        default=None,
        help=(
            "Optional override. Default: auto-discover via "
            "`mega-tron dirs list`."
        ),
    )
    p_stats.add_argument(
        "--all",
        action="store_true",
        help="Include skills with zero counters.",
    )
    p_stats.add_argument(
        "--source",
        choices=("auto", "sqlite", "frontmatter"),
        default="auto",
        help=(
            "Data source. `auto` (default) picks SQLite when a store "
            "exists, frontmatter otherwise. `sqlite` errors out if no "
            "store has been migrated; `frontmatter` reads SKILL.md "
            "mega_meta blocks directly."
        ),
    )
    p_stats.add_argument(
        "--by-host",
        action="store_true",
        help=(
            "Pivot per (skill, host). Requires --source sqlite (only "
            "the SQLite store carries host-tagged verdicts)."
        ),
    )
    p_stats.add_argument(
        "--top",
        type=int,
        default=None,
        help="Show only the top N skills ranked by (helpful - harmful).",
    )
    p_stats.add_argument("--json", action="store_true")
    p_stats.set_defaults(func=cmd_stats)

    # --- migrate-to-sqlite ---------------------------------------------------
    p_mig = sub.add_parser(
        "migrate-to-sqlite",
        help=(
            "One-shot: read every SKILL.md `mega_meta:` block and seed "
            "the SQLite verdict store. Reversible via --rollback."
        ),
    )
    p_mig.add_argument(
        "--skills-dir",
        default=None,
        help="Override discovered skill dirs (defaults to all registered roots).",
    )
    p_mig.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would happen without writing.",
    )
    p_mig.add_argument(
        "--backup-dir",
        default=None,
        help=(
            "Where to place per-file backups (default: "
            "~/.local/share/mega-tron/backup/<timestamp>/)."
        ),
    )
    p_mig.add_argument(
        "--force",
        action="store_true",
        help=(
            "Run even when the store already holds verdicts. "
            "Re-migrating will double-count — use only after a manual "
            "store wipe."
        ),
    )
    p_mig.add_argument(
        "--rollback",
        default=None,
        metavar="BACKUP_DIR",
        help=(
            "Restore SKILL.md files from a prior backup directory and "
            "rename the current store aside (`*.rolled-back-<ts>`)."
        ),
    )
    p_mig.set_defaults(func=cmd_migrate_to_sqlite)

    # --- export-frontmatter --------------------------------------------------
    p_exp = sub.add_parser(
        "export-frontmatter",
        help=(
            "Re-emit SKILL.md `mega_meta:` blocks from the SQLite store "
            "so the on-disk frontmatter stays in sync."
        ),
    )
    p_exp.add_argument(
        "--skills-dir",
        default=None,
        help="Override discovered skill dirs (defaults to all registered roots).",
    )
    p_exp.set_defaults(func=cmd_export_frontmatter)

    # --- search-verdicts ----------------------------------------------------
    p_sv = sub.add_parser(
        "search-verdicts",
        help=(
            "Full-text search over verdict reasons via SQLite FTS5. "
            'Example: `mega-tron search-verdicts "webhook signature"`.'
        ),
    )
    p_sv.add_argument(
        "query",
        help=(
            "FTS5 MATCH expression. Bare words are tokenised; "
            "quoted phrases match literally; AND/OR/NOT and prefix * "
            "are supported."
        ),
    )
    p_sv.add_argument(
        "--host",
        default=None,
        choices=("codex", "claude_code", "gemini_cli", "hermes", "other"),
        help="Restrict matches to one host's verdicts.",
    )
    p_sv.add_argument(
        "--verdict",
        default=None,
        choices=("HELPFUL", "HARMFUL", "NEUTRAL"),
        help="Restrict matches to one verdict label.",
    )
    p_sv.add_argument(
        "--since-days",
        type=int,
        default=None,
        help="Only return verdicts within the last N days.",
    )
    p_sv.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Cap on rows returned. Default 20.",
    )
    p_sv.add_argument("--json", action="store_true")
    p_sv.set_defaults(func=cmd_search_verdicts)

    # --- compact-embeddings -------------------------------------------------
    p_ce = sub.add_parser(
        "compact-embeddings",
        help=(
            "Collapse near-duplicate verdict embeddings within each "
            "(skill, label) group to bound disk + memory footprint. "
            "Time-series verdicts table is untouched."
        ),
    )
    p_ce.add_argument(
        "--threshold",
        type=float,
        default=0.95,
        help=(
            "Cosine similarity cutoff for clustering. Default 0.95 "
            "(true paraphrases). Lower = more aggressive."
        ),
    )
    p_ce.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be removed without mutating disk.",
    )
    p_ce.add_argument("--json", action="store_true")
    p_ce.set_defaults(func=cmd_compact_embeddings)

    # --- compact-skills -----------------------------------------------------
    p_cs = sub.add_parser(
        "compact-skills",
        help=(
            "Cluster SKILL.md embeddings and suppress near-duplicate "
            "losers from the routing matrix. Winner per cluster is "
            "picked by status > verdict score > mtime. Defaults to dry-run."
        ),
    )
    p_cs.add_argument(
        "--threshold",
        type=float,
        default=0.95,
        help=(
            "Cosine cutoff. Default 0.95 (true paraphrases). Lower = "
            "more aggressive clustering."
        ),
    )
    p_cs.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Persist suppressions to disk. Without this flag, runs as "
            "a dry-run preview — safer default because suppression "
            "directly changes routing top-K."
        ),
    )
    p_cs.add_argument(
        "--reset",
        action="store_true",
        help=(
            "Lift every previously-recorded suppression. The cache will "
            "repopulate the cleared skills on next warmup. Ignores "
            "--apply / --threshold."
        ),
    )
    p_cs.add_argument(
        "--skills-dir",
        default=None,
        help=(
            "Comma-separated skill roots; overrides auto-discovery. "
            "Use for CI / one-shot scripts."
        ),
    )
    p_cs.add_argument("--json", action="store_true")
    _add_cache_path(p_cs)
    p_cs.set_defaults(func=cmd_compact_skills)

    # --- qa-live ------------------------------------------------------------
    p_qa = sub.add_parser(
        "qa-live",
        help=(
            "End-to-end self-check: plants a marker skill in each "
            "wired host, drives a one-shot non-interactive call, and "
            "confirms a verdict row landed. Use after `setup` to verify "
            "the UserPromptSubmit → routing → Stop-hook loop works."
        ),
    )
    p_qa.add_argument(
        "--host",
        default=None,
        help=(
            "Comma-separated subset to check (codex,claude,gemini). "
            "Default: auto-detect every installed host."
        ),
    )
    p_qa.set_defaults(func=cmd_qa_live)

    # --- regressions ---------------------------------------------------------
    p_reg = sub.add_parser(
        "regressions",
        help=(
            "List skills whose helpful/harmful trend recently flipped "
            "(broken / regressed). Requires a migrated SQLite store."
        ),
    )
    p_reg.add_argument(
        "--window-days",
        type=int,
        default=30,
        help="Recent window in days; baseline is the equal-length stretch before it. Default 30.",
    )
    p_reg.add_argument(
        "--min-invocations",
        type=int,
        default=5,
        help=(
            "Minimum recent verdicts before a skill can be classified "
            "as broken/regressed. Lower = more sensitive + noisier. "
            "Default 5."
        ),
    )
    p_reg.add_argument(
        "--host",
        default=None,
        choices=("codex", "claude_code", "hermes", "other"),
        help="Restrict the analysis to one host's verdicts.",
    )
    p_reg.add_argument(
        "--include-all",
        action="store_true",
        help=(
            "Also list ``unused`` and ``stable`` rows. Useful for "
            "debugging \"why isn't this skill flagged?\""
        ),
    )
    p_reg.add_argument("--json", action="store_true")
    p_reg.set_defaults(func=cmd_regressions)

    # Hide internal subparsers from `--help` while keeping them callable.
    # argparse's `help=SUPPRESS` doesn't fully suppress subparser entries
    # in the help formatter, so we drop them from the _choices_actions
    # list directly.
    _HIDDEN_CMDS = {
        "build-cache",
        "hook",
        "stop-hook",
        "claude-hook",
        "claude-stop-hook",
        "gemini-hook",
        "gemini-stop-hook",
        "daemon",
    }
    sub._choices_actions = [
        c for c in sub._choices_actions if c.dest not in _HIDDEN_CMDS
    ]

    args = parser.parse_args(argv)
    _warn_if_unwired(args)
    return args.func(args)


def _warn_if_unwired(args: argparse.Namespace) -> None:
    """Print a one-time stderr nudge when the user runs any command
    before `mega-tron setup` has been called.

    Skipped for the setup/install commands themselves, for hook
    subprocesses (which are non-interactive and would be noisy), and
    when the user has explicitly silenced it via ``MEGA_QUIET=1``.

    The marker file is created by the install dispatcher on successful
    setup; its absence means either a fresh install or an aborted setup.
    """
    cmd = getattr(args, "func", None)
    cmd_name = getattr(cmd, "__name__", "") if cmd else ""
    if cmd_name in {"cmd_install", "cmd_upgrade"}:
        return
    # Hook commands run inside host CLI subprocesses; never print there.
    if cmd_name in {
        "cmd_hook", "cmd_stop_hook",
        "cmd_claude_hook", "cmd_claude_stop_hook",
        "cmd_gemini_hook", "cmd_gemini_stop_hook",
    }:
        return
    if os.environ.get("MEGA_QUIET"):
        return
    try:
        from mega_tron.config import data_dir
        marker = data_dir() / ".setup-done"
        if marker.exists():
            return
    except Exception:
        return
    print(
        "[mega-tron] You haven't run setup yet. Host CLIs (Codex / Claude / "
        "Gemini) will keep using their native catalogs until you do.\n"
        "            Run:  mega-tron setup\n"
        "            (Silence this with MEGA_QUIET=1.)",
        file=sys.stderr,
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
