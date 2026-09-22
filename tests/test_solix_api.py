"""Backend API tests for Solix Technologies rebuild."""
import os
import time
import uuid
import json
import requests
import pytest

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL') or open('/app/frontend/.env').read().split('REACT_APP_BACKEND_URL=')[1].split('\n')[0].strip()
BASE_URL = BASE_URL.rstrip('/')
API = f"{BASE_URL}/api"


# ---------- Health ----------
def test_root_health():
    r = requests.get(f"{API}/", timeout=15)
    assert r.status_code == 200
    j = r.json()
    assert j.get("service") == "solix-api"
    assert j.get("status") == "ok"


# ---------- Submissions ----------
def test_submission_create_and_list_demo():
    email = f"TEST_demo_{uuid.uuid4().hex[:8]}@example.com"
    payload = {"type": "demo", "email": email, "name": "TEST Demo",
               "company": "Acme", "interest": "enterprise-archiving",
               "message": "Please contact me"}
    r = requests.post(f"{API}/submissions", json=payload, timeout=15)
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["email"] == email
    assert data["type"] == "demo"
    assert "id" in data and "created_at" in data

    # list demo -- newest first
    r2 = requests.get(f"{API}/submissions", params={"type": "demo"}, timeout=15)
    assert r2.status_code == 200
    items = r2.json()
    assert isinstance(items, list) and len(items) >= 1
    assert items[0]["email"] == email  # newest first


def test_submission_invalid_email():
    r = requests.post(f"{API}/submissions",
                      json={"type": "demo", "email": "not-an-email"}, timeout=15)
    assert r.status_code == 422


def test_submission_invalid_type():
    r = requests.post(f"{API}/submissions",
                      json={"type": "bogus", "email": "a@b.com"}, timeout=15)
    assert r.status_code == 422


@pytest.mark.parametrize("t", ["contact", "newsletter", "career", "partner", "download"])
def test_submission_all_types(t):
    email = f"TEST_{t}_{uuid.uuid4().hex[:6]}@example.com"
    r = requests.post(f"{API}/submissions",
                      json={"type": t, "email": email, "name": "TEST"},
                      timeout=15)
    assert r.status_code == 201, r.text
    assert r.json()["type"] == t


# ---------- Chat ----------
def _consume_sse(resp, timeout=60):
    """Return list of parsed json events from an SSE response."""
    events = []
    start = time.time()
    for raw in resp.iter_lines(decode_unicode=True):
        if raw is None:
            continue
        if raw.startswith("data:"):
            try:
                events.append(json.loads(raw[5:].strip()))
            except Exception:
                pass
            if events and events[-1].get("done"):
                break
        if time.time() - start > timeout:
            break
    return events


def test_chat_short_session_id_422():
    r = requests.post(f"{API}/chat/stream",
                      json={"session_id": "abc", "message": "hello"}, timeout=15)
    assert r.status_code == 422


def test_chat_stream_and_history_and_multiturn_and_delete():
    session_id = f"test-{uuid.uuid4().hex[:10]}"
    # message 1
    with requests.post(f"{API}/chat/stream",
                       json={"session_id": session_id,
                             "message": "What is Solix Enterprise Archiving in one sentence?"},
                       stream=True, timeout=60) as r:
        assert r.status_code == 200
        assert "text/event-stream" in r.headers.get("content-type", "")
        events = _consume_sse(r, timeout=45)

    deltas = [e for e in events if "delta" in e]
    dones = [e for e in events if e.get("done")]
    errors = [e for e in events if "error" in e]
    assert not errors, f"stream errors: {errors}"
    assert len(deltas) >= 1, f"expected deltas, got {events}"
    assert len(dones) == 1

    # history
    r2 = requests.get(f"{API}/chat/{session_id}", timeout=15)
    assert r2.status_code == 200
    msgs = r2.json()
    assert len(msgs) >= 2
    assert msgs[0]["role"] == "user"
    assert msgs[1]["role"] == "assistant"
    assert len(msgs[1]["content"]) > 0

    # multi-turn (context)
    with requests.post(f"{API}/chat/stream",
                       json={"session_id": session_id,
                             "message": "And what problem does it solve?"},
                       stream=True, timeout=60) as r3:
        assert r3.status_code == 200
        events2 = _consume_sse(r3, timeout=45)
    assert any(e.get("done") for e in events2)

    r4 = requests.get(f"{API}/chat/{session_id}", timeout=15)
    assert r4.status_code == 200
    msgs2 = r4.json()
    assert len(msgs2) >= 4  # 2 user + 2 assistant

    # delete
    rd = requests.delete(f"{API}/chat/{session_id}", timeout=15)
    assert rd.status_code == 204
    r5 = requests.get(f"{API}/chat/{session_id}", timeout=15)
    assert r5.status_code == 200
    assert r5.json() == []
