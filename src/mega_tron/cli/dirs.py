"""`mega-tron dirs` — register/unregister/list skill-root directories."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from mega_tron.config import (
    Config,
    add_skill_dir,
    config_path,
    discover_skill_dirs,
    remove_skill_dir,
)


def cmd_dirs(args: argparse.Namespace) -> int:
    """Register / unregister / list skill-root directories.

    ``mega-tron search`` auto-discovers ``~/.claude/skills``,
    ``~/.codex/skills``, and ``$CODEX_HOME/skills`` out of the box.
    This command manages *additional* roots — e.g. project-local skill
    folders, team-shared NFS mounts, anything the user wants on the
    permanent search path. Registered dirs persist in
    ``~/.config/mega-tron/config.toml`` and are read on every
    invocation.
    """
    op = args.dirs_op
    cfg = Config.load()

    if op == "list":
        # Show three columns: the path, where it came from (standard /
        # config / env / missing), and whether it exists on disk.
        standard = {
            p.resolve(strict=False)
            for p in discover_skill_dirs(
                config=Config(),  # empty config → standard locations only
                existing_only=False,
            )
        }
        config_dirs = {d.resolve(strict=False) for d in cfg.extra_skill_dirs}
        all_dirs = discover_skill_dirs(config=cfg, existing_only=False)
        if args.json:
            out = []
            for d in all_dirs:
                resolved = d.resolve(strict=False)
                source = (
                    "config" if resolved in config_dirs
                    else "standard" if resolved in standard
                    else "env"
                )
                out.append(
                    {
                        "path": str(d),
                        "source": source,
                        "exists": d.exists(),
                    }
                )
            json.dump(out, sys.stdout, indent=2)
            sys.stdout.write("\n")
            return 0
        if not all_dirs:
            print("(no skill directories — register one with `mega-tron dirs add <path>`)")
            return 0
        width = max(len(str(d)) for d in all_dirs)
        print(f"  {'path'.ljust(width)}  source    exists")
        print(f"  {'-' * width}  --------  ------")
        for d in all_dirs:
            resolved = d.resolve(strict=False)
            source = (
                "config" if resolved in config_dirs
                else "standard" if resolved in standard
                else "env"
            )
            exists = "yes" if d.exists() else "no"
            print(f"  {str(d).ljust(width)}  {source:<8}  {exists}")
        print()
        print(f"  config file: {config_path()}")
        return 0

    if op == "add":
        path = Path(args.path).expanduser()
        if not path.exists():
            print(
                f"[dirs] warning: {path} does not exist yet — registering anyway",
                file=sys.stderr,
            )
        _, added = add_skill_dir(path)
        if added:
            print(f"[dirs] added {path}")
        else:
            print(f"[dirs] {path} was already registered")
        return 0

    if op == "remove":
        path = Path(args.path).expanduser()
        _, removed = remove_skill_dir(path)
        if removed:
            print(f"[dirs] removed {path}")
            return 0
        print(f"[dirs] {path} was not in the registered list", file=sys.stderr)
        return 1

    print(f"[dirs] unknown op {op!r}", file=sys.stderr)
    return 2
