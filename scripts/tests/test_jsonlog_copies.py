"""The JSON log formatter exists once per package (ADR 0018); the copies must not drift apart."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_worker_and_broker_jsonlog_are_identical():
    worker = (ROOT / "worker/src/fleet_worker/jsonlog.py").read_bytes()
    broker = (ROOT / "broker/src/fleet_broker/jsonlog.py").read_bytes()
    assert worker == broker, "edit one jsonlog.py, then copy it over the other"
