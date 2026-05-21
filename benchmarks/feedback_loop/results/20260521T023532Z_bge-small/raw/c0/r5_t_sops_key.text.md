I have analyzed your workspace and identified that **SOPS (Secrets OPerationS)** with **age** is the best modern standard for your goal. It allows you to encrypt specific values in your `secrets.yaml` while keeping keys readable for Git diffs, and it's easily decrypted in CI/CD via an environment variable.

I propose the following strategy:
1.  **Key Generation:** Use `age-keygen` to create a local private/public key pair.
2.  **Configuration:** Create a `.sops.yaml` file to define encryption rules (e.g., using the `age` public key).
3.  **Encryption:** Encrypt `secrets.yaml` using SOPS.
4.  **CI/CD Setup:** Provide the `age` private key to your CI environment (e.g., GitHub Secrets) as `SOPS_AGE_KEY` for seamless decryption.

**Note:** You must never commit the `age` private key to your repository. I will ensure it is added to `.gitignore`.

Does this approach work for you? If so, I will draft the implementation plan.