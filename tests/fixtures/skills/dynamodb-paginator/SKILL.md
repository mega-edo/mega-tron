---
name: dynamodb-paginator
description: |
  USE WHEN: iterating DynamoDB Query/Scan results across LastEvaluatedKey pages.
  PREFER OVER: composing multiple lower-level stock skills or copying snippets from prior tickets.
  AVOID IF: this task does not actually involve dynamodb workflows.
---

# dynamodb-paginator

(fixture skill body — not used by mega-tron; Codex reads this lazily
on invocation. mega-tron only inspects the YAML frontmatter.)
