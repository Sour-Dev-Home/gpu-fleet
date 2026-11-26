import pytest
from fastapi.testclient import TestClient
from fleet_broker import enqueue
from fleet_broker.app import create_app
from fleet_broker.enqueue import demo_jobs, main
from tokens import ADMIN, WORKER

ALL = {"footage", "derived", "open"}


def test_demo_jobs_pass_the_brokers_input_rules(db_url):
    # The real contract: every demo job is accepted by the real API (ADR 0011 field rules).
    app = create_app(db_url, WORKER, ADMIN, ALL)
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


def test_demo_jobs_default_to_the_echo_runner_composes_default(monkeypatch):
    jobs = posted_jobs(monkeypatch, ["--demo", "2", "--run", "x"])
    # echo-v1 jobs name the placeholder model "echo", which the echo runner declares (ADR 0015)
    assert {(j["model"], j["prompt_version"]) for j in jobs} == {("echo", "echo-v1")}


def test_a_non_echo_prompt_version_defaults_to_the_smoke_model(monkeypatch):
    jobs = posted_jobs(monkeypatch, ["--demo", "2", "--run", "x", "--prompt-version", "smoke-v1"])
    assert {(j["model"], j["prompt_version"]) for j in jobs} == {("qwen3:8b", "smoke-v1")}


def test_demo_jobs_are_open_by_default_and_the_class_can_be_chosen(monkeypatch):
    jobs = posted_jobs(monkeypatch, ["--demo", "2", "--run", "x"])
    assert {j["data_class"] for j in jobs} == {"open"}
    jobs = posted_jobs(monkeypatch, ["--demo", "2", "--run", "x", "--data-class", "footage"])
    assert {j["data_class"] for j in jobs} == {"footage"}


def test_an_unknown_data_class_is_refused(monkeypatch):
    with pytest.raises(SystemExit):
        posted_jobs(monkeypatch, ["--demo", "1", "--data-class", "secret"])


def test_prompt_version_and_model_can_be_chosen(monkeypatch):
    # e.g. --prompt-version smoke-v1 for the smoke runner, which refuses echo-v1 jobs
    argv = ["--demo", "2", "--run", "x", "--prompt-version", "smoke-v1", "--model", "qwen3:14b"]
    jobs = posted_jobs(monkeypatch, argv)
    assert {(j["model"], j["prompt_version"]) for j in jobs} == {("qwen3:14b", "smoke-v1")}


def test_all_accepted_succeeds(monkeypatch, capsys):
    monkeypatch.setattr(enqueue, "post_job", lambda *a: 201)
    assert main(["--demo", "3", "--run", "x"], env={"FLEET_ADMIN_TOKEN": ADMIN}) == 0
    assert "3/3 jobs added" in capsys.readouterr().out


# --- retries (ADR 0016) ---


class Net:
    """A scripted post_job: each call returns the next status, or raises it if an exception."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls = 0

    def __call__(self, url, token, job):
        self.calls += 1
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def run_enqueue(monkeypatch, net):
    sleeps = []
    monkeypatch.setattr(enqueue, "post_job", net)
    monkeypatch.setattr(enqueue, "_sleep", sleeps.append)
    monkeypatch.setattr(enqueue, "_random", lambda: 1.0)  # full jitter at its maximum
    code = main(["--demo", "1", "--run", "x"], env={"FLEET_ADMIN_TOKEN": ADMIN})
    return code, sleeps


def test_503_then_201_counts_as_added(monkeypatch, capsys):
    net = Net(503, 503, 201)
    code, sleeps = run_enqueue(monkeypatch, net)
    assert (code, net.calls, sleeps) == (0, 3, [0.5, 1.0])  # base 0.5 s, doubling
    assert "1/1 jobs added" in capsys.readouterr().out


def test_five_503s_fail_the_job(monkeypatch):
    net = Net(503, 503, 503, 503, 503)
    code, sleeps = run_enqueue(monkeypatch, net)
    assert (code, net.calls, len(sleeps)) == (1, 5, 4)


def test_422_is_tried_once(monkeypatch):
    net = Net(422)
    code, sleeps = run_enqueue(monkeypatch, net)
    assert (code, net.calls, sleeps) == (1, 1, [])


def test_connection_errors_are_retried(monkeypatch):
    import urllib.error

    net = Net(urllib.error.URLError("refused"), ConnectionResetError(), TimeoutError(), 201)
    code, _ = run_enqueue(monkeypatch, net)
    assert (code, net.calls) == (0, 4)


def test_backoff_is_capped_and_jittered():
    waits = [enqueue.backoff(a, None, lambda: 1.0) for a in range(1, 7)]
    assert waits == [0.5, 1.0, 2.0, 4.0, 8.0, 8.0]
    assert enqueue.backoff(3, None, lambda: 0.0) == 0.0


def test_post_job_reads_retry_after_from_a_503(monkeypatch):
    import email.message
    import urllib.error

    headers = email.message.Message()
    headers["Retry-After"] = "7"

    def fake_urlopen(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 503, "busy", headers, None)

    monkeypatch.setattr(enqueue.urllib.request, "urlopen", fake_urlopen)
    status = enqueue.post_job("http://x", "t", {"job_id": "j"})
    assert (int(status), status.retry_after) == (503, 7.0)


def test_retry_after_is_the_minimum_wait():
    assert enqueue.backoff(1, 3.0, lambda: 0.0) == 3.0
    assert enqueue.backoff(5, 1.0, lambda: 1.0) == 8.0


def test_the_servers_retry_after_is_the_minimum_wait(monkeypatch):
    busy = enqueue.Status(503)
    busy.retry_after = 3.0
    net = Net(busy, 201)
    code, sleeps = run_enqueue(monkeypatch, net)
    assert (code, sleeps) == (0, [3.0])  # jittered backoff alone would be 0.5


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("2", 2.0),
        ("-3", 0.0),
        ("1e9", 8.0),
        ("inf", None),
        ("nan", None),
        ("", None),
        ("Wed, 21 Oct 2015 07:28:00 GMT", None),
    ],
)
def test_retry_after_is_finite_and_capped(header, expected):
    # "inf" made time.sleep raise OverflowError; "1e9" slept for 31 years.
    assert enqueue.parse_retry_after(header) == expected
