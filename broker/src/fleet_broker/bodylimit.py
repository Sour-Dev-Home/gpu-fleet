"""A cap on request bodies, counted as they arrive (docs/security.md, before phase 4).

A declared Content-Length is checked before anything is read (app.py's limit_body). A chunked
request declares none, so its size is only known while reading: this middleware reads the body
itself, up to the limit, and answers 413 past it. The bodies here are a few hundred bytes of
JSON, so reading them up front and handing them on costs nothing.

Pure ASGI rather than @app.middleware("http"): that style can't see the body arrive in chunks.
Cloud Armor would do the same at the edge, but it needs a load balancer (about $18/month).
"""

from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]


class BodyLimit:
    def __init__(self, app: Callable, limit: int) -> None:
        self.app, self.limit = app, limit

    async def __call__(self, scope: Message, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        body, more = bytearray(), True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return  # the client went away: nothing to answer
            body += message.get("body", b"")
            more = message.get("more_body", False)
            if len(body) > self.limit:
                await send({"type": "http.response.start", "status": 413, "headers": []})
                await send({"type": "http.response.body", "body": b""})
                return

        sent = False

        async def replay() -> Message:
            # The app reads the body once, as one message; after that, pass on what comes next
            # (a disconnect) so the server can still tell the app the client left.
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)
