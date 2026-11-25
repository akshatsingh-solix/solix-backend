"""Through restarts and server errors (25 Nov 2026, "Network Error" on sign-in and actions): errors reach the browser
as readable JSON with CORS headers, and a retried write with the same Idempotency-Key never runs twice."""
import os

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "solix_test")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient
from starlette.middleware.cors import CORSMiddleware

import database
import resilience

ORIGIN = "https://akshatsingh-solix.github.io"


@pytest.fixture()
def app(monkeypatch):
    monkeypatch.setattr(database, "db", AsyncMongoMockClient()["resilience_test"])
    a = FastAPI()
    calls = {"n": 0, "fail": 0}

    @a.post("/api/thing")
    async def thing():
        calls["n"] += 1
        return {"made": calls["n"]}

    @a.post("/api/flaky")
    async def flaky():
        calls["fail"] += 1
        if calls["fail"] == 1:
            raise RuntimeError("database hiccup")
        return {"ok": True, "attempt": calls["fail"]}

    @a.get("/api/boom")
    async def boom():
        raise ValueError("bug")

    a.add_middleware(resilience.JSONErrors)
    a.add_middleware(resilience.Idempotency)
    a.add_middleware(CORSMiddleware, allow_origins=[ORIGIN], allow_methods=["*"], allow_headers=["*"], allow_credentials=True)
    a.calls = calls
    return a


def test_server_errors_are_readable_by_the_browser(app):
    r = TestClient(app, raise_server_exceptions=False).get("/api/boom", headers={"Origin": ORIGIN})
    assert r.status_code == 500 and "ref" in r.json() and "Something went wrong" in r.json()["detail"]
    assert r.headers.get("access-control-allow-origin") == ORIGIN  # without this the admin only saw "Network Error"


def test_a_retried_write_runs_once(app):
    c = TestClient(app)
    h = {"Idempotency-Key": "k-1", "Authorization": "Bearer a"}
    first, again = c.post("/api/thing", headers=h), c.post("/api/thing", headers=h)
    assert first.json() == again.json() == {"made": 1} and app.calls["n"] == 1
    assert again.headers.get("idempotent-replay") == "true"
    assert c.post("/api/thing", headers={**h, "Idempotency-Key": "k-2"}).json() == {"made": 2}  # a new key is a new action
    assert c.post("/api/thing", headers={"Idempotency-Key": "k-1", "Authorization": "Bearer someone-else"}).json() == {"made": 3}  # keys are per caller


def test_a_failed_attempt_is_not_remembered(app):
    c = TestClient(app, raise_server_exceptions=False)
    h = {"Idempotency-Key": "f-1"}
    assert c.post("/api/flaky", headers=h).status_code == 500
    assert c.post("/api/flaky", headers=h).json() == {"ok": True, "attempt": 2}  # the retry really runs


def test_requests_without_a_key_are_untouched(app):
    c = TestClient(app)
    c.post("/api/thing"), c.post("/api/thing")
    assert app.calls["n"] == 2
