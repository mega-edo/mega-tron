"""`mega-tron why` — score-decomposition explainer for ranking.

Shows how each term (semantic cosine, count_bonus, context_term, status
multiplier) contributed to ``final`` for a given ticket × skill pair.
Useful for debugging why a particular skill ranked where it did.
"""
from __future__ import annotations

import argparse
import json
import sys

from mega_tron.cli._common import _make_router


def cmd_why(args: argparse.Namespace) -> int:
    """Print the score decomposition for a ticket × skill (or top-N skills).

    Reads the cache via :class:`Router`, computes
    :func:`ranker.adjusted_score_breakdown` for each requested skill, and
    renders a table that shows how each term contributed to ``final``.
    """
    from mega_tron.verdicts.mega_meta import MegaMeta
    from mega_tron.ranker import adjusted_score_breakdown
    from mega_tron.dynamic_k import silence_penalty

    router = _make_router(args)
    router.warmup()
    entries = router.cache.entries()
    # Surfaced counts are read from the routes table once and cached
    # on the router instance. Used by the silence_penalty row below
    # (the same signal dynamic_k uses at K-selection time).
    surfaced_counts = router._ensure_surfaced_counts()  # noqa: SLF001 — same module's helper
    if not entries:
        print(f"[why] no cached skills under {args.skills_dir}", file=sys.stderr)
        return 1

    import numpy as np

    query_text = args.ticket
    q_vec = router.embedder.embed([query_text])  # (1, dim)
    full = router.cache.embeddings_matrix()
    nameonly = router.cache.name_embeddings_matrix()
    scores_full = (full @ q_vec.T).ravel()
    scores_name = (nameonly @ q_vec.T).ravel()
    semantic = np.maximum(scores_full, scores_name)
    q_flat = q_vec[0]

    # Select which entries to break down.
    if args.skill:
        idx_by_name = {e.name: i for i, e in enumerate(entries)}
        if args.skill not in idx_by_name:
            print(
                f"[why] skill {args.skill!r} not in cache "
                f"(known: {sorted(idx_by_name)[:8]}...)",
                file=sys.stderr,
            )
            return 1
        selected = [idx_by_name[args.skill]]
    else:
        # Top-N by adjusted score (using full blend so the displayed ranking
        # matches what `rank` would surface).
        from mega_tron.ranker import adjusted_score

        blended = np.array([
            adjusted_score(
                semantic=float(semantic[i]),
                query_vec=q_flat,
                meta=MegaMeta(
                    helpful_count=e.helpful_count,
                    harmful_count=e.harmful_count,
                    helpful_contexts=list(e.helpful_contexts),
                    harmful_contexts=list(e.harmful_contexts),
                    status=e.status,
                    consecutive_harmful=e.consecutive_harmful,
                ),
                helpful_ctx_embs=e.embedding_helpful_ctxs,
                harmful_ctx_embs=e.embedding_harmful_ctxs,
            )
            for i, e in enumerate(entries)
        ])
        selected = list(np.argsort(-blended)[:args.top_skills])

    payloads = []
    for i in selected:
        entry = entries[i]
        meta = MegaMeta(
            helpful_count=entry.helpful_count,
            harmful_count=entry.harmful_count,
            helpful_contexts=list(entry.helpful_contexts),
            harmful_contexts=list(entry.harmful_contexts),
            status=entry.status,
            consecutive_harmful=entry.consecutive_harmful,
        )
        bd = adjusted_score_breakdown(
            semantic=float(semantic[i]),
            query_vec=q_flat,
            meta=meta,
            helpful_ctx_embs=entry.embedding_helpful_ctxs,
            harmful_ctx_embs=entry.embedding_harmful_ctxs,
        )
        bd["name"] = entry.name
        bd["full_cos"] = float(scores_full[i])
        bd["name_cos"] = float(scores_name[i])
        # Silence penalty — applied to the K-selection copy of scores in
        # dynamic_k, NOT subtracted from `final`. Reported here for
        # transparency only.
        surfaced = int(surfaced_counts.get(entry.name, 0))
        bd["surfaced"] = surfaced
        bd["silence_penalty"] = float(
            silence_penalty(surfaced, entry.helpful_count, entry.harmful_count)
        )
        payloads.append(bd)

    if args.json:
        json.dump(payloads, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    for bd in payloads:
        weights = bd["weights"]
        print(f"\n  ticket: {query_text!r}")
        print(f"  skill:  {bd['name']}")
        print(
            f"  semantic       {bd['semantic']:+.4f}  "
            f"(full={bd['full_cos']:+.4f}, name={bd['name_cos']:+.4f})"
        )
        print(
            f"  count_bonus   {bd['count_bonus_contribution']:+.4f}  "
            f"(h={bd['helpful_count']}, ha={bd['harmful_count']}, "
            f"raw={bd['count_bonus_raw']:+.4f}, w={weights['count']:.2f})"
        )
        print(
            f"  context_match {bd['context_term_contribution']:+.4f}  "
            f"(help={bd['helpful_match']:+.4f}, "
            f"harm={bd['harmful_match']:+.4f} × {weights['harm']:.2f}, "
            f"w={weights['context']:.2f})"
        )
        print(
            f"  status_mult   × {bd['status_multiplier']:.2f}  "
            f"({bd['status']})"
        )
        print(f"  ─────────────────────────")
        print(f"  final          {bd['final']:+.4f}")
        # Silence penalty is informational: it does NOT subtract from
        # final. It affects only whether dynamic_k keeps this candidate
        # in the top-K. Surfaced count comes from the routes table.
        print(
            f"  silence_penalty −{bd['silence_penalty']:.4f}  "
            f"(surfaced={bd['surfaced']}, h={bd['helpful_count']}, "
            f"ha={bd['harmful_count']};  K-selection only, "
            f"does not change displayed final)"
        )
    return 0
