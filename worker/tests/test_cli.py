import os
import threading
import time

import pytest
from fakes import FakeBroker, FakeClock, FakeStore
from fleet_worker.backoff import Backoff
from fleet_worker.cli import Config, Stopper, build, echo
from fleet_worker.dir_store import DirStore
from fleet_worker.loop import Worker
from fleet_worker.protocol import Job

KEY = "f" * 64
TOKEN = "test-worker-token-0123456789abcdefghij"  # a test value, not a secret
ENV = {"FLEET_BROKER_URL": "http://127.0.0.1:8000", "FLEET_WORKER_TOKEN": TOKEN}


# --- DirStore ---


def test_dir_store_round_trip_and_overwrite(tmp_path):
    s = DirStore(tmp_path / "results")
    assert not s.exists(KEY)
    s.put(KEY, b"one")
    s.put(KEY, b"one")  # a duplicate run overwrites identical bytes: harmless
    assert s.exists(KEY)
    assert (tmp_path / "results" / KEY).read_bytes() == b"one"


@pytest.mark.parametrize("key", ["../../escape", "F" * 64, "", "a" * 63])
def test_dir_store_rejects_anything_but_a_result_key(tmp_path, key):
    with pytest.raises(ValueError):
        DirStore(tmp_path).put(key, b"x")


def test_a_failed_write_leaves_no_result_and_no_temp_file(tmp_path, monkeypatch):
    s = DirStore(tmp_path)

    def crash(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", crash)
    with pytest.raises(OSError):
        s.put(KEY, b"half")
    assert not s.exists(KEY)  # exists() must never see a partial result
    assert list(tmp_path.iterdir()) == []


# --- Config ---


@pytest.mark.parametrize("drop", ["FLEET_BROKER_URL", "FLEET_WORKER_TOKEN"])
def test_missing_settings_stop_with_a_clear_message(drop):
    env = {k: v for k, v in ENV.items() if k != drop}
    with pytest.raises(SystemExit, match=drop):
        Config(env)


def test_unknown_runner_is_refused():
    with pytest.raises(SystemExit, match="FLEET_RUNNER"):
        Config(ENV | {"FLEET_RUNNER": "nope"})


def test_config_never_shows_the_token():
    assert TOKEN not in repr(Config(ENV))


def test_plain_http_to_a_service_name_needs_an_explicit_opt_in(tmp_path):
    env = ENV | {"FLEET_BROKER_URL": "http://broker:8000", "FLEET_RESULTS_DIR": str(tmp_path)}
    with pytest.raises(ValueError, match="refusing"):
        build(Config(env))
    _, client = build(Config(env | {"FLEET_ALLOW_HTTP": "1"}))
    client.close()


def test_the_worker_declares_its_runners_capabilities_to_the_broker(tmp_path):
    worker, client = build(Config(ENV | {"FLEET_RESULTS_DIR": str(tmp_path)}))
    client.close()
    assert worker.broker.capabilities == ["echo-v1"]
    assert worker.broker.models == ["echo"]


def test_echo_runner_is_deterministic():
    job = Job("j", "a" * 64, "m", "v1")
    assert echo(job, lambda: None) == echo(job, lambda: None)


# --- graceful stop ---


def test_sigterm_wakes_an_idle_worker_immediately():
    stopper = Stopper()
    clock, store = FakeClock(), FakeStore()
    worker = Worker(FakeBroker([], store, clock), store, echo, clock)
    # Idle, it backs off for 30 s (pinned, no jitter). A stop must cut that short.
    long_wait = Backoff(base=30, cap=60, rng=lambda: 1.0)
    threading.Timer(0.2, stopper.handle, args=(15,)).start()
    started = time.monotonic()
    worker.run(should_stop=stopper.should_stop, sleep=stopper.sleep, backoff=long_wait)
    assert time.monotonic() - started < 2.0
