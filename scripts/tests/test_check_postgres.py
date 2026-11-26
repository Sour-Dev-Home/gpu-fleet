"""scripts/check.sh gives the broker tests a PostgreSQL (ADR 0020): a throwaway container on a
loopback port when FLEET_TEST_DATABASE_URL is unset, always removed, and none when it is set.

Runs the real script in a throwaway git repo, with stub `docker` and `uv` programs that log
their calls: no container is started.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

CHECK_SH = Path(__file__).resolve().parents[1] / "check.sh"
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="needs bash")

DOCKER = """#!/usr/bin/env bash
echo "docker $*" >> "$STUB_LOG"
case "$1" in
  port) echo "127.0.0.1:55432" ;;
  run) echo "container-id" ;;
esac
exit 0
"""
# a fake uv: prints what the broker's tests would see, and fails like a red test run
UV = """#!/usr/bin/env bash
echo "uv url=${FLEET_TEST_DATABASE_URL:-unset}" >> "$STUB_LOG"
[ "${STUB_UV_FAIL:-0}" = 1 ] && { echo "FAILED tests/x.py::t - AssertionError"; exit 1; }
echo "1 passed in 0.01s"
exit 0
"""


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy(CHECK_SH, repo / "scripts" / "check.sh")
    (repo / "broker").mkdir()
    (repo / "broker" / "pyproject.toml").write_text("[project]\nname='x'\n")
    for args in (["init", "-q"], ["add", "."]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "i"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo


def run_check(repo: Path, tmp_path: Path, **env) -> tuple[list[str], str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in (("docker", DOCKER), ("uv", UV)):
        stub = bin_dir / name
        stub.write_text(body, encoding="utf-8", newline="\n")
        stub.chmod(0o755)
    log = tmp_path / "calls.log"
    log.write_text("")
    base = {k: v for k, v in os.environ.items() if k != "FLEET_TEST_DATABASE_URL"}
    full = base | {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "STUB_LOG": str(log)} | env
    out = subprocess.run(
        [BASH, "scripts/check.sh", "tests"],
        check=False,
        cwd=repo,
        env=full,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return log.read_text().splitlines(), out.stdout + out.stderr


def test_without_a_url_a_throwaway_postgres_is_started_used_and_removed(repo, tmp_path):
    calls, _ = run_check(repo, tmp_path)
    run = [c for c in calls if c.startswith("docker run")]
    assert len(run) == 1
    assert "-p 127.0.0.1::5432" in run[0]  # loopback only, random host port
    assert "postgres:18@sha256:" in run[0]
    assert "docker rm -f" in calls[-1] or any(c.startswith("docker rm -f") for c in calls)
    uv = [c for c in calls if c.startswith("uv url=")]
    assert uv and all(c.startswith("uv url=postgresql://postgres:") for c in uv)
    assert all("@127.0.0.1:55432/postgres" in c for c in uv)


def test_the_container_is_removed_when_the_tests_fail(repo, tmp_path):
    calls, out = run_check(repo, tmp_path, STUB_UV_FAIL="1")
    assert "FAILED" in out
    name = next(c for c in calls if c.startswith("docker run")).split("--name ")[1].split()[0]
    assert f"docker rm -f -v {name}" in calls


def test_the_password_is_not_on_a_command_line(repo, tmp_path):
    calls, _ = run_check(repo, tmp_path)
    run = next(c for c in calls if c.startswith("docker run"))
    assert "-e POSTGRES_PASSWORD " in run + " "  # by name only: no =value
    assert "POSTGRES_PASSWORD=" not in run


def test_with_a_url_no_container_is_started(repo, tmp_path):
    url = "postgresql://u:p@db.example.invalid:5432/postgres"
    calls, _ = run_check(repo, tmp_path, FLEET_TEST_DATABASE_URL=url)
    assert not [c for c in calls if c.startswith("docker")]
    assert all(c == f"uv url={url}" for c in calls if c.startswith("uv url="))
