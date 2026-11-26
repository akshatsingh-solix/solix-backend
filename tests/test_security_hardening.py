"""Security hardening: the client address can't be chosen by the client once configured, sign-in guessing is capped
per account, chat transcripts need an unguessable id, enrollments can't skip approval through pause/resume, the seed
admin's changed password survives restarts, and the legacy submissions list honours the inbox access level.
In-process against an in-memory Mongo; invented people only."""
import asyncio
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

_loop = asyncio.new_event_loop()


def run(coro):
    return _loop.run_until_complete(coro)


@pytest.fixture(scope="module")
def app(isolated_app):
    return isolated_app


def _request(xff=None, peer="10.0.0.9", **headers):
    raw = [(k.replace("_", "-").encode(), v.encode()) for k, v in headers.items()]
    if xff is not None:
        raw.append((b"x-forwarded-for", xff.encode()))
    return Request({"type": "http", "headers": raw, "client": (peer, 1234), "method": "GET", "path": "/"})


# --- client address ---------------------------------------------------------------------------------------------

def test_client_ip_default_keeps_first_entry(monkeypatch):
    import client_ip
    monkeypatch.delenv("TRUSTED_PROXY_HOPS", raising=False)
    monkeypatch.delenv("CLIENT_IP_HEADER", raising=False)
    assert client_ip.client_ip(_request("1.1.1.1, 2.2.2.2")) == "1.1.1.1"
    assert client_ip.client_ip(_request()) == "10.0.0.9"


def test_client_ip_hops_ignore_what_the_client_prepended(monkeypatch):
    import client_ip
    monkeypatch.delenv("CLIENT_IP_HEADER", raising=False)
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "1")
    # The client sent "6.6.6.6"; the proxy appended the address it really saw.
    assert client_ip.client_ip(_request("6.6.6.6, 203.0.113.7")) == "203.0.113.7"
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "2")
    assert client_ip.client_ip(_request("6.6.6.6, 203.0.113.7, 172.16.0.1")) == "203.0.113.7"
    assert client_ip.client_ip(_request("203.0.113.7")) == "203.0.113.7"  # fewer entries than hops
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "nonsense")
    assert client_ip.client_ip(_request("6.6.6.6, 203.0.113.7")) == "6.6.6.6"


def test_client_ip_header_wins_when_configured(monkeypatch):
    import client_ip
    monkeypatch.setenv("CLIENT_IP_HEADER", "CF-Connecting-IP")
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "1")
    assert client_ip.client_ip(_request("6.6.6.6, 1.2.3.4", cf_connecting_ip="198.51.100.4")) == "198.51.100.4"
    assert client_ip.client_ip(_request("6.6.6.6, 1.2.3.4")) == "1.2.3.4"  # header missing: fall back to hops
    d = client_ip.describe(_request("6.6.6.6, 1.2.3.4", cf_connecting_ip="198.51.100.4"))
    assert d["by_hops"] == {"1": "1.2.3.4", "2": "6.6.6.6"} and d["candidate_headers"]["cf-connecting-ip"] == "198.51.100.4"


def test_client_ip_check_is_admin_only(app):
    admin, _ = app
    r = admin.get("/api/admin/ops/client-ip", headers={"X-Forwarded-For": "6.6.6.6"})
    assert r.status_code == 200 and r.json()["x_forwarded_for"] == ["6.6.6.6"]
    assert TestClient(admin.app).get("/api/admin/ops/client-ip").status_code == 401


# --- sign-in guessing --------------------------------------------------------------------------------------------

def test_rotating_addresses_cant_guess_past_the_account_cap(app):
    admin, _ = app
    r = admin.post("/api/admin/users", json={"email": "guessed@example.com", "name": "Guess Ed", "role": "viewer"})
    assert r.status_code == 201, r.text
    password = r.json()["temporary_password"]
    anon = TestClient(admin.app)
    for i in range(20):  # each attempt claims a new address, so the per-address lockout never trips
        bad = anon.post("/api/auth/login", json={"email": "guessed@example.com", "password": "wrong"}, headers={"X-Forwarded-For": f"198.18.0.{i}"})
        assert bad.status_code == 401
    locked = anon.post("/api/auth/login", json={"email": "guessed@example.com", "password": password}, headers={"X-Forwarded-For": "198.18.1.1"})
    assert locked.status_code == 429


# --- chat transcripts ---------------------------------------------------------------------------------------------

def test_transcripts_need_an_unguessable_id(app):
    _, db = app
    sid = str(uuid.uuid4())
    run(db.chat_messages.insert_one({"id": "m1", "session_id": sid, "role": "user", "content": "hi", "created_at": "2099-01-01T00:00:00+00:00"}))
    run(db.chat_messages.insert_one({"id": "m2", "session_id": "abc123", "role": "user", "content": "hi", "created_at": "2099-01-01T00:00:00+00:00"}))
    anon = TestClient(app[0].app)
    h = {"X-Forwarded-For": "192.0.2.50"}
    assert [m["id"] for m in anon.get(f"/api/chat/{sid}", headers=h).json()] == ["m1"]
    assert anon.get("/api/chat/abc123", headers=h).status_code == 404
    assert anon.delete("/api/chat/abc123", headers=h).status_code == 404
    assert run(db.chat_messages.count_documents({"session_id": "abc123"})) == 1
    assert anon.get("/api/chat/status", headers=h).status_code == 200  # the fixed route still wins


def test_transcript_lookups_are_rate_limited(app):
    anon = TestClient(app[0].app)
    h = {"X-Forwarded-For": "192.0.2.51"}
    codes = [anon.get(f"/api/chat/{uuid.uuid4()}", headers=h).status_code for _ in range(31)]
    assert codes[:30] == [200] * 30 and codes[30] == 429


# --- approval can't be skipped -----------------------------------------------------------------------------------

def _enrollment(db, eid, status, **extra):
    run(db.enrollments.insert_one({"id": eid, "contact_id": f"{eid}-c", "status": status, "steps": [{"day": 0, "subject": "s", "body": "b"}],
                                   "step_index": 0, "history": [], **extra}))


def _status(db, eid):
    return run(db.enrollments.find_one({"id": eid}))["status"]


@pytest.mark.parametrize("status", ["pending_approval", "stopped", "completed", "scheduled"])
def test_only_paused_enrollments_resume(app, status):
    admin, db = app
    eid = f"sec-resume-{status}"
    _enrollment(db, eid, status)
    assert admin.patch(f"/api/admin/outbound/enrollments/{eid}", json={"action": "resume"}).status_code == 409
    assert _status(db, eid) == status


def test_stopped_enrollments_cant_be_paused_back_to_life(app):
    admin, db = app
    _enrollment(db, "sec-stopped", "stopped")
    assert admin.patch("/api/admin/outbound/enrollments/sec-stopped", json={"action": "pause"}).status_code == 409


def test_a_draft_paused_before_approval_returns_to_the_queue(app):
    admin, db = app
    _enrollment(db, "sec-draft", "pending_approval")
    assert admin.patch("/api/admin/outbound/enrollments/sec-draft", json={"action": "pause"}).status_code == 200
    assert _status(db, "sec-draft") == "paused"
    assert admin.patch("/api/admin/outbound/enrollments/sec-draft", json={"action": "resume"}).status_code == 200
    assert _status(db, "sec-draft") == "pending_approval"
    # An approved one resumes into sending as before.
    _enrollment(db, "sec-live", "scheduled")
    admin.patch("/api/admin/outbound/enrollments/sec-live", json={"action": "pause"})
    admin.patch("/api/admin/outbound/enrollments/sec-live", json={"action": "resume"})
    assert _status(db, "sec-live") == "scheduled"


def test_an_ended_pause_never_sends_an_unapproved_draft(app):
    _, db = app
    import outbound
    past = "2000-01-01T00:00:00+00:00"
    _enrollment(db, "sec-obj-draft", "paused", paused_from="pending_approval", resume_at=past)
    _enrollment(db, "sec-obj-live", "paused", paused_from="active", resume_at=past, first_sent_at=past)
    run(outbound.tick())
    assert _status(db, "sec-obj-draft") == "pending_approval"
    assert _status(db, "sec-obj-live") == "active"


# --- seed admin ------------------------------------------------------------------------------------------------

def test_a_changed_admin_password_survives_a_restart(app, monkeypatch):
    admin, _ = app
    import auth
    email, env_pw, new_pw = os.environ["ADMIN_EMAIL"], os.environ["ADMIN_PASSWORD"], "Changed-Pass-2026"
    anon = TestClient(admin.app)
    login = lambda pw: anon.post("/api/auth/login", json={"email": email, "password": pw}, headers={"X-Forwarded-For": "192.0.2.60"}).status_code
    try:
        assert admin.post("/api/auth/password", json={"current_password": env_pw, "new_password": new_pw}).status_code == 204
        monkeypatch.delenv("ADMIN_PASSWORD_RESET", raising=False)
        run(auth.seed_admin())
        assert login(new_pw) == 200 and login(env_pw) == 401
    finally:
        monkeypatch.setenv("ADMIN_PASSWORD_RESET", "1")  # the recovery path puts the env password back
        run(auth.seed_admin())
    assert login(env_pw) == 200


# --- legacy submissions list ---------------------------------------------------------------------------------------

def test_legacy_submissions_list_follows_inbox_access(app):
    admin, _ = app
    import cache
    r = admin.post("/api/admin/users", json={"email": "no.inbox@example.com", "name": "No Inbox", "role": "sales"})
    body = r.json()
    rep = TestClient(admin.app)
    tok = rep.post("/api/auth/login", json={"email": "no.inbox@example.com", "password": body["temporary_password"]}).json()["access_token"]
    rep.headers.update({"Authorization": f"Bearer {tok}"})
    assert rep.get("/api/submissions").status_code == 200
    admin.put(f"/api/admin/access/users/{body['user']['id']}", json={"access": {"inbox": "none"}})
    cache.bump("access")
    assert rep.get("/api/submissions").status_code == 403
    assert rep.get("/api/admin/submissions").status_code == 403  # same answer as the admin route
    assert admin.get("/api/submissions").status_code == 200
