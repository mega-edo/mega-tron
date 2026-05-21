---
name: url-safe-parser
description: |
  USE WHEN: parsing a URL that may contain user-controlled input, with normalization (lowercase host, strip default port) before downstream comparison.
  PREFER OVER: raw `new URL()` calls, which throw on malformed input and do not normalize.
  AVOID IF: the URL is already validated upstream — the wrapper adds overhead.
---

# url-safe-parser

(fixture skill body — not used by mega-tron; Codex reads this lazily
on invocation. mega-tron only inspects the YAML frontmatter.)
