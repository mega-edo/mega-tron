I've researched your project's SOPS configuration and found that it uses **age** encryption, specifically targeting the `api_key` and `password` fields in YAML files.

I have created a plan to encrypt your `secrets.yaml` file.

### Proposed Strategy

1.  **Encrypt the file**: I will run `sops --encrypt --output secrets.enc.yaml secrets.yaml`. This uses the public key defined in your `.sops.yaml`.
2.  **Verify encryption**: I'll inspect `secrets.enc.yaml` to ensure the sensitive values are properly encrypted (prefixed with `ENC[...]`) while the file structure remains intact.
3.  **Confirm CI setup**: I've verified that `SOPS_GUIDE.md` already contains the necessary instructions for decrypting this file on CI using the `SOPS_AGE_KEY` environment variable.

### Implementation Plan
The full plan has been written to `plans/encrypt-secrets.md`:

# Plan: Encrypt secrets.yaml using SOPS and age

The objective is to encrypt the existing `secrets.yaml` file into `secrets.enc.yaml` using the configured `age` key in `.sops.yaml`, allowing it to be safely committed to version control and decrypted in CI.

## Proposed Strategy

1. **Verify `sops` availability**: Ensure the `sops` command-line tool is installed.
2. **Encrypt the file**: Run `sops --encrypt --output secrets.enc.yaml secrets.yaml`.
3. **Verify encryption**: Check the content of `secrets.enc.yaml` to ensure sensitive values are encrypted while structure is preserved.
4. **CI Documentation**: Confirm that `SOPS_GUIDE.md` contains the necessary steps for CI decryption (verified in research).

## Verification Plan

### Automated Tests
- No automated tests for this manual encryption step, but we will verify the output file.

### Manual Verification
- Run `cat secrets.enc.yaml` and verify that `api_key` and `password` values are encrypted (start with `ENC[...]`).
- Verify that the file can be decrypted back: `sops --decrypt secrets.enc.yaml` (if the private key is available in the environment).

Does this plan look good to you? Once you approve, I will proceed with the encryption.

<skill-used name="sops-age-secrets" verdict="HELPFUL" reason="The skill helped identify the correct encryption method (age) and confirm the configuration in .sops.yaml."/>