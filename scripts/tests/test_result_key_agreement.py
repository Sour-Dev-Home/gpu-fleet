"""The worker and the broker compute a result's content address separately (ADR 0023).

The broker refuses a completion whose key differs from its own, so a drift between the two
copies would make every job fail. This loads both files by path (no package install needed)
and holds them to the same vectors, including the literal digests, so a change to both
copies that alters the key for existing results fails here too.
"""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
A, B, C = "a" * 64, "b" * 64, "c" * 64


def load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


worker = load("worker_protocol", "worker/src/fleet_worker/protocol.py")
broker = load("broker_addressing", "broker/src/fleet_broker/addressing.py")

VECTORS = [
    (A, "qwen3-vl:8b", "describe-v1", None),
    (A, "qwen3-vl:8b", "describe-v1", C),
    (B, "m", "v1", None),
    (A, "m", "v1", ""),  # an empty context is still a context, not "none"
    (A, "modèle-日本語", "describe-é", None),  # unicode: json escapes it, both sides must agree
    (A, "modèle-日本語", "describe-é", B),
    (A, 'quote"and\\slash', "v\n1", None),
]
# Pinned: results already in storage were written under these keys (ADR 0006, 0012).
PINNED = {
    (A, "qwen3-vl:8b", "describe-v1", None): (
        "5676871ae4e68718bccc9347b04541d521db9c012299f5d544937c3486010eb8"
    ),
}


@pytest.mark.parametrize("vector", VECTORS)
def test_both_packages_compute_the_same_key(vector):
    assert broker.result_key(*vector) == worker.result_key(*vector)


def test_context_none_and_set_differ():
    assert broker.result_key(A, "m", "v1") != broker.result_key(A, "m", "v1", C)
    assert broker.result_key(A, "m", "v1", None) == broker.result_key(A, "m", "v1")


def test_the_formula_has_not_changed():
    for vector, digest in PINNED.items():
        assert broker.result_key(*vector) == digest
        assert worker.result_key(*vector) == digest
