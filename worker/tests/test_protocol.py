import hashlib
import json
import re

from fleet_worker.protocol import Job, result_key

INPUT = "a" * 64  # stands in for a real SHA-256 hex digest


def test_result_key_is_a_sha256_hex_digest():
    assert re.fullmatch(r"[0-9a-f]{64}", result_key(INPUT, "qwen3-vl:8b", "describe-v1"))


def test_result_key_is_deterministic():
    assert result_key(INPUT, "qwen3-vl:8b", "describe-v1") == result_key(
        INPUT, "qwen3-vl:8b", "describe-v1"
    )


def test_field_boundaries_cannot_collide():
    assert result_key("ab", "c", "d") != result_key("a", "bc", "d")


def test_each_field_changes_the_key():
    base = result_key(INPUT, "qwen3-vl:8b", "describe-v1")
    assert result_key(INPUT, "qwen3-vl:8b", "describe-v2") != base
    assert result_key(INPUT, "qwen3-vl:16b", "describe-v1") != base
    assert result_key("b" * 64, "qwen3-vl:8b", "describe-v1") != base


# --- context hash (ADR 0012) ---

INPUT, MODEL, PROMPT = "a" * 64, "qwen3-vl:8b", "describe-v3"


def test_no_context_keeps_the_original_key():
    # Pinned value from before context_sha256 existed: results cached by older
    # workers must still be found. If this changes, every cached result is orphaned.
    old = hashlib.sha256(json.dumps([INPUT, MODEL, PROMPT]).encode()).hexdigest()
    assert result_key(INPUT, MODEL, PROMPT) == old
    assert result_key(INPUT, MODEL, PROMPT, None) == old


def test_context_changes_the_key():
    bible_v1, bible_v2 = "1" * 64, "2" * 64
    keys = {
        result_key(INPUT, MODEL, PROMPT),
        result_key(INPUT, MODEL, PROMPT, bible_v1),
        result_key(INPUT, MODEL, PROMPT, bible_v2),
    }
    assert len(keys) == 3  # a new story bible is never served an old description


def test_job_includes_context_in_its_key():
    ctx = "1" * 64
    assert Job("j", INPUT, MODEL, PROMPT, ctx).result_key == result_key(INPUT, MODEL, PROMPT, ctx)
    assert Job("j", INPUT, MODEL, PROMPT).context_sha256 is None
