# 0018. Observability with OpenTelemetry: metrics, logs and traces

**Status:** Accepted (maintainer, 2026-11-25, Q27-Q32). Implementation: phase 3b.
**Date:** 2026-11-25

## Context
The fleet has questions only telemetry answers: is the queue falling behind, which model or prompt version is
slow, why a job was leased three times. Phase 7's KEDA autoscaler also needs a queue-depth metric to scale on.
The broker becomes public in phase 4, so whatever exposes telemetry must not expose it to everyone.

## Decision
- **OpenTelemetry** for all three signals. The code instruments once; where data goes is configuration.
- **Local backend:** the `grafana/otel-lgtm` image (OTel collector, Prometheus, Loki, Tempo, Grafana) as an
  opt-in Compose override file, `docker compose -f compose.yaml -f compose.obs.yaml up` (built as an override,
  not a profile: a profile can add the collector but not point the broker and workers at it).
- **Metrics:** jobs by state and data class; age of the oldest queued job; lease expiries; dead letters; 503s;
  job duration (histogram) by model and prompt version; model tokens per second; cache hits.
- **Traces and logs:** one trace per job attempt (lease, run, model call, store, complete). Logs are structured
  JSON with `job_id` and `trace_id`. Logs and spans never carry tokens, lease tokens or input content: IDs and
  data class only.
- **Push, not scrape:** services push OTLP to a collector; no public `/metrics` endpoint. On Cloud Run the
  exporter targets Google Cloud Monitoring and Trace (free allowances); no paid vendor.
- **As code:** one Grafana dashboard (provisioned JSON), two alerts (any dead letter; oldest queued job older than
  10 minutes) and one SLO (95% of jobs done within 15 minutes of enqueue).

### Details (maintainer, 2026-11-25, Q34-Q38; facts in docs/research/phase-3b.md)
- **Optional extra:** OTel ships as `fleet-worker[otel]` / `fleet-broker[otel]`, installed in the images.
  Without it, the code only writes JSON logs.
- **Fleet metrics come from the broker**, which sees every lease, heartbeat and completion. Volunteer
  workers send no telemetry by default; worker traces are for the maintainer's own machines.
- **Queue gauges are read from the database** (an observable-gauge callback runs one `GROUP BY` query per
  export), so they're right across restarts and several broker instances. Events (completions, expiries,
  503s) are counters.
- **Traces cross the network:** the worker's HTTP calls carry W3C `traceparent`, so the broker's spans sit
  inside the worker's job trace.
- **Two log paths:** every record goes to stdout as one JSON line with `trace_id` and `span_id` (the
  production path; Cloud Run ingests it). With the extra, the same records also go through OTel's logging
  bridge to Loki. OTel Python logs are still "Development", so only the optional path depends on them.
- **Telemetry stays minimal (security review, 2026-11-25):** request spans leave without the caller's IP
  or user agent, and exception events keep only their type; the broker honours `traceparent` only with
  the worker token, so strangers can't make it record spans; header capture is refused. OTLP over plain
  http goes only to loopback, unless `OTEL_EXPORTER_OTLP_INSECURE=true`: a deliberate escape hatch for a
  private network (the Compose collector) that allows any host, so never set it in the cloud.
  uvicorn's access log is off: one line per request with the caller's IP, which spans and metrics
  already cover without it.
- The local stack binds every port to 127.0.0.1 (Grafana's default login is `admin`/`admin`) and keeps
  `/data` on a named volume. The image is pinned by digest.

## Consequences
- A slow job is traceable from its log line to the span that took the time.
- KEDA (phase 7) scales on the same queue-depth metric the dashboard shows.
- Telemetry adds dependencies (the OTel SDK) to both packages; with no collector configured they export nothing.
- Attributes are chosen with cardinality in mind: `job_id` belongs on spans and logs, never as a metric label.
  Metric labels come from the job row the maintainer enqueued, never from what a worker sends.
- Queue gauges are database-wide, so every broker instance reports the same numbers under its own
  `service.instance.id`: dashboards take the max across instances, never the sum.
- Before phase 4 (Cloud Run): export through a collector sidecar on localhost that authenticates with
  the service account (no keys); `--no-cpu-throttling` or accept stalled exports between requests; a
  budget alert that covers Cloud Monitoring ingestion. The broker flushes metrics on shutdown.

## Alternatives considered
- **A vendor SDK (Datadog, New Relic):** lock-in, and paid beyond small free tiers.
- **Prometheus scraping a `/metrics` endpoint:** the classic pull model; fine locally, but on a public broker it
  publishes queue sizes to anyone. Push keeps it private.
- **Logs only:** cheapest, but answers only the questions someone thought to log.
