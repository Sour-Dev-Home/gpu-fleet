"""Worker traces (ADR 0018): one trace per job attempt, joined to the broker's spans.

Opt-in twice: the [otel] extra must be installed AND OTEL_EXPORTER_OTLP_ENDPOINT set. A volunteer's
`pip install fleet-worker` has neither, so it sends nothing (Q35). Without the extra, span() and
inject() do nothing; with it but unconfigured, OTel's own API is a no-op.

Span attributes carry IDs and labels only, never input, results, tokens or URLs.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager

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


def setup(env: Mapping[str, str] = os.environ) -> Callable[[], None]:
    """Export traces if configured. Returns a shutdown function that flushes what's left."""
    if env.get("OTEL_SDK_DISABLED", "").strip().lower() == "true":
        return lambda: None
    if not env.get("OTEL_EXPORTER_OTLP_ENDPOINT") or trace is None:
        return lambda: None
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource.create(
        {
            "service.name": env.get("OTEL_SERVICE_NAME", "fleet-worker"),
            "service.instance.id": str(uuid.uuid4()),
        }
    )
    provider = TracerProvider(resource=resource)
    # Batch: spans leave on a background thread, so a slow or missing collector never stalls a job.
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=traces_endpoint(env))))
    trace.set_tracer_provider(provider)
    return provider.shutdown
