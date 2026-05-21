"""`mega-tron embedder` — show or set the default embedder model.

Filename is `embedder_cmd` (not `embedder`) to avoid clashing with the
top-level ``mega_tron.embedder`` module that this subcommand reads.
"""
from __future__ import annotations

import argparse
import json
import sys

from mega_tron.cli._common import _default_cache_path
from mega_tron.config import Config, config_path, set_embedder_model


def cmd_embedder(args: argparse.Namespace) -> int:
    """Show or set the default embedder model.

    The model id is any HuggingFace sentence-transformers identifier —
    ``BAAI/bge-small-en-v1.5``, ``intfloat/e5-large-v2``,
    ``ThakiCloud/SKILLRET-Embedding-0.6B`` (default), etc. Changing it
    invalidates the cache (the cache filename is keyed on the slugged
    model id, so the next ``search`` builds a fresh ``.npz``).
    """
    op = args.embedder_op
    cfg = Config.load()

    if op == "show":
        if args.json:
            json.dump({"model": cfg.embedder_model}, sys.stdout)
            sys.stdout.write("\n")
            return 0
        print(f"  embedder model: {cfg.embedder_model}")
        print(f"  cache path:     {_default_cache_path(cfg.embedder_model)}")
        print(f"  config file:    {config_path()}")
        return 0

    if op == "set":
        new = set_embedder_model(args.model)
        print(f"[embedder] model set to {new.embedder_model}")
        print(f"           next `search` will build {_default_cache_path(new.embedder_model)}")
        return 0

    print(f"[embedder] unknown op {op!r}", file=sys.stderr)
    return 2
