"""Unit tests for customer accounts (accounts.py), run in-process against an
in-memory Mongo (mongomock-motor) - no deployed API or database needed."""
import os

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "solix_test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("ADMIN_EMAIL", "admin@example.com")
os.environ.setdefault("ADMIN_PASSWORD", "AdminPass!1")

import pytest
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient

import database

_mock_db = AsyncMongoMockClient()["solix_test"]
database.db = _mock_db

import accounts, auth, emailer, server  # noqa: E402

for mod in (accounts, auth, emailer, server):
    mod.db = _mock_db


@pytest.fixture()
def client():
    with TestClient(server.app) as c:
        yield c


def _signup(client, **over):
    body = {
        "first_name": "Ada", "last_name": "Lovelace", "company": "Analytical Engines",
        "email": "Ada@Example.com", "phone": "+1 555 000 0000", "password": "Engine#42",
        "job_title": "CTO", "company_size": "1,000-4,999", "country": "United Kingdom",
        "use_case": "Contract intelligence", "language": "fr",
    }
    body.update(over)
    return client.post("/api/accounts/signup", json=body)


def test_signup_creates_trial_account_and_lead(client):
    r = _signup(client, email="trial1@example.com")
    assert r.status_code == 201, r.text
    j = r.json()
    assert j["access_token"]
    acct = j["account"]
    assert acct["email"] == "trial1@example.com"
    assert acct["plan"] == "trial" and acct["language"] == "fr"
    assert "password" not in acct and "password_hash" not in acct

    import asyncio
    lead = asyncio.get_event_loop().run_until_complete(_mock_db.submissions.find_one({"email": "trial1@example.com", "type": "trial"}))
    assert lead and lead["company"] == "Analytical Engines" and "Company size: 1,000-4,999" in lead["message"]


def test_signup_rejects_duplicate_email_case_insensitively(client):
    assert _signup(client, email="dupe@example.com").status_code == 201
    r = _signup(client, email="DUPE@example.com")
    assert r.status_code == 409


@pytest.mark.parametrize("pw", ["short", "nodigits!!", "NoSymbol123", "a" * 28 + "1!x"])
def test_signup_enforces_password_rule(client, pw):
    assert _signup(client, email=f"pw{len(pw)}{pw[:3]}@example.com", password=pw).status_code == 422


def test_signin_me_and_wrong_password(client):
    _signup(client, email="login@example.com")
    bad = client.post("/api/accounts/signin", json={"email": "login@example.com", "password": "Wrong#123"})
    assert bad.status_code == 401
    ok = client.post("/api/accounts/signin", json={"email": "LOGIN@example.com", "password": "Engine#42"})
    assert ok.status_code == 200, ok.text
    token = ok.json()["access_token"]
    me = client.get("/api/accounts/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200 and me.json()["first_name"] == "Ada"


def test_account_token_cannot_reach_admin_routes(client):
    token = _signup(client, email="noadmin@example.com").json()["access_token"]
    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


def test_signin_locks_out_after_repeated_failures(client):
    _signup(client, email="lock@example.com")
    for _ in range(5):
        client.post("/api/accounts/signin", json={"email": "lock@example.com", "password": "Wrong#123"})
    r = client.post("/api/accounts/signin", json={"email": "lock@example.com", "password": "Engine#42"})
    assert r.status_code == 429


def test_forgot_password_answers_identically(client):
    _signup(client, email="forgot@example.com")
    a = client.post("/api/accounts/forgot-password", json={"email": "forgot@example.com"})
    b = client.post("/api/accounts/forgot-password", json={"email": "nobody@example.com"})
    assert a.status_code == b.status_code == 202 and a.json() == b.json()


def test_me_requires_token(client):
    assert client.get("/api/accounts/me").status_code == 401
