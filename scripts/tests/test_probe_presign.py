"""The report logic of scripts/probe-presign.py (the part that needs no AWS)."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "probe_presign", ROOT / "scripts" / "probe-presign.py"
)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)

ALL_REFUSED = {"control": 200, "wrong_length": 403, "wrong_checksum": 400, "overwrite": 412}


def test_all_three_refused_is_exit_0():
    code, lines = probe.verdicts(ALL_REFUSED)
    assert code == 0
    assert all(line.startswith("ENFORCED ") for line in lines) and len(lines) == 3


def test_an_accepted_wrong_length_is_reported_and_exit_1():
    code, lines = probe.verdicts(ALL_REFUSED | {"wrong_length": 200})
    assert code == 1
    (bad,) = [line for line in lines if line.startswith("NOT ENFORCED")]
    assert "longer than the signed length" in bad


def test_an_accepted_overwrite_is_reported():
    code, lines = probe.verdicts(ALL_REFUSED | {"overwrite": 200})
    assert code == 1 and any("If-None-Match" in line and "NOT" in line for line in lines)


def test_a_failed_control_makes_the_other_answers_meaningless():
    code, lines = probe.verdicts(ALL_REFUSED | {"control": 403})
    assert code == 2 and len(lines) == 1 and "CONTROL FAILED" in lines[0]
