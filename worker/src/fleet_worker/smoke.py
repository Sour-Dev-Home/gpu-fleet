"""The `smoke` runner: one tiny, fixed request to the model server (ADR 0014).

It proves the path every real runner will take (worker container -> model server -> GPU)
before real job inputs exist.

The prompt uses only what the Result key covers (here, the input hash), never the job id:
two jobs with the same input share one Result key, so they must get the same answer, or
the cache would hand one job a result written for another.
"""

from collections.abc import Callable, Collection

import httpx

from .loop import Runner
from .protocol import Job

# The one prompt this runner implements; jobs for any other version stop the worker.
PROMPT_VERSION = "smoke-v1"
# Settings the project requires for the local AMD GPU (CLAUDE.md): no mmap, explicit context.
OPTIONS = {"temperature": 0, "seed": 0, "num_ctx": 2048, "num_predict": 16, "use_mmap": False}


class ModelServerUnavailable(SystemExit):
    """Raised at startup: the worker can't do anything useful, so it exits with this message."""


class ModelNotFound(ModelServerUnavailable):
    """A job names a model the model server doesn't have. Like a prompt-version mismatch, that
    is misconfiguration, not a bad job: the worker stops instead of burning the job's attempts
    (ADR 0014 amendment)."""


def check_model_server(client: httpx.Client, model: str) -> set[str]:
    """Fail at startup, with a message a person can act on, instead of on every job.
    Returns the model names the server has: the worker declares them as its models (ADR 0015).

    model is FLEET_MODEL: the model this worker expects its jobs to name. Jobs choose their
    own model; this only catches the common mistake (server down, model not pulled) early.
    """
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
    return names


def make_smoke(client: httpx.Client, models: Collection[str] = ()) -> Runner:
    """models: the names the model server has (from check_model_server). Empty by default:
    the broker refuses a lease request that declares none, so it fails loudly, not quietly."""

    def smoke(job: Job, keepalive: Callable[[], None]) -> bytes:
        body = {
            # The job's model, not FLEET_MODEL: the model is part of the Result key, so the
            # result must come from the model the key names.
            "model": job.model,
            "prompt": f"Reply with exactly these words and nothing else: ok {job.input_sha256[:8]}",
            "stream": True,
            "think": False,  # qwen3 would otherwise spend the tokens on hidden reasoning
            "options": OPTIONS,
        }
        parts: list[str] = []
        # Streamed newline-delimited JSON: one object per chunk, the last has "done": true.
        with client.stream("POST", "/api/generate", json=body) as r:
            if r.status_code == 404:  # Ollama: {"error": "model '<name>' not found"}
                # Quote the server's own words: a wrong FLEET_MODEL_URL also gives 404, and the
                # body tells the two apart.
                said = r.read().decode("utf-8", "replace").strip()[:200]
                raise ModelNotFound(
                    f"fleet-worker: job {job.job_id} needs model {job.model!r}, which the model "
                    f"server at {client.base_url} doesn't have (it said: {said}); run "
                    f"`ollama pull {job.model}`. Stopping: nothing stored or completed."
                )
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

    smoke.prompt_versions = frozenset({PROMPT_VERSION})
    smoke.models = frozenset(models)
    return smoke
