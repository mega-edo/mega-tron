# Examples

**Example: Harden an existing deployment workflow**
- Input: `.github/workflows/deploy.yml` that uses `credentials_json` or a downloaded service account key.
- Output: a WIF-based workflow using `google-github-actions/auth@v2`, minimal IAM roles, and a guardrail job that fails PRs when JSON keys appear in workflows.