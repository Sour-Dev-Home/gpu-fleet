"""compose.yaml policy (ADR 0014): checked as text, since scripts/tests use the standard library only."""

from pathlib import Path

COMPOSE = Path(__file__).resolve().parents[2] / "compose.yaml"


def service(name: str) -> list[str]:
    """The lines of one service block: from `  name:` to the next two-space-indented key."""
    lines = COMPOSE.read_text(encoding="utf-8").splitlines()
    start = lines.index(f"  {name}:")
    block = []
    for line in lines[start + 1 :]:
        if line.strip() and not line.startswith("   "):
            break
        block.append(line.split("#")[0].strip())
    return block


def test_worker_does_not_restart_until_capability_filtering_lands():
    # With unless-stopped, a worker that stops on a job it can't run (prompt version, model)
    # restarts, leases the same job again and dead-letters it (ADR 0014 amendment, ADR 0015).
    assert 'restart: "no"' in service("worker")


def test_broker_restarts():
    assert "restart: unless-stopped" in service("broker")


def test_admin_token_goes_to_the_broker_only():
    assert not any("FLEET_ADMIN_TOKEN" in line for line in service("worker"))
    assert any(line.startswith("FLEET_ADMIN_TOKEN:") for line in service("broker"))


def test_broker_is_published_on_loopback_only():
    ports = [line for line in service("broker") if line.startswith('- "') and ":8000" in line]
    assert ports == ['- "127.0.0.1:8000:8000"']
