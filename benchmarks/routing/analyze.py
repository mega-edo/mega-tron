"""Generate headline tables from ``results/*.jsonl`` (DESIGN.md §4.4).

Prints (to stdout):
  Table 1 — per-condition headline metrics (macro_F1, token cost, F1/1Ktok)
  Table 2 — per-stratum F1 breakdown (k1 / k2 / k3 / null)
  Table 3 — null sub-stratum (small_talk / dev_trap / adversarial)
  Table 4 — mega-tron dynamic-K diagnostics (K distribution, branch reasons)

Each table is markdown-formatted so it can be pasted straight into
docs / commit messages.

Usage:
    uv run python benchmarks/routing/analyze.py
    uv run python benchmarks/routing/analyze.py --results-dir results/
"""
from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

ROUTING_DIR = Path(__file__).resolve().parent
DEFAULT_RESULTS_DIR = ROUTING_DIR / "results"


def _load_rows(results_dir: Path) -> dict[str, list[dict]]:
    """Return ``condition → list[row]`` from every ``*.jsonl`` in the dir."""
    rows: dict[str, list[dict]] = {}
    for path in sorted(results_dir.glob("*.jsonl")):
        cond = path.stem
        with path.open() as f:
            rows[cond] = [json.loads(line) for line in f]
    return rows


def _mean_score(rs: list[dict]) -> float:
    return sum(r["score"] for r in rs) / len(rs) if rs else 0.0


def _mean_tokens(rs: list[dict]) -> float:
    return sum(r["catalog_tokens"] for r in rs) / len(rs) if rs else 0.0


def _print_md_table(headers: list[str], rows: list[list[str]]) -> None:
    """Pretty-print a markdown table; columns auto-sized."""
    widths = [
        max(len(str(headers[c])), *(len(str(r[c])) for r in rows))
        for c in range(len(headers))
    ]
    def fmt(cells, align="left"):
        out = []
        for c, w in zip(cells, widths):
            s = str(c)
            if align == "right":
                out.append(s.rjust(w))
            else:
                out.append(s.ljust(w))
        return "| " + " | ".join(out) + " |"
    print(fmt(headers))
    sep_row = ["-" * w for w in widths]
    print("|" + "|".join(f"-{s}-" for s in sep_row) + "|")
    for r in rows:
        print(fmt(r))


# ----------------------------------------------------------------------
# Table 1 — Headline
# ----------------------------------------------------------------------

def table_headline(rows_by_cond: dict[str, list[dict]]) -> None:
    print("## Table 1 — Headline metrics")
    print()
    print("(partial-credit recall@K across all 200 queries, mean tokens "
          "per query, score per 1K tokens efficiency)")
    print()
    table: list[list[str]] = []
    for cond, rs in rows_by_cond.items():
        score = _mean_score(rs)
        tok = _mean_tokens(rs)
        eff = score / (tok / 1000) if tok > 0 else 0.0
        table.append([
            cond,
            f"{score:.3f}",
            f"{tok:7.0f}",
            f"{eff:5.2f}",
        ])
    _print_md_table(
        ["Condition", "score", "mean_tokens", "score/1Ktok"],
        table,
    )


# ----------------------------------------------------------------------
# Table 2 — per-stratum (k1, k2, k3, null)
# ----------------------------------------------------------------------

def table_per_stratum(rows_by_cond: dict[str, list[dict]]) -> None:
    print()
    print("## Table 2 — Per-stratum recall@K")
    print()
    print("(mean partial-credit recall split by query stratum: "
          "k=1, k=2, k=3, null)")
    print()
    strata = ("k1", "k2", "k3", "null")
    table: list[list[str]] = []
    for cond, rs in rows_by_cond.items():
        by_stratum = defaultdict(list)
        for r in rs:
            by_stratum[r["stratum"]].append(r)
        row = [cond]
        for s in strata:
            row.append(f"{_mean_score(by_stratum[s]):.3f}")
        table.append(row)
    _print_md_table(
        ["Condition", "k=1", "k=2", "k=3", "null"],
        table,
    )


# ----------------------------------------------------------------------
# Table 3 — null sub-stratum
# ----------------------------------------------------------------------

def table_null_sub(rows_by_cond: dict[str, list[dict]]) -> None:
    print()
    print("## Table 3 — Null sub-stratum (abstain quality)")
    print()
    print("(mean score on small-talk / dev-trap / adversarial null prompts; "
          "score=1 means correct K=0 abstain)")
    print()
    sub_kinds = ("small_talk", "dev_trap", "adversarial")
    table: list[list[str]] = []
    for cond, rs in rows_by_cond.items():
        by_kind = defaultdict(list)
        for r in rs:
            if r["stratum"] == "null":
                by_kind[r["null_kind"]].append(r)
        row = [cond]
        for k in sub_kinds:
            row.append(f"{_mean_score(by_kind[k]):.3f}")
        # FP rate
        null_rows = [r for r in rs if r["stratum"] == "null"]
        fp = sum(1 for r in null_rows if r["predicted_count"] > 0) / len(null_rows) if null_rows else 0.0
        row.append(f"{fp:.2f}")
        table.append(row)
    _print_md_table(
        ["Condition", "small_talk", "dev_trap", "adversarial", "FP_null"],
        table,
    )


# ----------------------------------------------------------------------
# Table 4 — mega-tron dynamic-K diagnostics
# ----------------------------------------------------------------------

def table_dynamic_k(rows_by_cond: dict[str, list[dict]]) -> None:
    print()
    print("## Table 4 — Dynamic-K diagnostics (mega-tron only)")
    print()
    print("(mean K across all queries, K=0 abstain rate on nulls, "
          "branch-reason distribution)")
    print()
    table: list[list[str]] = []
    for cond, rs in rows_by_cond.items():
        if not cond.startswith("mega-tron"):
            continue
        ks = [r["K"] for r in rs if r["K"] is not None]
        if not ks:
            continue
        null_rs = [r for r in rs if r["stratum"] == "null"]
        in_dist_rs = [r for r in rs if r["stratum"] != "null"]
        k_mean_all = statistics.mean(ks)
        k_mean_in = statistics.mean(r["K"] for r in in_dist_rs) if in_dist_rs else 0.0
        k_mean_null = statistics.mean(r["K"] for r in null_rs) if null_rs else 0.0
        null_abstain_rate = sum(1 for r in null_rs if r["K"] == 0) / len(null_rs) if null_rs else 0.0
        reasons = Counter(r["K_reason"] for r in rs)
        top3 = ", ".join(f"{name}={c}" for name, c in reasons.most_common(3))
        table.append([
            cond,
            f"{k_mean_all:.2f}",
            f"{k_mean_in:.2f}",
            f"{k_mean_null:.2f}",
            f"{null_abstain_rate:.2f}",
            top3,
        ])
    if not table:
        print("_(no mega-tron conditions in results)_")
        return
    _print_md_table(
        ["Condition", "K_mean", "K_in_dist", "K_null", "K=0_null", "top reasons"],
        table,
    )


# ----------------------------------------------------------------------
# Driver
# ----------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    args = p.parse_args(argv)

    rows_by_cond = _load_rows(args.results_dir)
    if not rows_by_cond:
        print(f"No results found in {args.results_dir}", flush=True)
        return 1

    # Order: vanilla first, then mega-tron embedders (so the comparison
    # reads top-to-bottom as vanilla → router)
    desired = [
        "vanilla-codex",
        "vanilla-claude",
        "vanilla-gemini",
        "mega-tron-bge-m3",
        "mega-tron-skillret",
        "mega-tron-bge-small",
    ]
    ordered = {k: rows_by_cond[k] for k in desired if k in rows_by_cond}
    # Append any extras not in the canonical list (e.g. user-added)
    for k, v in rows_by_cond.items():
        if k not in ordered:
            ordered[k] = v

    table_headline(ordered)
    table_per_stratum(ordered)
    table_null_sub(ordered)
    table_dynamic_k(ordered)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
