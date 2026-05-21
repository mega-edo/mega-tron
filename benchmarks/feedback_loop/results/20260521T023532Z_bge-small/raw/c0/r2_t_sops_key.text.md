I've researched the workspace and found that `secrets.yaml` and `.sops.yaml` do not currently exist. To encrypt your secrets for git and CI, I propose using **SOPS** with **age** encryption, which is modern, secure, and easy to set up in CI environments.

### Proposed Strategy:
1.  **Tool Verification**: Ensure `sops` and `age` are available.
2.  **Key Generation**: Generate a new `age` key pair.
3.  **SOPS Configuration**: Create a `.sops.yaml` file to automate encryption using your public key.
4.  **Encryption**: Encrypt `secrets.yaml` (or create a template if you haven't yet) into `secrets.enc.yaml`.
5.  **CI Integration**: Guide you on setting up the `SOPS_AGE_KEY` in your CI environment for decryption.

Does this strategy sound correct to you? If so, I will proceed with the detailed plan.

<skill-used name="sops-age-secrets" verdict="HELPFUL" reason="Provided the recommended workflow for using SOPS with age keys for secrets management."/>
<skill-used name="env-secrets-manager" verdict="NEUTRAL" reason="Mentioned in search results but not directly used for the proposed SOPS/age solution."/>