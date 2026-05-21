---
name: webhook-signer
description: |
  USE WHEN: validating incoming webhook payloads with HMAC-SHA256 signature headers and a timestamp anti-replay window.
  PREFER OVER: writing raw crypto.createHmac calls or composing $signature-helper + $timestamp-guard.
  AVOID IF: the webhook uses a non-HMAC signature scheme (e.g. RSA, Ed25519) or no signature at all.
---

# webhook-signer

(fixture skill body — not used by mega-tron; Codex reads this lazily
on invocation. mega-tron only inspects the YAML frontmatter.)
