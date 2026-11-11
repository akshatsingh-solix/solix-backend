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
