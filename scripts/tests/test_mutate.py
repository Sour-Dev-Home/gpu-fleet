import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mutate import classify, pytest_env


def test_all_tests_passing_means_the_mutant_survived():
    assert classify(0, "5 passed in 0.1s") == "SURVIVED"


def test_an_assertion_failure_means_caught():
    out = "FAILED tests/test_x.py::test_a - assert 30.0 < 2.0\n1 failed, 4 passed"
    assert classify(1, out) == "CAUGHT"


def test_a_name_error_is_invalid_not_caught():
    # The mutant from 2026-11-25: `import time` placed after its use.
    out = "FAILED tests/test_cli.py::test_sigterm - NameError: name 'time' is not defined\n"
    assert classify(1, out) == "INVALID"


def test_mixed_reasons_count_as_caught():
    out = (
        "FAILED tests/a.py::t1 - NameError: x\n"
        "FAILED tests/a.py::t2 - AssertionError: assert 1 == 2\n"
    )
    assert classify(1, out) == "CAUGHT"


def test_collection_errors_are_invalid():
    assert classify(2, "ERROR tests/a.py - SyntaxError: invalid syntax") == "INVALID"


def test_pytest_runs_wide_enough_to_show_the_error_name():
    # Regression, 2026-11-25: a narrow captured terminal trimmed "NameError" to "Nam...",
    # and an invalid mutant was graded CAUGHT.
    assert int(pytest_env()["COLUMNS"]) >= 1000
