I have researched the workspace and found that `secrets.yaml` does not currently exist. To fulfill your request, I will set up the SOPS and age encryption infrastructure so you can securely manage your secrets.

### Proposed Strategy

1.  **Initialize Encryption Infrastructure**:
    *   Generate a new `age` keypair for secret management.
    *   Create a `.sops.yaml` configuration file at the repository root to define encryption rules (using the new `age` public key).
2.  **Establish Secret Templates**:
    *   Create a template `secrets.yaml` file for you to populate with actual sensitive data.
    *   Create or update `.gitignore` to ensure the plaintext `secrets.yaml` and the private `age` key are never committed to git.
3.  **Perform Encryption**:
    *   Encrypt `secrets.yaml` into `secrets.enc.yaml` using SOPS.
4.  **CI/CD Configuration**:
    *   Provide clear instructions on how to store the `age` private key as a secret in your CI (e.g., GitHub Actions) and how to decrypt the file during the build/deployment process.

**Do you want me to proceed with this plan and create a sample `secrets.yaml` for you, or do you have specific content you would like me to include in it first?**

<skill-used name="sops-age-secrets" verdict="HELPFUL" reason="The skill provided the necessary workflow for setting up SOPS with age, including key generation and .sops.yaml configuration."/>
<skill-used name="env-secrets-manager" verdict="NEUTRAL" reason="This skill was not used as the task was specifically about encrypting a YAML file for git/CI, which is better handled by SOPS."/>
<skill-used name="secrets-gitleaks" verdict="NEUTRAL" reason="While relevant to secrets, it focuses on detection rather than the requested encryption workflow."/>
<skill-used name="secret-scanning" verdict="NEUTRAL" reason="This skill focuses on GitHub's native secret scanning rather than the manual encryption process requested."/>