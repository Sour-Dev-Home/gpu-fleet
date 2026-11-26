"""The content address of a job's result (ADR 0006, 0012, 0023).

The worker computes this key to store a result; the broker recomputes it from the job's own
stored fields and refuses a completion that names any other key. The worker has the same
function in fleet_worker/protocol.py (the packages share no code, ADR 0012);
scripts/tests/test_result_key_agreement.py fails if the two ever disagree.
"""

import hashlib
import json


def result_key(
    input_sha256: str, model: str, prompt_version: str, context_sha256: str | None = None
) -> str:
    """64 lowercase hex chars: SHA-256 of the JSON list [input, model, prompt version(, context)]."""
    parts = [input_sha256, model, prompt_version]
    if context_sha256 is not None:
        # Appended only when present: a job without context keeps the key it had before ADR 0012.
        parts.append(context_sha256)
    return hashlib.sha256(json.dumps(parts).encode("utf-8")).hexdigest()
