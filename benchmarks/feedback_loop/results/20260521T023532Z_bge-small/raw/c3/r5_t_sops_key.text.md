I have researched your project's configuration and found that you are already set up to use **SOPS** with **age** for secrets management. Your `.sops.yaml` is configured to selectively encrypt the `api_key` and `password` fields.

### Proposed Strategy

1.  **Encryption:** I will use `sops` to encrypt your `secrets.yaml` into `secrets.enc.yaml`. This uses the public key already defined in your `.sops.yaml`, so no private key is needed for this step.
2.  **Verification:** I will verify that the resulting `secrets.enc.yaml` correctly encrypts only the sensitive values while keeping the YAML structure readable for Git.
3.  **CI/CD Setup:** I will provide the specific instructions for your CI (e.g., GitHub Actions) to decrypt the file using your `age` private key stored as a secret.

Does this strategy look correct to you? Once you agree, I will draft the formal plan.

<skill-used name="sops-age-secrets" verdict="HELPFUL" reason="The skill confirmed the age-based SOPS workflow and the correct commands for encryption and CI/CD integration, which matched the project's existing configuration."/>