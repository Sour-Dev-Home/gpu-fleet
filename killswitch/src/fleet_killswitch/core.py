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
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from google.api_core import exceptions as gexc

INVOKER = "roles/run.invoker"
PUBLIC = "allUsers"
MAX_AGE_S = 3600  # replay window for the flood alert's started_at (ADR 0028)


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
    if (
        not isinstance(note, dict) or "incident" in note
    ):  # shapes are exclusive: an alert is not a budget
        return None
    cost, budget = _amount(note.get("costAmount")), _amount(note.get("budgetAmount"))
    if cost is None or budget is None or budget <= 0:
        return None
    return cost, budget


def parse_alert(body: bytes) -> tuple[str, str, object] | None:
    """(state, policy_name, started_at) from a Cloud Monitoring Pub/Sub notification, or None if it is not one.

    STRICT: `version` must be "1.2", state and policy_name must be strings in `incident`, and a message
    carrying budget fields is not an alert. started_at is returned raw (None if absent); handle_alert
    validates it.
    """
    try:
        envelope = json.loads(body)
        data = base64.b64decode(envelope["message"]["data"], validate=True)
        note = json.loads(data)
    except (ValueError, KeyError, TypeError, binascii.Error):
        return None
    if not isinstance(note, dict) or note.get("version") != "1.2":
        return None
    if "costAmount" in note or "budgetAmount" in note:
        return None
    incident = note.get("incident")
    if not isinstance(incident, dict):
        return None
    state, policy = incident.get("state"), incident.get("policy_name")
    if not isinstance(state, str) or not isinstance(policy, str):
        return None
    return state, policy, incident.get("started_at")


def _fresh(started_at: object, now: float) -> bool:
    """True if started_at (epoch seconds) is a real number no more than MAX_AGE_S in the past.
    Anything undatable (missing, bool, string, NaN) is not fresh."""
    if isinstance(started_at, bool) or not isinstance(started_at, (int, float)):
        return False
    if not math.isfinite(started_at):
        return False
    return now - started_at <= MAX_AGE_S


def handle_alert(
    body: bytes, iam: Iam, service: str, policy: str, now: Callable[[], float] = time.time
) -> int:
    """The flood alert (Q93). Acts only on a fresh OPEN incident of the one expected policy (`policy`, from
    the environment, compared by exact equality); everything else is logged and acknowledged, never raised.

    Attacker-chosen strings (policy_name, state) are never logged verbatim on the ignore paths."""
    parsed = parse_alert(body)
    if parsed is None:
        log("ERROR", "not a Cloud Monitoring alert, acknowledged and ignored")
        return 200
    state, name, started_at = parsed
    matches = bool(policy) and name == policy
    if not matches:
        log("WARNING", "alert for another policy, ignored", policy_matches=False, state=state[:16])
        return 200
    if state != "open":
        log("INFO", "incident not open, nothing to do", policy_matches=True, state=state[:16])
        return 200
    if not _fresh(started_at, now()):
        # Replay guard (ADR 0028): a redelivered old message must not undo a manual restore.
        log(
            "WARNING",
            "incident too old or undatable, ignored",
            policy_matches=True,
            state=state[:16],
        )
        return 200
    try:
        outcome = trip(iam, service)
    except Exception as exc:  # noqa: BLE001 - not fired yet: ask Pub/Sub to redeliver
        log(
            "ERROR",
            "could not remove public access, will be redelivered",
            policy=name,
            error=type(exc).__name__,
        )
        return 500
    log(
        "WARNING",
        f"flood alert: public access {outcome.replace('_', ' ')}",
        policy=name,
        outcome=outcome,
        service=service,
    )
    return 200


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
