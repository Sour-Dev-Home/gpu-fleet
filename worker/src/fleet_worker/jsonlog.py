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
