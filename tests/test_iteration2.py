"""Iteration 2 backend tests: admin auth, admin dashboard, email alerts, chat booking."""
import os
import time
import uuid
import json
import requests
import pytest

BASE_URL = (os.environ.get('REACT_APP_BACKEND_URL') or open('/app/frontend/.env').read().split('REACT_APP_BACKEND_URL=')[1].split('\n')[0].strip()).rstrip('/')
API = f"{BASE_URL}/api"

ADMIN_EMAIL = "admin@solix.com"
ADMIN_PASSWORD = "SolixAdmin!2026"


# ---------- fixtures ----------
@pytest.fixture(scope="module")
def token():
    r = requests.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, timeout=15)
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


@pytest.fixture(scope="module")
def auth_headers(token):
    return {"Authorization": f"Bearer {token}"}


# ---------- Auth ----------
def test_login_success():
    r = requests.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, timeout=15)
    assert r.status_code == 200
    j = r.json()
    assert "access_token" in j and isinstance(j["access_token"], str)
    assert j["user"]["email"] == ADMIN_EMAIL
    assert j["user"]["role"] == "admin"
    assert "id" in j["user"] and "name" in j["user"]


def test_login_wrong_password():
    r = requests.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": "wrong-pw-xxx"}, timeout=15)
    assert r.status_code == 401
    assert "Invalid email or password" in r.text


def test_me_without_auth():
    r = requests.get(f"{API}/auth/me", timeout=15)
    assert r.status_code == 401


def test_me_with_auth(auth_headers):
    r = requests.get(f"{API}/auth/me", headers=auth_headers, timeout=15)
    assert r.status_code == 200
    assert r.json()["email"] == ADMIN_EMAIL


def test_brute_force_lockout():
    # Use throwaway email so admin@solix.com isn't locked out
    email = f"locktest-{uuid.uuid4().hex[:6]}@solix.com"
    got_429 = False
    for i in range(7):
        r = requests.post(f"{API}/auth/login", json={"email": email, "password": "bad"}, timeout=15)
        if r.status_code == 429:
            got_429 = True
            break
    assert got_429, "expected 429 after 5 failed attempts"
    # verify admin still works
    r2 = requests.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, timeout=15)
    assert r2.status_code == 200


# ---------- Admin unauthenticated ----------
def test_admin_stats_requires_auth():
    r = requests.get(f"{API}/admin/stats", timeout=15)
    assert r.status_code == 401


def test_admin_submissions_requires_auth():
    r = requests.get(f"{API}/admin/submissions", timeout=15)
    assert r.status_code == 401


# ---------- Admin stats ----------
def test_admin_stats(auth_headers):
    r = requests.get(f"{API}/admin/stats", headers=auth_headers, timeout=15)
    assert r.status_code == 200
    j = r.json()
    for k in ("total", "last_7_days", "by_type", "chat_leads", "alerts_sent"):
        assert k in j
    assert isinstance(j["by_type"], dict)


# ---------- Admin list/search/pagination ----------
def test_admin_list_and_search(auth_headers):
    # seed a demo submission with a unique searchable term
    term = f"ZZQ{uuid.uuid4().hex[:6].upper()}"
    email = f"TEST_search_{uuid.uuid4().hex[:6]}@example.com"
    rc = requests.post(f"{API}/submissions", json={"type": "demo", "email": email, "name": f"TEST {term}", "company": "Search Co"}, timeout=15)
    assert rc.status_code == 201

    r = requests.get(f"{API}/admin/submissions", params={"type": "demo", "q": term, "page": 1, "page_size": 5}, headers=auth_headers, timeout=15)
    assert r.status_code == 200
    j = r.json()
    assert j["page"] == 1 and j["page_size"] == 5
    assert j["total"] >= 1
    assert any(term.lower() in (it.get("name") or "").lower() for it in j["items"])
    # newest first
    if len(j["items"]) >= 2:
        assert j["items"][0]["created_at"] >= j["items"][1]["created_at"]


# ---------- CSV export ----------
def test_admin_export_csv(auth_headers):
    r = requests.get(f"{API}/admin/submissions/export", params={"type": "demo"}, headers=auth_headers, timeout=30)
    assert r.status_code == 200
    assert "text/csv" in r.headers.get("content-type", "")
    assert "attachment" in r.headers.get("content-disposition", "").lower()
    assert ".csv" in r.headers.get("content-disposition", "")
    header = r.text.splitlines()[0]
    for col in ("id", "created_at", "type", "name", "email"):
        assert col in header


# ---------- Delete ----------
def test_admin_delete_submission(auth_headers):
    rc = requests.post(f"{API}/submissions", json={"type": "contact", "email": f"TEST_del_{uuid.uuid4().hex[:6]}@example.com", "name": "TEST del"}, timeout=15)
    sid = rc.json()["id"]
    r = requests.delete(f"{API}/admin/submissions/{sid}", headers=auth_headers, timeout=15)
    assert r.status_code == 204
    # 404 for unknown
    r2 = requests.delete(f"{API}/admin/submissions/nope-{uuid.uuid4().hex}", headers=auth_headers, timeout=15)
    assert r2.status_code == 404


# ---------- Settings ----------
def test_admin_settings_get_and_update(auth_headers):
    r = requests.get(f"{API}/admin/settings", headers=auth_headers, timeout=15)
    assert r.status_code == 200
    j = r.json()
    assert "alert_email" in j
    assert isinstance(j["alert_types"], list) and "demo" in j["alert_types"]

    new_email = "sales-test@example.com"
    r2 = requests.put(f"{API}/admin/settings", json={"alert_email": new_email}, headers=auth_headers, timeout=15)
    assert r2.status_code == 200
    assert r2.json()["alert_email"] == new_email

    r3 = requests.get(f"{API}/admin/settings", headers=auth_headers, timeout=15)
    assert r3.json()["alert_email"] == new_email

    # invalid email -> 422
    r4 = requests.put(f"{API}/admin/settings", json={"alert_email": "not-an-email"}, headers=auth_headers, timeout=15)
    assert r4.status_code == 422

    # restore to test inbox
    r5 = requests.put(f"{API}/admin/settings", json={"alert_email": "delivered@resend.dev"}, headers=auth_headers, timeout=15)
    assert r5.status_code == 200


# ---------- Notifications ----------
def test_admin_notifications_list(auth_headers):
    r = requests.get(f"{API}/admin/notifications", headers=auth_headers, timeout=15)
    assert r.status_code == 200
    assert isinstance(r.json(), list)


# ---------- Email alerts ----------
def test_email_alert_on_demo(auth_headers):
    email = f"TEST_alert_{uuid.uuid4().hex[:8]}@example.com"
    rc = requests.post(f"{API}/submissions", json={"type": "demo", "email": email, "name": "Alert Test", "company": "Alert Co"}, timeout=15)
    assert rc.status_code == 201
    sid = rc.json()["id"]

    # poll notifications for up to ~10s
    found = None
    for _ in range(10):
        time.sleep(1)
        r = requests.get(f"{API}/admin/notifications", params={"limit": 100}, headers=auth_headers, timeout=15)
        for n in r.json():
            if n.get("submission_id") == sid:
                found = n
                break
        if found:
            break
    assert found is not None, "no notification recorded"
    assert found["status"] in ("sent", "failed", "skipped")
    assert found["type"] == "demo"


def test_newsletter_no_alert(auth_headers):
    email = f"TEST_news_{uuid.uuid4().hex[:8]}@example.com"
    rc = requests.post(f"{API}/submissions", json={"type": "newsletter", "email": email}, timeout=15)
    assert rc.status_code == 201
    sid = rc.json()["id"]
    time.sleep(3)
    r = requests.get(f"{API}/admin/notifications", params={"limit": 100}, headers=auth_headers, timeout=15)
    assert not any(n.get("submission_id") == sid for n in r.json())


# ---------- Chat booking flow ----------
def _consume_sse(resp, timeout=90):
    events = []
    start = time.time()
    for raw in resp.iter_lines(decode_unicode=True):
        if raw and raw.startswith("data:"):
            try:
                events.append(json.loads(raw[5:].strip()))
            except Exception:
                pass
            if events and events[-1].get("done"):
                break
        if time.time() - start > timeout:
            break
    return events


def test_chat_booking_flow(auth_headers):
    session_id = f"book-{uuid.uuid4().hex[:10]}"
    turns = [
        "I want to book a demo of Enterprise Archiving",
        "I am Test Booker, test.booker+" + uuid.uuid4().hex[:6] + "@example.com, from Booker Corp",
        "Yes go ahead, please book it",
    ]
    all_events = []
    for msg in turns:
        with requests.post(f"{API}/chat/stream", json={"session_id": session_id, "message": msg}, stream=True, timeout=120) as r:
            assert r.status_code == 200
            events = _consume_sse(r, timeout=90)
            all_events.append(events)
            assert any(e.get("done") for e in events), f"no done event for turn: {msg}"

    # check demo_booked event in one of the turns
    booked = None
    for evs in all_events:
        for e in evs:
            if e.get("event") == "demo_booked":
                booked = e
                break
    assert booked is not None, "demo_booked event not seen"
    assert booked.get("submission_id")
    assert "email" in booked

    # verify submission persisted with source=chat
    r = requests.get(f"{API}/admin/submissions", params={"type": "demo", "q": "Booker Corp", "page_size": 20}, headers=auth_headers, timeout=15)
    assert r.status_code == 200
    items = r.json()["items"]
    matches = [it for it in items if it.get("company") == "Booker Corp" and it.get("source") == "chat"]
    assert matches, f"no chat demo submission found; items={items[:3]}"

    # history should not include tool messages
    r2 = requests.get(f"{API}/chat/{session_id}", timeout=15)
    assert r2.status_code == 200
    msgs = r2.json()
    roles = [m["role"] for m in msgs]
    assert all(r in ("user", "assistant") for r in roles)
    assert roles.count("user") >= 3


# ---------- Restore recipient at end ----------
def test_zz_restore_alert_email(auth_headers):
    r = requests.put(f"{API}/admin/settings", json={"alert_email": "delivered@resend.dev"}, headers=auth_headers, timeout=15)
    assert r.status_code == 200
    assert r.json()["alert_email"] == "delivered@resend.dev"
