"""The `smoke` runner: one tiny, fixed request to the model server (ADR 0014).

It proves the path every real runner will take (worker container -> model server -> GPU)
before real job inputs exist.

The prompt uses only what the Result key covers (here, the input hash), never the job id:
two jobs with the same input share one Result key, so they must get the same answer, or
the cache would hand one job a result written for another.
"""

from collections.abc import Callable

import httpx

from .protocol import Job

# Settings the project requires for the local AMD GPU (CLAUDE.md): no mmap, explicit context.
OPTIONS = {"temperature": 0, "seed": 0, "num_ctx": 2048, "num_predict": 16, "use_mmap": False}


class ModelServerUnavailable(SystemExit):
    """Raised at startup: the worker can't do anything useful, so it exits with this message."""


def check_model_server(client: httpx.Client, model: str) -> None:
    """Fail at startup, with a message a person can act on, instead of on every job."""
    try:
        tags = client.get("/api/tags").raise_for_status().json()
    except httpx.HTTPError as e:
        raise ModelServerUnavailable(
            f"fleet-worker: model server at {client.base_url} unreachable ({e!r})"
        ) from e
    names = {m["name"] for m in tags.get("models", [])}
    if model not in names:
        raise ModelServerUnavailable(
            f"fleet-worker: model {model!r} not on the model server; run `ollama pull {model}`"
        )


def make_smoke(client: httpx.Client, model: str) -> Callable[[Job, Callable[[], None]], bytes]:
    def smoke(job: Job, keepalive: Callable[[], None]) -> bytes:
        body = {
            "model": model,
            "prompt": f"Reply with exactly these words and nothing else: ok {job.input_sha256[:8]}",
            "stream": True,
            "think": False,  # qwen3 would otherwise spend the tokens on hidden reasoning
            "options": OPTIONS,
        }
        parts: list[str] = []
        # Streamed newline-delimited JSON: one object per chunk, the last has "done": true.
        with client.stream("POST", "/api/generate", json=body) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line:
                    continue
                chunk = httpx.Response(200, content=line).json()
                parts.append(chunk.get("response", ""))
                keepalive()  # optional since ADR 0014's heartbeat thread, but cheap
                if chunk.get("done"):
                    break
        return ("".join(parts).strip() + "\n").encode()

    return smoke
