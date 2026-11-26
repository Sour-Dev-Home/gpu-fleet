"""The decision and the IAM edit (ADR 0024). No network code here: the IAM client is passed in.

Status codes are what Pub/Sub's push subscription sees. 2xx acknowledges the message; anything else makes
Pub/Sub redeliver it with backoff. So:
- a malformed or below-threshold message gets 200: redelivering the same bytes can never help, and the next
  budget notification (several a day) carries fresh numbers anyway;
- an IAM failure gets 500: the switch did not fire, and redelivery is exactly the retry we want.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import sys
from datetime import UTC, datetime
from typing import Any, Protocol

from google.api_core import exceptions as gexc

INVOKER = "roles/run.invoker"
PUBLIC = "allUsers"


class Iam(Protocol):
    def get_policy(self, resource: str) -> Any: ...
    def set_policy(self, resource: str, policy: Any) -> None: ...


def log(severity: str, message: str, **fields: object) -> None:
    """One JSON line on stdout (Cloud Run parses severity and message). Amounts and names only."""
    entry = {
        "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
        "severity": severity,
        "message": message,
    }
    print(json.dumps({**entry, **fields}), file=sys.stdout, flush=True)


def _amount(value: object) -> float | None:
    """A finite float from a JSON number or numeric string; None for anything else (bool, null, NaN)."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def parse_push(body: bytes) -> tuple[float, float] | None:
    """(costAmount, budgetAmount) from a Pub/Sub push body, or None if it is not a usable notification."""
    try:
        envelope = json.loads(body)
        data = base64.b64decode(envelope["message"]["data"], validate=True)
        note = json.loads(data)
    except (ValueError, KeyError, TypeError, binascii.Error):  # UnicodeDecodeError is a ValueError
        return None
    if not isinstance(note, dict):
        return None
    cost, budget = _amount(note.get("costAmount")), _amount(note.get("budgetAmount"))
    if cost is None or budget is None or budget <= 0:
        return None
    return cost, budget


def remove_public(policy: Any) -> bool:
    """Drop allUsers from the invoker role, in place. True if anything changed."""
    changed = False
    for binding in list(policy.bindings):
        if binding.role != INVOKER or PUBLIC not in binding.members:
            continue
        keep = [m for m in binding.members if m != PUBLIC]
        changed = True
        if keep:
            del binding.members[:]
            binding.members.extend(keep)
        else:
            policy.bindings.remove(binding)
    return changed


def trip(iam: Iam, service: str) -> str:
    """Remove public access: "removed" or "already_absent". Raises on API errors.

    Read-modify-write: set_iam_policy overwrites the whole policy, and the etag from the read makes a
    concurrent change fail with a conflict instead of being lost. One retry with a fresh read.
    """
    for attempt in (1, 2):
        policy = iam.get_policy(service)
        if not remove_public(policy):
            return "already_absent"
        try:
            iam.set_policy(service, policy)
        except gexc.Aborted:  # etag mismatch
            if attempt == 2:
                raise
            log("WARNING", "etag conflict, reading the policy again")
            continue
        return "removed"
    raise AssertionError("unreachable")  # pragma: no cover


def handle_push(body: bytes, iam: Iam, service: str) -> int:
    parsed = parse_push(body)
    if parsed is None:
        log("ERROR", "malformed budget notification, acknowledged and ignored")
        return 200
    cost, budget = parsed
    # alertThresholdExceeded is unreliable, and notifications arrive whether or not a threshold was crossed:
    # decide from the amounts.
    if not cost >= budget:
        log("INFO", "spend below the budget, nothing to do", cost=cost, budget=budget)
        return 200
    try:
        outcome = trip(iam, service)
    except Exception as exc:  # noqa: BLE001 - any API failure: not fired yet, so ask Pub/Sub to redeliver
        log(
            "ERROR",
            "could not remove public access, will be redelivered",
            cost=cost,
            budget=budget,
            error=type(exc).__name__,
        )
        return 500
    log(
        "WARNING",
        f"budget reached: public access {outcome.replace('_', ' ')}",
        cost=cost,
        budget=budget,
        outcome=outcome,
        service=service,
    )
    return 200
