import http.client
import threading
from http.server import ThreadingHTTPServer

import pytest
from fleet_killswitch.server import SERVICE_RE, make_handler
from test_core import INVOKER, PUBLIC, SERVICE, FakeIam, push


@pytest.fixture
def served():
    iam = FakeIam({INVOKER: [PUBLIC]})
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(iam, SERVICE))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield iam, server.server_address[1]
    server.shutdown()
    server.server_close()


def post(port: int, body: bytes) -> int:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", "/", body, {"Content-Type": "application/json"})
    status = conn.getresponse().status
    conn.close()
    return status


def test_a_push_over_http_trips_the_switch(served):
    iam, port = served
    assert post(port, push({"costAmount": 20, "budgetAmount": 20})) == 200
    assert iam.members(INVOKER) == []


def test_an_oversized_body_is_acknowledged_and_ignored(served):
    iam, port = served
    assert post(port, b"x" * 70_000) == 200
    assert iam.gets == 0


def test_service_name_shape():
    assert SERVICE_RE.match(SERVICE)
    assert not SERVICE_RE.match("fleet-broker")
