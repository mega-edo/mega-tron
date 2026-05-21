"""Post-process feedback-loop experiment results into metrics + 4 graphs.

Reads c{0,3}_round_*.jsonl + c{0,3}_turns.jsonl produced by run.py,
emits summary.json and 4 PNG figures.

Usage:
  uv run python analyze.py <results_dir>

The results_dir is typically benchmarks/feedback_loop/results/<UTC-timestamp>.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean


FIXTURE_ROOT = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# Tier lookup (which tier does each skill belong to)
# ---------------------------------------------------------------------------


def build_tier_lookup() -> dict[str, str]:
    """Map skill_name → tier (real/poisoned/competing/noise) from pool/ layout.

    The directory name on disk is what the router uses as the skill
    name *unless* the frontmatter overrides it. For regex-debugger
    the frontmatter says `regex-visual-debugger`, so we also read
    each SKILL.md frontmatter `name:` and use that when present.
    """
    pool_root = FIXTURE_ROOT / "pool"
    out: dict[str, str] = {}
    for tier in ("real", "poisoned", "competing", "noise"):
        tier_dir = pool_root / tier
        if not tier_dir.exists():
            continue
        for entry in tier_dir.iterdir():
            if not entry.is_dir():
                continue
            skill_md = entry / "SKILL.md"
            if not skill_md.exists():
                continue
            # Default to dir name
            name = entry.name
            # Override with frontmatter name if present
            try:
                head = skill_md.read_text(errors="replace").splitlines()[:30]
                for line in head:
                    line = line.strip()
                    if line.startswith("name:"):
                        v = line.split(":", 1)[1].strip().strip("\"'")
                        if v:
                            name = v
                        break
            except Exception:
                pass
            out[name] = tier
    return out


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def load_round_log(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def compute_metrics(results_dir: Path, tier_lookup: dict[str, str]) -> dict:
    """Aggregate per (cond, round) metrics across all measurement files."""
    metrics: dict[str, dict] = {}
    for cond in ("c0", "c3"):
        per_round = {}
        for round_path in sorted(results_dir.glob(f"{cond}_round_*.jsonl")):
            r = int(round_path.stem.split("_")[-1])
            rows = load_round_log(round_path)
            # Build per-prompt rank lookup
            ranks_per_prompt: dict[str, dict[str, int]] = defaultdict(dict)
            scores_per_prompt: dict[str, dict[str, float]] = defaultdict(dict)
            status_per_prompt: dict[str, dict[str, str]] = defaultdict(dict)
            for row in rows:
                pid = row["prompt_id"]
                name = row["skill_name"]
                ranks_per_prompt[pid][name] = row["rank"]
                scores_per_prompt[pid][name] = row["score"]
                if row.get("status"):
                    status_per_prompt[pid][name] = row["status"]

            # Tier rank averages (over prompts that have at least one in-dist skill we expect)
            real_ranks, poisoned_ranks, competing_ranks, noise_ranks = [], [], [], []
            for pid, ranks in ranks_per_prompt.items():
                for name, rk in ranks.items():
                    tier = tier_lookup.get(name)
                    if tier == "real":
                        real_ranks.append(rk)
                    elif tier == "poisoned":
                        poisoned_ranks.append(rk)
                    elif tier == "competing":
                        competing_ranks.append(rk)
                    elif tier == "noise":
                        noise_ranks.append(rk)

            # Gold rank per in-dist prompt (only the expected_skill)
            gold_ranks = []
            poisoned_trap_ranks = []
            score_gaps = []  # real - poisoned, for trap prompts only
            # Hit-rate counters: how many in-dist prompts have gold in top-K.
            # Computed from the per-prompt rank lookup so we count one event
            # per prompt regardless of how many ranked rows it has.
            hit_at_1 = 0
            hit_at_3 = 0
            hit_at_5 = 0
            n_indist_prompts = 0
            for row in rows:
                if row.get("expected_skill") and row["skill_name"] == row["expected_skill"]:
                    gold_ranks.append(row["rank"])
            # One row per prompt for hit-rate (use first occurrence: gold may
            # not be in top-10 at all, in which case it gets no row — we
            # count that as a miss against every K).
            indist_prompt_ids = {
                row["prompt_id"]
                for row in rows
                if row.get("expected_skill")
            }
            n_indist_prompts = len(indist_prompt_ids)
            for pid in indist_prompt_ids:
                gold_name = next(
                    (row["expected_skill"] for row in rows if row["prompt_id"] == pid),
                    None,
                )
                if not gold_name:
                    continue
                rk = ranks_per_prompt.get(pid, {}).get(gold_name)
                if rk is None:
                    continue  # gold not in top-10, counts as miss everywhere
                if rk <= 1:
                    hit_at_1 += 1
                if rk <= 3:
                    hit_at_3 += 1
                if rk <= 5:
                    hit_at_5 += 1

            # Trap prompt analysis (need to know which prompts are traps)
            trap_pairs = _load_trap_pairs()
            for pid, gold_name in trap_pairs.items():
                poisoned_name = _load_poison_pairs().get(gold_name)
                if not poisoned_name:
                    continue
                poisoned_rank = ranks_per_prompt.get(pid, {}).get(poisoned_name)
                if poisoned_rank is not None:
                    poisoned_trap_ranks.append(poisoned_rank)
                gold_score = scores_per_prompt.get(pid, {}).get(gold_name)
                poisoned_score = scores_per_prompt.get(pid, {}).get(poisoned_name)
                if gold_score is not None and poisoned_score is not None:
                    score_gaps.append(gold_score - poisoned_score)

            # Archive tracking — any poisoned skill with status=archived
            archived_poisoned = set()
            for pid, statuses in status_per_prompt.items():
                for name, st in statuses.items():
                    if st == "archived" and tier_lookup.get(name) == "poisoned":
                        archived_poisoned.add(name)

            per_round[r] = {
                "real_avg_rank": mean(real_ranks) if real_ranks else None,
                "poisoned_avg_rank_all": mean(poisoned_ranks) if poisoned_ranks else None,
                "poisoned_trap_avg_rank": mean(poisoned_trap_ranks) if poisoned_trap_ranks else None,
                "competing_avg_rank": mean(competing_ranks) if competing_ranks else None,
                "noise_avg_rank": mean(noise_ranks) if noise_ranks else None,
                "gold_avg_rank": mean(gold_ranks) if gold_ranks else None,
                "score_gap_avg": mean(score_gaps) if score_gaps else None,
                "archived_poisoned": sorted(archived_poisoned),
                # Hit-rate of in-dist prompts whose gold landed in top-K.
                # Denominator is the count of in-dist prompts (10 in the
                # full fixture). Missing-from-top-10 prompts count as a
                # miss across all K's.
                "hit_at_1": hit_at_1 / n_indist_prompts if n_indist_prompts else None,
                "hit_at_3": hit_at_3 / n_indist_prompts if n_indist_prompts else None,
                "hit_at_5": hit_at_5 / n_indist_prompts if n_indist_prompts else None,
                "n_indist_prompts": n_indist_prompts,
            }
        metrics[cond] = per_round
    return metrics


def _load_trap_pairs() -> dict[str, str]:
    """prompt_id → gold skill name for trap prompts."""
    import yaml
    fx = yaml.safe_load((FIXTURE_ROOT / "fixtures.yaml").read_text())
    out = {}
    for row in fx.get("in_distribution", []):
        if row["id"].startswith("t_"):
            out[row["id"]] = row["expected_skill"]
    return out


def _load_poison_pairs() -> dict[str, str]:
    """real_skill_name → poisoned_skill_name."""
    mf = json.loads((FIXTURE_ROOT / "manifest.json").read_text())
    return mf.get("poison_pairs", {})


def compute_turn_stats(results_dir: Path) -> dict:
    """How often did the model emit a parseable <skill-used> tag."""
    stats = {}
    for cond in ("c0", "c3"):
        turn_path = results_dir / f"{cond}_turns.jsonl"
        if not turn_path.exists():
            continue
        total = 0
        with_tag = 0
        with_verdict = 0
        verdict_counts = defaultdict(int)
        for line in turn_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            total += 1
            tags = row.get("tags") or []
            if tags:
                with_tag += 1
                for t in tags:
                    v = (t.get("verdict") or "").upper()
                    if v in ("HELPFUL", "HARMFUL", "NEUTRAL"):
                        with_verdict += 1
                        verdict_counts[v] += 1
        stats[cond] = {
            "turns": total,
            "turns_with_any_tag": with_tag,
            "tags_with_valid_verdict": with_verdict,
            "verdict_breakdown": dict(verdict_counts),
            "verdict_emit_rate": with_tag / total if total else 0.0,
        }
    return stats


# ---------------------------------------------------------------------------
# Graphs
# ---------------------------------------------------------------------------


def make_graphs(results_dir: Path, metrics: dict) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    graphs_dir = results_dir / "graphs"
    graphs_dir.mkdir(exist_ok=True)

    # User-facing labels for the two conditions. The internal data keys
    # stay "c0"/"c3" (those flow through summary.json and the runner) but
    # graph readers see the descriptive names.
    LABELS = {"c0": "Semantic search only", "c3": "mega-tron"}

    # Get round axis from whichever cond has data
    rounds = []
    for cond_data in metrics.values():
        rounds = sorted(cond_data.keys())
        if rounds:
            break

    # --- Graph 1: top-K hit rate (the "upward trend" headline graph) ---
    # This is the cleanest "is routing improving?" signal: % of in-dist
    # prompts whose gold lands in top-1 / top-3 / top-5. If verdict
    # feedback is working, C3 lines rise across rounds while C0 stays
    # flat.
    fig, ax = plt.subplots(figsize=(10, 6))
    for k_key, k_label, color in [
        ("hit_at_1", "top-1", "tab:red"),
        ("hit_at_3", "top-3", "tab:green"),
        ("hit_at_5", "top-5", "tab:blue"),
    ]:
        for cond, style in [("c0", "--"), ("c3", "-")]:
            ys = [
                (metrics.get(cond, {}).get(r, {}).get(k_key) or 0.0) * 100
                for r in rounds
            ]
            ax.plot(
                rounds, ys, style, color=color, marker="o",
                label=f"{k_label} — {LABELS[cond]}", alpha=0.85,
            )
    ax.set_xlabel("session #")
    ax.set_ylabel("right skill in top-K (%)")
    ax.set_title(
        "Routing accuracy over sessions — higher is better\n"
        f"Dashed = {LABELS['c0']}   |   Solid = {LABELS['c3']}"
    )
    ax.set_ylim(0, 105)
    ax.grid(alpha=0.3)
    ax.legend(loc="best", ncol=2, fontsize=8)
    fig.tight_layout()
    fig.savefig(graphs_dir / "1_hit_rate.png", dpi=120)
    plt.close(fig)

    # --- Graph 2: score gap (real - poisoned) on trap prompts ---
    # The "did the blend invert the trap?" graph. Negative means raw
    # cosine still ranks poisoned above real; positive means feedback
    # has flipped the ordering.
    fig, ax = plt.subplots(figsize=(10, 5))
    for cond, style, color in [("c0", "--", "tab:blue"), ("c3", "-", "tab:purple")]:
        ys = [metrics.get(cond, {}).get(r, {}).get("score_gap_avg") for r in rounds]
        ax.plot(rounds, ys, style, color=color, marker="o", label=LABELS[cond])
    ax.axhline(0, color="black", linewidth=0.5)
    # Annotate the two zones so readers don't have to remember the sign convention.
    ax.text(
        0.99, 0.97, "above 0  →  real skill wins",
        transform=ax.transAxes, ha="right", va="top",
        fontsize=9, color="tab:green",
    )
    ax.text(
        0.99, 0.25, "below 0  →  trap skill wins",
        transform=ax.transAxes, ha="right", va="bottom",
        fontsize=9, color="tab:red",
    )
    ax.set_xlabel("session #")
    ax.set_ylabel("real-skill score  −  trap-skill score")
    ax.set_title("Did the router figure out the traps?")
    ax.grid(alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(graphs_dir / "2_score_gap.png", dpi=120)
    plt.close(fig)

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: analyze.py <results_dir>")
        return 2
    results_dir = Path(sys.argv[1]).resolve()
    if not results_dir.exists():
        print(f"results_dir not found: {results_dir}", file=sys.stderr)
        return 1
    tier_lookup = build_tier_lookup()
    print(f"tier_lookup size: {len(tier_lookup)}")
    metrics = compute_metrics(results_dir, tier_lookup)
    turn_stats = compute_turn_stats(results_dir)
    summary = {
        "results_dir": str(results_dir),
        "tier_pool_size": len(tier_lookup),
        "metrics": metrics,
        "turn_stats": turn_stats,
    }
    (results_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {results_dir / 'summary.json'}")

    # Tabular console output
    print()
    print(f"{'cond':4} {'round':>5} {'real':>6} {'pois(trap)':>11} {'compet':>7} {'noise':>6} {'gold':>6} {'gap':>7} {'archived':>9}")
    for cond in ("c0", "c3"):
        for r in sorted(metrics.get(cond, {}).keys()):
            m = metrics[cond][r]
            fmt = lambda v: "-" if v is None else f"{v:>6.2f}"
            archived_count = len(m.get("archived_poisoned") or [])
            print(
                f"{cond:4} {r:>5d} {fmt(m['real_avg_rank']):>6} {fmt(m['poisoned_trap_avg_rank']):>11} "
                f"{fmt(m['competing_avg_rank']):>7} {fmt(m['noise_avg_rank']):>6} "
                f"{fmt(m['gold_avg_rank']):>6} {fmt(m['score_gap_avg']):>7} {archived_count:>9d}"
            )
    print()
    print("turn stats:")
    for cond, stats in turn_stats.items():
        print(f"  {cond}: {stats}")

    try:
        make_graphs(results_dir, metrics)
        print(f"wrote graphs to {results_dir / 'graphs'}")
    except Exception as e:
        print(f"[warn] graph generation failed: {e}", file=sys.stderr)

    # --- One-line effect-size summary (C3 vs C0 at the last round) -----------
    # Headline metrics for "did verdict-feedback help, and by how much".
    # Compared at the final round so the effect is the cumulative outcome
    # of every verdict the model emitted across the experiment.
    print()
    print("=" * 72)
    print("Headline effect (C3 vs C0, final round)")
    print("=" * 72)
    c0_rounds = sorted(metrics.get("c0", {}).keys())
    c3_rounds = sorted(metrics.get("c3", {}).keys())
    if c0_rounds and c3_rounds:
        r_last = min(c0_rounds[-1], c3_rounds[-1])
        c0_first = metrics["c0"].get(c0_rounds[0], {})
        c0_last = metrics["c0"].get(r_last, {})
        c3_first = metrics["c3"].get(c3_rounds[0], {})
        c3_last = metrics["c3"].get(r_last, {})

        def _diff(a, b):
            if a is None or b is None:
                return None
            return a - b

        def _pct(v):
            if v is None:
                return "-"
            return f"{v * 100:+.1f}pp"

        def _rank(v):
            if v is None:
                return "-"
            return f"{v:.2f}"

        # Gold rank improvement
        c3_gold_delta = _diff(c3_first.get("gold_avg_rank"), c3_last.get("gold_avg_rank"))
        c0_gold_delta = _diff(c0_first.get("gold_avg_rank"), c0_last.get("gold_avg_rank"))
        net_gold_gain = _diff(c3_gold_delta, c0_gold_delta)

        # Score gap (gold − poisoned) on trap prompts
        c3_gap_delta = _diff(c3_last.get("score_gap_avg"), c3_first.get("score_gap_avg"))
        c0_gap_delta = _diff(c0_last.get("score_gap_avg"), c0_first.get("score_gap_avg"))

        # Hit rate improvement (top-3 — most useful production-side metric)
        c3_h3_delta = _diff(c3_last.get("hit_at_3"), c3_first.get("hit_at_3"))
        c0_h3_delta = _diff(c0_last.get("hit_at_3"), c0_first.get("hit_at_3"))

        # Archived poisoned at last round
        c0_archived = len(c0_last.get("archived_poisoned") or [])
        c3_archived = len(c3_last.get("archived_poisoned") or [])

        print(f"gold avg rank        C0: {_rank(c0_first.get('gold_avg_rank'))} -> {_rank(c0_last.get('gold_avg_rank'))}   "
              f"C3: {_rank(c3_first.get('gold_avg_rank'))} -> {_rank(c3_last.get('gold_avg_rank'))}   "
              f"net gain (C3 over C0): {_rank(net_gold_gain) if net_gold_gain is not None else '-'} ranks")
        print(f"score gap (trap)     C0 delta: {_rank(c0_gap_delta) if c0_gap_delta is not None else '-'}   "
              f"C3 delta: {_rank(c3_gap_delta) if c3_gap_delta is not None else '-'}")
        print(f"top-3 hit rate       C0: {_pct(c0_first.get('hit_at_3'))} -> {_pct(c0_last.get('hit_at_3'))} (delta {_pct(c0_h3_delta)})   "
              f"C3: {_pct(c3_first.get('hit_at_3'))} -> {_pct(c3_last.get('hit_at_3'))} (delta {_pct(c3_h3_delta)})")
        print(f"poisoned archived    C0: {c0_archived} / 5   C3: {c3_archived} / 5")
        print()
        # Interpretation hint — three plain-English verdicts on the data.
        # Tuned permissively: any positive movement on either signal counts
        # as "directional support"; the user can sanity-check from the
        # graphs whether the magnitude is meaningful for their use.
        verdict_lines: list[str] = []
        if net_gold_gain is not None and net_gold_gain > 0:
            verdict_lines.append(f"  ✓ C3 improved gold rank by {net_gold_gain:.2f} more than C0 over the run")
        elif net_gold_gain is not None and net_gold_gain <= 0:
            verdict_lines.append(f"  ✗ C3 did not improve gold rank vs C0 (net = {net_gold_gain:+.2f})")
        if c3_gap_delta is not None and c0_gap_delta is not None and c3_gap_delta > c0_gap_delta:
            verdict_lines.append(f"  ✓ C3 widened the gold−poisoned score gap by {c3_gap_delta - c0_gap_delta:+.3f} more than C0")
        if c3_archived > c0_archived:
            verdict_lines.append(f"  ✓ C3 archived {c3_archived - c0_archived} more poisoned skill(s) than C0")
        elif c3_archived == 0:
            verdict_lines.append("  · No poisoned archived under either condition (3-consecutive-HARMFUL streak not reached)")
        if verdict_lines:
            print("Interpretation:")
            for line in verdict_lines:
                print(line)
    else:
        print("(both conditions need data — re-run with --conditions c0 c3)")
    print("=" * 72)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
