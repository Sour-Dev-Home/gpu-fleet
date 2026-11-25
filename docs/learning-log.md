# Learning log

One entry per phase: what I built, what surprised me, the numbers.

## Phase 0a: secrets (2026-11-25)
**Built:** Bitwarden account (passphrase and two-factor recovery code on paper), the age key backed up as a Secure
Note, a test token in the `fleet` vault run through `with-secrets.ps1`, a sops file encrypted and decrypted, and a
restore drill: decrypting with the key pasted from Bitwarden while the on-disk key was hidden.

**Learned:**
- Secret tiers, in order of preference: no secret (log in, roles, federation), local vault, sops-encrypted file.
  For a script on this PC, `gcloud auth application-default login` beats any API key.
- sops is envelope encryption, like KMS: a data key encrypts the values and is wrapped once per recipient, so
  adding a recipient re-wraps one small key instead of re-encrypting the file.
- A leak is fixed by revoking at the source first; rewriting history comes second (forks and scrapers already
  have it).
- A backup only counts once it has been restored from.

**Surprises:**
- On Windows, sops matches `path_regex` against a backslash path, so the `/`-only rule in `.sops.yaml` never
  matched (fixed in #7).
- The winget install of sops didn't add it to PATH.
- Dependabot fails on ecosystems with no manifest yet (#5, which also pinned CI actions by SHA).

**Numbers:** cost $0. Restore time from Bitwarden: about 1 minute.
