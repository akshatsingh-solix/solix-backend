"""Access control (permissions.py): role templates, per-user overrides, switched-off features, owner-only account
edits and the per-user dashboard (home.py). Runs in-process against an in-memory Mongo; invented people only."""
import os

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "solix_test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("ADMIN_EMAIL", "admin@example.com")
os.environ.setdefault("ADMIN_PASSWORD", "AdminPass!1")
os.environ["OPS_LOOP"] = "0"

import asyncio
import sys

import pytest
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient

import cache
import database
import server  # noqa: F401

PATCHED = ("server", "intent", "leads_admin", "admin", "auth", "emailer", "accounts", "delivery", "chat", "chat_leads", "content", "events", "press", "site_settings", "seo", "knowledge")
_loop = asyncio.new_event_loop()


def run(coro):
    return _loop.run_until_complete(coro)


@pytest.fixture(scope="module")
def admin():
    if getattr(database.db, "client", None) is database.client:
        database.db = AsyncMongoMockClient()["solix_test"]
    for name in PATCHED:
        mod = sys.modules.get(name)
        if mod is not None and hasattr(mod, "db"):
            mod.db = database.db
    with TestClient(server.app) as c:
        token = c.post("/api/auth/login", json={"email": os.environ["ADMIN_EMAIL"], "password": os.environ["ADMIN_PASSWORD"]}).json()["access_token"]
        c.headers.update({"Authorization": f"Bearer {token}"})
        yield c


def _login_as(admin, email, role, name="Test Person"):
    r = admin.post("/api/admin/users", json={"email": email, "name": name, "role": role})
    assert r.status_code == 201, r.text
    body = r.json()
    c = TestClient(server.app)
    tok = c.post("/api/auth/login", json={"email": email, "password": body["temporary_password"]}).json()
    c.headers.update({"Authorization": f"Bearer {tok['access_token']}"})
    return c, body["user"]["id"]


def _company(name, owner=None):
    import radar_service as rs

    async def go():
        c = await rs.get_or_create_company(name, domain=name.lower().replace(" ", "") + ".example", source="manual")
        await database.db.companies.update_one({"id": c["id"]}, {"$set": {"owner": owner}})
        return c["id"]
    return run(go())


def test_defaults_keep_what_each_role_could_do(admin):
    sales, _ = _login_as(admin, "rep.one@example.com", "sales")
    me = sales.get("/api/auth/me").json()
    assert me["access"]["radar"] == "edit" and me["access"]["content"] == "view" and me["dashboard"] == "rep"
    assert sales.get("/api/admin/content").status_code != 403  # everyone could read every area before
    viewer, _ = _login_as(admin, "boss@example.com", "viewer")
    assert viewer.get("/api/auth/me").json()["dashboard"] == "executive"
    assert viewer.put("/api/admin/team", json={"members": []}).status_code == 403  # writes still follow the roles
    assert admin.get("/api/auth/me").json()["access"]["settings"] == "full"


def test_override_takes_away_and_gives(admin):
    sales, uid = _login_as(admin, "rep.two@example.com", "sales")
    cid = _company("Overrideco Holdings", owner="rep.two@example.com")
    assert sales.patch(f"/api/admin/radar/accounts/{cid}", json={"notes": "first"}).status_code == 200
    # Admin takes Radar down to read-only for this person: reads work, edits do not.
    r = admin.put(f"/api/admin/access/users/{uid}", json={"access": {"radar": "view", "leads": "none"}})
    assert r.status_code == 200, r.text
    cache.bump("access")
    assert sales.get("/api/admin/radar/accounts").status_code == 200
    assert sales.patch(f"/api/admin/radar/accounts/{cid}", json={"notes": "second"}).status_code == 403
    assert sales.get("/api/admin/leads").status_code == 403
    assert sales.get("/api/auth/me").json()["access"]["leads"] == "none"
    # Null hands the feature back to the role.
    admin.put(f"/api/admin/access/users/{uid}", json={"access": {"radar": None, "leads": None}})
    assert sales.get("/api/admin/leads").status_code == 200
    # Full Radar grants what was admin-only (Radar settings) to one person.
    admin.put(f"/api/admin/access/users/{uid}", json={"access": {"radar": "full"}})
    assert sales.get("/api/auth/me").json()["access"]["radar"] == "full"
    assert sales.patch(f"/api/admin/radar/accounts/{_company('Notmine Partners', owner='someone@example.com')}", json={"notes": "as manager"}).status_code == 200


def test_switched_off_feature_hides_it_but_never_from_admins(admin):
    editor, _ = _login_as(admin, "writer@example.com", "editor")
    assert admin.put("/api/admin/access", json={"disabled": ["seo"]}).status_code == 200
    assert editor.get("/api/admin/seo/config").status_code == 403
    assert "seo" in editor.get("/api/auth/me").json()["disabled_features"]
    assert admin.get("/api/admin/seo/config").status_code != 403
    assert admin.put("/api/admin/access", json={"disabled": ["settings"]}).status_code == 422  # can't lock the door behind you
    admin.put("/api/admin/access", json={"disabled": []})
    assert editor.get("/api/admin/seo/config").status_code != 403


def test_role_template_change_applies_to_everyone_in_role(admin):
    comp, _ = _login_as(admin, "dpo@example.com", "compliance")
    assert comp.get("/api/admin/radar/accounts").status_code == 200
    admin.put("/api/admin/access", json={"roles": {"compliance": {"radar": "none"}}})
    assert comp.get("/api/admin/radar/accounts").status_code == 403
    admin.put("/api/admin/access", json={"roles": {"compliance": {"radar": "view"}}})
    bad = admin.put("/api/admin/access", json={"roles": {"compliance": {"radar": "owner"}}})
    assert bad.status_code == 422
    # Only admins manage access.
    assert comp.get("/api/admin/access").status_code == 403


def test_only_the_owner_or_admin_edits_an_account(admin):
    rep, _ = _login_as(admin, "rep.three@example.com", "sales")
    other = _company("Someoneelse Industries", owner="rep.four@example.com")
    r = rep.patch(f"/api/admin/radar/accounts/{other}", json={"notes": "mine now"})
    assert r.status_code == 403 and "owner" in r.json()["detail"]
    free = _company("Unclaimed Logistics", owner=None)
    assert rep.patch(f"/api/admin/radar/accounts/{free}", json={"owner": "rep.four@example.com"}).status_code == 403  # can't assign it to others
    assert rep.patch(f"/api/admin/radar/accounts/{free}", json={"owner": "rep.three@example.com"}).status_code == 200  # can claim it
    assert rep.patch(f"/api/admin/radar/accounts/{free}", json={"notes": "now I can"}).status_code == 200
    assert admin.patch(f"/api/admin/radar/accounts/{other}", json={"notes": "admin can"}).status_code == 200


def test_home_dashboard_follows_assignment(admin):
    rep, uid = _login_as(admin, "rep.five@example.com", "sales")
    _company("Homepage Analytics", owner="rep.five@example.com")
    h = rep.get("/api/admin/home").json()
    assert h["dashboard"] == "rep" and h["data"]["accounts"]["total"] == 1
    assert rep.get("/api/admin/home", params={"view": "manager"}).json()["dashboard"] == "rep"  # can't peek at another view
    admin.put(f"/api/admin/access/users/{uid}", json={"seniority": "manager"})
    h = rep.get("/api/admin/home").json()
    assert h["dashboard"] == "manager" and "coverage" in h["data"] and isinstance(h["data"]["reps"], list)
    admin.put(f"/api/admin/access/users/{uid}", json={"dashboard": "marketing"})
    assert rep.get("/api/admin/home").json()["dashboard"] == "marketing"
    assert admin.put(f"/api/admin/access/users/{uid}", json={"dashboard": "bogus"}).status_code == 422
    comp = admin.get("/api/admin/home", params={"view": "compliance"}).json()
    assert comp["dashboard"] == "compliance" and "suppressed" in comp["data"]
    listing = admin.get("/api/admin/access").json()
    row = next(u for u in listing["users"] if u["id"] == uid)
    assert row["lands_on"] == "marketing" and row["seniority"] == "manager"
