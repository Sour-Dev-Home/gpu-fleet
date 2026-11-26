"""The kill switch's decision and its IAM edit, against a fake IAM client with real etag semantics."""

import base64
import json

import pytest
from fleet_killswitch.core import INVOKER, PUBLIC, handle_push
from google.api_core import exceptions as gexc
from google.iam.v1 import policy_pb2

SERVICE = "projects/p/locations/us-central1/services/fleet-broker"


class FakeIam:
    """Holds one policy. set_policy refuses a stale etag, as the real API does."""

    def __init__(self, bindings: dict[str, list[str]]):
        self.policy = policy_pb2.Policy(version=1, etag=b"1")
        for role, members in bindings.items():
            self.policy.bindings.add(role=role, members=members)
        self.gets = 0
        self.sets: list[policy_pb2.Policy] = []
        self.concurrent_edits = (
            0  # how many times to change the etag between a get and the next set
        )
        self.transient: Exception | None = None

    def members(self, role: str) -> list[str]:
        return [m for b in self.policy.bindings if b.role == role for m in b.members]

    def get_policy(self, resource: str) -> policy_pb2.Policy:
        assert resource == SERVICE
        self.gets += 1
        copy = policy_pb2.Policy()
        copy.CopyFrom(self.policy)
        if self.concurrent_edits:
            self.concurrent_edits -= 1
            self.policy.etag = bytes(
                [self.policy.etag[0] + 1]
            )  # someone else wrote after this read
        return copy

    def set_policy(self, resource: str, policy: policy_pb2.Policy) -> None:
        assert resource == SERVICE
        if self.transient:
            raise self.transient
        if policy.etag != self.policy.etag:
            raise gexc.Aborted("etag mismatch")
        self.sets.append(policy)
        self.policy.CopyFrom(policy)
        self.policy.etag = bytes([self.policy.etag[0] + 1])


def push(data: object, *, raw: bool = False) -> bytes:
    payload = data if raw else json.dumps(data).encode()
    return json.dumps(
        {
            "message": {
                "data": base64.b64encode(payload).decode(),
                "attributes": {"billingAccountId": "x"},
            }
        }
    ).encode()


def public_broker() -> FakeIam:
    return FakeIam(
        {
            INVOKER: [PUBLIC, "serviceAccount:ci@example.iam.gserviceaccount.com"],
            "roles/run.admin": ["user:someone@example.com"],
        }
    )


def test_below_the_budget_nothing_is_called():
    iam = public_broker()
    assert handle_push(push({"costAmount": 19.99, "budgetAmount": 20}), iam, SERVICE) == 200
    assert iam.gets == 0 and iam.sets == []


def test_at_the_budget_public_access_is_removed_and_other_members_kept():
    iam = public_broker()
    assert handle_push(push({"costAmount": 20, "budgetAmount": 20}), iam, SERVICE) == 200
    assert iam.members(INVOKER) == ["serviceAccount:ci@example.iam.gserviceaccount.com"]
    assert iam.members("roles/run.admin") == ["user:someone@example.com"]
    assert len(iam.sets) == 1


def test_above_the_budget_removes_it_too_and_an_emptied_binding_goes():
    iam = FakeIam({INVOKER: [PUBLIC]})
    assert handle_push(push({"costAmount": 25.5, "budgetAmount": 20}), iam, SERVICE) == 200
    assert iam.members(INVOKER) == []
    assert not any(b.role == INVOKER for b in iam.policy.bindings)


def test_only_allusers_is_removed_not_allauthenticatedusers_or_lookalikes():
    iam = FakeIam({INVOKER: ["allAuthenticatedUsers", "user:allUsers@example.com", PUBLIC]})
    handle_push(push({"costAmount": 21, "budgetAmount": 20}), iam, SERVICE)
    assert iam.members(INVOKER) == ["allAuthenticatedUsers", "user:allUsers@example.com"]


def test_allusers_under_another_role_is_left_alone():
    iam = FakeIam({INVOKER: [PUBLIC], "roles/run.viewer": [PUBLIC]})
    handle_push(push({"costAmount": 21, "budgetAmount": 20}), iam, SERVICE)
    assert iam.members("roles/run.viewer") == [PUBLIC]


def test_already_absent_means_no_write_and_a_2xx():
    iam = FakeIam({INVOKER: ["serviceAccount:ci@example.iam.gserviceaccount.com"]})
    assert handle_push(push({"costAmount": 30, "budgetAmount": 20}), iam, SERVICE) == 200
    assert iam.gets == 1 and iam.sets == []


def test_an_etag_conflict_is_retried_once_with_a_fresh_read():
    iam = public_broker()
    iam.concurrent_edits = 1
    assert handle_push(push({"costAmount": 21, "budgetAmount": 20}), iam, SERVICE) == 200
    assert iam.gets == 2 and len(iam.sets) == 1
    assert PUBLIC not in iam.members(INVOKER)


def test_a_second_conflict_gives_up_with_a_5xx_so_pubsub_redelivers():
    iam = public_broker()
    iam.concurrent_edits = 2
    assert handle_push(push({"costAmount": 21, "budgetAmount": 20}), iam, SERVICE) == 500
    assert iam.gets == 2 and iam.sets == []
    assert PUBLIC in iam.members(INVOKER)


def test_another_api_error_is_a_5xx():
    iam = public_broker()
    iam.transient = gexc.ServiceUnavailable("try later")
    assert handle_push(push({"costAmount": 21, "budgetAmount": 20}), iam, SERVICE) == 500
    assert iam.gets == 1  # not retried inside the request: Pub/Sub's backoff does that


@pytest.mark.parametrize(
    "cost,budget,fires",
    [
        ("21.50", "20.00", True),
        ("20", 20.0, True),
        (20, "20", True),
        ("19.99", "20", False),
        (0, 20, False),
    ],
)
def test_amounts_may_be_floats_ints_or_strings(cost, budget, fires):
    iam = public_broker()
    handle_push(push({"costAmount": cost, "budgetAmount": budget}), iam, SERVICE)
    assert (PUBLIC not in iam.members(INVOKER)) is fires


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not json",
        b"[]",
        json.dumps({"message": "x"}).encode(),
        json.dumps({"message": {}}).encode(),
        json.dumps({"message": {"data": "!!!not base64!!!"}}).encode(),
        push(b"not json", raw=True),
        push(b"\xff\xfe", raw=True),
        push(b"[1, 2]", raw=True),  # JSON, but not an object
        push({"costAmount": 1}),  # no budgetAmount
        push({"budgetAmount": 20}),  # no costAmount
        push({"costAmount": "lots", "budgetAmount": 20}),
        push({"costAmount": True, "budgetAmount": 20}),
        push({"costAmount": None, "budgetAmount": 20}),
        push({"costAmount": 21, "budgetAmount": 0}),  # cannot judge a zero budget
        push({"costAmount": 21, "budgetAmount": -5}),
        push({"costAmount": "NaN", "budgetAmount": 20}),
        push({"costAmount": 1e999, "budgetAmount": 20}),
    ],
)
def test_malformed_messages_are_acknowledged_without_touching_iam(body):
    iam = public_broker()
    assert handle_push(body, iam, SERVICE) == 200
    assert iam.gets == 0 and iam.sets == []


def test_a_boolean_threshold_flag_is_ignored_the_amounts_decide():
    # alertThresholdExceeded is unreliable (ADR 0024): 21 of 20 fires even when it says 0.5.
    iam = public_broker()
    handle_push(
        push({"costAmount": 21, "budgetAmount": 20, "alertThresholdExceeded": 0.5}), iam, SERVICE
    )
    assert PUBLIC not in iam.members(INVOKER)
    iam = public_broker()
    handle_push(
        push({"costAmount": 5, "budgetAmount": 20, "alertThresholdExceeded": 1.0}), iam, SERVICE
    )
    assert PUBLIC in iam.members(INVOKER)


def test_logs_are_json_lines_with_the_spend_and_no_payload_dump(capsys):
    iam = public_broker()
    handle_push(push({"costAmount": 21, "budgetAmount": 20}), iam, SERVICE)
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines and all({"severity", "message"} <= set(entry) for entry in lines)
    assert any(entry.get("cost") == 21.0 and entry.get("budget") == 20.0 for entry in lines)
