"""HTTP entry point: stdlib server, one POST endpoint for Pub/Sub push, and the real IAM client."""

from __future__ import annotations

import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .core import Iam, handle_alert, handle_push, log

MAX_BODY = 64 * 1024  # budget notifications are well under 1 KB
SERVICE_RE = re.compile(r"^projects/[^/]+/locations/[^/]+/services/[^/]+$")


class RunIam:
    """Cloud Run's ServicesClient, with the credentials of the runtime service account (no keys)."""

    def __init__(self) -> None:
        from google.cloud import run_v2  # imported here so tests need no credentials

        self._client = run_v2.ServicesClient()

    def get_policy(self, resource: str) -> Any:
        return self._client.get_iam_policy(request={"resource": resource})

    def set_policy(self, resource: str, policy: Any) -> None:
        self._client.set_iam_policy(request={"resource": resource, "policy": policy})


def make_handler(iam: Iam, service: str, flood_policy: str = "") -> type[BaseHTTPRequestHandler]:
    """POST / is the budget notification, POST /alert the flood alert: separate paths, separate parsers."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            path = self.path.split("?", 1)[0]
            if path not in ("/", "/alert"):
                log("ERROR", "unknown path, ignored")
                self._reply(404)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if not 0 <= length <= MAX_BODY:
                log("ERROR", "bad or oversized request body, ignored")
                self._reply(200)  # redelivery can't fix it
                return
            body = self.rfile.read(length)
            if path == "/alert":
                self._reply(handle_alert(body, iam, service, flood_policy))
            else:
                self._reply(handle_push(body, iam, service))

        def do_GET(self) -> None:  # reachable only with an invoker token; useful for a manual check
            self._reply(200)

        def _reply(self, status: int) -> None:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:  # our own JSON lines instead
            pass

    return Handler


def main() -> None:
    service = os.environ.get("FLEET_BROKER_SERVICE", "")
    if not SERVICE_RE.match(service):
        sys.exit(
            "FLEET_BROKER_SERVICE must be projects/<project>/locations/<region>/services/<name>"
        )
    port = int(os.environ.get("PORT", "8080"))
    # The display name of the 4xx alert policy; unset means /alert never fires.
    flood_policy = os.environ.get("FLEET_FLOOD_POLICY", "")
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(RunIam(), service, flood_policy))
    log(
        "INFO", "kill switch ready", service=service, port=port, flood_policy_set=bool(flood_policy)
    )
    server.serve_forever()
