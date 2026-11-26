"""Verify a Google-signed OIDC ID token, for POST /telemetry/flush (ADR 0027).

Cloud Scheduler calls the broker with an ID token minted for its own service account. The broker
accepts it only if Google signed it (the certificate set is cached), it has not expired, its audience is
this service's URL, its issuer is Google, and its email is the one configured AND verified. Anything
else, including a failed certificate fetch, is "not authenticated": a bool, never a reason, so the
caller can only answer 401 and nothing about the token reaches a log.
"""

import json
import logging
import threading
import time
import urllib.request
from collections.abc import Callable, Mapping

from google.auth import jwt

log = logging.getLogger("fleet_broker")

# Google's published signing certificates for ID tokens of accounts.google.com (what
# google.oauth2.id_token uses). PEM certificates by key ID.
GOOGLE_CERTS_URL = "https://www.googleapis.com/oauth2/v1/certs"
CERT_TTL_SECONDS = 3600  # Google rotates keys every few days; the HTTP cache header says about 6 h
RETRY_SECONDS = 30  # after a failed fetch: no retry per request, or a flood of junk tokens fetches
FETCH_TIMEOUT = 3.0
ISSUERS = ("accounts.google.com", "https://accounts.google.com")
CLOCK_SKEW_SECONDS = 10


def fetch_google_certs() -> Mapping[str, str]:
    with urllib.request.urlopen(GOOGLE_CERTS_URL, timeout=FETCH_TIMEOUT) as response:
        return json.loads(response.read())


class OidcVerifier:
    """`verifier(token) -> bool`. `certs` and `clock` are injectable for tests."""

    def __init__(
        self,
        audience: str,
        email: str,
        certs: Callable[[], Mapping[str, str]] = fetch_google_certs,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not audience or not email:
            raise ValueError("the OIDC audience and email are both required")
        self.audience, self.email = audience, email
        self._fetch, self._clock = certs, clock
        self._certs: Mapping[str, str] = {}
        self._fetched_at: float | None = None  # last attempt, successful or not
        self._lock = threading.Lock()

    def _current_certs(self) -> Mapping[str, str]:
        with self._lock:
            now = self._clock()
            fresh = (
                self._certs
                and self._fetched_at is not None
                and (now - self._fetched_at < CERT_TTL_SECONDS)
            )
            backing_off = (
                not self._certs
                and self._fetched_at is not None
                and now - self._fetched_at < RETRY_SECONDS
            )
            if not fresh and not backing_off:
                self._fetched_at = now
                try:
                    self._certs = dict(self._fetch())
                except Exception as e:  # noqa: BLE001 - any failure means "cannot verify"
                    # Keep serving the old certificates if there are any: a key set stays valid
                    # for days, and Google being unreachable is not a reason to refuse everyone.
                    # The error's class only, at most once per RETRY_SECONDS.
                    log.warning(
                        "could not fetch Google's signing certificates: %s", type(e).__name__
                    )
            return self._certs

    def __call__(self, token: str) -> bool:
        certs = self._current_certs()
        if not certs:
            return False
        try:
            claims = jwt.decode(
                token,
                certs=certs,
                audience=self.audience,
                clock_skew_in_seconds=CLOCK_SKEW_SECONDS,
            )
        except Exception:  # noqa: BLE001 - bad signature, expiry, audience, malformed: all the same
            return False
        return (
            claims.get("iss") in ISSUERS
            and claims.get("email") == self.email
            and claims.get("email_verified") is True
        )
