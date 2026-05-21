---
name: postgres-tx-wrapper
description: |
  USE WHEN: wrapping a multi-statement Postgres operation in a savepoint-aware transaction.
  PREFER OVER: composing multiple lower-level stock skills or copying snippets from prior tickets.
  AVOID IF: this task does not actually involve postgres workflows.
---

# postgres-tx-wrapper

(fixture skill body — not used by mega-tron; Codex reads this lazily
on invocation. mega-tron only inspects the YAML frontmatter.)
