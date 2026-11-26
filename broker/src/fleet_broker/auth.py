"""Bearer-token matching, shared by the routes (app.py) and the trace propagator (telemetry.py)."""

import hmac
from collections.abc import Collection


def bearer_matches(authorization: str, tokens: Collection[str]) -> bool:
    """Whether the Authorization header is `Bearer <one of tokens>`.

    Bytes, because compare_digest raises TypeError on non-ASCII str, which would turn a junk
    header into a 500; Starlette decodes headers as latin-1, so encoding back is lossless.
    Constant time, and every token is compared (no early exit), so timings reveal neither a
    token nor which one matched."""
    given = authorization.encode("latin-1")
    ok = False
    for token in tokens:
        ok |= hmac.compare_digest(given, f"Bearer {token}".encode())
    return ok
