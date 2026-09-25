"""Chat concierge: OpenRouter setup and falling through busy free models."""
import os

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "solix_test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("ADMIN_EMAIL", "admin@example.com")
os.environ.setdefault("ADMIN_PASSWORD", "AdminPass!1")

import httpx  # noqa: E402
import openai  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from mongomock_motor import AsyncMongoMockClient  # noqa: E402

import database  # noqa: E402
import accounts, admin, auth, cache, chat, content, delivery, emailer, events, intent, leads_admin, migrate, server, site_settings  # noqa: E402,E401

MODULES = (accounts, admin, auth, chat, content, delivery, emailer, events, intent, leads_admin, migrate, server, site_settings)


def _chunk(content=None, finish=None):
    delta = type("D", (), {"content": content, "tool_calls": None})()
    choice = type("C", (), {"delta": delta, "finish_reason": finish})()
    return type("E", (), {"choices": [choice]})()


class FakeStream:
    def __init__(self, chunks):
        self.chunks = chunks

    def __aiter__(self):
        async def gen():
            for c in self.chunks:
                yield c
        return gen()


class FakeClient:
    def __init__(self, behaviour):
        self.behaviour, self.calls = behaviour, []
        self.chat = type("Chat", (), {"completions": self})()

    async def create(self, model, **kw):
        self.calls.append(model)
        b = self.behaviour[model]
        if isinstance(b, Exception):
            raise b
        return FakeStream(b)


def _rate_limited():
    req = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    return openai.RateLimitError("busy", response=httpx.Response(429, request=req), body=None)


@pytest.fixture()
def client(monkeypatch):
    mock = AsyncMongoMockClient()["solix_test"]
    monkeypatch.setattr(database, "db", mock)
    for m in MODULES:
        monkeypatch.setattr(m, "db", mock, raising=False)
    cache.clear()
    with TestClient(server.app) as c:
        yield c


def _stream(client):
    r = client.post("/api/chat/stream", json={"session_id": "sess-123", "message": "What is application retirement?"})
    return r.text


def test_falls_through_to_next_model(client, monkeypatch):
    fake = FakeClient({"a:free": _rate_limited(), "b:free": [], "c:free": [_chunk("Retiring "), _chunk("apps.", "stop")]})
    monkeypatch.setattr(chat, "_client", fake)
    monkeypatch.setattr(chat, "CHAT_MODELS", ["a:free", "b:free", "c:free"])
    body = _stream(client)
    assert fake.calls == ["a:free", "b:free", "c:free"]
    assert '"delta": "Retiring "' in body and '"done": true' in body


def test_all_models_down_reports_unavailable(client, monkeypatch):
    fake = FakeClient({"a:free": _rate_limited(), "b:free": _rate_limited()})
    monkeypatch.setattr(chat, "_client", fake)
    monkeypatch.setattr(chat, "CHAT_MODELS", ["a:free", "b:free"])
    assert "temporarily unavailable" in _stream(client)


def test_openrouter_setup(monkeypatch):
    monkeypatch.setattr(chat, "OPENROUTER_API_KEY", "sk-or-v1-test")
    monkeypatch.delenv("CHAT_MODELS", raising=False)
    c, models = chat._chat_setup()
    assert str(c.base_url).startswith("https://openrouter.ai/api/v1")
    assert models[0] == "google/gemma-4-31b-it:free" and models[-1] == "openrouter/free"
    monkeypatch.setenv("CHAT_MODELS", "nvidia/nemotron-3.5-lightning:free")
    assert chat._chat_setup()[1] == ["nvidia/nemotron-3.5-lightning:free"]
