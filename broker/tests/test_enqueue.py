from fastapi.testclient import TestClient
from fleet_broker import enqueue
from fleet_broker.app import create_app
from fleet_broker.enqueue import demo_jobs, main
from tokens import ADMIN, WORKER


def test_demo_jobs_pass_the_brokers_input_rules(tmp_path):
    # The real contract: every demo job is accepted by the real API (ADR 0011 field rules).
    app = create_app(tmp_path / "broker.db", WORKER, ADMIN)
    admin = TestClient(app, headers={"Authorization": f"Bearer {ADMIN}"})
    for job in demo_jobs(5, "t"):
        assert admin.post("/jobs", json=job).status_code == 201


def test_a_new_run_reuses_the_same_inputs_under_new_ids():
    a, b = demo_jobs(3, "a"), demo_jobs(3, "b")
    assert [j["input_sha256"] for j in a] == [j["input_sha256"] for j in b]  # cached on run b
    assert {j["job_id"] for j in a}.isdisjoint(j["job_id"] for j in b)
    assert len({j["input_sha256"] for j in a}) == 3


def test_missing_admin_token_is_refused(capsys):
    assert main(["--demo", "1"], env={}) == 2
    assert "FLEET_ADMIN_TOKEN" in capsys.readouterr().err


def test_any_rejected_job_fails_the_command(monkeypatch):
    statuses = iter([201, 401])
    monkeypatch.setattr(enqueue, "post_job", lambda *a: next(statuses))
    assert main(["--demo", "2", "--run", "x"], env={"FLEET_ADMIN_TOKEN": ADMIN}) == 1


def posted_jobs(monkeypatch, argv):
    jobs = []
    monkeypatch.setattr(enqueue, "post_job", lambda url, token, job: jobs.append(job) or 201)
    assert main(argv, env={"FLEET_ADMIN_TOKEN": ADMIN}) == 0
    return jobs


def test_demo_jobs_default_to_the_smoke_runners_version_and_model(monkeypatch):
    jobs = posted_jobs(monkeypatch, ["--demo", "2", "--run", "x"])
    assert {(j["model"], j["prompt_version"]) for j in jobs} == {("qwen3:8b", "smoke-v1")}


def test_prompt_version_and_model_can_be_chosen(monkeypatch):
    # e.g. --prompt-version echo-v1 for the echo runner, which refuses smoke-v1 jobs
    argv = ["--demo", "2", "--run", "x", "--prompt-version", "echo-v1", "--model", "qwen3:14b"]
    jobs = posted_jobs(monkeypatch, argv)
    assert {(j["model"], j["prompt_version"]) for j in jobs} == {("qwen3:14b", "echo-v1")}


def test_all_accepted_succeeds(monkeypatch, capsys):
    monkeypatch.setattr(enqueue, "post_job", lambda *a: 201)
    assert main(["--demo", "3", "--run", "x"], env={"FLEET_ADMIN_TOKEN": ADMIN}) == 0
    assert "3/3 jobs added" in capsys.readouterr().out
