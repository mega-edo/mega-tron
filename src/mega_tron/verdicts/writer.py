"""Lightweight verdict dual-write helper for host Stop hooks.

The Stop hooks (codex / claude_code / gemini_cli) need to persist
verdicts to BOTH the SKILL.md ``mega_meta:`` frontmatter AND the
SQLite ``verdicts`` time-series table — without paying the
``MegaCore`` construction cost (which spins up an Embedder + Router,
i.e. PyTorch). This helper is the small surface the hooks actually
need.

Behaviour mirrors :meth:`mega_tron.core.MegaCore.record_verdicts`:

- If the default store file exists, insert one row per HELPFUL /
  HARMFUL / NEUTRAL verdict into ``verdicts`` (host-tagged, dedup'd
  via ``UNIQUE(session_id, skill_name, host)``).
- Always update the SKILL.md frontmatter via
  :func:`mega_tron.verdicts.mega_meta.apply_evaluations` so ``cat SKILL.md``
  keeps showing the running counters.
- When ``MEGA_VERDICT_EMBED`` is enabled and the verdict carries a
  reason, also embed the reason text and append it to
  :class:`mega_tron.verdicts.embeddings.VerdictEmbeddingsStore`.
  This populates the self-improving routing signal the Router
  consumes via the ``related_helpful_max`` / ``related_harmful_max``
  inputs to :func:`mega_tron.ranker.adjusted_score`.

The SQLite + embedding paths are best-effort: any error there is
logged but does not block the frontmatter write — the same fail-open
contract the host Stop hooks already follow for everything else.

The embedding path is gated by ``MEGA_VERDICT_EMBED`` so installs
without sentence-transformers / numpy / etc. (or hooks that need to
finish in < 100ms) can opt out. Default: enabled when the embedder
package imports cleanly.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Iterable, Literal


HostName = Literal["codex", "claude_code", "gemini_cli", "hermes", "other"]


def _verdict_embedding_enabled() -> bool:
    """Return whether to embed verdict reasons.

    ``MEGA_VERDICT_EMBED=0`` disables the path entirely (the host
    hook stays sub-100ms because it skips PyTorch cold-load).
    Default: on — installs without an embedder dep will still no-op
    silently when the import fails.
    """
    raw = os.environ.get("MEGA_VERDICT_EMBED", "").strip().lower()
    if not raw:
        return True
    return raw not in ("0", "false", "no", "off")


def persist_verdicts(
    *,
    skills_dir: Path,
    verdicts: Iterable[dict],
    host: HostName,
    session_id: str | None,
    log_prefix: str = "[mega-tron verdict]",
) -> "Any":
    """Dual-write ``verdicts`` (legacy + SQLite + verdict embeddings).
    Returns the :class:`EvaluationOutcome` from the legacy frontmatter
    write so the caller can keep its existing logging.

    ``verdicts`` matches the legacy ``apply_evaluations`` schema —
    a list of dicts each carrying ``skill`` / ``verdict`` /
    ``reason``. We don't introduce a new wire format here; the host
    hook keeps parsing the model's last message into the same dicts
    it always has.
    """
    from mega_tron.verdicts.mega_meta import apply_evaluations

    items = list(verdicts)

    # Guarantee a session_id. Without this, multiple Stop-hook fires
    # from the same host with no session in the payload would all
    # write rows with ``session_id IS NULL``; SQLite's UNIQUE
    # constraint treats NULL as distinct, so dedup is silently
    # bypassed and the verdicts table accumulates dupes. The
    # fallback embeds the hook fire time so retries within the same
    # second still collapse, while genuine reruns get distinct ids.
    if not session_id or not isinstance(session_id, str):
        import datetime as _dt

        session_id = (
            f"_anon-{host}-"
            f"{_dt.datetime.now(_dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        )

    # ---- SQLite write (best-effort; auto-creates the store on first
    # verdict so a fresh install starts collecting time-series data
    # without needing a separate migration step) ----
    inserted: list[tuple[int, str, str, str | None]] = []
    # ^^^ (verdict_id, skill_name, label, reason) for the embedding path.
    try:
        from mega_tron.config import store_path
        from mega_tron.verdicts.store import Store

        store = Store(store_path())
        store.initialize()
        for v in items:
            name = v.get("skill")
            label = (v.get("verdict") or "").upper()
            # Only HELPFUL/HARMFUL/NEUTRAL are persisted; any other label
            # is silently dropped.
            if not name or label not in ("HELPFUL", "HARMFUL", "NEUTRAL"):
                continue
            reason = v.get("reason")
            try:
                written = store.record_verdict(
                    skill_name=name,
                    verdict=label,
                    host=host,
                    reason=reason,
                    session_id=session_id,
                    skill_dir=str(skills_dir / name),
                )
            except Exception as e:  # noqa: BLE001
                print(
                    f"{log_prefix} SQLite write failed for "
                    f"{name!r}: {e}",
                    file=sys.stderr,
                )
                continue
            if not written or not reason:
                # written=False on dedup retry → embedding already
                # exists; skip. No reason → nothing to embed.
                continue
            # Fetch the row id of the verdict we just inserted so the
            # embedding store can cross-reference back into the
            # time-series. The (session_id, skill_name, host) tuple
            # is unique post-insert (we just wrote it), so a most-
            # recent-row lookup on that combination is exact.
            try:
                with store._connect() as conn:
                    if session_id is None:
                        cur = conn.execute(
                            "SELECT id FROM verdicts "
                            "WHERE skill_name=? AND host=? AND session_id IS NULL "
                            "ORDER BY id DESC LIMIT 1",
                            (name, host),
                        )
                    else:
                        cur = conn.execute(
                            "SELECT id FROM verdicts "
                            "WHERE skill_name=? AND host=? AND session_id=? "
                            "ORDER BY id DESC LIMIT 1",
                            (name, host, session_id),
                        )
                    row = cur.fetchone()
                if row is not None:
                    inserted.append((int(row[0]), name, label, reason))
            except Exception as e:  # noqa: BLE001
                # Looking up the id is best-effort; skipping the
                # embedding path is fine.
                print(
                    f"{log_prefix} id-lookup failed for {name!r}: {e}",
                    file=sys.stderr,
                )
    except Exception as e:  # noqa: BLE001
        # Store init or import failure — never block the legacy path.
        print(
            f"{log_prefix} SQLite write skipped (store unavailable): {e}",
            file=sys.stderr,
        )

    # ---- Verdict-embedding write (best-effort; gated by env var) ----
    if inserted and _verdict_embedding_enabled():
        try:
            from mega_tron.embedder import fingerprint_of, make_embedder
            from mega_tron.verdicts.embeddings import (
                AUTO_COMPACT_COSINE,
                AUTO_COMPACT_THRESHOLD,
                VerdictEmbeddingsStore,
            )

            embedder = make_embedder()
            reasons = [r for _, _, _, r in inserted]
            embeddings = embedder.embed(reasons)  # (N, dim) L2-normalised
            ves = VerdictEmbeddingsStore(
                fingerprint=fingerprint_of(embedder)
            )
            for (vid, name, label, _reason), vec in zip(
                inserted, embeddings
            ):
                ves.append(
                    verdict_id=vid,
                    skill_name=name,
                    label=label,
                    embedding=vec,
                )

            # Auto-compact when the corpus crosses the high-water
            # mark. Cheap (per-group matmul), runs at most once per
            # stop-hook batch. The SQLite verdicts table is untouched
            # so regression analysis remains time-series-true.
            if len(ves) > AUTO_COMPACT_THRESHOLD:
                report = ves.compact(
                    threshold=AUTO_COMPACT_COSINE,
                    get_reason=store.get_verdict_reason,
                )
                if report["removed"] > 0:
                    print(
                        f"{log_prefix} compacted verdict embeddings: "
                        f"{report['before']} → {report['after']} "
                        f"(collapsed {report['clusters_collapsed']} "
                        f"near-duplicate clusters across "
                        f"{report['groups_scanned']} (skill,label) groups)",
                        file=sys.stderr,
                    )

            ves.save()
        except Exception as e:  # noqa: BLE001
            print(
                f"{log_prefix} verdict-embedding write skipped: {e}",
                file=sys.stderr,
            )

    # ---- Frontmatter write (canonical for cumulative counters) ----
    return apply_evaluations(
        skills_dir=skills_dir,
        evaluations=items,
        session_id=session_id,
    )


__all__ = ["persist_verdicts"]
