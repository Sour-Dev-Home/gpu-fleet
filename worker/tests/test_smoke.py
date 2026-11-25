import json

import httpx
import pytest
from fleet_worker.cli import Config
from fleet_worker.protocol import Job
from fleet_worker.smoke import ModelServerUnavailable, check_model_server, make_smoke

JOB = Job("j7", "a" * 64, "m", "v1")
SAME_INPUT = Job("j8", "a" * 64, "m", "v1")  # another job, same input: same Result key
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
    result = make_smoke(model_server(handler), "qwen3:8b")(JOB, lambda: beats.append(1))
    assert result == b"ok aaaaaaaa\n"
    assert len(beats) == 3  # one per chunk, including the final one


def test_smoke_sends_the_settings_the_amd_gpu_needs():
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return streamed("ok")

    make_smoke(model_server(handler), "qwen3:8b")(JOB, lambda: None)
    body = sent[0]
    assert body["model"] == "qwen3:8b"
    assert body["think"] is False
    assert body["options"]["use_mmap"] is False
    assert body["options"]["num_ctx"] == 2048
    assert body["options"]["temperature"] == 0
    assert "aaaaaaaa" in body["prompt"]


def test_a_model_server_error_is_not_swallowed():
    smoke = make_smoke(model_server(lambda r: httpx.Response(500)), "qwen3:8b")
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

    smoke = make_smoke(model_server(handler), "qwen3:8b")
    smoke(JOB, lambda: None)
    smoke(SAME_INPUT, lambda: None)
    assert JOB.result_key == SAME_INPUT.result_key
    assert prompts[0] == prompts[1]
