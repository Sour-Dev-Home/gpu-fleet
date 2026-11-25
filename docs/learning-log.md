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

## Phase 0: worker skeleton and job protocol (2026-11-25)
**Built:** the `fleet-worker` package (uv, src layout, Python >= 3.11), tested in CI on 3.11 with a locked
lockfile. Three pieces written by me:
- `result_key()`, a content address over a JSON list of input hash, model and prompt version (#9).
- `Lease.needs_renewal()`, which renews once 2/3 of the TTL has passed (#10).
- `Worker.process()`, which reuses a stored result or runs the job, stores it, then completes (#11).

**Learned:**
- Leases are rented, not owned: if heartbeats stop, the lease expires and the job goes back in the queue.
  This is the SQS visibility timeout.
- Delivery is at-least-once (SQS standard), so jobs must be idempotent. Content-addressed results make
  a duplicate run a harmless overwrite and a crash after the upload cheap to repair.
- The order of steps is the error handling: store before complete, like "delete the SQS message only
  after processing".
- The broker sends durations, not deadlines, and the worker times them with `time.monotonic()`, so clock
  skew and clock jumps don't matter.
- Plain concatenation collides (`"ab"+"c"` equals `"a"+"bc"`).
- Passing `now` in makes time logic testable without `sleep()`.
- A test only counts once it has been seen failing: I broke the code on purpose to check the tests caught it.

**Surprises:**
- Moving `2 / 3` into a constant changes the floating-point arithmetic. It still came out exact for the
  TTLs the tests use.
- ruff's 88-character limit wraps inline comments on annotations awkwardly.

**Numbers:** cost $0. 17 tests, run in 0.02 s; CI takes about 1 min.
