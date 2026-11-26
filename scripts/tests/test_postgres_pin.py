"""The PostgreSQL image is pinned by digest (ADR 0020), and the same one everywhere: Compose runs
what the tests ran against. A bump must change all three places, or this fails."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FILES = ["compose.yaml", "scripts/check.sh", ".github/workflows/ci.yml"]
PIN = re.compile(r"postgres:18@sha256:([0-9a-f]{64})")


def digests(name: str) -> set[str]:
    return set(PIN.findall((ROOT / name).read_text(encoding="utf-8")))


def test_every_postgres_image_is_pinned_by_digest_and_they_agree():
    found = {name: digests(name) for name in FILES}
    assert all(len(d) == 1 for d in found.values()), found
    assert len({next(iter(d)) for d in found.values()}) == 1, found


def test_no_unpinned_postgres_image_anywhere():
    for name in FILES:
        text = (ROOT / name).read_text(encoding="utf-8")
        for line in text.splitlines():
            # an image reference, not a comment that mentions the tag
            if re.search(r"(image:|PG_IMAGE=)\s*postgres:\d", line) and not PIN.search(line):
                raise AssertionError(f"{name}: postgres image without a digest: {line.strip()}")


def test_healthchecks_ask_over_tcp_not_the_init_servers_socket():
    # The image's init phase runs a temporary server that listens on the socket only.
    for name in ("compose.yaml", ".github/workflows/ci.yml"):
        text = (ROOT / name).read_text(encoding="utf-8")
        checks = re.findall(r"pg_isready[^\"\n]*", text)
        assert checks, name
        assert all("-h 127.0.0.1" in c for c in checks), (name, checks)


def test_the_ci_postgres_port_is_bound_to_loopback():
    text = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "127.0.0.1:5432:5432" in text


def test_compose_publishes_no_postgres_port():
    text = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    block = text.split("\n  postgres:\n", 1)[1].split("\n  broker:\n", 1)[0]
    assert "ports:" not in re.sub(r"#.*", "", block)  # comments may mention the word
