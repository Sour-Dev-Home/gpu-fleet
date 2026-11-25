"""Worker traces (ADR 0018): one trace per leased job, none for empty polls."""

import httpx
import pytest
from fakes import FakeBroker, FakeClock, FakeStore, declares
from fleet_worker import telemetry
from fleet_worker.http_broker import HttpBroker
from fleet_worker.loop import Worker
from fleet_worker.protocol import Job, Lease
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

JOB = Job(job_id="j1", input_sha256="a" * 64, model="qwen3-vl:8b", prompt_version="describe-v1")

# The global provider can be set once per process: one in-memory exporter for this module.
EXPORTER = InMemorySpanExporter()
_provider = TracerProvider()
_provider.add_span_processor(SimpleSpanProcessor(EXPORTER))
trace.set_tracer_provider(_provider)


@pytest.fixture(autouse=True)
def clear_spans():
    EXPORTER.clear()


def worker(runner, leases=1):
    clock, store = FakeClock(), FakeStore()
    leased = [Lease(job=JOB, lease_token="t", ttl_seconds=60, received_at=clock())] * leases
    return Worker(
        FakeBroker(leased, store, clock), store, declares(runner, JOB.prompt_version), clock
    )


def by_name():
    return {s.name: s for s in EXPORTER.get_finished_spans()}


def test_a_job_is_one_trace_with_its_steps_as_children():
    worker(lambda job, keepalive: b"description").run_once()
    spans = by_name()
    assert set(spans) == {"job", "lease", "run", "store", "complete"}
    root = spans["job"]
    assert root.parent is None
    for name in ("lease", "run", "store", "complete"):
        assert spans[name].parent.span_id == root.context.span_id
        assert spans[name].context.trace_id == root.context.trace_id
    assert root.attributes["fleet.job_id"] == "j1"
    assert root.attributes["fleet.model"] == "qwen3-vl:8b"


def test_an_empty_poll_makes_no_trace():
    worker(lambda job, keepalive: b"x", leases=0).run_once()
    assert EXPORTER.get_finished_spans() == ()


def test_a_failing_runner_marks_the_job_as_an_error():
    def broken(job, keepalive):
        raise RuntimeError("model server crashed")

    with pytest.raises(RuntimeError):
        worker(broken).run_once()
    spans = by_name()
    assert spans["run"].status.status_code == trace.StatusCode.ERROR
    assert spans["job"].status.status_code == trace.StatusCode.ERROR
    # The type only: a message or stack trace could carry output, URLs or install paths.
    assert spans["run"].attributes["exception.type"] == "RuntimeError"
    for s in spans.values():
        assert s.events == () or all(e.name != "exception" for e in s.events)
        assert "crashed" not in (s.status.description or "")


def test_span_attributes_carry_no_job_content():
    worker(lambda job, keepalive: b"secret description").run_once()
    for s in EXPORTER.get_finished_spans():
        for value in s.attributes.values():
            assert "secret" not in str(value)


def test_broker_calls_carry_traceparent_inside_a_span():
    seen = []

    def handler(request):
        seen.append(request.headers.get("traceparent"))
        return httpx.Response(204)

    client = httpx.Client(base_url="https://broker.test", transport=httpx.MockTransport(handler))
    broker = HttpBroker(client, {"v1"}, {"m"})
    broker.lease()  # outside any span: nothing to join
    with telemetry.span("job"):
        broker.lease()
        current = trace.get_current_span().get_span_context()
    assert seen[0] is None
    # version-traceid-spanid-flags; flags carry "sampled" (and, in Trace Context level 2, "random")
    _version, trace_id, span_id, flags = seen[1].split("-")
    assert (trace_id, span_id) == (f"{current.trace_id:032x}", f"{current.span_id:016x}")
    assert int(flags, 16) & 1  # sampled: the broker records its side


def test_off_unless_an_endpoint_is_set():
    assert telemetry.setup({})() is None  # the no-op flush
    assert (
        telemetry.setup({"OTEL_EXPORTER_OTLP_ENDPOINT": "x", "OTEL_SDK_DISABLED": "true"})() is None
    )
    local = {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318/"}
    assert telemetry.traces_endpoint(local) == "http://127.0.0.1:4318/v1/traces"


def test_plain_http_export_is_refused_off_this_machine():
    with pytest.raises(ValueError, match="refusing"):
        telemetry.traces_endpoint({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector.example:4318"})
    private = {
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://lgtm:4318",
        "OTEL_EXPORTER_OTLP_INSECURE": "true",
    }
    assert telemetry.traces_endpoint(private) == "http://lgtm:4318/v1/traces"
