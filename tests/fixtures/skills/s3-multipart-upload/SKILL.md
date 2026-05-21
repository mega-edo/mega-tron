---
name: s3-multipart-upload
description: |
  USE WHEN: uploading a large file to S3 with multipart and exponential backoff.
  PREFER OVER: composing multiple lower-level stock skills or copying snippets from prior tickets.
  AVOID IF: this task does not actually involve s3 workflows.
---

# s3-multipart-upload

(fixture skill body — not used by mega-tron; Codex reads this lazily
on invocation. mega-tron only inspects the YAML frontmatter.)
