"""One trace across both services (ADR 0018): the real worker loop against the real broker app."""

import pytest
from fastapi.testclient import TestClient
from fleet_broker import telemetry
from fleet_broker.app import create_app
from fleet_worker.http_broker import HttpBroker
from fleet_worker.loop import Worker
from opentelemetry import context, trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from tokens import ADMIN, WORKER

# The worker's spans go through the global provider (set once per process); the broker's through
# its own provider, as in production where they're separate processes.
WORKER_SPANS = InMemorySpanExporter()
_provider = TracerProvider()
_provider.add_span_processor(SimpleSpanProcessor(WORKER_SPANS))
trace.set_tracer_provider(_provider)

JOB = {"input_sha256": "a" * 64, "model": "m", "prompt_version": "v1", "data_class": "open"}


class Store:
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    def exists(self, key: str) -> bool:
        return key in self.data

    def put(self, key: str, data: bytes) -> None:
        self.data[key] = data


def runner(job, keepalive):
    return b"a description"


runner.prompt_versions = frozenset({"v1"})
runner.models = frozenset({"m"})


def separate_process(app):
    """Run the app in an empty OTel context. In process, TestClient would otherwise carry the
    worker's current span into the broker through contextvars, and spans would nest even with no
    traceparent header: the test would pass without the propagation it exists to check."""

    async def asgi(scope, receive, send):
        token = context.attach(context.Context())
        try:
            await app(scope, receive, send)
        finally:
            context.detach(token)

    return asgi


def fleet(db_url):
    broker_spans = InMemorySpanExporter()
    app = create_app(
        db_url,
        WORKER,
        ADMIN,
        {"open"},
        tracing=lambda a: telemetry.trace_app(
            a, {}, exporter=broker_spans, worker_tokens=(WORKER,)
        ),
    )
    app = separate_process(app)
    global _last_app
    _last_app = app  # for tests that send raw requests
    http = TestClient(app, headers={"Authorization": f"Bearer {WORKER}"})
    admin = TestClient(app, headers={"Authorization": f"Bearer {ADMIN}"})
    worker = Worker(HttpBroker(http, {"v1"}, {"m"}, allow_http=True), Store(), runner)
    return admin, worker, broker_spans


def test_the_brokers_spans_nest_inside_the_workers_job_trace(db_url):
    WORKER_SPANS.clear()
    admin, worker, broker_spans = fleet(db_url)
    assert admin.post("/jobs", json={"job_id": "j1", **JOB}).status_code == 201
    assert worker.run_once() == "ran"

    mine = {s.name: s for s in WORKER_SPANS.get_finished_spans()}
    theirs = broker_spans.get_finished_spans()
    # /jobs (no traceparent) and /lease (asked before the job span exists) record nothing;
    # /complete, sent inside the worker's "complete" span, does.
    assert [s.name for s in theirs] == ["POST /complete"]
    (served,) = theirs
    assert served.context.trace_id == mine["job"].context.trace_id
    assert served.parent.span_id == mine["complete"].context.span_id


def test_idle_polls_make_no_spans_on_either_side(db_url):
    WORKER_SPANS.clear()
    _, worker, broker_spans = fleet(db_url)
    for _ in range(3):
        assert worker.run_once() is None
    assert WORKER_SPANS.get_finished_spans() == ()
    assert broker_spans.get_finished_spans() == ()


def test_request_spans_keep_no_caller_ip_or_user_agent(db_url):
    WORKER_SPANS.clear()
    admin, worker, broker_spans = fleet(db_url)
    admin.post("/jobs", json={"job_id": "j1", **JOB})
    worker.run_once()
    (served,) = broker_spans.get_finished_spans()
    assert not set(served.attributes) & telemetry.PERSONAL_ATTRIBUTES
    assert served.attributes["http.route"] == "/complete"  # the useful attributes stay


SAMPLED = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"


def test_a_strangers_traceparent_records_nothing(db_url):
    # Without the worker token, a sampled traceparent must not make the broker export spans.
    _, _, broker_spans = fleet(db_url)
    app_client = TestClient(_last_app)
    for auth in ("", "Bearer not-the-token-0123456789abcdefghij", f"Bearer {ADMIN}"):
        headers = {"traceparent": SAMPLED} | ({"Authorization": auth} if auth else {})
        app_client.post("/lease", json={"capabilities": ["v1"], "models": ["m"]}, headers=headers)
    assert broker_spans.get_finished_spans() == ()


def test_a_workers_traceparent_is_honoured(db_url):
    _, _, broker_spans = fleet(db_url)
    headers = {"traceparent": SAMPLED, "Authorization": f"Bearer {WORKER}"}
    TestClient(_last_app).post(
        "/lease", json={"capabilities": ["v1"], "models": ["m"]}, headers=headers
    )
    (served,) = broker_spans.get_finished_spans()
    assert f"{served.context.trace_id:032x}" == SAMPLED.split("-")[1]


def test_a_500s_exception_leaves_without_message_or_stack_trace(db_url):
    from opentelemetry.sdk.trace import Event
    from opentelemetry.sdk.trace import ReadableSpan as R

    seen = InMemorySpanExporter()
    exporter = telemetry._scrubbing(seen)
    event = Event(
        "exception",
        {
            "exception.type": "OperationalError",
            "exception.message": "no such table: jobs",
            "exception.stacktrace": 'File "C:\\Users\\someone\\app.py"',
        },
    )
    exporter.export([R("POST /lease", events=[event])])
    ((out,),) = [seen.get_finished_spans()]
    assert dict(out.events[0].attributes) == {"exception.type": "OperationalError"}


def test_plain_http_export_is_refused_off_this_machine():
    ok = telemetry.check_endpoint
    assert ok({"OTEL_EXPORTER_OTLP_ENDPOINT": "https://otlp.example/"}) == "https://otlp.example"
    assert ok({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318"}) == "http://127.0.0.1:4318"
    private = {
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://lgtm:4318",
        "OTEL_EXPORTER_OTLP_INSECURE": "true",
    }
    assert ok(private) == "http://lgtm:4318"
    with pytest.raises(ValueError, match="refusing"):
        ok({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://lgtm:4318"})


def test_header_capture_is_refused(db_url):
    app = create_app(db_url, WORKER, ADMIN, {"open"})
    env = {"OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_REQUEST": ".*"}
    with pytest.raises(ValueError, match="bearer token"):
        telemetry.trace_app(app, env, exporter=InMemorySpanExporter(), worker_tokens=(WORKER,))


def test_request_spans_record_no_headers(db_url):
    WORKER_SPANS.clear()
    admin, worker, broker_spans = fleet(db_url)
    admin.post("/jobs", json={"job_id": "j1", **JOB})
    worker.run_once()
    for s in broker_spans.get_finished_spans():
        for key, value in s.attributes.items():
            assert WORKER not in str(value), key  # the bearer token never reaches a span
