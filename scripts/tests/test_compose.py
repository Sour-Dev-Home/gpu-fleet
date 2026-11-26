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


def test_worker_restarts_a_bounded_number_of_times():
    # A runner that crashes on every job (a model server answering 404 behind a proxy) would
    # lease, crash and restart forever under unless-stopped, burning an attempt per cycle until
    # every job is dead. Three restarts burn at most one attempt on each of three jobs, which
    # can't dead-letter any of them, then the worker stays down (ADR 0014 amendment).
    assert "restart: on-failure:3" in service("worker")


def test_broker_clearance_is_explicit_config():
    # ADR 0015: required by the broker, no default; the local stack's workers are all the
    # maintainer's, so they may see every data class.
    assert "FLEET_WORKER_CLEARANCE: footage,derived,open" in service("broker")


def test_broker_restarts():
    assert "restart: unless-stopped" in service("broker")


def test_admin_token_goes_to_the_broker_only():
    assert not any("FLEET_ADMIN_TOKEN" in line for line in service("worker"))
    assert any(line.startswith("FLEET_ADMIN_TOKEN:") for line in service("broker"))


def test_broker_is_published_on_loopback_only():
    ports = [line for line in service("broker") if line.startswith('- "') and ":8000" in line]
    assert ports == ['- "127.0.0.1:8000:8000"']


OBS = COMPOSE.parent / "compose.obs.yaml"


def obs_service(name: str) -> list[str]:
    lines = OBS.read_text(encoding="utf-8").splitlines()
    start = lines.index(f"  {name}:")
    block = []
    for line in lines[start + 1 :]:
        if line.strip() and not line.startswith("   "):
            break
        block.append(line.split("#")[0].strip())
    return block


def test_grafana_requires_a_login():
    # otel-lgtm enables anonymous Admin by default: any local process or browser page would be admin.
    assert 'GF_AUTH_ANONYMOUS_ENABLED: "false"' in obs_service("lgtm")


def test_observability_stack_publishes_only_grafana_on_loopback():
    # OTLP (4317/4318), Prometheus, Tempo and Loki stay on the Compose network.
    published = [line for line in obs_service("lgtm") if line.startswith('- "') and ":" in line]
    assert published == ['- "127.0.0.1:3000:3000"']
