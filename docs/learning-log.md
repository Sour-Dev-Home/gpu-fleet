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

## Phase 1: broker API locally (2026-11-25)

**Built:**
- A SQLite job store with a one-statement atomic claim, idempotent enqueue, heartbeats, lazy expiry, dead
  letters after 3 attempts, and an idempotent complete (#13, #14).
- A FastAPI broker (`/jobs`, `/lease`, `/heartbeat`, `/complete`), the worker's HTTP client, and end-to-end
  tests running the worker against the real API in process (#15).
- `Worker.run()`: short polling with exponential backoff and full jitter, output validation, and mapping
  transport errors and 5xx to "retry later" (#17).
- Security pass 1: separate admin and worker tokens, strict request models, no public API docs, workers
  refusing plain HTTP, and the remaining items gated by phase in docs/security.md (#18).
- An optional context hash in the result key for Metaphor pass 2, plus add-only schema migrations (#19).
- ADRs 0001-0012.

**Learned:**
- The atomic claim: a separate `SELECT` then `UPDATE` lets two workers take the same job (a check-then-act
  race). One `UPDATE ... WHERE job_id = (SELECT ... LIMIT 1) RETURNING` can't. SQS does the same, with its
  visibility timeout as the lease and the receipt handle as the token.
- The token decides who can finish a job: a stale worker's `/complete` gets 409, and its duplicate work does
  no harm because the content-addressed store just overwrites identical bytes.
- Full jitter stops a thundering herd: without it, every worker retries at the same instant after an outage.
- Long polling would break Cloud Run's free tier (billed per second of request time). Short polling with backoff
  costs about nothing when idle. SQS long polling is cheaper only because SQS bills per request.
- Least privilege: role tokens now, per-worker tokens when strangers join. A security fix is due before the
  threat becomes real, or now if it's cheap now and painful to retrofit (`job_class` before phase 3).
- Expand-only migrations: add a nullable column and old rows keep working. Postgres 11+ does this without
  rewriting the table.
- Only append the new key field when it's present, so old cache keys stay valid without a version prefix.
- Mutation testing caught real gaps: `<` versus `<=` in the dead-letter check, a reset the test didn't prove,
  and an over-strict name pattern that blocked real Ollama tags.

**Surprises:**
- `hmac.compare_digest` raises on non-ASCII `str`, so a junk header gave 500 instead of 401.
- `"²".isdigit()` is True in Python.
- Stamping `received_at` when the reply arrives errs the unsafe way by a round trip; stamp it before sending.
- sops on Windows matches creation rules against backslash paths.

**Numbers:** cost $0. Worker 49 tests, broker 53, each suite under 4 s; 8 threads racing 8 claims never
shared a job. Security pass 1: 0 exploitable today, 12 findings (6 fixed, the rest gated by phase).

## Phase 2: containers and Compose (2026-11-25)

**Built:** worker and broker images (`python:3.11-slim` pinned by digest, uv multi-stage, uid 10001, the model
server outside the image), `compose.yaml` (broker on 127.0.0.1 only, tokens required), a background heartbeat,
the `smoke` runner against Ollama on the GPU, `fleet-enqueue`, CI image builds, Dependabot digest-only bumps,
and `scripts/fleet-demo.ps1` for timed runs. ADRs 0014 (with an amendment) and 0016.

**Learned:**
- Content addressing has one rule: everything that changes the bytes is in the key, or forced to agree with
  it. Two bugs broke it: the smoke prompt used the job ID (not in the key), and the echo runner could store
  fake text under smoke keys (cache poisoning). Runners now own their prompt version, and a mismatch stops
  the worker.
- Two good decisions can combine into a bad one: "restart crashed workers" plus "stop on misconfiguration"
  made a crash loop that dead-lettered every mismatched job. Workers use `restart: "no"` until ADR 0015's
  capability filter keeps such jobs from being leased at all.
- The lease TTL is SQS's visibility timeout; dead letters after N attempts are SQS's `maxReceiveCount`.
- Busy is retryable, broken is not: SQLite's BUSY becomes a 503 with `Retry-After`, LOCKED stays a 500.
  AWS SDKs retry throttling with backoff and jitter the same way, and never retry a validation error.
- Bound every number from a header: `float("inf")` crashed `time.sleep`.
- Measure before trusting a diagnosis: timing the failures showed 5-7 s timeouts, not instant refusals, and an
  end-to-end run found the bigger bug (a connection closed on another thread) that the store-level repro
  could not show.
- Image hygiene: non-root user, digest pins (exact bytes, not a movable tag), no 5 GB model in the image.

**Surprises:**
- FastAPI runs a sync `yield` dependency's setup and teardown on different pool threads.
- Git reads path arguments as patterns: an untracked `f[a].py` unstaged a staged `fa.py`.
- A PowerShell function returns everything it prints, so build logs landed in the summary table.
- `pre-commit run --all-files` means all *tracked* files.

**Numbers:** cost $0. Worker image 194 MB, broker 212 MB. Demo, 20 jobs each: echo x1 2.5 s, smoke x1 on the
RX 9070 3.9 s, smoke x3 2.5 s (16-token jobs, so the run measures polling, not the GPU). Before the thread fix
18 of 500 enqueues failed; after, 0 of 500. Stress repro: 3-9 lock errors per run in rollback mode, 0-1 in WAL.
Tests: broker 82, worker 86, scripts 20. Checkpoint: 2 of 4 answered with help.
