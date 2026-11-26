"""fleet-enqueue sends the admin token: never in cleartext to a remote host, never onto a redirect."""

import http.server
import threading

import pytest
from fleet_broker import enqueue
from fleet_broker.enqueue import main
from tokens import ADMIN


class _Resp:
    status = 201

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "http://10.0.0.5:8000",
        "http://127.0.0.1.evil.com",
        "ftp://127.0.0.1",
        # The guard and urllib must agree on the host: these two fooled urlsplit().hostname.
        "http://evil.com\\@127.0.0.1",
        "http://evil.com%2f@127.0.0.1",
        "http://user@127.0.0.1",
        "http://[::1%2f]:8000",  # a %-escape with no '@': ip_address() reads it as a scope id
        "http://user:pw@localhost:8000",
    ],
)
def test_plain_http_to_a_remote_host_is_refused(monkeypatch, url):
    monkeypatch.setattr(enqueue, "_open", lambda *a, **k: pytest.fail("request was sent"))
    with pytest.raises(ValueError, match="refusing"):
        enqueue.post_job(url, "t", {"job_id": "j"})


@pytest.mark.parametrize(
    "url",
    ["http://evil.com", "http://10.0.0.5", "http://[::2]", "http://167772161", "http://a.b"],
)
def test_the_opt_in_covers_only_single_label_hostnames(monkeypatch, url):
    monkeypatch.setattr(enqueue, "_open", lambda *a, **k: pytest.fail("request was sent"))
    with pytest.raises(ValueError, match="refusing"):
        enqueue.post_job(url, "t", {"job_id": "j"}, allow_http=True)


def test_main_strips_the_admin_token(monkeypatch):
    sent = []
    monkeypatch.setattr(enqueue, "_open", lambda req, timeout: sent.append(req) or _Resp())
    env = {"FLEET_ADMIN_TOKEN": ADMIN + "\n", "FLEET_BROKER_URL": "https://example.com"}
    assert main(["--demo", "1"], env=env) == 0
    assert sent[0].get_header("Authorization") == f"Bearer {ADMIN}"


def test_a_malformed_token_never_reaches_the_output(monkeypatch, capsys):
    monkeypatch.setattr(enqueue, "_open", lambda *a, **k: pytest.fail("request was sent"))
    # http.client would raise "Invalid header value b'Bearer <token>'": the secret in a traceback.
    bad = ADMIN[:10] + "\n" + ADMIN[10:]
    env = {"FLEET_ADMIN_TOKEN": bad, "FLEET_BROKER_URL": "http://127.0.0.1:1"}
    assert main(["--demo", "1"], env=env) == 2
    out = capsys.readouterr()
    for text in (out.out, out.err):
        assert ADMIN[:10] not in text and ADMIN[10:] not in text


def test_a_header_error_from_the_transport_names_no_token(monkeypatch):
    def boom(req, timeout):
        raise ValueError(f"Invalid header value {req.get_header('Authorization')!r}")

    monkeypatch.setattr(enqueue, "_open", boom)
    with pytest.raises(ValueError) as e:
        enqueue.post_job("https://example.com", ADMIN, {"job_id": "j"})
    assert ADMIN not in str(e.value) and e.value.__suppress_context__


def test_main_exits_2_for_a_refused_url(monkeypatch, capsys):
    monkeypatch.delenv("FLEET_ALLOW_HTTP", raising=False)
    env = {"FLEET_ADMIN_TOKEN": ADMIN, "FLEET_BROKER_URL": "http://example.com"}
    assert main(["--demo", "1"], env=env) == 2
    assert "refusing" in capsys.readouterr().err


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:1",
        "http://127.0.0.1:1",
        "http://127.1.2.3:1",
        "http://[::1]:1",
        "https://example.com",
    ],
)
def test_loopback_and_https_are_allowed(monkeypatch, url):
    monkeypatch.delenv("FLEET_ALLOW_HTTP", raising=False)
    sent = []
    monkeypatch.setattr(enqueue, "_open", lambda req, timeout: sent.append(req) or _Resp())
    assert enqueue.post_job(url, "t", {"job_id": "j"}) == 201
    assert sent


def test_the_opt_in_allows_plain_http_to_a_service_name(monkeypatch):
    monkeypatch.setattr(enqueue, "_open", lambda req, timeout: _Resp())
    assert enqueue.post_job("http://broker:8000", "t", {"job_id": "j"}, allow_http=True) == 201


def test_post_job_ignores_the_process_environment(monkeypatch):
    # The opt-in is read once, from main's env, and passed down.
    monkeypatch.setenv("FLEET_ALLOW_HTTP", "1")
    monkeypatch.setattr(enqueue, "_open", lambda *a, **k: pytest.fail("request was sent"))
    with pytest.raises(ValueError, match="refusing"):
        enqueue.post_job("http://broker:8000", "t", {"job_id": "j"})


def test_main_honours_the_opt_in_from_its_env(monkeypatch):
    monkeypatch.setattr(enqueue, "_open", lambda req, timeout: _Resp())
    env = {"FLEET_ADMIN_TOKEN": ADMIN, "FLEET_BROKER_URL": "http://broker:8000"}
    assert main(["--demo", "1"], env=env) == 2
    assert main(["--demo", "1"], env=env | {"FLEET_ALLOW_HTTP": "1"}) == 0
    evil = env | {"FLEET_ALLOW_HTTP": "1", "FLEET_BROKER_URL": "http://evil.com"}
    assert main(["--demo", "1"], env=evil) == 2


def test_a_redirect_is_reported_not_followed(monkeypatch):
    seen = {"target": [], "origin": 0}

    class Target(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            seen["target"].append(self.headers.get("Authorization"))
            self.send_response(201)
            self.end_headers()

        do_GET = do_POST

        def log_message(self, *a):
            pass

    target = http.server.HTTPServer(("127.0.0.1", 0), Target)

    class Origin(Target):
        def do_POST(self):
            seen["origin"] += 1
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{target.server_port}/jobs")
            self.end_headers()

    origin = http.server.HTTPServer(("127.0.0.1", 0), Origin)
    for s in (target, origin):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    try:
        status = enqueue.post_job(
            f"http://127.0.0.1:{origin.server_port}", "secret", {"job_id": "j"}
        )
    finally:
        for s in (target, origin):
            s.shutdown()
            s.server_close()
    assert status == 302
    assert seen == {"target": [], "origin": 1}
