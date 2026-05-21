"""Build 50 SKILL.md fixtures (5 hand-crafted + 45 programmatic).

Run with:
    cd ~/Downloads/mega-tron
    python tests/fixtures/build_fixtures.py

Idempotent — overwrites existing fixtures.
"""
from __future__ import annotations

import shutil
from pathlib import Path

FIXTURES = Path(__file__).parent / "skills"


HAND_CRAFTED: list[tuple[str, str]] = [
    (
        "webhook-signer",
        """USE WHEN: validating incoming webhook payloads with HMAC-SHA256 signature headers and a timestamp anti-replay window.
PREFER OVER: writing raw crypto.createHmac calls or composing $signature-helper + $timestamp-guard.
AVOID IF: the webhook uses a non-HMAC signature scheme (e.g. RSA, Ed25519) or no signature at all.""",
    ),
    (
        "git-amend-staged",
        """USE WHEN: a small fixup is needed on the last local commit and the branch has NOT been pushed.
PREFER OVER: composing $git-reset + $git-add + $git-commit, which loses the original commit metadata.
AVOID IF: the branch is shared (pushed to origin) — amending would require force-push.""",
    ),
    (
        "prisma-where-builder",
        """USE WHEN: composing a Prisma findMany / findFirst query whose where clause must combine three or more fields with mixed equality / range / contains operators.
PREFER OVER: hand-writing nested AND/OR objects, which is error-prone and silently drops typos.
AVOID IF: the query has only one or two filters — direct Prisma syntax is clearer.""",
    ),
    (
        "linear-issue-resolver",
        """USE WHEN: a PR is ready to merge and the corresponding Linear ticket should transition to Done with the PR URL attached.
PREFER OVER: opening the Linear UI manually or composing $linear-comment + $linear-state-transition.
AVOID IF: the ticket has subtasks still open — manual review of dependencies is required first.""",
    ),
    (
        "url-safe-parser",
        """USE WHEN: parsing a URL that may contain user-controlled input, with normalization (lowercase host, strip default port) before downstream comparison.
PREFER OVER: raw `new URL()` calls, which throw on malformed input and do not normalize.
AVOID IF: the URL is already validated upstream — the wrapper adds overhead.""",
    ),
]


# Programmatic fixtures cover a wide enough surface that semantic ranking has
# room to distinguish. We mix domains (web/db/git/test/cloud) and verbs.
DOMAINS = [
    ("api-route-handler", "registering a new HTTP route handler with input validation and structured error envelopes"),
    ("rate-limiter-redis", "applying a per-user token-bucket rate limit backed by Redis SETEX"),
    ("jwt-verifier", "verifying a JWT bearer token with signature and audience claims"),
    ("oauth-callback", "handling an OAuth 2.0 redirect callback with PKCE code exchange"),
    ("session-cookie-setter", "setting an http-only secure session cookie with rolling expiry"),
    ("csrf-token-issuer", "issuing and validating a double-submit CSRF token"),
    ("password-hash-argon2", "hashing a user password with Argon2id at OWASP-recommended parameters"),
    ("totp-mfa-verifier", "verifying a TOTP one-time password during multi-factor auth"),
    ("sql-migration-up", "writing a forward database migration with idempotent guards"),
    ("sql-migration-down", "writing a safe database rollback migration without data loss"),
    ("postgres-tx-wrapper", "wrapping a multi-statement Postgres operation in a savepoint-aware transaction"),
    ("redis-pipeline-batch", "batching multiple Redis writes through a single pipeline for atomicity"),
    ("kafka-producer", "publishing an event to Kafka with idempotent producer config"),
    ("kafka-consumer", "consuming Kafka messages with manual offset commit and dead-letter handling"),
    ("s3-multipart-upload", "uploading a large file to S3 with multipart and exponential backoff"),
    ("s3-presigned-url", "issuing a short-lived presigned S3 URL for browser-direct download"),
    ("dynamodb-paginator", "iterating DynamoDB Query/Scan results across LastEvaluatedKey pages"),
    ("cloudfront-invalidator", "invalidating CloudFront edge cache for a path glob after deploy"),
    ("docker-multistage", "writing a Dockerfile with build-time vs runtime stage separation"),
    ("k8s-rollout-restart", "performing a kubectl rollout restart with readiness gate"),
    ("helm-values-template", "templating Helm chart values with environment-scoped overrides"),
    ("terraform-state-import", "importing existing cloud resources into Terraform state without recreating"),
    ("github-action-cache", "configuring a GitHub Actions cache key for monorepo build artifacts"),
    ("circleci-matrix-job", "defining a CircleCI matrix job across Node and Python versions"),
    ("playwright-page-fixture", "writing a Playwright test that waits for network idle before assertions"),
    ("vitest-mock-fetch", "mocking global fetch in a Vitest test with MSW handlers"),
    ("jest-snapshot-update", "updating a Jest snapshot only for an intended UI change"),
    ("cypress-network-stub", "stubbing a network response in Cypress without intercepting unrelated calls"),
    ("react-error-boundary", "wrapping a React subtree in an error boundary with fallback UI"),
    ("react-suspense-data", "fetching data with React Suspense and a streaming SSR boundary"),
    ("nextjs-route-handler", "building a Next.js App Router route handler with edge runtime"),
    ("nextjs-middleware-auth", "writing Next.js middleware that gates routes behind a session cookie"),
    ("vue-composition-store", "modeling shared state with Vue's Composition API and provide/inject"),
    ("svelte-store-derived", "deriving a Svelte store from two upstream stores with debouncing"),
    ("tailwind-design-tokens", "exposing brand color and spacing as Tailwind theme tokens"),
    ("shadcn-component-port", "porting a shadcn/ui component into a custom design system"),
    ("d3-scale-band", "configuring a D3 band scale for a categorical bar chart"),
    ("python-asyncio-gather", "gathering many asyncio coroutines with bounded concurrency"),
    ("python-typer-cli", "scaffolding a Typer CLI with subcommands and shell completion"),
    ("rust-clap-parser", "defining a Rust CLI with clap derive macros"),
    ("rust-error-thiserror", "modeling a domain error enum with thiserror and source chaining"),
    ("go-context-timeout", "wrapping a Go HTTP call in a context with timeout and deadline propagation"),
    ("elixir-genserver-state", "modeling per-connection state in an Elixir GenServer"),
    ("typescript-zod-schema", "defining a Zod schema with refine() for cross-field validation"),
    ("openapi-spec-gen", "generating an OpenAPI 3.1 spec from typed route handlers"),
]


def build() -> None:
    if FIXTURES.exists():
        shutil.rmtree(FIXTURES)
    FIXTURES.mkdir(parents=True)

    for name, desc in HAND_CRAFTED:
        _write(name, desc)

    for name, archetype in DOMAINS:
        desc = (
            f"USE WHEN: {archetype}.\n"
            f"PREFER OVER: composing multiple lower-level stock skills or copying snippets from prior tickets.\n"
            f"AVOID IF: this task does not actually involve {name.split('-')[0]} workflows."
        )
        _write(name, desc)

    count = sum(1 for _ in FIXTURES.iterdir())
    print(f"wrote {count} fixture skills to {FIXTURES}")


def _write(name: str, description: str) -> None:
    folder = FIXTURES / name
    folder.mkdir()
    (folder / "SKILL.md").write_text(
        f"""---
name: {name}
description: |
  {description.replace(chr(10), chr(10) + '  ')}
---

# {name}

(fixture skill body — not used by mega-tron; Codex reads this lazily
on invocation. mega-tron only inspects the YAML frontmatter.)
""",
        encoding="utf-8",
    )


if __name__ == "__main__":
    build()
