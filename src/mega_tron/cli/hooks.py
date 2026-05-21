"""Hook subcommand delegators.

Each `mega-tron <host>-hook` and `mega-tron <host>-stop-hook` is a
thin wrapper that imports and calls the host's actual hook handler.
The wrappers exist so the CLI surface is uniform across hosts (every
host has the same two subcommand shapes) and the heavy host modules
stay lazy-imported.

`cmd_hook` and `cmd_stop_hook` are aliases for the Codex pair —
preserved because the Codex CLI references them directly in
``~/.codex/hooks.json``.
"""
from __future__ import annotations

import argparse


def cmd_hook(args: argparse.Namespace) -> int:
    from mega_tron.hosts.codex.hook import cmd_hook as _cmd_hook

    return _cmd_hook(args)


def cmd_stop_hook(args: argparse.Namespace) -> int:
    from mega_tron.hosts.codex.stop_hook import cmd_stop_hook as _cmd_stop

    return _cmd_stop(args)


def cmd_claude_hook(args: argparse.Namespace) -> int:
    from mega_tron.hosts.claude_code.hook import cmd_claude_hook as _cmd

    return _cmd(args)


def cmd_claude_stop_hook(args: argparse.Namespace) -> int:
    from mega_tron.hosts.claude_code.stop_hook import cmd_claude_stop_hook as _cmd

    return _cmd(args)


def cmd_gemini_hook(args: argparse.Namespace) -> int:
    from mega_tron.hosts.gemini_cli.hook import cmd_gemini_hook as _cmd

    return _cmd(args)


def cmd_gemini_stop_hook(args: argparse.Namespace) -> int:
    from mega_tron.hosts.gemini_cli.stop_hook import cmd_gemini_stop_hook as _cmd

    return _cmd(args)
