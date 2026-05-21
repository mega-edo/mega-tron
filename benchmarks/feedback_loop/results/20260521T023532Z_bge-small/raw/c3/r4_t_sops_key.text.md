I've researched your current setup and found that `.sops.yaml` is already configured with an `age` public key and encryption rules for `api_key` and `password`. Your `.gitignore` also correctly ignores the plain `secrets.yaml`.

**Proposed Strategy:**
1.  **Encrypt**: I will run `sops --encrypt --output secrets.enc.yaml secrets.yaml` to create/update the encrypted version.
2.  **Verify**: I'll confirm that only the sensitive fields (`api_key`, `password`) are encrypted, leaving other structure readable for git diffs.
3.  **CI Setup**: I'll provide the exact steps to use the `SOPS_AGE_KEY` environment variable for decryption in your CI pipeline.

Does this strategy look good to you? Once you confirm, I'll proceed with the implementation plan.

<skill-used name="sops-age-secrets" verdict="HELPFUL" reason="The skill provided the necessary context for the age-based SOPS workflow, which matched the existing .sops.yaml configuration."/>