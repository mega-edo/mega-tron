"""Verdict lifecycle — write, store, accumulate, analyse.

This subpackage owns everything that happens to a HELPFUL/HARMFUL/
NEUTRAL judgment between the moment a Stop hook captures
it and the moment a routing decision blends it back in:

- :mod:`mega_tron.verdicts.writer`     — dual-write entry point that
  Stop hooks call (one verdict in, frontmatter + SQLite + embedding
  out).
- :mod:`mega_tron.verdicts.store`      — SQLite spine. Time-series
  verdict rows + FTS5 full-text index over reasons. Source of truth
  for time-windowed queries.
- :mod:`mega_tron.verdicts.mega_meta`  — SKILL.md ``mega_meta:``
  frontmatter accessor. Cumulative counters + helpful/harmful
  contexts; source of truth for the routing-time ROI blend.
- :mod:`mega_tron.verdicts.embeddings` — verdict-text embedding store
  consumed by the ranker's context-match term.
- :mod:`mega_tron.verdicts.regressions` — time-windowed classifier
  ("broken" / "regressed" / "unused" / "stable") over the SQLite
  time-series.
- :mod:`mega_tron.verdicts.migration`  — one-shot frontmatter →
  SQLite migration with rollback support. The two stores were
  designed to evolve together; migration carries the legacy
  frontmatter rows in.

This subpackage exposes no curated namespace re-exports — import the
specific submodule you need (``from mega_tron.verdicts.store import
Store``). The package boundary just clusters six closely-related
files; it is not an attempt at a stable facade.
"""
