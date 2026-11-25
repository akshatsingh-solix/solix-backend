"""Internal traffic (internal_traffic.py): Solix's own people are recognised by email domain, address list, staff
list, office network, network lookup and marked browsers; the flag spreads to everything that is the same person;
every metric hides it by default and can show it or only it; a manual "not internal" wins over every rule.
In-memory Mongo; invented people and documentation-range IP addresses only."""
import asyncio
import os

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "solix_test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("ADMIN_EMAIL", "admin@example.com")
os.environ.setdefault("ADMIN_PASSWORD", "AdminPass!1")
os.environ["OPS_LOOP"] = "0"

import pytest  # noqa: E402

import cache
import database
import internal_traffic as it

OFFICE_IP = "203.0.113.40"  # TEST-NET-3: never a real network
HOME_IP = "198.51.100.7"     # TEST-NET-2
_loop = asyncio.new_event_loop()


def run(coro):
    return _loop.run_until_complete(coro)


def db():
    return database.db


@pytest.fixture(scope="module")
def client(isolated_app):
    c = isolated_app[0]
    r = c.put("/api/admin/internal", json={"domains": ["solix.com", "solixtechnologies.com"], "emails": ["contractor@gmail.example"],
                                           "ip_ranges": [{"cidr": "203.0.113.0/24", "label": "Santa Clara office"}]})
    assert r.status_code == 200, r.text
    return c


def _submit(client, email, ip=HOME_IP, **extra):
    r = client.post("/api/submissions", json={"type": "demo", "email": email, "name": "Test Person", "company": "Example", **extra},
                    headers={"X-Forwarded-For": ip})
    assert r.status_code == 201, r.text
    return r.json()


def _lead(email):
    return run(db().leads.find_one({"email": email}, {"_id": 0}))


def _track(client, vid, ip=HOME_IP, context=None):
    body = {"visitor_id": vid, "session_id": f"s-{vid}", "context": context or {},
            "events": [{"type": "page_view", "path": "/products/enterprise-archiving"}, {"type": "page_view", "path": "/pricing"}]}
    r = client.post("/api/track", content=__import__("json").dumps(body), headers={"X-Forwarded-For": ip, "Content-Type": "text/plain"})
    assert r.status_code == 204, r.text


def test_settings_validate_ranges_and_domains(client):
    bad = client.put("/api/admin/internal", json={"ip_ranges": [{"cidr": "not-an-ip"}]})
    assert bad.status_code == 422
    bad = client.put("/api/admin/internal", json={"domains": ["not a domain"]})
    assert bad.status_code == 422
    got = client.get("/api/admin/internal", headers={"X-Forwarded-For": OFFICE_IP}).json()
    assert got["settings"]["ip_ranges"][0]["cidr"] == "203.0.113.0/24"
    assert got["you"]["match"]["detail"] == "Santa Clara office"  # "is the network I'm on internal?"
    assert "_nets" not in got["settings"]


def test_company_email_is_internal_everywhere_and_hidden_from_metrics(client):
    sub = _submit(client, "tester@india.solix.com")  # any subdomain counts
    stored = run(db().submissions.find_one({"id": sub["id"]}, {"_id": 0}))
    assert stored["internal"] is True and stored["internal_reason"]["kind"] == "email_domain"
    lead = _lead("tester@india.solix.com")
    assert lead["internal"] is True

    _submit(client, "buyer@prospect.example")
    default = client.get("/api/admin/leads").json()
    emails = {l["email"] for l in default["items"]}
    assert "buyer@prospect.example" in emails and "tester@india.solix.com" not in emails
    only = {l["email"] for l in client.get("/api/admin/leads", params={"internal": "only"}).json()["items"]}
    assert only == {"tester@india.solix.com"}
    both = {l["email"] for l in client.get("/api/admin/leads", params={"internal": "include"}).json()["items"]}
    assert {"tester@india.solix.com", "buyer@prospect.example"} <= both

    inbox = {s["email"] for s in client.get("/api/admin/submissions").json()["items"]}
    assert "tester@india.solix.com" not in inbox and "buyer@prospect.example" in inbox
    stats = client.get("/api/admin/stats").json()
    assert stats["internal"]["submissions"] >= 1

    overview = client.get("/api/admin/reports/overview").json()
    assert overview["internal"]["mode"] == "exclude" and overview["internal"]["leads"] >= 1
    with_staff = client.get("/api/admin/reports/overview", params={"internal": "include"}).json()
    assert with_staff["kpis"]["leads"] == overview["kpis"]["leads"] + overview["internal"]["leads"]


def test_internal_form_skips_the_sales_alert(client):
    assert run(it.should_alert({"type": "demo", "internal": True})) is False
    assert run(it.should_alert({"type": "demo"})) is True


def test_listed_address_and_staff_users_are_internal(client):
    _submit(client, "contractor@gmail.example")
    assert _lead("contractor@gmail.example")["internal_reason"]["kind"] == "email"
    _submit(client, os.environ["ADMIN_EMAIL"])  # a MarkHub user filling in a form on the site
    assert _lead(os.environ["ADMIN_EMAIL"].lower())["internal_reason"]["kind"] == "staff"


def test_office_network_flags_browsing_and_the_lead_it_becomes(client):
    _track(client, "visitor-office-1", ip=OFFICE_IP)
    v = run(db().visitors.find_one({"id": "visitor-office-1"}, {"_id": 0}))
    assert v["internal"] is True and v["internal_reason"]["kind"] == "ip"
    assert v["internal_reason"]["detail"] == "Santa Clara office"
    assert OFFICE_IP not in str(v)  # the rule is kept, never the address
    events = run(db().events.find({"visitor_id": "visitor-office-1"}, {"_id": 0}).to_list(None))
    assert events and all(e.get("internal") for e in events)

    # The same person fills in a form from home with a personal address: still one of ours.
    _submit(client, "someone@personal.example", ip=HOME_IP, visitor_id="visitor-office-1")
    lead = _lead("someone@personal.example")
    assert lead["internal"] is True
    assert run(db().submissions.find_one({"email": "someone@personal.example"}, {"_id": 0}))["internal"] is True

    _track(client, "visitor-prospect-1", ip=HOME_IP)
    assert not run(db().visitors.find_one({"id": "visitor-prospect-1"}, {"_id": 0})).get("internal")


def test_marked_browser_and_markhub_sign_in(client):
    _track(client, "visitor-device-1", context={"internal": "1"})  # opened the site with ?solix_internal=1
    assert run(db().visitors.find_one({"id": "visitor-device-1"}, {"_id": 0}))["internal_reason"]["kind"] == "device"

    _track(client, "visitor-staff-laptop", ip=HOME_IP)
    r = client.post("/api/admin/internal/this-browser", json={"visitor_id": "visitor-staff-laptop"})
    assert r.status_code == 200, r.text
    v = run(db().visitors.find_one({"id": "visitor-staff-laptop"}, {"_id": 0}))
    assert v["internal"] is True and v["internal_reason"]["kind"] == "device"
    assert run(db().events.count_documents({"visitor_id": "visitor-staff-laptop", "internal": True})) == 2


def test_manual_not_internal_wins_over_rules_and_rescans(client):
    _submit(client, "partner-engineer@solix.com")
    lead = _lead("partner-engineer@solix.com")
    assert lead["internal"] is True
    r = client.post(f"/api/admin/leads/{lead['id']}/internal", json={"internal": False})
    assert r.status_code == 200, r.text
    assert r.json()["internal"] is False
    assert "partner-engineer@solix.com" in {l["email"] for l in client.get("/api/admin/leads", params={"q": "partner-engineer"}).json()["items"]}

    client.post("/api/admin/internal/rescan")
    _submit(client, "partner-engineer@solix.com")  # a new form doesn't flip it back either
    assert _lead("partner-engineer@solix.com")["internal"] is False
    assert not run(db().submissions.find_one({"email": "partner-engineer@solix.com"}, {"_id": 0}, sort=[("created_at", -1)])).get("internal")

    # And by hand the other way: a prospect's address that is really a colleague's.
    _submit(client, "colleague@personal2.example")
    other = _lead("colleague@personal2.example")
    client.post(f"/api/admin/leads/{other['id']}/internal", json={"internal": True})
    assert _lead("colleague@personal2.example")["internal_reason"]["kind"] == "manual"


def test_rescan_flags_history_and_clears_removed_rules(client):
    async def legacy():
        await db().leads.insert_one({"id": "legacy-1", "email": "old.timer@solixtechnologies.com", "stage": "mql", "created_at": "2026-01-02T00:00:00+00:00",
                                     "visitor_ids": ["legacy-visitor"]})
        await db().submissions.insert_one({"id": "legacy-sub", "type": "contact", "email": "old.timer@solixtechnologies.com", "created_at": "2026-01-02T00:00:00+00:00"})
        await db().visitors.insert_one({"id": "legacy-visitor", "lead_id": "legacy-1", "created_at": "2026-01-01T00:00:00+00:00"})
        await db().events.insert_one({"id": "legacy-ev", "visitor_id": "legacy-visitor", "type": "page_view", "path": "/", "at": "2026-01-01T00:00:00+00:00"})
    run(legacy())
    counts = client.post("/api/admin/internal/rescan").json()
    assert counts["leads_flagged"] >= 1
    assert _lead("old.timer@solixtechnologies.com")["internal"] is True
    assert run(db().submissions.find_one({"id": "legacy-sub"}, {"_id": 0}))["internal"] is True
    assert run(db().events.find_one({"id": "legacy-ev"}, {"_id": 0}))["internal"] is True

    # The domain is dropped from the rules: its rule-based flags go; flags with their own reason stay.
    client.put("/api/admin/internal", json={"domains": ["solix.com"], "ip_ranges": [{"cidr": "203.0.113.0/24", "label": "Santa Clara office"}]})
    counts = client.post("/api/admin/internal/rescan").json()
    assert counts["leads_cleared"] >= 1
    assert not _lead("old.timer@solixtechnologies.com")["internal"]
    assert run(db().visitors.find_one({"id": "visitor-office-1"}, {"_id": 0}))["internal"] is True
    client.put("/api/admin/internal", json={"domains": ["solix.com", "solixtechnologies.com"], "emails": ["contractor@gmail.example"],
                                           "ip_ranges": [{"cidr": "203.0.113.0/24", "label": "Santa Clara office"}]})


def test_sol_conversation_follows_a_shared_company_email(client):
    sid = "chat-internal-1"
    r = client.post("/api/chat/capture", json={"session_id": sid, "message": "I'm testing, reach me at qa.team@solix.com"}, headers={"X-Forwarded-For": HOME_IP})
    assert r.status_code == 200, r.text
    sub = run(db().submissions.find_one({"source_page": f"chat:{sid}"}, {"_id": 0}))
    assert sub["internal"] is True
    assert it.session_internal(sid)

    async def messages():
        await db().chat_messages.insert_many([
            {"id": "m1", "session_id": "chat-prospect-1", "role": "user", "content": "How does archiving work?", "created_at": "2026-11-20T10:00:00+00:00"},
            {"id": "m2", "session_id": sid, "role": "user", "content": "test question", "created_at": "2026-11-20T11:00:00+00:00"},
        ])
        await it.mark_chat_session(sid, it._reason("linked"))
    run(messages())
    listed = {c["session_id"] for c in client.get("/api/admin/chats").json()["items"]}
    assert "chat-prospect-1" in listed and sid not in listed
    only = client.get("/api/admin/chats", params={"internal": "only"}).json()["items"]
    assert [c["session_id"] for c in only] == [sid] and only[0]["internal"] is True

    # A colleague's chat that turns out to be a real customer: cleared by hand.
    assert client.post(f"/api/admin/chats/{sid}/internal", json={"internal": False}).status_code == 200
    assert sid in {c["session_id"] for c in client.get("/api/admin/chats").json()["items"]}


def test_summary_counts_by_reason(client):
    s = client.get("/api/admin/internal").json()["summary"]
    assert s["leads"]["total"] >= 3
    assert {"email_domain", "staff"} <= set(s["leads"]["by_reason"])
    assert s["visitors"]["total"] >= 2 and s["events"] >= 2


def test_rules_can_be_switched_off(client):
    client.put("/api/admin/internal", json={"enabled": False})
    try:
        _submit(client, "new.hire@solix.com")
        assert not _lead("new.hire@solix.com").get("internal")
    finally:
        client.put("/api/admin/internal", json={"enabled": True, "domains": ["solix.com", "solixtechnologies.com"],
                                               "ip_ranges": [{"cidr": "203.0.113.0/24", "label": "Santa Clara office"}]})
        cache.bump("internal")


def test_sales_can_flag_but_not_change_the_rules(client):
    from fastapi.testclient import TestClient
    import server
    r = client.post("/api/admin/users", json={"email": "rep.internal@example.com", "name": "Rep Person", "role": "sales"})
    assert r.status_code == 201, r.text
    rep = TestClient(server.app)
    tok = rep.post("/api/auth/login", json={"email": "rep.internal@example.com", "password": r.json()["temporary_password"]}).json()["access_token"]
    rep.headers.update({"Authorization": f"Bearer {tok}"})
    assert rep.put("/api/admin/internal", json={"domains": ["example.com"]}).status_code == 403
    assert rep.post("/api/admin/internal/rescan").status_code == 403
    lead = _lead("buyer@prospect.example")
    assert rep.post(f"/api/admin/leads/{lead['id']}/internal", json={"internal": True}).status_code == 200
    assert rep.post("/api/admin/internal/this-browser", json={"visitor_id": "visitor-rep-laptop"}).status_code == 200
