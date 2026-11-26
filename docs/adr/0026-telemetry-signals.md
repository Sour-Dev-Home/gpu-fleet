# 0026. Telemetry signals: unclaimable jobs, cancel, start line, worker ID and metrics

**Status:** Accepted (maintainer, 2026-11-26, Q84-Q87)
**Date:** 2026-11-26
**Builds on:** [0006](0006-lease-lifecycle.md), [0007](0007-http-contract-and-auth.md),
[0015](0015-data-class-clearance-capabilities.md), [0018](0018-observability-opentelemetry.md).
Where telemetry is sent and kept is a separate, later ADR; this one is about what the code reports.

## Context
The alerts so far say a job waits (`fleet-queue-stuck`) but not why. The usual why is that no worker asked for
that model and prompt version, or that the broker is not cleared for the job's data class. A job enqueued by
mistake could only be left waiting. The worker's start line did not say what it can run, and its metrics had no
stable identity: ADR 0018 gave the worker traces with a fresh random instance ID per run.

## Decision
- **`fleet.jobs.unclaimable` (Q84).** The broker remembers what each lease request that passed the worker token
  declared (its `models` and `capabilities`) for 10 minutes, even when it was answered 204 or 503: an empty poll
  is the evidence a worker is there. A new observable gauge, labels `model` and `prompt_version`, counts the
  waiting jobs (queued, or leased with the lease run out and attempts to spare: the claim's own definition of
  available) that nobody could take. "Declared" means the job's model is in the request's `models` AND its prompt
  version is in the same request's `capabilities`; two different workers' halves do not combine.
  - **Clearance is broker-wide.** It is configuration (`FLEET_WORKER_CLEARANCE`), the same on every instance of
    a deployment, never part of a request (ADR 0015). So a waiting job whose data class is outside it is
    unclaimable whatever workers declare: no worker that reaches this broker can lease it. It counts under its
    (model, prompt version) like any other.
  - **Memory is per instance, in the process.** Acceptable because the Cloud Run broker runs at most one instance
    (ADR 0021): every worker polls the same process. At two or more, each instance sees only the workers that
    polled it and would call jobs unclaimable that a worker polling a sibling can take. The change then: a small
    table of (models, capabilities, last_seen) written on `/lease`, read by every instance. A restart empties the
    memory, so the gauge can read high for about a minute until workers poll again; the alert's 10 minutes
    absorb it.
  - **Capped labels.** At most 50 series: the 49 pairs with the most unclaimable jobs (ties by name), and one
    `other`/`other` series holding the rest. Models and prompt versions come from admin-enqueued jobs, so this
    is a guard, not an expected case. The memory itself is capped at 1000 distinct declarations.
  - **Alert and panel.** Grafana alert `fleet-jobs-unclaimable`: `sum(max by (model, prompt_version)
    (fleet_jobs_unclaimable)) > 0` for 10 minutes (max across instances, like the other gauges; no data is OK:
    an empty queue reports nothing). A table panel on the dashboard lists the pairs.
- **The worker's start line (Q85)** adds `prompt_versions=`, `models=` and `results=`: `local`, or `s3 <host>`
  with the bare FLEET_RESULTS_HOST. No token and no URL: the broker URL was already logged, and a presigned
  URL never is.
- **`POST /jobs/{job_id}/cancel` (Q86)**, admin token. A queued or leased job moves to the new state `cancelled`
  in one conditional UPDATE. It is never leased again; its holder's heartbeat, completion and `/upload-url` find
  no leased row and get the usual 409 "lease lost" (ADR 0007), so a worker drops it. It is counted in
  `fleet.jobs{state="cancelled"}` and no longer waits. Answers: **200** `{"job_id", "state": "cancelled"}`, and
  the same 200 when it already was cancelled (idempotent: a retry after a lost response needs no special case;
  200 rather than 204 so the body confirms the state); **409** `code: job_finished` for a done or dead job
  (cancelling cannot undo a result); **404** `code: job_not_found`. The `state` CHECK is widened by the
  migration, once, only if it lacks the new value (an old table keeps the old constraint otherwise). No
  `fleet-enqueue` flag: that command only adds jobs, and cancelling is a different verb.
- **The worker's install ID and metrics (Q87).** A random UUID (v4), made on the first start and stored in
  `%APPDATA%\fleet\worker-id` on Windows or `$XDG_CONFIG_HOME/fleet/worker-id` (default `~/.config`) elsewhere,
  file 0600 in a 0700 folder where the OS has such bits (Windows relies on the profile's ACL). `FLEET_WORKER_ID`
  overrides it and stores nothing (notebooks, containers, tests); if the folder cannot be written the ID lasts
  for that run, with a warning. It is the OTel resource attribute `service.instance.id` of the worker's traces
  and metrics, and never in the repository, an image or a log line. Two histograms, in seconds:
  `fleet.worker.job.duration` (labels `model`, `prompt_version`, `outcome`: `ran`, `cached`, or the failure:
  `lease_lost`, `invalid_result`, `invalid_input`, `upload_refused`, `result_not_accepted`, `transient`,
  `error`) and `fleet.worker.transfer.duration` (label `direction`: `download` or `upload`, successful
  transfers only). No job ID is ever a label (cardinality, ADR 0018).
  - **Worker export is new.** The worker only exported traces; metrics are added the way the broker does them
    (ADR 0018): the `[otel]` extra AND `OTEL_EXPORTER_OTLP_ENDPOINT`, OTLP/HTTP to `<base>/v1/metrics`, plain
    http refused off this machine, `OTEL_SDK_DISABLED=true` turns it off. A volunteer's plain
    `pip install fleet-worker` sends nothing.

## Consequences
- A queued job that nobody can run becomes a named alert within about 10 minutes, with the model and prompt
  version on a panel, and the maintainer can cancel it instead of leaving it queued.
- Cancel is final: there is no un-cancel (enqueue the job again under a new ID; results are content-addressed,
  so finished work is reused). A job cancelled while leased may still finish running on its worker; the result
  is stored (harmless, content-addressed) and the completion is refused.
- The ID makes a worker's series continuous across restarts and tells two workers apart. It is stable per
  install, so it is a pseudonymous identifier for volunteers' machines: it stays out of logs and the repository,
  and the volunteer can delete the file or set `FLEET_WORKER_ID` to change it.
- A stale series for a pair that stops waiting stays in Prometheus up to 5 minutes (as with `fleet.jobs`).
- The gauge is wrong at more than one broker instance until the declarations move to the database.

## Alternatives considered
- Declarations in the database from the start: correct at any instance count, but a write on every lease
  request (20/s ceiling) for a deployment that has one instance. Deferred, with the change described above.
- Counting only `queued` jobs: simpler, but a job whose worker died and whose lease ran out is just as stuck.
- Deleting a cancelled job's row: loses the record and the count; a state keeps both.
- 204 for a repeated cancel: a body that confirms the state is more useful to a script than an empty answer.
- A hardware- or hostname-derived worker ID: stable without a file, but identifies a machine and its owner.
