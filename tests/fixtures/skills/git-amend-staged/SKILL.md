---
name: git-amend-staged
description: |
  USE WHEN: a small fixup is needed on the last local commit and the branch has NOT been pushed.
  PREFER OVER: composing $git-reset + $git-add + $git-commit, which loses the original commit metadata.
  AVOID IF: the branch is shared (pushed to origin) — amending would require force-push.
---

# git-amend-staged

(fixture skill body — not used by mega-tron; Codex reads this lazily
on invocation. mega-tron only inspects the YAML frontmatter.)
