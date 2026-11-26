import pytest
from fleet_worker.cli import Config

TOKEN = "test-worker-token-0123456789abcdefghij"  # a test value, not a secret
ENV = {"FLEET_BROKER_URL": "http://127.0.0.1:8000", "FLEET_WORKER_TOKEN": TOKEN}


def test_config_strips_whitespace_from_the_token():
    # A secret store often hands back "token\n"; a header can't carry it.
    assert Config(ENV | {"FLEET_WORKER_TOKEN": TOKEN + "\n"}).token == TOKEN


def test_a_whitespace_only_token_counts_as_missing():
    with pytest.raises(SystemExit, match="FLEET_WORKER_TOKEN"):
        Config(ENV | {"FLEET_WORKER_TOKEN": " \n"})
