"""Every broker test runs against a real PostgreSQL, in its own fresh database.

FLEET_TEST_DATABASE_URL is an admin URL (a role that may CREATE DATABASE). scripts/check.sh
starts a throwaway postgres:18 and sets it; CI has a service container. Without it the tests
fail loudly: a skipped store test would be a green check that tested nothing.
"""

import os
import uuid
from urllib.parse import urlsplit, urlunsplit

import fleet_broker.app
import psycopg
import pytest
from fleet_broker.store import JobStore, make_pool


def _with_dbname(url: str, name: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path="/" + name))


@pytest.fixture(scope="session")
def admin_url() -> str:
    url = os.environ.get("FLEET_TEST_DATABASE_URL")
    if not url:
        pytest.fail(
            "FLEET_TEST_DATABASE_URL is not set: run scripts/check.sh (it starts a throwaway "
            "postgres:18) or point it at a Postgres you may CREATE DATABASE in",
            pytrace=False,
        )
    return url


_POOLS: list = []  # every pool a test opened, closed before its database is dropped


def tracked_pool(*args, **kwargs):
    _POOLS.append(pool := make_pool(*args, **kwargs))
    return pool


def make_store(db_url: str, max_attempts: int = 3, **kwargs) -> JobStore:
    """A JobStore with its own pool on the test's database."""
    return JobStore(tracked_pool(db_url), max_attempts=max_attempts, **kwargs)


@pytest.fixture
def db_url(admin_url, monkeypatch) -> str:
    """The URL of a new, empty database, dropped after the test. Pools opened during the test
    (by the app or by make_store) are closed first: tests don't run the lifespan unless they
    use `with TestClient`."""
    name = f"fleet_test_{uuid.uuid4().hex[:12]}"
    monkeypatch.setattr(fleet_broker.app, "make_pool", tracked_pool)
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
        try:
            yield _with_dbname(admin_url, name)
        finally:
            while _POOLS:
                _POOLS.pop().close()
            # FORCE: ends sessions a test left behind (a test may have killed or held some)
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def open_store(db_url):
    """A factory for JobStores on the test's database, each with its own pool."""
    return lambda max_attempts=3, **kwargs: make_store(db_url, max_attempts, **kwargs)
