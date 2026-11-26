"""Footage, results, databases and secrets never enter an image's build context (ADR 0014).

Each Docker build context needs a .dockerignore that excludes at least these patterns.
"""

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BUILD_CONTEXTS = ["worker", "broker", "killswitch"]  # each directory with a Dockerfile
REQUIRED = [
    ".git",
    ".venv",
    "__pycache__",
    "data/",
    "results/",
    "secrets/",
    "*.db",
    "*.db-*",
    ".private-patterns.txt",
    "tests/",
    # .dockerignore patterns are anchored at the context root: these catch nested copies too.
    "**/__pycache__",
    "**/*.db",
    "**/*.db-*",
]


def patterns(context: str) -> set[str]:
    path = REPO / context / ".dockerignore"
    if not path.is_file():
        return set()
    lines = (line.strip() for line in path.read_text(encoding="utf-8").splitlines())
    return {line for line in lines if line and not line.startswith("#")}


def test_every_dockerfile_has_its_context_listed():
    with_dockerfile = sorted(p.parent.name for p in REPO.glob("*/Dockerfile"))
    assert with_dockerfile == sorted(BUILD_CONTEXTS)


@pytest.mark.parametrize("context", BUILD_CONTEXTS)
def test_dockerignore_excludes_the_required_patterns(context):
    missing = [p for p in REQUIRED if p not in patterns(context)]
    assert missing == [], f"{context}/.dockerignore is missing {missing}"


@pytest.mark.parametrize("context", BUILD_CONTEXTS)
def test_dockerignore_never_re_includes_anything(context):
    # A later "!pattern" line would undo an exclusion above it.
    assert [p for p in patterns(context) if p.startswith("!")] == []
