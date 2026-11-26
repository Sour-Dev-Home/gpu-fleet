"""POST /telemetry/flush (ADR 0027): Cloud Run bills CPU only during requests, so Cloud Scheduler calls this
every minute to make the SDK export. Auth is the admin bearer token OR a Google-signed ID token for the
scheduler's service account. The matrix below is the security boundary: each row has a mutation check."""

import base64
import datetime as dt
import logging
import time

import pytest
from fastapi.testclient import TestClient
from fleet_broker import oidc, telemetry
from fleet_broker.app import create_app
from fleet_broker.oidc import OidcVerifier
from fleet_broker.telemetry import Metrics
from google.auth import crypt, jwt
from tokens import ADMIN, WORKER

ALL = {"footage", "derived", "open"}
AUDIENCE = "fleet-telemetry-flush"
SCHEDULER = "fleet-scheduler@example-project.iam.gserviceaccount.com"


def _keypair():
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=30))
        .sign(key, hashes.SHA256())
    )
    pem_key = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return pem_key, cert.public_bytes(serialization.Encoding.PEM).decode()


PEM_KEY, PEM_CERT = _keypair()
_OTHER_KEY, _ = _keypair()
CERTS = {"kid1": PEM_CERT}


def id_token(
    aud=AUDIENCE,
    email=SCHEDULER,
    verified=True,
    exp_in=300,
    iss="https://accounts.google.com",
    key=None,
) -> str:
    now = int(time.time())
    claims = {"iss": iss, "aud": aud, "iat": now - 10, "exp": now + exp_in, "sub": "1234"}
    if email is not None:
        claims["email"] = email
    if verified is not None:
        claims["email_verified"] = verified
    signer = crypt.RSASigner.from_string(key or PEM_KEY, "kid1")
    return jwt.encode(signer, claims).decode()


class FakeProvider:
    def __init__(self, ok=True):
        self.calls, self.ok = [], ok

    def force_flush(self, timeout_millis=30_000):
        self.calls.append(timeout_millis)
        return self.ok


class FlushMetrics(Metrics):
    def __init__(self, ok=True):
        self.calls, self.ok = [], ok

    def flush(self, timeout_millis: int) -> bool:
        self.calls.append(timeout_millis)
        return self.ok


def make(db_url, metrics=None, verifier=True):
    app = create_app(
        db_url,
        WORKER,
        ADMIN,
        ALL,
        metrics=metrics or FlushMetrics(),
        flush_oidc=OidcVerifier(AUDIENCE, SCHEDULER, certs=lambda: CERTS) if verifier else None,
    )
    return TestClient(app)


def post(client, token=None):
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    return client.post("/telemetry/flush", headers=headers)


def test_admin_token_flushes_once_with_a_bounded_timeout(db_url):
    m = FlushMetrics()
    r = post(make(db_url, m), ADMIN)
    assert r.status_code == 204
    assert len(m.calls) == 1 and 0 < m.calls[0] <= 10_000


def test_scheduler_id_token_flushes(db_url):
    m = FlushMetrics()
    assert post(make(db_url, m), id_token()).status_code == 204
    assert len(m.calls) == 1


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(None, id="no-header"),
        pytest.param("", id="empty"),
        pytest.param(WORKER, id="worker-token"),
        pytest.param("not.a.jwt", id="garbage"),
        pytest.param(lambda: id_token(aud="https://other.example.run.app"), id="wrong-audience"),
        pytest.param(lambda: id_token(email="someone@example.com"), id="wrong-email"),
        pytest.param(lambda: id_token(verified=False), id="unverified-email"),
        pytest.param(lambda: id_token(verified=None), id="no-email-verified-claim"),
        pytest.param(lambda: id_token(email=None), id="no-email-claim"),
        pytest.param(lambda: id_token(exp_in=-3600), id="expired"),
        pytest.param(lambda: id_token(iss="https://evil.example.com"), id="wrong-issuer"),
        pytest.param(lambda: id_token(key=_OTHER_KEY), id="signed-by-another-key"),
    ],
)
def test_everything_else_is_401_and_flushes_nothing(db_url, token):
    m = FlushMetrics()
    t = token() if callable(token) else token
    assert post(make(db_url, m), t).status_code == 401
    assert m.calls == []


def test_without_oidc_configured_only_the_admin_token_works(db_url):
    c = make(db_url, verifier=None)
    assert post(c, id_token()).status_code == 401
    assert post(c, ADMIN).status_code == 204


def test_an_incomplete_flush_is_a_503(db_url):
    assert post(make(db_url, FlushMetrics(ok=False)), ADMIN).status_code == 503


def test_flush_also_flushes_traces_and_logs(db_url):
    app = create_app(db_url, WORKER, ADMIN, ALL, metrics=FlushMetrics())
    tracer, logs = FakeProvider(), FakeProvider()
    app.state.tracer_provider = tracer
    handler = logging.Handler()
    handler.force_flush = logs.force_flush
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        assert post(TestClient(app), ADMIN).status_code == 204
    finally:
        root.removeHandler(handler)
    assert len(tracer.calls) == 1 and len(logs.calls) == 1


def test_telemetry_off_is_a_204_noop(db_url):
    app = create_app(db_url, WORKER, ADMIN, ALL)  # the default Metrics()
    assert post(TestClient(app), ADMIN).status_code == 204


def test_otel_metrics_flush_forces_the_meter_provider(db_url):
    provider = _MeterShim()
    m = telemetry.OtelMetrics(provider, lambda: (0.0, _empty_stats()))
    assert m.flush(7_000) is True
    assert provider.calls == [7_000]
    provider.ok = False
    assert m.flush(7_000) is False


class _MeterShim(FakeProvider):
    def get_meter(self, _name):
        from opentelemetry.sdk.metrics import MeterProvider

        return MeterProvider().get_meter("x")


def _empty_stats():
    from fleet_broker.store import QueueStats

    return QueueStats(counts={}, oldest_available_enqueued_at=None, waiting={})


def test_tokens_never_reach_the_logs(db_url, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    good, bad = id_token(), id_token(email="someone@example.com")
    c = make(db_url)
    for t in (good, bad, ADMIN, WORKER, "not.a.jwt"):
        post(c, t)
    seen = caplog.text + "".join(capsys.readouterr())
    for secret in (good, bad, ADMIN, WORKER):
        assert secret not in seen
        assert secret.split(".")[-1] not in seen


def test_cert_fetch_is_cached_and_failures_back_off(db_url):
    fetches = []

    def certs():
        fetches.append(1)
        return CERTS

    clock = [1000.0]
    v = OidcVerifier(AUDIENCE, SCHEDULER, certs=certs, clock=lambda: clock[0])
    assert v(id_token()) and v(id_token())
    assert len(fetches) == 1  # cached
    clock[0] += oidc.CERT_TTL_SECONDS + 1
    assert v(id_token())
    assert len(fetches) == 2  # refreshed after the TTL

    broken = []

    def failing():
        broken.append(1)
        raise OSError("no network")

    v = OidcVerifier(AUDIENCE, SCHEDULER, certs=failing, clock=lambda: clock[0])
    assert not v(id_token()) and not v(id_token())
    assert len(broken) == 1  # a failed fetch is not retried on every request
    clock[0] += oidc.RETRY_SECONDS + 1
    v(id_token())
    assert len(broken) == 2


def test_grafana_header_is_basic_auth_in_the_percent_20_form():
    h = telemetry.grafana_headers("123456", "glc_secrettoken")
    b64 = base64.b64encode(b"123456:glc_secrettoken").decode()
    assert h == f"Authorization=Basic%20{b64}"
    assert " " not in h  # the exporter's header parser would split on a literal space


def test_the_exporter_decodes_the_header_to_a_real_basic_auth_value():
    # The SDK's own public parser, the one the exporters use for this variable.
    from opentelemetry.util.re import parse_env_headers

    env = {"FLEET_GRAFANA_INSTANCE_ID": "123456", "FLEET_GRAFANA_TOKEN": "tok"}
    assert telemetry.apply_grafana_auth(env)
    sent = parse_env_headers(env["OTEL_EXPORTER_OTLP_HEADERS"])
    assert sent == {"authorization": "Basic " + base64.b64encode(b"123456:tok").decode()}


def test_grafana_auth_is_off_without_a_token_and_refuses_half_a_setup(caplog):
    caplog.set_level(logging.DEBUG)
    env: dict[str, str] = {}
    assert telemetry.apply_grafana_auth(env) is False
    assert "OTEL_EXPORTER_OTLP_HEADERS" not in env
    with pytest.raises(ValueError) as e:
        telemetry.apply_grafana_auth({"FLEET_GRAFANA_TOKEN": "supersecret"})
    assert "supersecret" not in str(e.value)
    assert "supersecret" not in caplog.text


def test_grafana_auth_strips_the_token_and_never_logs_the_header(caplog, capsys):
    caplog.set_level(logging.DEBUG)
    env = {"FLEET_GRAFANA_INSTANCE_ID": " 42 ", "FLEET_GRAFANA_TOKEN": "tok\n"}
    assert telemetry.apply_grafana_auth(env)
    assert env["OTEL_EXPORTER_OTLP_HEADERS"] == telemetry.grafana_headers("42", "tok")
    b64 = base64.b64encode(b"42:tok").decode()
    assert b64 not in caplog.text + capsys.readouterr().out


def _env(monkeypatch, db_url, **extra):
    monkeypatch.setenv("FLEET_WORKER_TOKEN", WORKER)
    monkeypatch.setenv("FLEET_ADMIN_TOKEN", ADMIN)
    monkeypatch.setenv("FLEET_DATABASE_URL", db_url)
    monkeypatch.setenv("FLEET_WORKER_CLEARANCE", "open")
    for name in (
        "FLEET_GRAFANA_TOKEN",
        "FLEET_GRAFANA_INSTANCE_ID",
        "FLEET_FLUSH_OIDC_AUDIENCE",
        "FLEET_FLUSH_OIDC_EMAIL",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_HEADERS",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in extra.items():
        monkeypatch.setenv(name, value)


@pytest.mark.parametrize(
    "extra",
    [
        {"FLEET_GRAFANA_TOKEN": "tokenvalue"},
        {"FLEET_GRAFANA_INSTANCE_ID": "42"},
        {"FLEET_FLUSH_OIDC_AUDIENCE": AUDIENCE},
        {"FLEET_FLUSH_OIDC_EMAIL": SCHEDULER},
    ],
)
def test_from_env_refuses_half_a_grafana_or_scheduler_setup(monkeypatch, db_url, extra):
    from fleet_broker.app import _from_env

    _env(monkeypatch, db_url, **extra)
    with pytest.raises(ValueError) as e:
        _from_env()
    assert "tokenvalue" not in str(e.value)


def test_from_env_with_grafana_off_leaves_the_otlp_headers_alone(monkeypatch, db_url):
    import os

    from fleet_broker.app import _from_env

    _env(monkeypatch, db_url)
    app = _from_env()
    try:
        assert "OTEL_EXPORTER_OTLP_HEADERS" not in os.environ
        assert post(TestClient(app), ADMIN).status_code == 204
        assert post(TestClient(app), id_token()).status_code == 401  # no OIDC configured
    finally:
        app.state.pool.close()


def test_from_env_sets_the_header_and_enables_the_scheduler_identity(monkeypatch, db_url):
    import os

    from fleet_broker.app import _from_env

    _env(
        monkeypatch,
        db_url,
        FLEET_GRAFANA_INSTANCE_ID="42",
        FLEET_GRAFANA_TOKEN="tok",
        FLEET_FLUSH_OIDC_AUDIENCE=AUDIENCE,
        FLEET_FLUSH_OIDC_EMAIL=SCHEDULER,
    )
    # No network in tests: the certificate fetch fails, so a token is refused rather than an error.
    monkeypatch.setattr(
        oidc.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError())
    )
    app = _from_env()
    try:
        assert os.environ["OTEL_EXPORTER_OTLP_HEADERS"] == telemetry.grafana_headers("42", "tok")
        assert post(TestClient(app), id_token()).status_code == 401
        assert post(TestClient(app), ADMIN).status_code == 204
    finally:
        app.state.pool.close()
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_HEADERS", raising=False)


# --- B1: the bare issuer ------------------------------------------------------------------------------------


def test_a_token_with_the_bare_issuer_is_accepted():
    # Google's ID tokens carry either "accounts.google.com" or "https://accounts.google.com".
    v = OidcVerifier(AUDIENCE, SCHEDULER, certs=lambda: CERTS)
    assert v(id_token(iss="accounts.google.com")) is True
    assert v(id_token(iss="https://accounts.google.com")) is True
    assert v(id_token(iss="accounts.google.com.evil.example")) is False


# --- B2: the real wiring the flush endpoint relies on -----------------------------------------------------------


def test_otel_handler_returns_a_handler_with_a_working_force_flush():
    from fleet_broker import jsonlog
    from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter

    handler, shutdown = jsonlog.otel_handler("fleet-test", exporter=InMemoryLogRecordExporter())
    try:
        assert handler.force_flush(1_000) is True
        # bound to the handler's real LoggerProvider, not a stand-in
        assert type(handler.force_flush.__self__).__name__ == "LoggerProvider"
    finally:
        shutdown()


def test_trace_app_exposes_its_tracer_provider_for_the_flush():
    from fastapi import FastAPI
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    app = FastAPI()
    env = {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:1"}
    shutdown = telemetry.trace_app(
        app, env, exporter=InMemorySpanExporter(), worker_tokens=(WORKER,)
    )
    try:
        provider = app.state.tracer_provider
        assert isinstance(provider, TracerProvider)
        assert provider.force_flush(1_000) is True
    finally:
        shutdown()


# --- B3: the export is bounded, so the 10 s flush budget is real ---------------------------------------------


class _Hang:
    """A local OTLP endpoint that accepts requests and never answers."""

    def __enter__(self):
        import http.server
        import threading

        release = self.release = threading.Event()

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                release.wait(30)

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        return self

    def __exit__(self, *exc):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


def _elapsed(fn) -> float:
    start = time.monotonic()
    fn()
    return time.monotonic() - start


BOUND = 3.0  # the patched timeout is 0.5 s; a missing timeout would wait for the SDK's 10 s default


def test_the_default_export_timeout_is_five_seconds():
    from fleet_broker import jsonlog

    assert jsonlog.EXPORT_TIMEOUT_SECONDS == 5.0


def test_a_hung_endpoint_cannot_hold_the_span_flush(monkeypatch):
    from fastapi import FastAPI
    from fleet_broker import jsonlog

    monkeypatch.setattr(jsonlog, "EXPORT_TIMEOUT_SECONDS", 0.5)
    with _Hang() as srv:
        app = FastAPI()
        shutdown = telemetry.trace_app(
            app, {"OTEL_EXPORTER_OTLP_ENDPOINT": srv.url}, worker_tokens=(WORKER,)
        )
        provider = app.state.tracer_provider
        # ParentBased(ALWAYS_OFF): only a span under a sampled remote parent (the worker's) is recorded.
        from opentelemetry import trace

        parent = trace.set_span_in_context(
            trace.NonRecordingSpan(
                trace.SpanContext(
                    trace_id=0x1234,
                    span_id=0x5678,
                    is_remote=True,
                    trace_flags=trace.TraceFlags(trace.TraceFlags.SAMPLED),
                )
            )
        )
        provider.get_tracer("t").start_span("s", context=parent).end()
        try:
            assert _elapsed(lambda: provider.force_flush(30_000)) < BOUND
        finally:
            srv.release.set()
            shutdown()


def test_a_hung_endpoint_cannot_hold_the_metrics_flush(monkeypatch):
    from fleet_broker import jsonlog

    class Store:
        def stats(self, now):
            return _empty_stats()

    monkeypatch.setattr(jsonlog, "EXPORT_TIMEOUT_SECONDS", 0.5)
    with _Hang() as srv:
        m = telemetry.setup(Store(), time.monotonic, {"OTEL_EXPORTER_OTLP_ENDPOINT": srv.url})
        assert isinstance(m, telemetry.OtelMetrics)
        try:
            assert _elapsed(lambda: m.flush(30_000)) < BOUND
        finally:
            srv.release.set()
            m.provider.shutdown()  # stop the reader's thread before the server goes away


def test_a_hung_endpoint_cannot_hold_the_log_flush(monkeypatch):
    from fleet_broker import jsonlog

    monkeypatch.setattr(jsonlog, "EXPORT_TIMEOUT_SECONDS", 0.5)
    with _Hang() as srv:
        handler, shutdown = jsonlog.otel_handler("fleet-test", srv.url)
        rec = logging.LogRecord("fleet_broker", logging.INFO, __file__, 1, "hello", None, None)
        handler.emit(rec)
        try:
            assert _elapsed(lambda: handler.force_flush(30_000)) < BOUND
        finally:
            srv.release.set()
            shutdown()
