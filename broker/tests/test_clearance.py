"""ADR 0015: a job is leased only to a worker whose clearance covers its data class and whose
declared capabilities (prompt versions, models) include the job's. All in the one claim statement."""

import sqlite3

import pytest
from fastapi.testclient import TestClient
from fleet_broker.app import _from_env, create_app
from fleet_broker.store import JobStore
from tokens import ADMIN, WORKER

NOW = 1_800_000_000.0
ALL = frozenset({"footage", "derived", "open"})
PV = frozenset({"v1"})
MODELS = frozenset({"m"})


def claim(store, clearance=ALL, capabilities=PV, models=MODELS):
    return store.claim(NOW, 60, clearance, capabilities, models)


def make(tmp_path, **jobs):
    """jobs: job_id -> (data_class, prompt_version, model)."""
    store = JobStore(tmp_path / "broker.db")
    for job_id, (data_class, pv, model) in jobs.items():
        store.add(job_id, "a" * 64, model, pv, data_class=data_class)
    return store


# --- schema ---


def test_old_database_gets_a_nullable_data_class_column(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE jobs (job_id TEXT PRIMARY KEY, input_sha256 TEXT NOT NULL, model TEXT NOT NULL,"
        " prompt_version TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL"
        " DEFAULT 0, lease_token TEXT, lease_expires_at REAL, result_key TEXT, context_sha256 TEXT)"
    )
    db.execute(
        "INSERT INTO jobs (job_id, input_sha256, model, prompt_version) VALUES ('o','a','m','v1')"
    )
    db.commit()
    db.close()
    store = JobStore(path)
    assert store.get("o")["data_class"] is None


def test_null_data_class_is_footage(tmp_path):
    store = make(tmp_path, j=(None, "v1", "m"))
    assert claim(store, clearance=frozenset({"open", "derived"})) is None
    assert claim(store, clearance=frozenset({"footage"})).job_id == "j"


# --- the three conditions ---


def test_security_open_clearance_never_leases_footage_or_null(tmp_path):
    store = make(tmp_path, f=("footage", "v1", "m"), n=(None, "v1", "m"), d=("derived", "v1", "m"))
    assert claim(store, clearance=frozenset({"open"})) is None
    for job_id in "fnd":
        assert store.get(job_id)["state"] == "queued"
        assert store.get(job_id)["attempts"] == 0


def test_clearance_picks_only_matching_class_even_behind_other_jobs(tmp_path):
    store = make(tmp_path, f=("footage", "v1", "m"), o=("open", "v1", "m"))
    assert claim(store, clearance=frozenset({"open"})).job_id == "o"
    assert claim(store, clearance=frozenset({"open"})) is None
    assert store.get("f")["state"] == "queued"


def test_prompt_version_must_be_declared(tmp_path):
    store = make(tmp_path, j=("open", "smoke-v1", "m"))
    assert claim(store, capabilities=frozenset({"echo-v1"})) is None
    assert claim(store, capabilities=frozenset({"echo-v1", "smoke-v1"})).job_id == "j"


def test_model_must_be_declared(tmp_path):
    store = make(tmp_path, j=("open", "v1", "qwen3:8b"))
    assert claim(store, models=frozenset({"echo"})) is None
    assert claim(store, models=frozenset({"echo", "qwen3:8b"})).job_id == "j"


def test_a_job_nobody_can_run_stays_queued_and_never_dead_letters(tmp_path):
    store = make(tmp_path, j=("footage", "v1", "m"))
    for i in range(10):  # more tries than max_attempts
        assert claim(store, clearance=frozenset({"open"})) is None
    row = store.get("j")
    assert (row["state"], row["attempts"]) == ("queued", 0)


def test_empty_sets_lease_nothing(tmp_path):
    store = make(tmp_path, j=("open", "v1", "m"))
    assert claim(store, clearance=frozenset()) is None
    assert claim(store, capabilities=frozenset()) is None
    assert claim(store, models=frozenset()) is None


def test_values_are_parameters_not_sql(tmp_path):
    store = make(tmp_path, j=("open", "v1", "m"))
    assert claim(store, capabilities=frozenset({"x') OR 1=1 --"})) is None


# --- API ---

JOB = {
    "job_id": "j0",
    "input_sha256": "a" * 64,
    "model": "m",
    "prompt_version": "v1",
    "data_class": "open",
}
CAPS = {"capabilities": ["v1"], "models": ["m"]}


def client(tmp_path, token, clearance=ALL):
    app = create_app(tmp_path / "broker.db", WORKER, ADMIN, clearance=clearance)
    return TestClient(app, headers={"Authorization": f"Bearer {token}"})


@pytest.mark.parametrize("value", [None, "secret", "", "OPEN", 1])
def test_post_jobs_rejects_missing_or_unknown_data_class(tmp_path, value):
    job = {k: v for k, v in JOB.items() if k != "data_class"}
    if value is not None:
        job["data_class"] = value
    assert client(tmp_path, ADMIN).post("/jobs", json=job).status_code == 422


@pytest.mark.parametrize("value", ["footage", "derived", "open"])
def test_post_jobs_accepts_each_data_class(tmp_path, value):
    assert (
        client(tmp_path, ADMIN).post("/jobs", json=JOB | {"data_class": value}).status_code == 201
    )


@pytest.mark.parametrize(
    "body",
    [
        None,
        {},
        {"capabilities": ["v1"]},
        {"models": ["m"]},
        {"capabilities": [], "models": ["m"]},
        {"capabilities": ["v1"], "models": []},
        {"capabilities": ["bad name!"], "models": ["m"]},
        {"capabilities": ["v1"] * 33, "models": ["m"]},
        {"capabilities": ["v1"], "models": ["m"] * 33},
        {"capabilities": "v1", "models": ["m"]},
        {"capabilities": ["v1"], "models": ["m"], "clearance": ["footage"]},
    ],
)
def test_lease_requires_valid_capabilities(tmp_path, body):
    c = client(tmp_path, WORKER)
    assert c.post("/lease", json=body).status_code == 422


def test_lease_with_open_clearance_skips_footage_over_http(tmp_path):
    admin = client(tmp_path, ADMIN)
    admin.post("/jobs", json=JOB | {"job_id": "f", "data_class": "footage"})
    admin.post("/jobs", json=JOB | {"job_id": "o"})
    worker = client(tmp_path, WORKER, clearance=frozenset({"open"}))
    assert worker.post("/lease", json=CAPS).json()["job_id"] == "o"
    assert worker.post("/lease", json=CAPS).status_code == 204


def test_lease_skips_jobs_for_other_capabilities_over_http(tmp_path):
    admin = client(tmp_path, ADMIN)
    admin.post("/jobs", json=JOB | {"prompt_version": "smoke-v1"})
    worker = client(tmp_path, WORKER)
    assert worker.post("/lease", json=CAPS).status_code == 204
    assert (
        worker.post("/lease", json={"capabilities": ["smoke-v1"], "models": ["m"]}).status_code
        == 200
    )


# --- clearance configuration ---


@pytest.mark.parametrize(
    "clearance", [frozenset(), frozenset({"secret"}), frozenset({"open", "x"})]
)
def test_create_app_rejects_empty_or_unknown_clearance(tmp_path, clearance):
    with pytest.raises(ValueError, match="clearance"):
        create_app(tmp_path / "broker.db", WORKER, ADMIN, clearance=clearance)


def test_clearance_is_required(tmp_path):
    with pytest.raises(TypeError):
        create_app(tmp_path / "broker.db", WORKER, ADMIN)


def env(monkeypatch, tmp_path, clearance):
    monkeypatch.setenv("FLEET_WORKER_TOKEN", WORKER)
    monkeypatch.setenv("FLEET_ADMIN_TOKEN", ADMIN)
    monkeypatch.setenv("FLEET_DB", str(tmp_path / "e.db"))
    monkeypatch.delenv("FLEET_WORKER_CLEARANCE", raising=False)
    if clearance is not None:
        monkeypatch.setenv("FLEET_WORKER_CLEARANCE", clearance)


@pytest.mark.parametrize("value", [None, "", " , ", "footage,secret", "Open"])
def test_from_env_requires_a_valid_clearance(monkeypatch, tmp_path, value):
    env(monkeypatch, tmp_path, value)
    with pytest.raises(ValueError, match="FLEET_WORKER_CLEARANCE"):
        _from_env()


def test_a_real_broker_writes_no_access_log(monkeypatch, tmp_path):
    # One line per request with the caller's IP: personal data for every volunteer (ADR 0018).
    import logging

    access = logging.getLogger("uvicorn.access")
    monkeypatch.setattr(access, "disabled", False)  # restored after the test
    env(monkeypatch, tmp_path, "open")
    _from_env()
    assert access.disabled


def test_from_env_accepts_the_previous_tokens_during_a_rotation(monkeypatch, tmp_path):
    old_worker, old_admin = "old-worker-" + "w" * 32, "old-admin-" + "a" * 32
    env(monkeypatch, tmp_path, "open")
    monkeypatch.setenv("FLEET_WORKER_TOKEN_PREVIOUS", old_worker)
    monkeypatch.setenv("FLEET_ADMIN_TOKEN_PREVIOUS", old_admin)
    app = _from_env()
    assert (
        TestClient(app, headers={"Authorization": f"Bearer {old_admin}"})
        .post("/jobs", json=JOB)
        .status_code
        == 201
    )
    w = TestClient(app, headers={"Authorization": f"Bearer {old_worker}"})
    assert w.post("/lease", json=CAPS).status_code == 200


def test_from_env_reads_the_rate_limit(monkeypatch, tmp_path):
    env(monkeypatch, tmp_path, "open")
    monkeypatch.setenv("FLEET_WORKER_BURST", "1")
    w = TestClient(_from_env(), headers={"Authorization": f"Bearer {WORKER}"})
    assert [w.post("/lease", json=CAPS).status_code for _ in range(2)] == [204, 429]
    monkeypatch.setenv("FLEET_WORKER_RATE", "0")
    with pytest.raises(ValueError, match="rate"):
        _from_env()


def test_from_env_reads_the_clearance_list(monkeypatch, tmp_path):
    env(monkeypatch, tmp_path, "derived, open")
    c = TestClient(_from_env(), headers={"Authorization": f"Bearer {ADMIN}"})
    c.post("/jobs", json=JOB | {"data_class": "footage"})
    w = TestClient(_from_env(), headers={"Authorization": f"Bearer {WORKER}"})
    assert w.post("/lease", json=CAPS).status_code == 204
