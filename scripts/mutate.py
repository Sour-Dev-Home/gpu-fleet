"""Mutation check: break the code on purpose and confirm a test notices.

    uv run --no-project scripts/mutate.py FILE FROM TO [--pkg DIR] [-- PYTEST ARGS]

Replaces FROM with TO in FILE (FROM must occur exactly once), runs the package's tests,
always restores the file, and prints one verdict:

  CAUGHT    a test failed on an assertion: the tests guard this behaviour
  SURVIVED  every test passed: no test notices this change (a gap)
  INVALID   the mutant broke the code itself (NameError, SyntaxError, a collection error):
            the tests didn't judge the behaviour at all, so this proves nothing

Exit status: 0 for CAUGHT, 1 for SURVIVED, 2 for INVALID.
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

# Errors that mean the mutant broke the program, not the behaviour under test.
BROKEN = ("NameError", "SyntaxError", "IndentationError", "ImportError", "ModuleNotFoundError")


def classify(returncode: int, output: str) -> str:
    if returncode == 0:
        return "SURVIVED"
    if returncode != 1:  # 2 interrupted/collection error, 3 internal, 4 usage, 5 no tests
        return "INVALID"
    reasons = re.findall(r"^(?:FAILED|ERROR) \S+ - (\w+)", output, flags=re.MULTILINE)
    if reasons and all(r in BROKEN for r in reasons):
        return "INVALID"
    return "CAUGHT"


def pytest_env() -> dict[str, str]:
    # pytest trims "FAILED test - NameError: ..." to the terminal width ("- Nam..."), and a
    # captured run has a narrow default width. classify() needs the error name intact.
    return {**os.environ, "COLUMNS": "10000"}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    extra: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, extra = argv[:i], argv[i + 1 :]
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("file", type=Path)
    p.add_argument("old")
    p.add_argument("new")
    p.add_argument("--pkg", type=Path, help="package dir to run pytest in (default: guessed)")
    args = p.parse_args(argv)

    # Bytes, not text: text mode translates newlines on Windows, so a "restored" LF file
    # would come back as CRLF. Restoring must give back exactly the original bytes.
    source = args.file.read_bytes()
    old, new = args.old.encode(), args.new.encode()
    count = source.count(old)
    if count != 1:
        print(f"FROM must occur exactly once in {args.file}, found {count}", file=sys.stderr)
        return 2
    pkg = args.pkg or next(
        (d for d in args.file.resolve().parents if (d / "pyproject.toml").exists()), None
    )
    if pkg is None:
        print("no package found: pass --pkg", file=sys.stderr)
        return 2

    try:
        # inside the try, so even a partial write is restored
        args.file.write_bytes(source.replace(old, new))
        cmd = ["uv", "run", "-q", "pytest", "-q", "-p", "no:cacheprovider", "-rfE", *extra]
        # check=False: a failing test run is the expected outcome, read from returncode
        run = subprocess.run(
            cmd, check=False, cwd=pkg, capture_output=True, text=True, env=pytest_env()
        )
    finally:
        args.file.write_bytes(source)  # always restore, even on Ctrl-C

    output = run.stdout + run.stderr
    verdict = classify(run.returncode, output)
    lines = output.strip().splitlines()
    print(f"{verdict}: {lines[-1] if lines else '(no output)'}")
    if verdict != "SURVIVED":
        for line in re.findall(r"^(?:FAILED|ERROR) .*$", output, flags=re.MULTILINE)[:5]:
            print(f"  {line}")
    return {"CAUGHT": 0, "SURVIVED": 1, "INVALID": 2}[verdict]


if __name__ == "__main__":
    sys.exit(main())
