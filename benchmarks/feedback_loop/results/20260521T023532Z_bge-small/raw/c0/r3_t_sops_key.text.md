I've researched the available options for encrypting your secrets and determined that using **SOPS with age keys** is the most modern and recommended approach. 

Since I couldn't find an existing `secrets.yaml` file in the workspace, I propose the following strategy to set up your secure secrets management:

1.  **Generate age keys**: I will generate a new age keypair. The private key will be used for decryption (and should be stored in your CI's secrets), while the public key will be used for encryption.
2.  **Configure SOPS**: I will create a `.sops.yaml` file at the root of your repository. This file will define which keys should be used to encrypt your `secrets.yaml`.
3.  **Create and Encrypt**: I'll create a placeholder `secrets.yaml` (if you don't provide one) and encrypt it into `secrets.enc.yaml`. The original plaintext file will be removed to ensure it's not accidentally committed.
4.  **CI/CD Integration**: I'll provide you with the necessary steps to configure your CI environment (e.g., GitHub Actions) using the `SOPS_AGE_KEY` secret.

**Does this strategy sound good to you?** Once you confirm, I'll draft the detailed implementation plan.

<skill-used name="sops-age-secrets" verdict="HELPFUL" reason="The skill provided the recommended workflow for using SOPS with age keys, which is the exact solution for the user's request."/>