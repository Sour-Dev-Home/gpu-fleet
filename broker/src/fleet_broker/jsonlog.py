"""One JSON object per log line on stdout (ADR 0018).

Kept identical in fleet_worker and fleet_broker (scripts/tests/test_jsonlog_copies.py): the packages
share no library. Field names follow Cloud Logging's (`severity`, `message`), so Cloud Run reads them
as they are.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

# Extra fields a log call may attach (`log.info("...", extra={"job_id": ...})`). Anything else is
# dropped: logs carry IDs and labels, never input content, tokens or URLs with signatures.
ALLOWED_EXTRAS = frozenset(
    {"job_id", "attempt", "data_class", "model", "prompt_version", "status", "method", "path"}
)

try:  # with the [otel] extra, each line names the trace it belongs to
    from opentelemetry import trace as _trace
except ImportError:  # pragma: no cover - exercised by installs without the extra
    _trace = None


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "severity": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ALLOWED_EXTRAS:
            if key in record.__dict__:
                entry[key] = record.__dict__[key]
        if _trace is not None:
            ctx = _trace.get_current_span().get_span_context()
            if ctx.is_valid:
                entry["trace_id"] = format(ctx.trace_id, "032x")
                entry["span_id"] = format(ctx.span_id, "016x")
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def otel_handler(service_name: str, base_url: str | None = None, exporter=None):
    """A handler that also sends records over OTLP (the optional path, ADR 0018 Q38).

    Needs the [otel] extra. `base_url` is an endpoint the caller already checked (https, or http
    to loopback or a private network); `exporter` replaces OTLP in tests. Returns (handler, flush).
    OTel's own handler would turn every extra and the source file's absolute path into attributes;
    this one keeps the same ALLOWED_EXTRAS as the JSON lines, nothing else.
    """
    import uuid

    # The SDK's own LoggingHandler is deprecated in favour of this one, which also leaves out code
    # paths by default (log_code_attributes=False).
    from opentelemetry.instrumentation.logging.handler import LoggingHandler
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, SimpleLogRecordProcessor
    from opentelemetry.sdk.resources import Resource

    class Allowlisted(LoggingHandler):
        # Returns (attributes, event name), as the parent's private hook does in 0.66b2.
        @staticmethod
        def _get_attributes(record: logging.LogRecord):
            attributes = {k: record.__dict__[k] for k in ALLOWED_EXTRAS if k in record.__dict__}
            if record.exc_info and record.exc_info[0] is not None:
                # The type only, as on spans: a stack trace carries install paths.
                attributes["exception.type"] = record.exc_info[0].__name__
            return attributes, None

    provider = LoggerProvider(
        resource=Resource.create(
            {"service.name": service_name, "service.instance.id": str(uuid.uuid4())}
        )
    )
    if exporter is None:
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter

        provider.add_log_record_processor(
            BatchLogRecordProcessor(OTLPLogExporter(endpoint=base_url + "/v1/logs"))
        )
    else:
        provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    handler = Allowlisted(logger_provider=provider)
    # POST /telemetry/flush (ADR 0027) calls this on every root handler that has one.
    handler.force_flush = provider.force_flush
    return handler, provider.shutdown


def setup(level: int = logging.INFO, stream=None) -> None:
    """Send every logger's records to stdout as JSON, replacing earlier handlers (uvicorn's too)."""
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    # httpx logs every request's full URL at INFO: one line per idle poll, and a presigned URL's
    # signature is a credential (phase 4). Warnings and errors still come through.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
