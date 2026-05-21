"""End-to-end routing benchmark runner (DESIGN.md §4.3).

Six conditions × 200 queries = 1,200 deterministic cells:

  - vanilla-codex
  - vanilla-claude
  - vanilla-gemini
  - mega-tron-bge-m3       (BAAI/bge-m3)
  - mega-tron-skillret     (ThakiCloud/SKILLRET-Embedding-0.6B)
  - mega-tron-bge-small    (BAAI/bge-small-en-v1.5)

Output: ``results/<condition>.jsonl`` — one row per query plus an
aggregated ``results/summary.json``.

Each row carries enough metadata for ``analyze.py`` to recompute every
DESIGN-§3 metric without re-running anything.

Usage:
    uv run python benchmarks/routing/run.py
    uv run python benchmarks/routing/run.py --conditions mega-tron-bge-m3
    uv run python benchmarks/routing/run.py --skip-mega-tron      # vanilla only
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
ROUTING_DIR = Path(__file__).resolve().parent
SKILLS_DIR = ROUTING_DIR / "skills"
BENCH_DIR = ROUTING_DIR / "200bench"
RESULTS_DIR = ROUTING_DIR / "results"
CACHE_DIR = ROUTING_DIR / ".cache"

sys.path.insert(0, str(REPO_ROOT))

from benchmarks.routing.scoring import score_query  # noqa: E402
from benchmarks.routing.vanilla_sim import (  # noqa: E402
    PoolSkill,
    simulate_claude_catalog,
    simulate_codex_catalog,
    simulate_gemini_catalog,
)

# Three embedders we sweep on the mega-tron side.
MEGA_TRON_EMBEDDERS: dict[str, str] = {
    "mega-tron-bge-m3": "BAAI/bge-m3",
    "mega-tron-skillret": "ThakiCloud/SKILLRET-Embedding-0.6B",
    "mega-tron-bge-small": "BAAI/bge-small-en-v1.5",
}

VANILLA_CONDITIONS = ("vanilla-codex", "vanilla-claude", "vanilla-gemini")
ALL_CONDITIONS = list(VANILLA_CONDITIONS) + list(MEGA_TRON_EMBEDDERS.keys())


# ----------------------------------------------------------------------
# Pool + fixture loading
# ----------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _parse_skill_frontmatter(skill_md: Path) -> tuple[str, str]:
    """Return ``(name, description)`` from frontmatter; empty strings on failure."""
    content = skill_md.read_text(encoding="utf-8", errors="ignore")
    m = _FRONTMATTER_RE.match(content)
    if not m:
        return "", ""
    fm = yaml.safe_load(m.group(1)) or {}
    return (
        str(fm.get("name", "")).strip(),
        str(fm.get("description", "")).strip(),
    )


def load_pool() -> tuple[list[PoolSkill], dict[str, str]]:
    """Load 500 pool skills.

    Returns ``(pool_skills, sha8_by_name)`` so the F1 mapping can join
    gold sha → predicted name.
    """
    manifest = json.loads((BENCH_DIR / "pool_manifest.json").read_text())
    pool: list[PoolSkill] = []
    sha8_by_name: dict[str, str] = {}
    for entry in manifest["entries"]:
        sha8 = entry["sha8"]
        name, desc = _parse_skill_frontmatter(SKILLS_DIR / sha8 / "SKILL.md")
        if not name:
            # Fall back to original_dirname so the skill still has an
            # identifier even if frontmatter parsing fails.
            name = entry["original_dirname"]
        pool.append(PoolSkill(name=name, description=desc))
        sha8_by_name[name] = sha8
    return pool, sha8_by_name


def load_golds() -> dict[str, str]:
    """Return ``gold_id → sha8`` mapping."""
    data = json.loads((BENCH_DIR / "golds.json").read_text())
    return {g["gold_id"]: g["sha8"] for g in data["golds"]}


def load_queries() -> list[dict]:
    data = yaml.safe_load((BENCH_DIR / "queries.yaml").read_text())
    return list(data["queries"])


def stratum_for(query: dict) -> str:
    """Classify a query into k1/k2/k3/null for per-stratum reporting."""
    if query.get("null_kind") is not None:
        return "null"
    n = len(query.get("golds") or [])
    return f"k{n}" if n in (1, 2, 3) else f"k{n}"


# ----------------------------------------------------------------------
# Vanilla measurement
# ----------------------------------------------------------------------

def measure_vanilla_condition(
    condition: str,
    pool: list[PoolSkill],
    sha8_by_name: dict[str, str],
    gold_id_to_sha8: dict[str, str],
    queries: list[dict],
) -> list[dict]:
    """Score every query against a static (prompt-independent) vanilla
    catalog. The catalog is built once per condition since vanilla
    behaviour doesn't depend on the query."""
    if condition == "vanilla-codex":
        cb = simulate_codex_catalog(pool)
    elif condition == "vanilla-claude":
        cb = simulate_claude_catalog(pool)
    elif condition == "vanilla-gemini":
        cb = simulate_gemini_catalog(pool)
    else:
        raise ValueError(condition)

    # The catalog is the same for every query → predicted_set is
    # constant. We use ``all_emitted_names`` (not ``predicted_skills``):
    # a skill counts as "predicted" if its NAME made it into the
    # catalog at all, even when the description was truncated or
    # dropped. The model still sees the name and can invoke the skill;
    # the description-only definition was too strict.
    predicted_sha8: set[str] = {
        sha8_by_name[n] for n in cb.all_emitted_names if n in sha8_by_name
    }

    rows: list[dict] = []
    for q in queries:
        gold_sha8 = {gold_id_to_sha8[g] for g in (q.get("golds") or [])}
        s = score_query(q["id"], gold_sha8, predicted_sha8)
        rows.append({
            "query_id": q["id"],
            "condition": condition,
            "catalog_tokens": cb.tokens,
            "score": s.score,
            "hit_count": s.hit_count,
            "gold_count": s.gold_count,
            "stratum": stratum_for(q),
            "null_kind": q.get("null_kind"),
            "length_bucket": q.get("length_bucket"),
            "predicted_count": len(predicted_sha8),
            "K": None,
            "K_reason": None,
            "top1_score": None,
        })
    return rows


# ----------------------------------------------------------------------
# mega-tron measurement
# ----------------------------------------------------------------------

@dataclass
class MegaTronContext:
    embedder_model: str
    router: object  # Router (typed loosely to keep imports lazy)
    sha8_by_name: dict[str, str]
    catalog_tokens_renderer: callable


def _render_megatron_catalog(ranked_skills: list, pool_by_name: dict[str, PoolSkill]) -> int:
    """Compute the token count of mega-tron's injected top-K block.

    Uses the same wire format Codex's hook would emit (``- name: desc``),
    tokenized with the same o200k_base encoder used by the vanilla
    simulators — apples-to-apples token comparison.
    """
    from benchmarks.routing.vanilla_sim import _enc
    lines: list[str] = []
    for rs in ranked_skills:
        name = rs.name
        desc = pool_by_name.get(name, PoolSkill(name, "")).description
        lines.append(f"- {name}: {desc}\n")
    text = "".join(lines)
    return len(_enc().encode(text))


def setup_megatron(embedder_model: str, sha8_by_name: dict[str, str]) -> MegaTronContext:
    """Build a Router for one embedder, against our pool."""
    from mega_tron.cache import Cache
    from mega_tron.embedder import make_embedder
    from mega_tron.router import Router

    print(f"[run] loading embedder: {embedder_model}", file=sys.stderr, flush=True)
    embedder = make_embedder(embedder_model)
    fp = getattr(embedder, "fingerprint", None) or embedder_model
    safe_fp = re.sub(r"[^A-Za-z0-9._@-]", "_", fp)
    cache_path = CACHE_DIR / f"{safe_fp}.npz"
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    cache = Cache(cache_path)
    router = Router(
        skills_dirs=[SKILLS_DIR],
        embedder=embedder,
        cache=cache,
        use_eval=False,  # routing benchmark — no eval blend (no verdicts on pool)
    )

    print(f"[run] warming cache (500 skills)...", file=sys.stderr, flush=True)
    t0 = time.time()
    n_new, n_reused, invalid = router.warmup()
    print(
        f"[run]   embedded {n_new}, reused {n_reused}, invalid {len(invalid)} "
        f"in {time.time()-t0:.1f}s",
        file=sys.stderr, flush=True,
    )

    return MegaTronContext(
        embedder_model=embedder_model,
        router=router,
        sha8_by_name=sha8_by_name,
        catalog_tokens_renderer=lambda ranked, pool_by_name: _render_megatron_catalog(ranked, pool_by_name),
    )


def measure_megatron_condition(
    condition: str,
    ctx: MegaTronContext,
    pool: list[PoolSkill],
    gold_id_to_sha8: dict[str, str],
    queries: list[dict],
) -> list[dict]:
    """Run mega-tron's Router with dynamic K against every query."""
    pool_by_name = {s.name: s for s in pool}

    # Use the embedder's profile k_max so weak embedders (bge-small,
    # k_max=20) get the wider K window dynamic-K may want to emit.
    # Hardcoding top_k=10 silently caps weak-tier elbow cuts at 10.
    from mega_tron.dynamic_k import profile_for as _profile_for
    _cfg = _profile_for(ctx.embedder_model)
    _top_k = _cfg.k_max

    rows: list[dict] = []
    for q in queries:
        ranked = ctx.router.rank(q["query"], top_k=_top_k, dynamic=True)
        K, reason = ctx.router.last_dynamic or (len(ranked), "no-dynamic")
        predicted_sha8 = {
            ctx.sha8_by_name[rs.name] for rs in ranked if rs.name in ctx.sha8_by_name
        }
        gold_sha8 = {gold_id_to_sha8[g] for g in (q.get("golds") or [])}
        s = score_query(q["id"], gold_sha8, predicted_sha8)

        top1_score = float(ranked[0].score) if ranked else 0.0
        catalog_tokens = ctx.catalog_tokens_renderer(ranked, pool_by_name)

        rows.append({
            "query_id": q["id"],
            "condition": condition,
            "catalog_tokens": catalog_tokens,
            "score": s.score,
            "hit_count": s.hit_count,
            "gold_count": s.gold_count,
            "stratum": stratum_for(q),
            "null_kind": q.get("null_kind"),
            "length_bucket": q.get("length_bucket"),
            "predicted_count": len(predicted_sha8),
            "K": K,
            "K_reason": reason,
            "top1_score": top1_score,
        })
    return rows


# ----------------------------------------------------------------------
# Driver
# ----------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument(
        "--conditions",
        nargs="*",
        choices=ALL_CONDITIONS,
        help="Subset of conditions to run. Default: all.",
    )
    p.add_argument(
        "--skip-mega-tron",
        action="store_true",
        help="Run only vanilla simulators (no embedder model needed).",
    )
    p.add_argument(
        "--out-dir",
        type=Path,
        default=RESULTS_DIR,
    )
    args = p.parse_args(argv)

    conditions = args.conditions or ALL_CONDITIONS
    if args.skip_mega_tron:
        conditions = [c for c in conditions if c in VANILLA_CONDITIONS]

    print(f"[run] loading pool + fixture...", file=sys.stderr)
    pool, sha8_by_name = load_pool()
    gold_id_to_sha8 = load_golds()
    queries = load_queries()
    print(
        f"[run]   pool={len(pool)}, golds={len(gold_id_to_sha8)}, "
        f"queries={len(queries)}",
        file=sys.stderr,
    )

    args.out_dir.mkdir(parents=True, exist_ok=True)

    summary: dict = {
        "_meta": {
            "pool_size": len(pool),
            "gold_count": len(gold_id_to_sha8),
            "query_count": len(queries),
            "conditions": conditions,
        },
        "per_condition": {},
    }

    for cond in conditions:
        t0 = time.time()
        if cond in VANILLA_CONDITIONS:
            rows = measure_vanilla_condition(cond, pool, sha8_by_name, gold_id_to_sha8, queries)
        else:
            embedder_model = MEGA_TRON_EMBEDDERS[cond]
            ctx = setup_megatron(embedder_model, sha8_by_name)
            rows = measure_megatron_condition(cond, ctx, pool, gold_id_to_sha8, queries)

        # Write per-condition JSONL
        out_path = args.out_dir / f"{cond}.jsonl"
        with out_path.open("w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

        # Aggregate
        mean_all = sum(r["score"] for r in rows) / len(rows)
        in_dist = [r for r in rows if r["stratum"] != "null"]
        null_rows = [r for r in rows if r["stratum"] == "null"]
        in_dist_score = sum(r["score"] for r in in_dist) / len(in_dist) if in_dist else 0.0
        null_score = sum(r["score"] for r in null_rows) / len(null_rows) if null_rows else 0.0
        # FP_null = fraction of null prompts where ANY skill was predicted
        fp_null_rate = sum(1 for r in null_rows if r["predicted_count"] > 0) / len(null_rows) if null_rows else 0.0
        mean_tokens = sum(r["catalog_tokens"] for r in rows) / len(rows)

        summary["per_condition"][cond] = {
            "mean_score": mean_all,
            "in_dist_mean_score": in_dist_score,
            "null_mean_score": null_score,
            "fp_null_rate": fp_null_rate,
            "mean_catalog_tokens": mean_tokens,
            "wall_seconds": round(time.time() - t0, 2),
        }
        print(
            f"[run] {cond:25}: score={mean_all:.3f}  in_dist={in_dist_score:.3f}  "
            f"null={null_score:.3f}  FP_null={fp_null_rate:.2f}  "
            f"mean_tokens={mean_tokens:.0f}  ({time.time()-t0:.1f}s)",
            file=sys.stderr,
        )

    (args.out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"[run] summary → {args.out_dir / 'summary.json'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
