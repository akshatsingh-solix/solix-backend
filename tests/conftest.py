"""Shared test setup.

* The Radar's keyless web sources (public registries, RSS, DNS-over-HTTPS) stay off, so no suite reaches
  the internet unless a test turns a source on and mocks it.
* Provider cooldowns are per process: a provider one test made fail would otherwise still be resting in
  the next test, so every test starts with fresh provider state."""
import os

import pytest

os.environ.setdefault("RADAR_KEYLESS_SOURCES", "off")


@pytest.fixture(autouse=True)
def _fresh_providers():
    import providers
    providers.reset()
    yield
    providers.reset()


@pytest.fixture(scope="module")
def isolated_app():
    """The whole API on its own fresh in-memory Mongo, restored afterwards, so one module's data never changes
    another module's counts. Yields (signed-in admin TestClient, database)."""
    import sys

    os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
    os.environ.setdefault("DB_NAME", "solix_test")
    os.environ.setdefault("JWT_SECRET", "test-secret")
    os.environ.setdefault("ADMIN_EMAIL", "admin@example.com")
    os.environ.setdefault("ADMIN_PASSWORD", "AdminPass!1")
    os.environ["OPS_LOOP"] = "0"
    from fastapi.testclient import TestClient
    from mongomock_motor import AsyncMongoMockClient

    import cache
    import database
    import server
    import tool_registry

    previous = database.db
    fresh = AsyncMongoMockClient()["solix_isolated"]
    patched = {}
    database.db = fresh
    for name, mod in list(sys.modules.items()):
        if getattr(mod, "db", None) is previous and name not in ("database",):
            patched[name] = mod
            mod.db = fresh
    cache.clear()
    tool_registry.clear_policy_cache()
    try:
        with TestClient(server.app) as c:
            token = c.post("/api/auth/login", json={"email": os.environ["ADMIN_EMAIL"], "password": os.environ["ADMIN_PASSWORD"]}).json()["access_token"]
            c.headers.update({"Authorization": f"Bearer {token}"})
            yield c, fresh
    finally:
        database.db = previous
        for mod in patched.values():
            mod.db = previous
        cache.clear()
        tool_registry.clear_policy_cache()
