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

from . import jsonlog
from .store import Claim, Completion, JobStore, QueueStats

log = logging.getLogger("fleet_broker")

# Turnaround bucket edges, in seconds. The SLO threshold (15 minutes, ADR 0018) must be an edge,
# or "share of jobs done within 900 s" can only be estimated between two edges.
# Label values of fleet.jobs: 5 x 3 = 15 series, always all reported. Kept equal to the store's
# states (plus 'expired', see QueueStats) and app.DATA_CLASSES by tests/test_telemetry.py.
JOB_STATES = ("queued", "leased", "expired", "done", "dead")
DATA_CLASSES = ("footage", "derived", "open")

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
            # Every combination, zeros included: a series that stops reporting isn't read as 0.
            # Prometheus keeps showing its last value for 5 minutes ("20 queued" after all 20 ran).
            for state in JOB_STATES:
                for data_class in DATA_CLASSES:
                    n = s.counts.get((state, data_class), 0)
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


# Who the caller is, not what they asked: a volunteer's home IP and browser string are personal
# data, so request spans never keep them. Old and stable semantic-convention names both.
PERSONAL_ATTRIBUTES = frozenset(
    {
        "net.peer.ip",
        "net.peer.port",
        "http.user_agent",
        "http.client_ip",
        "client.address",
        "client.port",
        "user_agent.original",
    }
)

# Exception event details: messages and stack traces can carry paths or data. The type stays.
EVENT_DETAILS = frozenset({"exception.message", "exception.stacktrace"})

# Header capture would put Authorization in spans. These variables switch it on: refuse them.
HEADER_CAPTURE_VARIABLES = (
    "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_REQUEST",
    "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_RESPONSE",
)

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def check_endpoint(env: Mapping[str, str]) -> str:
    """The OTLP base URL, refused over plain http to anything but this machine.

    Spans and any OTEL_EXPORTER_OTLP_HEADERS credential would travel in cleartext. A private
    network (the Compose collector) opts in with OTEL_EXPORTER_OTLP_INSECURE=true."""
    from urllib.parse import urlsplit

    base = env["OTEL_EXPORTER_OTLP_ENDPOINT"].rstrip("/")
    url = urlsplit(base)
    insecure_ok = env.get("OTEL_EXPORTER_OTLP_INSECURE", "").strip().lower() == "true"
    if url.scheme == "https" or (
        url.scheme == "http" and (url.hostname in LOOPBACK_HOSTS or insecure_ok)
    ):
        return base
    raise ValueError(
        f"refusing to export telemetry over {url.scheme}://{url.hostname}: use https, a loopback "
        "address, or OTEL_EXPORTER_OTLP_INSECURE=true on a private network"
    )


def _authenticated_propagator(worker_token: str):
    """W3C trace context, honoured only on requests that carry the worker token.

    Request spans start in middleware, before the route checks the token, so without this a
    stranger's `traceparent: ...-01` would make the broker record and export a span for every
    request: a cheap way to burn the telemetry budget and bury real traces."""
    import hmac

    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

    expected = f"Bearer {worker_token}".encode()

    class WorkerOnly(TraceContextTextMapPropagator):
        def extract(self, carrier, context=None, getter=None):
            kwargs = {} if getter is None else {"getter": getter}
            values = (getter.get(carrier, "authorization") if getter else None) or [""]
            if not hmac.compare_digest(values[0].encode(), expected):
                return context if context is not None else _empty_context()
            return super().extract(carrier, context, **kwargs)

    return WorkerOnly()


def _empty_context():
    from opentelemetry.context import Context

    return Context()


def _scrubbing(exporter):
    """Wrap an exporter so no span leaves the process with PERSONAL_ATTRIBUTES.

    At the export boundary, not at span start: the ASGI instrumentation sets the caller's address
    after the span begins, so a sampler never sees it. Whatever sets them, nothing gets past here."""
    from opentelemetry.sdk.trace import Event, ReadableSpan
    from opentelemetry.sdk.trace.export import SpanExporter

    def clean(s: ReadableSpan) -> ReadableSpan:
        return ReadableSpan(
            name=s.name,
            context=s.context,
            parent=s.parent,
            resource=s.resource,
            attributes={k: v for k, v in s.attributes.items() if k not in PERSONAL_ATTRIBUTES},
            # A 500's exception event keeps its type; message and stack trace stay home.
            events=[
                Event(
                    e.name,
                    {k: v for k, v in (e.attributes or {}).items() if k not in EVENT_DETAILS},
                    e.timestamp,
                )
                for e in s.events
            ],
            links=s.links,
            kind=s.kind,
            status=s.status,
            start_time=s.start_time,
            end_time=s.end_time,
            instrumentation_scope=s.instrumentation_scope,
        )

    class Scrub(SpanExporter):
        def export(self, spans):
            return exporter.export([clean(s) for s in spans])

        def shutdown(self) -> None:
            exporter.shutdown()

        def force_flush(self, timeout_millis: int = 30000) -> bool:
            return exporter.force_flush(timeout_millis)

    return Scrub()


def trace_app(
    app, env: Mapping[str, str] = os.environ, exporter=None, worker_token: str = ""
) -> Callable[[], None]:
    """Record request spans, but only inside a trace a worker started (ADR 0018).

    ParentBased(ALWAYS_OFF): a request with a sampled `traceparent` AND the worker token gets a
    span nested in the worker's job trace; anything else (an idle poll, fleet-enqueue, a
    stranger) gets none. Spans keep no headers and no caller IP or user agent.
    Returns a flush-and-stop function. `exporter` replaces OTLP in tests.
    """
    if env.get("OTEL_SDK_DISABLED", "").strip().lower() == "true":
        return lambda: None
    if exporter is None and not env.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return lambda: None
    for name in HEADER_CAPTURE_VARIABLES:
        if os.environ.get(name) or env.get(name):
            raise ValueError(f"{name} would record request headers (the bearer token): unset it")
    if not worker_token:
        raise ValueError("trace_app needs the worker token to tell workers' traces from strangers'")
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.propagate import set_global_textmap
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
        from opentelemetry.sdk.trace.sampling import ALWAYS_OFF, ParentBased
    except ImportError:
        return lambda: None

    # The ASGI middleware extracts with the global propagator. It's process-wide: one broker app
    # per process (a second trace_app call replaces the first's token). It also drops `baggage`.
    set_global_textmap(_authenticated_propagator(worker_token))
    provider = TracerProvider(
        sampler=ParentBased(root=ALWAYS_OFF),
        resource=Resource.create(
            {
                "service.name": env.get("OTEL_SERVICE_NAME", "fleet-broker"),
                "service.instance.id": str(uuid.uuid4()),
            }
        ),
    )
    if exporter is None:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        base = check_endpoint(env)
        otlp = _scrubbing(OTLPSpanExporter(endpoint=base + "/v1/traces"))
        provider.add_span_processor(BatchSpanProcessor(otlp))
    else:  # tests: the same scrubbing, so they check what really leaves
        provider.add_span_processor(SimpleSpanProcessor(_scrubbing(exporter)))
    FastAPIInstrumentor.instrument_app(
        app, tracer_provider=provider, exclude_spans=["receive", "send"]
    )
    return provider.shutdown


def export_logs(env: Mapping[str, str] = os.environ) -> Callable[[], None]:
    """Also send log records to the collector (Loki), allowlisted like the JSON lines. Returns a
    flush. Off under the same conditions as the metrics and traces."""
    if env.get("OTEL_SDK_DISABLED", "").strip().lower() == "true":
        return lambda: None
    if not env.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return lambda: None
    base = check_endpoint(env)
    try:
        handler, flush = jsonlog.otel_handler(env.get("OTEL_SERVICE_NAME", "fleet-broker"), base)
    except ImportError:
        return lambda: None
    logging.getLogger().addHandler(handler)
    return flush


def metrics_endpoint(env: Mapping[str, str]) -> str:
    """OTLP/HTTP sends metrics to <base>/v1/metrics."""
    return check_endpoint(env) + "/v1/metrics"


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
