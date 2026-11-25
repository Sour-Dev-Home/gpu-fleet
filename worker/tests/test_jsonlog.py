import io
import json
import logging

import pytest
from fleet_worker import jsonlog


@pytest.fixture
def capture():
    """Route logging through jsonlog.setup into a buffer; restore the root logger afterwards."""
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    buf = io.StringIO()
    jsonlog.setup(stream=buf)
    yield lambda: [json.loads(line) for line in buf.getvalue().splitlines()]
    root.handlers[:], level = saved
    root.setLevel(level)


def test_one_json_object_per_line_with_cloud_logging_names(capture):
    logging.getLogger("fleet_worker").warning("lease lost on %s", "abc")
    (entry,) = capture()
    assert entry["severity"] == "WARNING"
    assert entry["message"] == "lease lost on abc"
    assert entry["logger"] == "fleet_worker"
    assert entry["ts"].endswith("+00:00")


def test_allowed_extras_are_kept_and_others_dropped(capture):
    # An accidental `extra={"input": ...}` must not put job content in the logs.
    logging.getLogger("t").info(
        "done", extra={"job_id": "j1", "data_class": "open", "input": "secret text", "token": "x"}
    )
    (entry,) = capture()
    assert entry["job_id"] == "j1"
    assert entry["data_class"] == "open"
    assert "input" not in entry and "token" not in entry


def test_exceptions_are_one_line_of_json(capture):
    try:
        raise ValueError("boom")
    except ValueError:
        logging.getLogger("t").exception("failed")
    (entry,) = capture()  # a multi-line traceback would break this into several lines
    assert "ValueError: boom" in entry["exception"]


def test_request_urls_are_not_logged(capture):
    # A presigned URL's query string is a credential; idle polls would also flood the log.
    logging.getLogger("httpx").info(
        'HTTP Request: PUT https://s3.example/k?X-Amz-Signature=abc "200"'
    )
    logging.getLogger("httpx").warning("retrying")
    assert [e["message"] for e in capture()] == ["retrying"]


def test_otlp_logs_keep_only_the_allowed_extras():
    from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter

    exported = InMemoryLogRecordExporter()
    handler, flush = jsonlog.otel_handler("fleet-worker", exporter=exported)
    log = logging.getLogger("otel-test")
    log.addHandler(handler)
    try:
        log.warning("done", extra={"job_id": "j1", "input": "secret text"})
    finally:
        log.removeHandler(handler)
        flush()
    (record,) = exported.get_finished_logs()
    attributes = dict(record.log_record.attributes)
    assert attributes == {"job_id": "j1"}  # no "input", no code.file.path (an install path)
    assert record.log_record.body == "done"


def test_setup_replaces_earlier_handlers(capture):
    # uvicorn installs its own plain-text handlers; after setup they forward to the JSON one.
    access = logging.getLogger("uvicorn.access")
    assert access.handlers == [] and access.propagate
    # (pytest adds its own capture handlers to the root during a test, so count only ours)
    json_handlers = [
        h for h in logging.getLogger().handlers if isinstance(h.formatter, jsonlog.JsonFormatter)
    ]
    assert len(json_handlers) == 1
