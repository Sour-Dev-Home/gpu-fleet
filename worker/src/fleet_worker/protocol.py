"""The job protocol shared by worker and broker.

A result's storage key is derived from everything that determines its content:
the input's SHA-256, the model, and the prompt version. Two workers that run the
same job (at-least-once delivery) therefore write identical bytes to the same key,
so a duplicate run is a harmless overwrite, and a finished result is never recomputed.
"""

import hashlib
import json


def result_key(input_sha256: str, model: str, prompt_version: str) -> str:
    """Return the content address (64 lowercase hex chars) for a job's result.

    input_sha256: hex SHA-256 of the job's input bytes (e.g. a clip or a transcript).
    model: model identifier, e.g. "qwen3-vl:8b".
    prompt_version: version label of the prompt template, e.g. "describe-v3".
    """

    data = json.dumps([input_sha256, model, prompt_version])
    return hashlib.sha256(data.encode("utf-8")).hexdigest()
