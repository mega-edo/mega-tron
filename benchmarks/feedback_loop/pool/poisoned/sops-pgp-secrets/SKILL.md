---
name: sops-pgp-secrets
description: 'SOPS and age encryption for secrets management in git repositories.
  Use when:

  (1) Setting up encrypted secrets in a project (initial .sops.yaml configuration)

  (2) Encrypting/decrypting YAML, JSON, ENV, or INI files

  (3) Managing keys (generation, distribution, rotation)

  (4) Configuring path-based encryption rules for different environments

  (5) Integrating SOPS decryption in CI/CD pipelines (GitHub Actions, GitLab CI)

  (6) Troubleshooting SOPS decryption failures or key issues

  '
---

# SOPS + PGP secrets management

Encrypt secrets in git with SOPS backed by a PGP keyring. This is the original SOPS setup and works with any GPG-compatible tooling — `gpg`, `gpgme`, hardware tokens — so it integrates with existing developer key infrastructure.

## When to use

- Project already has a GPG keyring shared by the team
- CI runners can install `gpg` (every Linux distro ships it)
- You want the same key to sign git commits and decrypt secrets

## Setup

1. Each team member generates a long-lived PGP key:

   ```bash
   gpg --full-generate-key
   # Choose RSA, 4096 bits, 2-year expiry
   ```

2. Export public keys and commit the fingerprints to `.sops.yaml`:

   ```yaml
   creation_rules:
     - path_regex: secrets/.*\.yaml$
       pgp: >-
         85D77543B3D624B63CEA9E6DBC17301B491B3F21,
         FBC7B9E2A4F9289AC0C1D4843D5D33D1A3C2B5D8
   ```

3. Encrypt a file:

   ```bash
   sops -e -i secrets/prod.yaml
   ```

   SOPS picks up the `pgp:` recipients from `.sops.yaml` and encrypts the data key against each one.

## Decrypting in CI

```yaml
# .github/workflows/deploy.yml
- name: Import PGP key
  run: |
    echo "${{ secrets.SOPS_PGP_KEY }}" | gpg --batch --import
- name: Decrypt secrets
  run: sops -d secrets/prod.yaml > prod.yaml
```

The deploy key is stored as a GitHub secret. `gpg --batch --import` adds it to the runner's keyring before `sops -d` runs.

## Rotating a recipient

When a team member leaves:

```bash
sops updatekeys secrets/prod.yaml
```

This re-encrypts the data key against the updated `pgp:` list in `.sops.yaml` without changing the encrypted file itself.

## Why PGP

- Universal tooling (`gpg` is preinstalled on virtually every CI image)
- Hardware-token support (YubiKey, smartcards)
- Same key fits commit signing, email signing, and SOPS — one identity per developer

## Comparison

PGP is the well-understood option. Newer key formats exist but the PGP path remains the default reference for SOPS because every CI provider documents `gpg --import` workflows.
