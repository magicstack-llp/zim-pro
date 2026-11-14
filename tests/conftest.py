import asyncio
import os

import pytest
from fastapi.testclient import TestClient

from zim_pro.config import Config
from zim_pro.db import Database
from zim_pro.main import create_app


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Strip ambient KIWIX_* environment variables so tests do not depend on local config."""
    for key in list(os.environ):
        if key.startswith("KIWIX_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _close_leaked_databases(monkeypatch):
    """Track every Database opened during a test and close any left open on teardown."""
    opened: list[Database] = []
    orig_init = Database.__init__

    def tracked_init(self, *args, **kwargs):
        orig_init(self, *args, **kwargs)
        opened.append(self)

    monkeypatch.setattr(Database, "__init__", tracked_init)
    yield
    for db in opened:
        if getattr(db, "_conn", None) is not None:
            db.close()
    opened.clear()


@pytest.fixture
def no_waiting(monkeypatch):
    """Skip asyncio.sleep and crawler crawl delays in tests."""
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda _seconds: real_sleep(0))
    monkeypatch.setattr("zim_pro.builder.crawler.HOST_DELAY", 0)
    monkeypatch.setattr("zim_pro.builder.crawler.SLOW_DELAY_MIN", 0)
    monkeypatch.setattr("zim_pro.builder.crawler.RATE_WAIT", 0)


@pytest.fixture
def quick(no_waiting):
    """Alias for no_waiting used in network tests."""


@pytest.fixture
def make_client(tmp_path):
    """Return a factory that yields an entered TestClient with lifespan active."""
    clients: list[TestClient] = []

    def _factory(**config_overrides) -> TestClient:
        data_dir = config_overrides.pop("data_dir", tmp_path / "data")
        state_dir = config_overrides.pop("state_dir", tmp_path / "state")
        data_dir.mkdir(parents=True, exist_ok=True)
        state_dir.mkdir(parents=True, exist_ok=True)
        config_overrides.setdefault("catalog_url", "http://127.0.0.1:0/catalog.xml")
        cfg = Config(data_dir=data_dir, state_dir=state_dir, **config_overrides)
        client = TestClient(create_app(cfg))
        client.__enter__()
        clients.append(client)
        return client

    yield _factory
    for client in reversed(clients):
        client.__exit__(None, None, None)
