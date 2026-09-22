"""Iteration 3 backend tests: Lead status/notes, owner assignment, team management, press PDF."""
import os
import uuid
import requests
import pytest

BASE_URL = (os.environ.get('REACT_APP_BACKEND_URL') or open('/app/frontend/.env').read().split('REACT_APP_BACKEND_URL=')[1].split('\n')[0].strip()).rstrip('/')
API = f"{BASE_URL}/api"

ADMIN_EMAIL = "admin@solix.com"
ADMIN_PASSWORD = "SolixAdmin!2026"


@pytest.fixture(scope="module")
def auth_headers():
    r = requests.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, timeout=15)
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture(scope="module")
def seed_submission(auth_headers):
    """Create one demo submission for use across tests."""
    email = f"TEST_iter3_{uuid.uuid4().hex[:8]}@example.com"
    r = requests.post(f"{API}/submissions", json={"type": "demo", "email": email, "name": "TEST Iter3", "company": "Iter3 Corp"}, timeout=15)
    assert r.status_code == 201
    return r.json()


# ---------- Team management ----------
def test_get_team_current(auth_headers):
    r = requests.get(f"{API}/admin/team", headers=auth_headers, timeout=15)
    assert r.status_code == 200
    j = r.json()
    assert "members" in j and isinstance(j["members"], list)
    emails = [m["email"].lower() for m in j["members"]]
    assert "priya@solix.com" in emails
    assert "marcus@solix.com" in emails


def test_put_team_dedupe_and_restore(auth_headers):
    # Get current
    original = requests.get(f"{API}/admin/team", headers=auth_headers, timeout=15).json()["members"]
    # Add a temp third + dupes
    payload = {"members": original + [
        {"name": "Temp Tester", "email": "TempTester@Solix.com"},
        {"name": "Dupe", "email": "temptester@solix.com"},  # dedupe by lowercase
    ]}
    r = requests.put(f"{API}/admin/team", headers=auth_headers, json=payload, timeout=15)
    assert r.status_code == 200
    emails = [m["email"] for m in r.json()["members"]]
    assert emails.count("temptester@solix.com") == 1
    # Restore
    r2 = requests.put(f"{API}/admin/team", headers=auth_headers, json={"members": original}, timeout=15)
    assert r2.status_code == 200
    final_emails = [m["email"].lower() for m in r2.json()["members"]]
    assert "priya@solix.com" in final_emails and "marcus@solix.com" in final_emails
    assert "temptester@solix.com" not in final_emails


def test_put_team_invalid_email(auth_headers):
    r = requests.put(f"{API}/admin/team", headers=auth_headers, json={"members": [{"name": "Bad", "email": "not-an-email"}]}, timeout=15)
    assert r.status_code == 422


# ---------- Owner assignment (PATCH) ----------
def test_patch_owner_set_and_unset(auth_headers, seed_submission):
    sid = seed_submission["id"]
    # set
    r = requests.patch(f"{API}/admin/submissions/{sid}", headers=auth_headers, json={"owner": "priya@solix.com"}, timeout=15)
    assert r.status_code == 200
    assert r.json()["owner"] == "priya@solix.com"
    # GET verifies persistence via list filter
    r2 = requests.get(f"{API}/admin/submissions", headers=auth_headers, params={"owner": "priya@solix.com", "page_size": 200}, timeout=15)
    assert r2.status_code == 200
    assert any(it["id"] == sid for it in r2.json()["items"])
    # unset with empty string
    r3 = requests.patch(f"{API}/admin/submissions/{sid}", headers=auth_headers, json={"owner": ""}, timeout=15)
    assert r3.status_code == 200
    assert r3.json().get("owner") in (None, "")
    # unassigned filter
    r4 = requests.get(f"{API}/admin/submissions", headers=auth_headers, params={"owner": "unassigned", "page_size": 200}, timeout=15)
    assert any(it["id"] == sid for it in r4.json()["items"])


def test_owner_filter_excludes(auth_headers, seed_submission):
    sid = seed_submission["id"]
    # currently unassigned; owner=priya should not include it
    r = requests.get(f"{API}/admin/submissions", headers=auth_headers, params={"owner": "priya@solix.com", "page_size": 200}, timeout=15)
    assert r.status_code == 200
    assert not any(it["id"] == sid for it in r.json()["items"])


# ---------- Status tracking ----------
def test_patch_status_and_notes(auth_headers, seed_submission):
    sid = seed_submission["id"]
    r = requests.patch(f"{API}/admin/submissions/{sid}", headers=auth_headers, json={"status": "contacted", "notes": "Called them"}, timeout=15)
    assert r.status_code == 200
    j = r.json()
    assert j["status"] == "contacted"
    assert j["notes"] == "Called them"
    assert "updated_at" in j


def test_patch_status_invalid(auth_headers, seed_submission):
    r = requests.patch(f"{API}/admin/submissions/{seed_submission['id']}", headers=auth_headers, json={"status": "bogus"}, timeout=15)
    assert r.status_code == 422


def test_patch_empty_body_400(auth_headers, seed_submission):
    r = requests.patch(f"{API}/admin/submissions/{seed_submission['id']}", headers=auth_headers, json={}, timeout=15)
    assert r.status_code == 400


def test_patch_unknown_id_404(auth_headers):
    r = requests.patch(f"{API}/admin/submissions/nonexistent-{uuid.uuid4().hex}", headers=auth_headers, json={"status": "contacted"}, timeout=15)
    assert r.status_code == 404


def test_status_filter_new_includes_no_status(auth_headers):
    r = requests.get(f"{API}/admin/submissions", headers=auth_headers, params={"status": "new", "page_size": 200}, timeout=15)
    assert r.status_code == 200
    # All items should have status new or missing (normalized to 'new')
    for it in r.json()["items"]:
        assert it.get("status", "new") == "new"


def test_status_filter_qualified(auth_headers, seed_submission):
    sid = seed_submission["id"]
    requests.patch(f"{API}/admin/submissions/{sid}", headers=auth_headers, json={"status": "qualified"}, timeout=15)
    r = requests.get(f"{API}/admin/submissions", headers=auth_headers, params={"status": "qualified", "page_size": 200}, timeout=15)
    assert r.status_code == 200
    ids = [it["id"] for it in r.json()["items"]]
    assert sid in ids


# ---------- Stats includes by_status / by_owner ----------
def test_stats_new_fields(auth_headers):
    r = requests.get(f"{API}/admin/stats", headers=auth_headers, timeout=15)
    assert r.status_code == 200
    j = r.json()
    assert "by_status" in j and isinstance(j["by_status"], dict)
    assert "by_owner" in j and isinstance(j["by_owner"], dict)


# ---------- CSV export has new columns and honors filter ----------
def test_csv_has_status_owner_notes(auth_headers):
    r = requests.get(f"{API}/admin/submissions/export", headers=auth_headers, timeout=30)
    assert r.status_code == 200
    header = r.text.splitlines()[0]
    for col in ("status", "owner", "notes"):
        assert col in header, f"missing column {col} in CSV header: {header}"


def test_csv_owner_filter(auth_headers, seed_submission):
    sid = seed_submission["id"]
    requests.patch(f"{API}/admin/submissions/{sid}", headers=auth_headers, json={"owner": "marcus@solix.com"}, timeout=15)
    r = requests.get(f"{API}/admin/submissions/export", headers=auth_headers, params={"owner": "marcus@solix.com"}, timeout=30)
    assert r.status_code == 200
    lines = r.text.splitlines()
    # every data row (skip header) must have marcus in it
    for line in lines[1:]:
        if line.strip():
            assert "marcus@solix.com" in line
    # cleanup
    requests.patch(f"{API}/admin/submissions/{sid}", headers=auth_headers, json={"owner": ""}, timeout=15)


# ---------- Press PDF ----------
PRESS_PAYLOAD = {
    "id": "enterprise-edition-launch",
    "title": "Test Release Title",
    "date": "2026-06-02",
    "category": "Product",
    "summary": "A short summary.",
    "blocks": [
        {"type": "p", "text": "Paragraph body."},
        {"type": "h2", "text": "Section"},
        {"type": "ul", "items": ["one", "two"]},
        {"type": "quote", "text": "Quoted", "cite": "Someone"},
    ],
    "boilerplate": "About Solix",
    "contact": "press@solix.com",
}


def test_press_pdf_ok():
    r = requests.post(f"{API}/press/pdf", json=PRESS_PAYLOAD, timeout=30)
    assert r.status_code == 200
    assert "application/pdf" in r.headers.get("content-type", "")
    assert r.content[:4] == b"%PDF"
    assert "solix-press-enterprise-edition-launch.pdf" in r.headers.get("content-disposition", "")


def test_press_pdf_invalid_id_pattern():
    bad = {**PRESS_PAYLOAD, "id": "Invalid ID With Spaces!"}
    r = requests.post(f"{API}/press/pdf", json=bad, timeout=15)
    assert r.status_code == 422


# ---------- Media kit SVG ----------
def test_brand_logo_svg():
    r = requests.get(f"{BASE_URL}/brand/solix-logo-dark.svg", timeout=15)
    assert r.status_code == 200
    assert "svg" in r.text[:200].lower()


# ---------- Cleanup ----------
def test_zz_cleanup(auth_headers, seed_submission):
    requests.delete(f"{API}/admin/submissions/{seed_submission['id']}", headers=auth_headers, timeout=15)
