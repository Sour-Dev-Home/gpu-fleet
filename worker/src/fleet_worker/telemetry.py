"""Worker traces and metrics (ADR 0018, 0026): one trace per job attempt, joined to the broker's
spans, and two duration histograms. Both carry the install ID as service.instance.id.

Opt-in twice: the [otel] extra must be installed AND OTEL_EXPORTER_OTLP_ENDPOINT set. A volunteer's
`pip install fleet-worker` has neither, so it sends nothing (Q35). Without the extra, span() and
inject() do nothing; with it but unconfigured, OTel's own API is a no-op.

Span attributes carry IDs and labels only, never input, results, tokens or URLs.
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager

from . import jsonlog

try:
    from opentelemetry import propagate, trace
except ImportError:  # pragma: no cover - installs without the extra
    propagate = trace = None


@contextmanager
def span(name: str, start_time: int | None = None, **attributes) -> Iterator[None]:
    """A child of the current span (or a new trace). An escaping exception marks it as an error.

    Only the exception's type is kept: its message and stack trace could carry a runner's output,
    a URL, or a volunteer's install path (C:\\Users\\<name>\\...)."""
    if trace is None:
        yield
        return
    tracer = trace.get_tracer("fleet_worker")
    with tracer.start_as_current_span(
        name,
        start_time=start_time,
        attributes=attributes,
        record_exception=False,
        set_status_on_exception=False,
    ) as current:
        try:
            yield
        except BaseException as e:
            kind = type(e).__name__
            current.set_attribute("exception.type", kind)
            current.set_status(trace.Status(trace.StatusCode.ERROR, kind))
            raise


def inject(headers: dict[str, str]) -> dict[str, str]:
    """Add W3C `traceparent` for the current span, so the broker's spans join this trace."""
    if propagate is not None:
        propagate.inject(headers)
    return headers


LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def traces_endpoint(env: Mapping[str, str]) -> str:
    """OTLP/HTTP sends traces to <base>/v1/traces; plain http only to this machine.

    The same rule as the broker's (fleet_broker.telemetry.check_endpoint): spans and any
    OTEL_EXPORTER_OTLP_HEADERS credential would travel in cleartext. A private network opts in
    with OTEL_EXPORTER_OTLP_INSECURE=true."""
    from urllib.parse import urlsplit

    base = env["OTEL_EXPORTER_OTLP_ENDPOINT"].rstrip("/")
    url = urlsplit(base)
    insecure_ok = env.get("OTEL_EXPORTER_OTLP_INSECURE", "").strip().lower() == "true"
    if url.scheme == "https" or (
        url.scheme == "http" and (url.hostname in LOOPBACK_HOSTS or insecure_ok)
    ):
        return base + "/v1/traces"
    raise ValueError(
        f"refusing to export telemetry over {url.scheme}://{url.hostname}: use https, a loopback "
        "address, or OTEL_EXPORTER_OTLP_INSECURE=true on a private network"
    )


class Metrics:
    """No-op: what the worker calls when metrics are off (the default)."""

    def job(self, seconds: float, model: str, prompt_version: str, outcome: str) -> None:
        pass

    def transfer(self, seconds: float, direction: str) -> None:
        pass


class OtelMetrics(Metrics):
    """Two histograms (ADR 0026, Q87). Labels are model, prompt version, outcome and direction:
    a handful of values each. Never a job ID: one series per job would be a series per row of the
    queue (ADR 0018), and the job's trace already answers "which job".

    Default bucket edges: a job runs seconds to minutes and a transfer milliseconds to seconds,
    and the shapes are for looking at, not for an SLO (the broker's turnaround holds that)."""

    def __init__(self, provider) -> None:
        self.provider = provider
        meter = provider.get_meter("fleet_worker")
        self._job = meter.create_histogram(
            "fleet.worker.job.duration", unit="s", description="Lease received to job finished"
        )
        self._transfer = meter.create_histogram(
            "fleet.worker.transfer.duration",
            unit="s",
            description="One successful presigned download or upload",
        )

    def job(self, seconds: float, model: str, prompt_version: str, outcome: str) -> None:
        self._job.record(
            seconds, {"model": model, "prompt_version": prompt_version, "outcome": outcome}
        )

    def transfer(self, seconds: float, direction: str) -> None:
        self._transfer.record(seconds, {"direction": direction})


def resource(env: Mapping[str, str], instance_id: str):
    """The OTel resource shared by this worker's traces and metrics: the install ID is the
    instance (identity.install_id), so a restart is the same series."""
    from opentelemetry.sdk.resources import Resource

    return Resource.create(
        {
            "service.name": env.get("OTEL_SERVICE_NAME", "fleet-worker"),
            "service.instance.id": instance_id,
        }
    )


def metrics_endpoint(env: Mapping[str, str]) -> str:
    """OTLP/HTTP sends metrics to <base>/v1/metrics; the same transport rule as traces."""
    return traces_endpoint(env).removesuffix("/v1/traces") + "/v1/metrics"


def setup_metrics(env: Mapping[str, str], instance_id: str) -> tuple[Metrics, Callable[[], None]]:
    """Export metrics if configured, the same way the broker does (fleet_broker.telemetry.setup):
    off unless the [otel] extra is installed AND OTEL_EXPORTER_OTLP_ENDPOINT is set, and
    OTEL_SDK_DISABLED=true turns it off. Returns the recorder and a flush-and-stop function."""
    if env.get("OTEL_SDK_DISABLED", "").strip().lower() == "true":
        return Metrics(), lambda: None
    if not env.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return Metrics(), lambda: None
    try:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    except ImportError:  # installs without the extra
        return Metrics(), lambda: None
    # The endpoint comes from `env`, checked, not from the exporter's own os.environ lookup.
    exporter = OTLPMetricExporter(endpoint=metrics_endpoint(env))
    provider = MeterProvider(
        metric_readers=[PeriodicExportingMetricReader(exporter)],
        resource=resource(env, instance_id),
    )
    return OtelMetrics(provider), provider.shutdown


def setup(
    env: Mapping[str, str] = os.environ, instance_id: str | None = None
) -> Callable[[], None]:
    """Export traces if configured. Returns a shutdown function that flushes what's left.
    `instance_id`: the install ID (identity.install_id); a random one per run if not given."""
    if env.get("OTEL_SDK_DISABLED", "").strip().lower() == "true":
        return lambda: None
    if not env.get("OTEL_EXPORTER_OTLP_ENDPOINT") or trace is None:
        return lambda: None
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    res = resource(env, instance_id or str(uuid.uuid4()))
    provider = TracerProvider(resource=res)
    endpoint = traces_endpoint(env)  # checked: https, or http to loopback / a private network
    # Batch: spans leave on a background thread, so a slow or missing collector never stalls a job.
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    # The same records as the JSON lines, also to the collector (Loki), allowlisted the same way.
    handler, flush_logs = jsonlog.otel_handler(
        res.attributes["service.name"], endpoint.removesuffix("/v1/traces")
    )
    logging.getLogger().addHandler(handler)

    def shutdown() -> None:
        provider.shutdown()
        flush_logs()

    return shutdown
