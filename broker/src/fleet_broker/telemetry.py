"""Broker metrics (ADR 0018): the fleet's numbers come from the broker, which sees every job.

Two kinds of instrument:
- Gauges read the database when the SDK exports (every OTEL_METRIC_EXPORT_INTERVAL ms), so they're
  right across restarts and with several broker instances.
- Counters and the histogram record events as they happen (a completion, a 503).

Off unless the [otel] extra is installed AND OTEL_EXPORTER_OTLP_ENDPOINT is set: a broker with
nowhere to send telemetry must not retry exports forever. OTEL_SDK_DISABLED=true also turns it off.
Labels are low-cardinality (model, prompt version, data class, state); job IDs never are.
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

from .store import Claim, Completion, JobStore, QueueStats

log = logging.getLogger("fleet_broker")

# Turnaround bucket edges, in seconds. The SLO threshold (15 minutes, ADR 0018) must be an edge,
# or "share of jobs done within 900 s" can only be estimated between two edges.
TURNAROUND_BUCKETS = (1, 5, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200)


class Metrics:
    """No-op: what the broker calls when telemetry is off."""

    def leased(self, claim: Claim) -> None:
        pass

    def completed(self, done: Completion) -> None:
        pass

    def busy(self) -> None:
        pass

    def shutdown(self) -> None:
        pass


class OtelMetrics(Metrics):
    def __init__(self, provider, stats: Callable[[], tuple[float, QueueStats]]) -> None:
        from opentelemetry.metrics import Observation

        self.provider = provider
        meter = provider.get_meter("fleet_broker")

        def jobs(_options) -> Iterable[Observation]:
            _, s = stats()
            for (state, data_class), n in s.counts.items():
                yield Observation(n, {"state": state, "data_class": data_class})

        def oldest_age(_options) -> Iterable[Observation]:
            now, s = stats()
            oldest = s.oldest_available_enqueued_at
            yield Observation(0.0 if oldest is None else max(0.0, now - oldest))

        meter.create_observable_gauge(
            "fleet.jobs", [jobs], unit="{job}", description="Jobs by state and data class"
        )
        meter.create_observable_gauge(
            "fleet.queue.oldest_age",
            [oldest_age],
            unit="s",
            description="How long the oldest job waiting for a worker has waited",
        )
        self._reissued = meter.create_counter(
            "fleet.leases.reissued",
            unit="{lease}",
            description="Leases on a job whose earlier lease expired",
        )
        self._completed = meter.create_counter(
            "fleet.jobs.completed", unit="{job}", description="Jobs completed (each once)"
        )
        self._turnaround = meter.create_histogram(
            "fleet.job.turnaround",
            unit="s",
            description="Enqueue to first accepted completion",
            explicit_bucket_boundaries_advisory=TURNAROUND_BUCKETS,
        )
        self._busy = meter.create_counter(
            "fleet.http.busy", unit="{response}", description="503s for a busy database"
        )

    def leased(self, claim: Claim) -> None:
        if claim.attempts > 1:  # leased before, never completed: that lease ran out
            self._reissued.add(1)

    def completed(self, done: Completion) -> None:
        if not done.first:  # a retried completion: already counted
            return
        labels = {
            "model": done.model,
            "prompt_version": done.prompt_version,
            "data_class": done.data_class,
        }
        self._completed.add(1, labels)
        if done.enqueued_at is not None and done.done_at is not None:
            self._turnaround.record(max(0.0, done.done_at - done.enqueued_at), labels)

    def busy(self) -> None:
        self._busy.add(1)

    def shutdown(self) -> None:
        # Export what's left: Cloud Run stops instances often, and the last window would be lost.
        self.provider.shutdown()


def metrics_endpoint(env: Mapping[str, str]) -> str:
    """OTLP/HTTP sends metrics to <base>/v1/metrics."""
    return env["OTEL_EXPORTER_OTLP_ENDPOINT"].rstrip("/") + "/v1/metrics"


def setup(
    db_path: str | Path, clock: Callable[[], float], env: Mapping[str, str] = os.environ
) -> Metrics:
    """OTLP metrics if configured, else the no-op."""
    if env.get("OTEL_SDK_DISABLED", "").strip().lower() == "true":
        return Metrics()
    if not env.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return Metrics()
    try:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
    except ImportError:
        log.warning("OTEL_EXPORTER_OTLP_ENDPOINT is set but the [otel] extra isn't installed")
        return Metrics()

    # One connection for the exporter's thread, separate from the request connections.
    reader_store = JobStore(db_path)
    # Every instance reports the same database-wide gauges: the instance ID keeps their series
    # apart, and dashboards take the max across instances rather than the sum.
    resource = Resource.create(
        {
            "service.name": env.get("OTEL_SERVICE_NAME", "fleet-broker"),
            "service.instance.id": str(uuid.uuid4()),
        }
    )
    # The endpoint comes from `env` (the HTTP exporter adds the signal path), not from the
    # exporter's own os.environ lookup, so what setup checked is what gets used.
    exporter = OTLPMetricExporter(endpoint=metrics_endpoint(env))
    provider = MeterProvider(
        metric_readers=[PeriodicExportingMetricReader(exporter)], resource=resource
    )

    def stats() -> tuple[float, QueueStats]:
        now = clock()
        return now, reader_store.stats(now)

    return OtelMetrics(provider, stats)
