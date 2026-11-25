"""scripts/check.sh lints untracked files, then leaves the index as it found it.

Runs the real script in a throwaway git repo with a stub `pre-commit` that fails when any
file it can see (git ls-files) contains BAD, the way a real hook fails on a bad new file.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

CHECK_SH = Path(__file__).resolve().parents[1] / "check.sh"
BASH = shutil.which("bash")
STUB = """#!/usr/bin/env bash
# fails when a file pre-commit would check (tracked or intent-to-add) contains BAD
if git ls-files -z | xargs -0 grep -l BAD >/dev/null 2>&1; then echo "stub: BAD found"; exit 1; fi
exit 0
"""

pytestmark = pytest.mark.skipif(BASH is None, reason="needs bash")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy(CHECK_SH, repo / "scripts" / "check.sh")
    git(repo, "init", "-q")
    git(repo, "add", "scripts/check.sh")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "init")
    return repo


def run_check(repo: Path, tmp_path: Path) -> str:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "pre-commit"
    stub.write_text(STUB, encoding="utf-8", newline="\n")
    stub.chmod(0o755)
    env = os.environ | {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    # check=False: the repo has no pyproject.toml, so the script always ends "nothing tested"
    # (exit 1). The tests read the pre-commit line instead.
    out = subprocess.run(
        [BASH, "scripts/check.sh"], check=False, cwd=repo, env=env, capture_output=True, text=True
    )
    return out.stdout + out.stderr


def test_an_untracked_bad_file_fails_and_stays_untracked(repo, tmp_path):
    (repo / "bad.py").write_text("BAD\n")
    out = run_check(repo, tmp_path)
    assert "pre-commit: FAILED" in out
    assert git(repo, "status", "--short") == "?? bad.py\n"


def test_an_untracked_good_file_passes_and_stays_untracked(repo, tmp_path):
    (repo / "good name.py").write_text("fine\n")  # a space: paths pass NUL-separated
    out = run_check(repo, tmp_path)
    assert "pre-commit: ok" in out
    assert git(repo, "status", "--short") == '?? "good name.py"\n'


def test_files_the_user_staged_stay_staged(repo, tmp_path):
    (repo / "staged.py").write_text("fine\n")
    git(repo, "add", "staged.py")
    (repo / "new.py").write_text("fine\n")
    run_check(repo, tmp_path)
    assert git(repo, "status", "--short") == "A  staged.py\n?? new.py\n"


def test_a_file_name_that_looks_like_a_pattern_touches_only_itself(repo, tmp_path):
    # git reads path arguments as patterns: "f[a].py" also matches "fa.py", so a pattern-reading
    # undo would unstage the user's fa.py.
    (repo / "fa.py").write_text("fine\n")
    git(repo, "add", "fa.py")
    (repo / "f[a].py").write_text("fine\n")
    run_check(repo, tmp_path)
    assert git(repo, "status", "--short") == "A  fa.py\n?? f[a].py\n"


def test_no_untracked_files_leaves_the_index_alone(repo, tmp_path):
    (repo / "staged.py").write_text("fine\n")
    git(repo, "add", "staged.py")
    run_check(repo, tmp_path)
    assert git(repo, "status", "--short") == "A  staged.py\n"
