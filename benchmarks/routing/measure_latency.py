"""Per-query end-to-end latency measurement for the mega-tron router.

Measures wall-clock time for the full routing pipeline:
  embed(query) → cosine matmul → dynamic_k → render catalog block

Reports p50, p95, p99 latency per condition. Warm-cache assumption —
the first three queries are discarded so model-load and JIT compile
don't contaminate timing.

Usage:
    uv run python benchmarks/routing/measure_latency.py \\
        --config bge-m3 --config skillret --config bge-small
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
ROUTING_DIR = Path(__file__).resolve().parent
SKILLS_DIR = ROUTING_DIR / "skills"
RESULTS_DIR = ROUTING_DIR / "results"
CACHE_DIR = ROUTING_DIR / ".cache"

sys.path.insert(0, str(REPO_ROOT))

from benchmarks.routing.run import (  # noqa: E402
    load_pool, load_golds, load_queries, MEGA_TRON_EMBEDDERS,
)


def _safe_fp(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._@-]", "_", s)


def setup_router(embedder_model: str):
    from mega_tron.cache import Cache
    from mega_tron.embedder import make_embedder, fingerprint_of
    from mega_tron.router import Router

    embedder = make_embedder(embedder_model)
    fp = fingerprint_of(embedder)
    cache = Cache(CACHE_DIR / f"{_safe_fp(fp)}.npz")
    router = Router(
        skills_dirs=[SKILLS_DIR],
        embedder=embedder,
        cache=cache,
        use_eval=False,
    )
    router.warmup()
    return router


def measure(router, queries, warmup_queries: int = 3) -> list[dict]:
    """Run router.rank(dynamic=True) and time each call."""
    for q in queries[:warmup_queries]:
        router.rank(q["query"], top_k=10, dynamic=True)

    rows = []
    for q in queries:
        t0 = time.perf_counter()
        ranked = router.rank(q["query"], top_k=10, dynamic=True)
        dt_ms = (time.perf_counter() - t0) * 1000
        rows.append({
            "query_id": q["id"],
            "latency_ms": dt_ms,
            "k": len(ranked),
        })
    return rows


def summarize(rows: list[dict]) -> dict:
    lats = [r["latency_ms"] for r in rows]
    return {
        "n": len(lats),
        "mean_ms": round(statistics.mean(lats), 2),
        "p50_ms": round(statistics.median(lats), 2),
        "p95_ms": round(np.percentile(lats, 95), 2),
        "p99_ms": round(np.percentile(lats, 99), 2),
        "max_ms": round(max(lats), 2),
        "mean_k": round(statistics.mean(r["k"] for r in rows), 2),
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument(
        "--config",
        action="append",
        required=True,
        help="Repeatable. Embedder short name: 'bge-m3', 'skillret', or 'bge-small'.",
    )
    args = p.parse_args()

    _pool, _sha8_by_name = load_pool()
    _gold_id_to_sha8 = load_golds()
    queries = load_queries()

    summary = {}
    for short_name in args.config:
        embedder_key = next(
            (k for k in MEGA_TRON_EMBEDDERS if short_name in k or k.endswith(short_name)),
            None,
        )
        if embedder_key is None:
            print(f"Unknown embedder short name: {short_name}", file=sys.stderr)
            continue
        model = MEGA_TRON_EMBEDDERS[embedder_key]
        print(f"\n=== {embedder_key} ===", file=sys.stderr)
        router = setup_router(model)
        rows = measure(router, queries)
        s = summarize(rows)
        summary[embedder_key] = s
        print(
            f"  mean={s['mean_ms']:.1f}ms  p50={s['p50_ms']:.1f}  "
            f"p95={s['p95_ms']:.1f}  p99={s['p99_ms']:.1f}  max={s['max_ms']:.1f}  "
            f"k_mean={s['mean_k']:.1f}",
            file=sys.stderr,
        )

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / "latency.json"
    out.write_text(json.dumps(summary, indent=2) + "\n")
    print(f"\nSummary → {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
