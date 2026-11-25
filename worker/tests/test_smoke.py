import json

import httpx
import pytest
from fakes import FakeBroker, FakeClock, FakeStore
from fleet_worker.cli import Config
from fleet_worker.loop import Worker
from fleet_worker.protocol import Job, Lease
from fleet_worker.smoke import ModelServerUnavailable, check_model_server, make_smoke

JOB = Job("j7", "a" * 64, "qwen3:8b", "smoke-v1")
SAME_INPUT = Job("j8", "a" * 64, "qwen3:8b", "smoke-v1")  # another job, same input: same Result key
TOKEN = "test-worker-token-0123456789abcdefghij"  # a test value, not a secret
ENV = {"FLEET_BROKER_URL": "http://127.0.0.1:8000", "FLEET_WORKER_TOKEN": TOKEN}


def model_server(handler) -> httpx.Client:
    return httpx.Client(base_url="http://model", transport=httpx.MockTransport(handler))


def streamed(*texts: str) -> httpx.Response:
    lines = [json.dumps({"response": t, "done": False}) for t in texts]
    lines.append(json.dumps({"response": "", "done": True}))
    return httpx.Response(200, content="\n".join(lines).encode())


def test_smoke_joins_the_stream_and_heartbeats_per_chunk():
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return streamed("ok", " aaaaaaaa")

    beats = []
    result = make_smoke(model_server(handler))(JOB, lambda: beats.append(1))
    assert result == b"ok aaaaaaaa\n"
    assert len(beats) == 3  # one per chunk, including the final one


def test_smoke_sends_the_settings_the_amd_gpu_needs():
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return streamed("ok")

    make_smoke(model_server(handler))(JOB, lambda: None)
    body = sent[0]
    assert body["model"] == "qwen3:8b"
    assert body["think"] is False
    assert body["options"]["use_mmap"] is False
    assert body["options"]["num_ctx"] == 2048
    assert body["options"]["temperature"] == 0
    assert "aaaaaaaa" in body["prompt"]


def test_smoke_asks_for_the_jobs_model():
    # The model is part of the Result key, so the result must come from that model,
    # whatever FLEET_MODEL the worker was started with.
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return streamed("ok")

    job = Job("j9", "a" * 64, "qwen3:14b", "smoke-v1")
    make_smoke(model_server(handler))(job, lambda: None)
    assert sent[0]["model"] == "qwen3:14b"


def test_smoke_implements_smoke_v1_only():
    smoke = make_smoke(model_server(lambda r: streamed("ok")))
    assert getattr(smoke, "prompt_versions", None) == frozenset({"smoke-v1"})


def ollama_404(request):
    # What Ollama answers on /api/generate for a model it doesn't have.
    model = json.loads(request.content)["model"]
    return httpx.Response(404, json={"error": f"model '{model}' not found"})


def test_a_missing_model_stops_the_worker_with_the_pull_command():
    job = Job("j9", "a" * 64, "qwen3:14b", "smoke-v1")
    with pytest.raises(SystemExit, match=r"ollama pull qwen3:14b"):
        make_smoke(model_server(ollama_404))(job, lambda: None)


def test_a_404_quotes_what_the_server_said():
    # A wrong FLEET_MODEL_URL also answers 404; the server's own words tell the two apart.
    job = Job("j9", "a" * 64, "qwen3:14b", "smoke-v1")
    with pytest.raises(SystemExit, match=r"model 'qwen3:14b' not found"):
        make_smoke(model_server(ollama_404))(job, lambda: None)


def test_a_missing_model_stores_and_completes_nothing():
    clock, store = FakeClock(), FakeStore()
    broker = FakeBroker([Lease(JOB, "t", 60, clock())], store, clock)
    worker = Worker(broker, store, make_smoke(model_server(ollama_404)), clock)
    polls = iter(range(5))  # bounded: a worker that moved on would end the test, not hang it
    with pytest.raises(SystemExit, match="qwen3:8b"):
        worker.run(sleep=lambda s: None, should_stop=lambda: next(polls, None) is None)
    assert store.puts == 0
    assert broker.completed == []


def test_a_model_server_error_is_not_swallowed():
    smoke = make_smoke(model_server(lambda r: httpx.Response(500)))
    with pytest.raises(httpx.HTTPStatusError):
        smoke(JOB, lambda: None)


def test_startup_check_passes_when_the_model_is_there():
    tags = {"models": [{"name": "qwen3:8b"}]}
    check_model_server(model_server(lambda r: httpx.Response(200, json=tags)), "qwen3:8b")


def test_startup_check_names_a_missing_model():
    tags = {"models": [{"name": "llama3:8b"}]}
    with pytest.raises(ModelServerUnavailable, match="ollama pull qwen3:8b"):
        check_model_server(model_server(lambda r: httpx.Response(200, json=tags)), "qwen3:8b")


def test_startup_check_reports_an_unreachable_server():
    def refuse(request):
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(ModelServerUnavailable, match="unreachable"):
        check_model_server(model_server(refuse), "qwen3:8b")


def test_smoke_needs_a_model_url():
    with pytest.raises(SystemExit, match="FLEET_MODEL_URL"):
        Config(ENV | {"FLEET_RUNNER": "smoke"})
    Config(ENV | {"FLEET_RUNNER": "smoke", "FLEET_MODEL_URL": "http://model"})


def test_the_prompt_depends_only_on_what_the_result_key_covers():
    # Two jobs sharing an input share a Result key, so they must be asked the same thing.
    prompts = []

    def handler(request):
        prompts.append(json.loads(request.content)["prompt"])
        return streamed("ok")

    smoke = make_smoke(model_server(handler))
    smoke(JOB, lambda: None)
    smoke(SAME_INPUT, lambda: None)
    assert JOB.result_key == SAME_INPUT.result_key
    assert prompts[0] == prompts[1]


def test_startup_check_returns_the_models_on_the_server():
    tags = {"models": [{"name": "qwen3:8b"}, {"name": "llama3:8b"}]}
    names = check_model_server(model_server(lambda r: httpx.Response(200, json=tags)), "qwen3:8b")
    assert names == {"qwen3:8b", "llama3:8b"}


def test_smoke_declares_the_models_it_was_given():
    smoke = make_smoke(model_server(lambda r: streamed("ok")), {"qwen3:8b", "llama3:8b"})
    assert smoke.models == frozenset({"qwen3:8b", "llama3:8b"})
