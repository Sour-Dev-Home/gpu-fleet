import re

from fleet_worker.protocol import result_key

INPUT = "a" * 64  # stands in for a real SHA-256 hex digest


def test_result_key_is_a_sha256_hex_digest():
    assert re.fullmatch(
        r"[0-9a-f]{64}", result_key(INPUT, "qwen3-vl:8b", "describe-v1")
    )


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
