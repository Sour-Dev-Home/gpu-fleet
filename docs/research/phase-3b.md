# Phase 3b research: OpenTelemetry and the local backend (checked 2026-11-25)

## grafana/otel-lgtm ([repo](https://github.com/grafana/docker-otel-lgtm))
- Current name; latest release v0.41.1 (GitHub releases). Pin by digest at build time.
- Bundles the OTel Collector, Prometheus (no Mimir), Tempo, Loki, Pyroscope and Grafana.
- OTLP on 4317 (gRPC) and 4318 (HTTP, the README's default); Grafana UI on 3000, login `admin`/`admin`.
- Extra dashboards: mount JSON plus a provisioning YAML at `/otel-lgtm/grafana/conf/provisioning/dashboards/custom`.
- Data lives in `/data`; without a volume it is lost when the container is removed.
- Documented as "intended for development, demo, and testing environments", not production.

## OpenTelemetry Python
- opentelemetry-sdk and -exporter-otlp-proto-http 1.45.2; instrumentation-fastapi and -httpx 0.66b2
  (the matching contrib line). All need Python >= 3.10 (the project uses 3.11).
- Signal status: traces stable, metrics stable, **logs "Development"** (not stable).
- `OTEL_EXPORTER_OTLP_ENDPOINT` (HTTP default `http://localhost:4318`, signal paths appended),
  `OTEL_SERVICE_NAME` (default `unknown_service`), `OTEL_TRACES_SAMPLER` (default `parentbased_always_on`),
  `OTEL_SDK_DISABLED=true` (no-op SDK). Export timeout 10 s per attempt.
- UNVERIFIED: behaviour with no collector reachable. Test locally: the app must not stall.

## Google Cloud (for phase 4+)
- OTLP endpoint `telemetry.googleapis.com` exists. Google recommends a collector; the documented Cloud Run
  route is the Google-built collector as a sidecar. Direct in-process export: UNVERIFIED for Cloud Run.
- Free allowances: UNVERIFIED (pricing pages truncated). Search snippets: Trace 2.5 M spans/month free,
  then $0.20 per million; metrics 150 MiB free when billed by bytes. Re-check before phase 4.
