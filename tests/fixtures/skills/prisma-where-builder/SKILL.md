---
name: prisma-where-builder
description: |
  USE WHEN: composing a Prisma findMany / findFirst query whose where clause must combine three or more fields with mixed equality / range / contains operators.
  PREFER OVER: hand-writing nested AND/OR objects, which is error-prone and silently drops typos.
  AVOID IF: the query has only one or two filters — direct Prisma syntax is clearer.
---

# prisma-where-builder

(fixture skill body — not used by mega-tron; Codex reads this lazily
on invocation. mega-tron only inspects the YAML frontmatter.)
