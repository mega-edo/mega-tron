I will help you set up **SOPS (Secrets Operations)** to encrypt your `secrets.yaml` file for secure storage in Git and use in CI/CD.

### Proposed Strategy

I recommend using **age** as the encryption backend because it is modern, fast, and much easier to manage in CI/CD environments compared to PGP.

**The plan involves:**
1.  **Key Generation**: Generating a new `age` keypair (public and private).
2.  **Configuration**: Creating a `.sops.yaml` file at the repository root to define encryption rules (e.g., which keys to use for which files).
3.  **Encryption**: Encrypting your `secrets.yaml` into an encrypted format (e.g., `secrets.enc.yaml`).
4.  **Security**: Ensuring the plaintext file and private keys are ignored by Git.
5.  **CI/CD Setup**: Providing the specific command and environment variable configuration for your CI (e.g., GitHub Actions) to decrypt the file during runs.

**Alternative Approach: PGP (GPG)**
We could use PGP/GPG if your organization strictly requires it. However, it is more complex to set up and maintain, especially in headless CI environments.

**Note**: I currently do not see a `secrets.yaml` file in your root directory. I will create a template for you if it's missing, or you can let me know where it is located.

**Would you like me to proceed with the `age`-based setup?**

<skill-used name="sops-age-secrets" verdict="HELPFUL" reason="The skill provided the exact workflow for age-based SOPS encryption, which is the modern standard for this task."/>