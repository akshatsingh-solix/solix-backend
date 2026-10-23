"""End-to-end tests for the Radar and EchoLocate (radar*.py, outbound.py, compliance.py,
ops.py), run in-process against an in-memory Mongo (mongomock-motor).

Fixtures use invented companies only: real Radar data must never be committed here."""
import os

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "solix_test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("ADMIN_EMAIL", "admin@example.com")
os.environ.setdefault("ADMIN_PASSWORD", "AdminPass!1")
os.environ["OPS_LOOP"] = "0"
os.environ["PUBLIC_API_URL"] = "https://api.example.test"
os.environ["INSTANTLY_WEBHOOK_SECRET"] = "hook-secret"
for k in ("GROQ_API_KEY", "GEMINI_API_KEY", "MISTRAL_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY", "SOL_CUSTOM_BASE_URL", "INSTANTLY_API_KEY", "REACHER_URL"):
    os.environ.pop(k, None)

import asyncio
import json
import sys

import pytest
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient

import database

import server  # noqa: E402,F401  (imports every router)

_mock_db = None
PATCHED = ("server", "intent", "leads_admin", "admin", "auth", "emailer", "accounts", "delivery", "chat", "chat_leads", "content", "events", "press", "site_settings", "seo", "knowledge")


def _bind_memory_db():
    """Use the in-memory database another test module already installed, or install one.
    Done when the fixture starts (not at import) so module import order can't split the data."""
    global _mock_db
    if getattr(database.db, "client", None) is database.client:  # still the real database
        database.db = AsyncMongoMockClient()["solix_test"]
    _mock_db = database.db
    for name in PATCHED:  # modules that did `from database import db` keep their own reference
        mod = sys.modules.get(name)
        if mod is not None and hasattr(mod, "db"):
            mod.db = _mock_db


import seo  # noqa: E402


async def _no_news(client, q):
    return []


@pytest.fixture(autouse=True)
def offline_news(monkeypatch):
    monkeypatch.setattr(seo, "_news", _no_news)  # keep these tests offline without leaking into test_seo


import compliance  # noqa: E402
import outbound  # noqa: E402
import radar_collectors as rc  # noqa: E402
import radar_research  # noqa: E402

RADAR = {
    "generated": "2026-10-16 15:39",
    "accounts": [{"Company": "Acme Mutual Insurance", "Lead Strength": "Strong"}, {"Company": "Globex Bank", "Lead Strength": "Medium"}],
    "jobs": [
        {"Company": "Acme Mutual Insurance", "Account Strength": "Strong", "Lead Strength": "Strong", "Competitor Product": "OpenText Documentum", "Job Title": "ECM Platform Engineer",
         "Location": "Hartford, CT", "State": "Connecticut", "Country": "United States", "Date": "2026-10-10", "Owner": "", "Status": "New",
         "Posting URL": "https://jobs.example.test/1", "Matching Sentence": "Administer OpenText Documentum D2 and xPlore."},
        {"Company": "Acme Mutual Insurance", "Account Strength": "Strong", "Lead Strength": "Medium", "Competitor Product": "Informatica IDMC", "Job Title": "Director, Data Integration",
         "Location": "Hartford, CT", "State": "Connecticut", "Country": "United States", "Date": "2026-10-11", "Owner": "", "Status": "New",
         "Posting URL": "https://jobs.example.test/2", "Matching Sentence": "Own the Informatica IDMC roadmap."},
        {"Company": "Globex Bank", "Account Strength": "Medium", "Lead Strength": "Medium", "Competitor Product": "Smarsh", "Job Title": "Manager, Surveillance",
         "Location": "San Jose, CA", "State": "California", "Country": "United States", "Date": "2026-10-05", "Owner": "Pat Rep", "Status": "Contacted",
         "Posting URL": "https://jobs.example.test/3", "Matching Sentence": "Experience with Smarsh archiving."},
    ],
    "problems": [], "linkedin": [], "dns": [],
    "research": [{
        "Company": "Acme Mutual Insurance", "Score": 7, "Verdict": "Ready for Outreach", "Date": "2026-10-16", "Headline": "Acme is consolidating content platforms.",
        "Summary": "Acme is hiring to run its content platform [1].", "Trigger": "New platform team [1].",
        "Evidence": [{"Title": "Job posting", "URL": "https://jobs.example.test/1", "Date": "2026-10-10"}], "Why Solix Wins": "One archive [1].",
        "Contacts": [{"Name": "Jordan Example", "Title": "Chief Information Officer", "LinkedIn URL": ""}],
        "First Touch": {"Subject": "After the consolidation", "Body": "Hi {first name},\n\nTeams consolidating platforms often keep old systems only for their records.\n\nBest,\n{rep name}"},
        "Gaps": "Confirm sponsor.", "Brief Path": "research/acme.md"}],
}


@pytest.fixture(scope="module")
def client():
    _bind_memory_db()
    with TestClient(server.app) as c:
        token = c.post("/api/auth/login", json={"email": os.environ["ADMIN_EMAIL"], "password": os.environ["ADMIN_PASSWORD"]}).json()["access_token"]
        c.headers.update({"Authorization": f"Bearer {token}"})
        yield c


_loop = asyncio.new_event_loop()


def run(coro):
    # A private loop: asyncio.run() would clear the main thread's loop, which other test modules use.
    return _loop.run_until_complete(coro)


def _company(client, name):
    items = client.get("/api/admin/radar/accounts", params={"q": name}).json()["items"]
    assert items, f"{name} not found"
    return items[0]


# --- Radar ------------------------------------------------------------------------------

def test_import_builds_accounts_and_keeps_human_columns(client):
    client.put("/api/admin/team", json={"members": [{"name": "Andy Hebert", "email": "andy@example.com"}, {"name": "Pat Rep", "email": "pat@example.com"}]})
    r = client.post("/api/admin/radar/import", files={"file": ("radar.json", json.dumps(RADAR), "application/json")})
    assert r.status_code == 200, r.text
    counts = r.json()
    assert counts["signals_new"] == 3 and counts["research"] == 1 and counts["contacts"] == 1
    acme = _company(client, "Acme Mutual")
    assert acme["strength"] == "Strong"
    assert acme["owner"] == "andy@example.com" and acme["owner_basis"] == "state"  # CT -> Andy Hebert
    assert acme["status"] == "ready" and acme["research_score"] == 7
    assert "enterprise-content-services" == acme["primary_product"] or acme["primary_line"] in ("ecs", "platform")
    globex = _company(client, "Globex")
    assert globex["owner"] == "pat@example.com" and globex["owner_basis"] == "manual"  # the human Owner column wins over CA routing
    assert globex["status"] == "contacted"
    # Re-importing never duplicates signals or research.
    again = client.post("/api/admin/radar/import", files={"file": ("radar.json", json.dumps(RADAR), "application/json")}).json()
    assert again["signals_new"] == 0 and again["research"] == 0


def test_html_import_and_legacy_contract(client):
    html = '<html><script type="application/json" id="radar-data">' + json.dumps(RADAR) + "</script></html>"
    assert client.post("/api/admin/radar/import", files={"file": ("radar.html", html, "text/html")}).status_code == 200
    legacy = client.get("/api/admin/radar/legacy.json").json()
    assert set(legacy) == {"generated", "accounts", "jobs", "problems", "linkedin", "dns", "research"}
    job = next(j for j in legacy["jobs"] if j["Company"] == "Acme Mutual Insurance")
    assert {"Account Strength", "Lead Strength", "Competitor Product", "Job Title", "Posting URL", "Matching Sentence", "Owner", "Status"} <= set(job)
    assert legacy["research"][0]["First Touch"]["Subject"] == "After the consolidation"
    x = client.get("/api/admin/radar/export.xlsx")
    assert x.status_code == 200 and x.content[:2] == b"PK"


def test_summary_seen_and_updated(client):
    s = client.get("/api/admin/radar/summary").json()
    assert s["accounts"] >= 2 and s["fresh"]["new"] >= 2 and s["ready"] >= 1
    assert any(c["product"] == "OpenText Documentum" for c in s["competitors"])
    client.post("/api/admin/radar/seen", json={"all": True})
    assert client.get("/api/admin/radar/summary").json()["fresh"] == {"new": 0, "updated": 0}
    acme = _company(client, "Acme Mutual")
    client.post(f"/api/admin/radar/accounts/{acme['id']}/signals", json={"type": "news", "title": "Acme announces cloud migration", "evidence_url": "https://news.example.test/a"})
    fresh = client.get("/api/admin/radar/summary").json()["fresh"]
    assert fresh == {"new": 0, "updated": 1}  # a new signal makes a seen account "Updated" again
    items = client.get("/api/admin/radar/accounts", params={"tab": "news"}).json()["items"]
    assert any(i["id"] == acme["id"] for i in items)


def test_manual_owner_survives_recompute_and_routing_preview(client):
    acme = _company(client, "Acme Mutual")
    r = client.patch(f"/api/admin/radar/accounts/{acme['id']}", json={"owner": "pat@example.com", "notes": "Warm via partner"})
    assert r.status_code == 200 and r.json()["owner_basis"] == "manual"
    client.post("/api/admin/radar/recompute")
    acme = _company(client, "Acme Mutual")
    assert acme["owner"] == "pat@example.com" and acme["notes"] == "Warm via partner"
    p = client.get("/api/admin/radar/routing/preview", params={"company": "Initech", "country": "United States", "location": "Fresno, CA 93650"}).json()
    assert p["rep"] == "Hameed Hemmat" and p["basis"] == "state"
    detail = client.get(f"/api/admin/radar/accounts/{acme['id']}").json()
    assert any(h["action"] == "owner" for h in detail["account"]["history"])


def test_inbound_lead_links_to_account_and_deal_excludes_it(client):
    globex = _company(client, "Globex")
    client.patch(f"/api/admin/radar/accounts/{globex['id']}", json={"domains": ["globexbank.example"]})
    r = client.post("/api/submissions", json={"type": "download", "email": "cfo@globexbank.example", "name": "Casey CFO", "company": "Globex Bank", "interest": "application-retirement"})
    assert r.status_code == 201
    lead = run(_mock_db.leads.find_one({"email": "cfo@globexbank.example"}))
    assert lead["company_id"] == globex["id"]
    assert client.patch(f"/api/admin/leads/{lead['id']}", json={"stage": "sql"}).status_code == 200
    g = client.get(f"/api/admin/radar/accounts/{globex['id']}").json()["account"]
    assert g["excluded_reason"].startswith("Active deal") and g["status"] == "meeting_booked"
    assert "website" in g["signal_types"]


def test_lead_before_account_links_by_domain_and_takes_owner(client):
    client.post("/api/submissions", json={"type": "demo", "email": "ops@initech.example", "name": "Ira Ops"})
    lead = run(_mock_db.leads.find_one({"email": "ops@initech.example"}, {"_id": 0}))
    assert not lead.get("company_id") and not lead.get("owner")
    acc = client.post("/api/admin/radar/accounts", json={"name": "Initech", "domain": "initech.example", "country": "United States"}).json()
    lead = run(_mock_db.leads.find_one({"email": "ops@initech.example"}, {"_id": 0}))
    assert lead["company_id"] == acc["id"] and lead["id"] in acc["lead_ids"]
    client.patch(f"/api/admin/radar/accounts/{acc['id']}", json={"owner": "pat@example.com"})
    lead = run(_mock_db.leads.find_one({"email": "ops@initech.example"}, {"_id": 0}))
    assert lead["owner"] == "pat@example.com" and lead["history"][-1]["action"] == "owner"
    # Someone else's lead is flagged in #conflicts, never moved.
    client.patch(f"/api/admin/leads/{lead['id']}", json={"owner": "sam@example.com"})
    client.patch(f"/api/admin/radar/accounts/{acc['id']}", json={"notes": "touch"})
    assert run(_mock_db.leads.find_one({"id": lead["id"]}))["owner"] == "sam@example.com"
    assert run(_mock_db.ops_outbox.find_one({"event": "owner_conflict", "payload.channel": "conflicts"}))
    assert run(_mock_db.ops_outbox.find_one({"event": "lead.updated"})) is None  # Digiprise isn't configured


def test_channel_for_outbound_sourced_leads():
    import scoring
    assert scoring.channel_for({"outbound": True}) == "outbound"
    assert scoring.channel_for({"landing": "radar"}) == "outbound"  # leads promoted before the channel existed
    assert scoring.channel_for({"landing": "/pricing"}) == "direct"


def test_research_queue_without_a_model_drafts_a_rules_brief(client):
    acme = _company(client, "Acme Mutual")
    r = client.post(f"/api/admin/radar/accounts/{acme['id']}/research")
    assert r.status_code == 200
    run(radar_research.run_queue(limit=5))
    drafts = client.get("/api/admin/radar/research", params={"status": "draft"}).json()
    assert drafts and drafts[0]["provider"] == "rules"
    bad = {"first_touch": {"subject": "Moving off Documentum", "body": "Hi"}, "summary": "x [9]", "evidence": [{"n": 1}], "score": 5}
    levels = {c["level"] for c in radar_research.validate(bad, {})}
    assert "block" in levels
    rej = client.patch(f"/api/admin/radar/research/{drafts[0]['id']}", json={"action": "approve", "first_touch": {"subject": "About Documentum", "body": "Hi"}})
    assert rej.status_code == 422  # competitor named in the first touch blocks approval


def test_collector_classification(client):
    sigs = rc.classify_posting("Acme Mutual Insurance", "Senior Content Engineer", "You will run OpenText Documentum and Smarsh.", url="https://x.test/j", source="greenhouse")
    assert {s["competitor_key"] for s in sigs} == {"opentext-documentum", "smarsh"}
    assert rc.classify_posting("TekSystems Staffing", "Engineer", "Documentum", url="u", source="x") == []
    prob = rc.classify_posting("Acme", "Architect", "Lead our application retirement programme for legacy ERP.", url="u2", source="x")
    assert prob and prob[0]["type"] == "problem_job"


# --- EchoLocate ----------------------------------------------------------------------------

async def _valid(email):
    return {"status": "valid", "detail": "test"}


def test_gate_blocks_until_lia_signed_and_germany_blocked(client, monkeypatch):
    monkeypatch.setattr(outbound, "verify_email", _valid)
    acme = _company(client, "Acme Mutual")
    c = client.post("/api/admin/outbound/contacts", json={"name": "Jordan Smith", "email": "jordan@acmemutual.example", "title": "Chief Information Officer", "company_id": acme["id"], "country": "United States"})
    assert c.status_code == 201, c.text
    contact = c.json()
    assert contact["email_status"] == "valid" and contact["persona"] == "target"
    gate = client.get(f"/api/admin/compliance/gate/{contact['id']}").json()
    assert not gate["ok"] and any("Legitimate Interest" in r for r in gate["reasons"])
    lia = client.post("/api/admin/compliance/lia", json={"region": "us", "campaign_type": "Archiving outbound", "purpose": "Offer archiving to firms hiring for it",
                                                           "necessity": "Email is the least intrusive route", "balancing": "B2B role-relevant, easy opt-out"}).json()
    client.post(f"/api/admin/compliance/lia/{lia['id']}/sign")
    assert client.get(f"/api/admin/compliance/gate/{contact['id']}").json()["ok"]
    de = client.post("/api/admin/outbound/contacts", json={"name": "Max Muster", "email": "max@acmemutual.example", "title": "CIO", "company_id": acme["id"], "country": "Germany"}).json()
    g = client.get(f"/api/admin/compliance/gate/{de['id']}").json()
    assert not g["ok"] and any("Germany" in r for r in g["reasons"])


def test_region_falls_back_to_signals_when_no_hq():
    # Radar accounts usually have no HQ on file: their routed territory and signal countries stand in.
    us = {"territory": "USA", "countries": ["United States"], "hq": {}}
    assert compliance.recipient_location({}, us) == ({"United States"}, "us", False)
    uk = {"territory": "Europe & UK", "countries": ["United Kingdom"]}
    assert compliance.recipient_location({}, uk)[1] == "uk"
    assert compliance.recipient_location({}, {"territory": "Europe & UK", "countries": ["United Kingdom", "France"]})[1] == "eu"
    assert compliance.recipient_location({"country": "Canada"}, uk) == ({"Canada"}, "us", True)
    mixed = {"name": "Initech", "territory": "USA", "countries": ["United States", "Germany"], "signal_count": 2, "score": 80}
    g = run(compliance.gate({"email": "a@initech.example", "email_status": "valid", "name": "A", "title": "CIO", "lawful_basis": "legitimate_interest"}, mixed))
    assert any("set the contact's country" in r for r in g["reasons"])
    g = run(compliance.gate({"email": "a@initech.example", "email_status": "valid", "name": "A", "title": "CIO", "lawful_basis": "legitimate_interest", "country": "United States"}, mixed))
    assert not any("Germany" in r for r in g["reasons"])


def test_mark_verified_clears_the_verification_gate(client, monkeypatch):
    async def _risky(email):
        return {"status": "mx_only", "detail": "no SMTP check"}
    monkeypatch.setattr(outbound, "verify_email", _risky)
    acme = _company(client, "Acme Mutual")
    c = client.post("/api/admin/outbound/contacts", json={"name": "Riley Park", "email": "riley@acmemutual.example", "title": "Records Manager", "company_id": acme["id"], "country": "United States"}).json()
    g = client.get(f"/api/admin/compliance/gate/{c['id']}").json()
    assert any("not verified" in r for r in g["reasons"])
    client.put("/api/admin/compliance/settings", json={"require_verified_email": False})
    g = client.get(f"/api/admin/compliance/gate/{c['id']}").json()
    assert not any("not verified" in r for r in g["reasons"]) and any("not verified" in w for w in g["warnings"])
    client.put("/api/admin/compliance/settings", json={"require_verified_email": True})
    r = client.patch(f"/api/admin/outbound/contacts/{c['id']}", json={"mark_verified": True})
    assert r.status_code == 200 and r.json()["email_status"] == "valid" and "admin@example.com" in r.json()["verify_detail"]
    assert not any("not verified" in r for r in client.get(f"/api/admin/compliance/gate/{c['id']}").json()["reasons"])
    run(_mock_db.contacts.update_one({"id": c["id"]}, {"$set": {"email_status": "invalid"}}))
    assert client.patch(f"/api/admin/outbound/contacts/{c['id']}", json={"mark_verified": True}).status_code == 409
    run(_mock_db.contacts.delete_one({"id": c["id"]}))


@pytest.mark.parametrize("text,label", [
    ("Thanks, this is timely. Can we find 30 minutes next week to talk about retiring the old case system?", "meeting"),
    ("How about Tuesday at 10am?", "meeting"),
    ("Sounds interesting, tell me more next week.", "interested"),
    ("Please don't hesitate to send details.", "interested"),
    ("Not right now, maybe next quarter.", "objection"),
    ("No thanks, we're all set.", "not_interested"),
    ("Please remove me from your list.", "unsubscribe"),
    ("Got it.", "neutral"),
])
def test_rule_classifier_without_a_model(text, label):
    assert run(outbound.classify(text))["label"] == label


def test_enroll_approve_send_reply_flow(client, monkeypatch):
    monkeypatch.setattr(outbound, "_in_window", lambda s, n: True)
    contact = run(_mock_db.contacts.find_one({"email": "jordan@acmemutual.example"}, {"_id": 0}))
    seqs = client.get("/api/admin/outbound/sequences").json()
    assert len(seqs) == 2
    eu = next(s for s in seqs if s["region"] == "eu")
    us = next(s for s in seqs if s["region"] == "us")
    wrong = client.post("/api/admin/outbound/enrollments", json={"contact_ids": [contact["id"]], "sequence_id": eu["id"]}).json()
    assert not wrong["created"] and "written for EU" in wrong["blocked"][0]["reasons"][0]
    r = client.post("/api/admin/outbound/enrollments", json={"contact_ids": [contact["id"]], "sequence_id": us["id"]}).json()
    assert r["created"] and not r["blocked"]
    enr = r["created"][0]
    assert enr["status"] == "pending_approval" and "Documentum" not in json.dumps(enr["steps"])
    # Sending is off: nothing goes out even when approved.
    client.patch(f"/api/admin/outbound/enrollments/{enr['id']}", json={"action": "approve"})
    assert client.post("/api/admin/outbound/tick").json().get("skipped") == "off"
    client.put("/api/admin/compliance/settings", json={"sending_enabled": True, "provider": "dry_run"})
    out = client.post("/api/admin/outbound/tick").json()
    assert out["sent"] == 1
    sends = client.get("/api/admin/outbound/sends").json()
    assert sends["total"] == 1 and sends["items"][0]["status"] == "simulated"
    acme = _company(client, "Acme Mutual")
    assert acme["status"] == "contacted"
    # A positive reply stops the sequence and lands an MQL lead on the account.
    rep = client.post("/api/admin/outbound/replies", json={"contact_id": contact["id"], "text": "Sounds interesting, tell me more next week."}).json()
    assert rep["label"] == "interested"
    e = run(_mock_db.enrollments.find_one({"id": enr["id"]}))
    assert e["status"] == "replied"
    lead = run(_mock_db.leads.find_one({"email": "jordan@acmemutual.example"}))
    assert lead["stage"] == "mql" and lead["company_id"] == acme["id"] and "outbound" in lead["tags"]
    assert lead["channel"] == "outbound" and lead["owner"] == "pat@example.com"
    assert run(_mock_db.contacts.find_one({"email": "jordan@acmemutual.example"}))["lead_id"] == lead["id"]
    d = client.get(f"/api/admin/leads/{lead['id']}").json()
    assert d["account"]["id"] == acme["id"] and len(d["outbound"]["enrollments"]) == 1 and d["outbound"]["replies"][0]["label"] == "interested"
    kinds = {(t["kind"], t["type"]) for t in d["timeline"]}
    assert {("outbound", "enrolled"), ("outbound", "sent"), ("outbound", "reply")} <= kinds
    inf = client.get("/api/admin/reports/overview").json()["influence"]
    assert inf["outbound_sourced"]["leads"] == 1 and inf["radar_accounts"]["leads"] >= 2 and inf["sends"] == 1 and inf["positive_replies"] == 1
    assert [r["status"] for r in inf["account_funnel"]] == ["signal", "contacted", "replied", "meeting_booked", "opportunity", "won"]
    linked = client.get("/api/admin/leads", params={"linked": "radar"}).json()["items"]
    assert lead["id"] in {i["id"] for i in linked} and all(i.get("company_id") for i in linked)
    assert [i["id"] for i in client.get("/api/admin/leads", params={"linked": "outbound"}).json()["items"]] == [lead["id"]]
    assert _company(client, "Acme Mutual")["status"] == "replied"
    k = client.get("/api/admin/outbound/kpis").json()
    assert k["sends"] == 1 and k["positive"] == 1 and k["dod"]["qualified_reply_this_week"]


def test_inbound_form_stops_outbound_and_links_the_person(client, monkeypatch):
    monkeypatch.setattr(outbound, "verify_email", _valid)
    acme = _company(client, "Acme Mutual")
    c = client.post("/api/admin/outbound/contacts", json={"name": "Lee Park", "email": "lee@acmemutual.example", "title": "Chief Data Officer", "company_id": acme["id"], "country": "United States"}).json()
    us = next(s for s in client.get("/api/admin/outbound/sequences").json() if s["region"] == "us")
    r = client.post("/api/admin/outbound/enrollments", json={"contact_ids": [c["id"]], "sequence_id": us["id"]}).json()
    assert r["created"], r
    # Our first email reached them before they filled in the form.
    run(_mock_db.sends.insert_one({"id": "lee-1", "email": "lee@acmemutual.example", "contact_id": c["id"], "status": "simulated", "sent_at": "2026-01-01T09:00:00+00:00", "step": 1}))
    client.post("/api/submissions", json={"type": "demo", "email": "lee@acmemutual.example", "name": "Lee Park"})
    e = run(_mock_db.enrollments.find_one({"id": r["created"][0]["id"]}))
    assert e["status"] == "stopped" and "requested a demo" in e["history"][-1]["detail"]
    lead = run(_mock_db.leads.find_one({"email": "lee@acmemutual.example"}))
    assert run(_mock_db.contacts.find_one({"id": c["id"]}))["lead_id"] == lead["id"] and lead["company_id"] == acme["id"]
    assert lead["channel"] == "outbound" and "outbound" in lead["tags"]  # first touch was the email, not the form
    assert run(_mock_db.ops_outbox.find_one({"event": "inbound_handoff"}))
    # The gate sees the funnel: a person already in a live deal can't be cold-emailed.
    client.patch(f"/api/admin/leads/{lead['id']}", json={"stage": "sql"})
    assert any("sales pipeline" in x for x in client.get(f"/api/admin/compliance/gate/{c['id']}").json()["reasons"])
    assert run(_mock_db.ops_outbox.find_one({"event": "lead_stage", "payload.channel": "pipeline"}))


def test_unsubscribe_link_and_webhook_bounce(client):
    token = compliance.unsubscribe_token("jordan@acmemutual.example")
    assert client.get("/api/outbound/unsubscribe", params={"t": token}).status_code == 200
    assert client.post("/api/outbound/unsubscribe", data={"t": token}).status_code == 200
    assert run(compliance.is_suppressed("jordan@acmemutual.example")) == "unsubscribed"
    lead = run(_mock_db.leads.find_one({"email": "jordan@acmemutual.example"}))
    assert lead["do_not_contact"] == "unsubscribed" and lead["history"][-1]["action"] == "suppressed"
    assert client.get(f"/api/admin/leads/{lead['id']}").json()["suppressed"]["reason"] == "unsubscribed"
    assert client.post("/api/outbound/unsubscribe", data={"t": token + "x"}).status_code == 400
    bad = client.post("/api/outbound/webhooks/instantly", json={"event_type": "email_bounced", "lead_email": "max@acmemutual.example"})
    assert bad.status_code == 401
    ok = client.post("/api/outbound/webhooks/instantly", json={"event_type": "email_bounced", "lead_email": "max@acmemutual.example"}, headers={"X-Solix-Webhook-Secret": "hook-secret"})
    assert ok.json()["matched"] is True
    assert run(compliance.is_suppressed("max@acmemutual.example")) == "bounced"


def test_erasure_cascades_and_keeps_only_a_hash(client):
    r = client.post("/api/admin/compliance/erasure", json={"email": "jordan@acmemutual.example", "confirm": True}).json()
    assert r["counts"]["contacts"] == 1 and r["counts"]["leads"] == 1
    assert run(_mock_db.contacts.find_one({"email": "jordan@acmemutual.example"})) is None
    sup = run(_mock_db.suppression.find_one({"email_hash": compliance.email_hash("jordan@acmemutual.example")}))
    assert sup["reason"] == "gdpr_erasure" and sup["email"] is None
    assert r["counts"]["account_history"] >= 1
    acme = run(_mock_db.companies.find_one({"name": "Acme Mutual Insurance"}))
    assert not any("jordan" in (h.get("detail") or "").lower() or "Jordan Smith" in (h.get("detail") or "") for h in acme["history"])
    assert run(_mock_db.ops_outbox.find_one({"payload.email": "jordan@acmemutual.example", "target": {"$nin": ["instantly_delete", "instantly_block"]}})) is None


def test_bounce_rate_pauses_sending_and_watchdog_reports(client):
    async def seed():
        for i in range(25):
            await _mock_db.sends.insert_one({"id": f"s{i}", "sent_at": "2099-01-01T00:00:00+00:00", "status": "sent", "bounced": i < 3, "sequence_id": "x", "variant": "A", "step": 1})
        return await outbound.bounce_check(await compliance.outbound_settings())
    pct = run(seed())
    assert pct == 12.0
    s = client.get("/api/admin/compliance/settings").json()
    assert s["paused"] and "Bounce rate" in s["pause_reason"]
    h = client.get("/api/admin/ops/health").json()
    assert any(c["code"] == "outbound_paused" and not c["ok"] for c in h["checks"])
    assert run(_mock_db.ops_outbox.count_documents({"target": "slack"})) >= 1


def test_overlapping_runs_send_each_step_and_message_once(client):
    import ops
    run(_mock_db.contacts.insert_one({"id": "race-c", "email": "race@initech.example", "name": "Race Test"}))
    enr = {"id": "race-e", "contact_id": "race-c", "company_id": None, "sequence_id": "x", "variant": "A", "step_index": 0, "status": "active", "provider": "dry_run",
           "steps": [{"subject": "s1", "body": "b1", "day": 0}, {"subject": "s2", "body": "b2", "day": 3}], "history": []}
    run(_mock_db.enrollments.insert_one(dict(enr)))
    s = run(compliance.outbound_settings())
    async def together(*coros):
        return await asyncio.gather(*coros)
    results = run(together(outbound.send_step(dict(enr), s), outbound.send_step(dict(enr), s)))
    assert sorted(results) == ["simulated", "skipped"]
    assert run(_mock_db.sends.count_documents({"enrollment_id": "race-e"})) == 1
    e = run(_mock_db.enrollments.find_one({"id": "race-e"}))
    assert e["step_index"] == 1 and e.get("claim_until") is None  # released for the next step
    pending = run(_mock_db.ops_outbox.count_documents({"status": "pending", "next_attempt_at": {"$lte": "2999"}}))
    a, b = run(together(ops.process_outbox(500), ops.process_outbox(500)))
    assert pending and sum(a.values()) + sum(b.values()) == pending  # every queued message handled exactly once


def test_collector_saves_in_bulk_links_and_is_idempotent(client, monkeypatch):
    # Atlas costs a round trip per call, so a run saves its postings in bulk, never one by one.
    import radar_run
    import radar_service as rs
    run(rs.save_radar_settings({"collectors": {"ats": True, "crustdata": False, "dns": False, "news": False, "filings": False, "tenders": True},
                                "targets": [{"company": "Initrode Corp", "domain": "initrode.example", "ats": "greenhouse", "board": "initrode"}]}))
    run(_mock_db.leads.insert_one({"id": "lead-initrode", "email": "cio@initrode.example", "stage": "mql", "score": 40, "product_scores": {}, "owner": None}))

    async def postings(client_, target):
        return [{"company": "Initrode Corp", "domain": "initrode.example", "type": "competitor_job", "source": "greenhouse", "source_ref": f"gh-{i}",
                 "title": "ECM Engineer", "role_title": "ECM Engineer", "role_strength": "Strong", "competitor_key": "opentext-documentum",
                 "competitor_product": "OpenText Documentum", "country": "United States", "posted_at": "2026-10-15"} for i in range(3)]

    async def tenders(client_, settings):
        return [{"company": "Ministry of Records", "type": "tender", "source": "ted", "source_ref": "ted-1", "title": "Records archive", "public_sector": True, "country": "France"}]
    monkeypatch.setitem(radar_run.rc.ATS, "greenhouse", postings)
    monkeypatch.setattr(radar_run.rc, "ted_tenders", tenders)
    coll = type(_mock_db.company_signals)
    original = coll.update_one

    async def one_by_one(self, *a, **k):
        if self.name == "company_signals":
            raise AssertionError("signals saved one at a time")
        return await original(self, *a, **k)
    monkeypatch.setattr(coll, "update_one", one_by_one)

    first = {"id": "run-1", "counts": {}, "notes": []}
    touched = run(radar_run.collect(first))
    assert first["counts"]["signals_new"] == 4 and first["counts"]["signals_seen"] == 0 and len(touched) == 2
    initrode = run(_mock_db.companies.find_one({"key": rm_key("Initrode Corp")}, {"_id": 0}))
    assert "initrode.example" in initrode["domains"] and "lead-initrode" in initrode["lead_ids"]
    assert run(_mock_db.leads.find_one({"id": "lead-initrode"}))["company_id"] == initrode["id"]
    assert run(_mock_db.companies.find_one({"key": rm_key("Ministry of Records")}))["industry"] == "Public sector"
    again = {"id": "run-2", "counts": {}, "notes": []}
    run(radar_run.collect(again))
    assert again["counts"]["signals_new"] == 0 and again["counts"]["signals_seen"] == 4
    assert run(_mock_db.companies.count_documents({"key": rm_key("Initrode Corp")})) == 1


def rm_key(name):
    import radar_model
    return radar_model.company_key(name)


def test_bulk_paths_make_no_per_account_round_trips(client, monkeypatch):
    # Recompute, "mark all as seen" and re-import touch every account: one call each would take minutes on Atlas.
    coll = type(_mock_db.companies)
    originals = {m: getattr(coll, m) for m in ("find_one", "update_one")}

    def guard(method):
        async def call(self, *a, **k):
            if self.name in ("companies", "radar_seen", "company_signals"):
                raise AssertionError(f"{self.name}.{method} called per account")
            return await originals[method](self, *a, **k)
        return call
    for m in originals:
        monkeypatch.setattr(coll, m, guard(m))
    assert client.post("/api/admin/radar/recompute").json()["recomputed"] >= 3
    assert client.post("/api/admin/radar/seen", json={"all": True}).status_code == 200
    r = client.post("/api/admin/radar/import", files={"file": ("radar.json", json.dumps(RADAR), "application/json")})
    assert r.status_code == 200 and r.json()["signals_new"] == 0


# --- The advanced Radar ----------------------------------------------------------------------

def test_sales_team_becomes_the_radar_reps_once(client):
    import radar_model
    import radar_service as rs
    assert run(_mock_db.settings.find_one({"key": "migrations"}))["sales_team_radar_v1"]  # applied at startup
    # On a database that hasn't had it yet: the old names are replaced, and kept in the migration record.
    run(_mock_db.settings.delete_one({"key": "migrations"}))
    old = run(rs.sales_team())
    assert run(rs.ensure_sales_team()) is True
    team = run(rs.sales_team())
    assert [m["name"] for m in team] == [n for n, _ in radar_model.SALES_REPS] and len(team) == 14
    assert {"name": "Bo Deas", "email": "bo.deas@solix.com"} in team and {"name": "Samit Mandal", "email": "samit.mandal@solix.com"} in team
    assert run(_mock_db.settings.find_one({"key": "migrations"}))["sales_team_radar_v1"]["previous_members"] == old
    # Edits made afterwards in Settings are never overwritten.
    client.put("/api/admin/team", json={"members": [*team, {"name": "New Rep", "email": "new.rep@solix.com"}]})
    assert run(rs.ensure_sales_team()) is False and len(run(rs.sales_team())) == 15
    meta = client.get("/api/admin/radar/meta").json()
    assert [m["name"] for m in meta["team"]][:14] == [n for n, _ in radar_model.SALES_REPS]
    assert all(m["email"] != "admin@example.com" for m in meta["team"])  # staff logins aren't sales reps
    assert meta["rep_patch"]["Bob Wright"] == "Canada" and "United States" in meta["countries"]


def test_search_reaches_job_titles_and_places(client):
    client.post("/api/admin/radar/recompute")
    names = {a["name"] for a in client.get("/api/admin/radar/accounts", params={"q": "surveillance"}).json()["items"]}
    assert names == {"Globex Bank"}  # "Manager, Surveillance" is a job title, not in the company's name
    assert "search_text" not in client.get("/api/admin/radar/accounts").json()["items"][0]
    assert any(a["name"].startswith("Acme") for a in client.get("/api/admin/radar/accounts", params={"q": "hartford"}).json()["items"])


def test_next_action_rules():
    import radar_model as rmod
    base = {"status": "new", "owner": "a@x.com", "strength": "Medium", "score": 10, "signal_types": ["competitor_job"]}

    def code(c, **kw):
        out = rmod.next_action({**base, **c}, reachable=kw.get("reachable", 0), enrolled=kw.get("enrolled", False), research=kw.get("research"),
                               signals=kw.get("signals", []), refresh_days=30)
        return out and out["code"]
    assert code({"excluded_reason": "Existing customer", "status": "replied"}) is None
    assert code({"status": "replied"}) == "book_meeting"
    assert code({"owner": None, "strength": "Strong"}) == "assign"
    assert code({}, research={"status": "draft"}) == "review_dive"
    assert code({"surge": True, "signals_7d": 4}) == "act_now"
    assert code({"status": "ready"}, research={"status": "approved"}) == "add_people"
    assert code({"status": "ready"}, reachable=2, research={"status": "approved"}) == "start_outreach"
    assert code({"status": "ready"}, reachable=2, enrolled=True, research={"status": "approved", "created_at": "2099-01-01"}) is None
    assert code({"strength": "Strong"}) == "deep_dive"
    old = {"status": "approved", "created_at": "2026-01-01T00:00:00+00:00"}
    assert code({}, research=old, signals=[{"first_seen_at": "2026-06-01T00:00:00+00:00"}]) == "refresh_dive"
    assert code({}, research=old, signals=[{"first_seen_at": "2025-12-01T00:00:00+00:00"}]) is None


def test_todo_view_score_breakdown_and_trend(client):
    todo = client.get("/api/admin/radar/accounts", params={"tab": "todo"}).json()["items"]
    assert todo and all(a["next_action"]["label"] and a["next_action"]["why"] for a in todo)
    s = client.get("/api/admin/radar/summary").json()
    assert s["tabs"]["todo"] == len(todo)
    assert len(s["trend"]["weeks"]) == 12 and all(len(x["counts"]) == 12 for x in s["trend"]["series"])
    for a in client.get("/api/admin/radar/accounts").json()["items"]:
        assert abs(sum((a.get("score_parts") or {}).values()) - (a.get("score") or 0)) <= 1  # the parts explain the score


def test_dns_changes_against_the_last_scan(client, monkeypatch):
    import radar_collectors as rcol
    import radar_run
    import radar_service as rs
    first = rcol.dns_signals_from("Initech", "initech.example", {"MX": ["10 initech-example.mail.mimecast.com."]})
    assert [x["action"] for x in first] == ["present"]  # a first scan is the baseline
    moved = rcol.dns_signals_from("Initech", "initech.example", {"MX": ["10 mx1.pphosted.com."]}, {"MX": ["10 initech-example.mail.mimecast.com."]})
    assert {(x["competitor_key"], x["action"]) for x in moved} == {("proofpoint", "added"), ("mimecast", "removed")}
    assert "new since the last scan" in next(x for x in moved if x["action"] == "added")["title"]
    # A record type whose lookup failed is never read as removed.
    assert rcol.dns_signals_from("Initech", "initech.example", {}, {"MX": ["10 initech-example.mail.mimecast.com."]}) == []

    run(rs.save_radar_settings({"collectors": {"ats": False, "crustdata": False, "dns": True, "news": False, "filings": False, "tenders": False}, "dns_domains": []}))
    scans = iter([{"MX": ["10 initech-example.mail.mimecast.com."]}, {"MX": ["10 mx1.pphosted.com."]}])

    async def records(client_, domain, use_crtsh=True):
        return next(scans) if domain == "initech.example" else {}
    monkeypatch.setattr(radar_run.rc, "dns_records", records)
    one = {"id": "dns-1", "counts": {}, "notes": []}
    run(radar_run.collect(one))
    two = {"id": "dns-2", "counts": {}, "notes": []}
    run(radar_run.collect(two))
    assert two["counts"]["dns_changes"] == 2
    snap = run(_mock_db.radar_dns.find_one({"domain": "initech.example"}))
    assert snap["records"]["MX"] == ["10 mx1.pphosted.com."]
    acc = next(a for a in client.get("/api/admin/radar/accounts", params={"q": "initech"}).json()["items"] if a["name"] == "Initech")
    sigs = client.get(f"/api/admin/radar/accounts/{acc['id']}").json()["signals"]
    assert {(x.get("competitor_key"), x.get("action")) for x in sigs if x["type"] == "dns"} >= {("proofpoint", "added"), ("mimecast", "removed"), ("mimecast", "present")}


def test_rep_scorecard(client):
    import radar_service as rs
    run(rs.recompute_all())
    rows = client.get("/api/admin/radar/reps").json()["reps"]
    team = {m["email"] for m in run(rs.sales_team())}
    assert team <= {r["owner"] for r in rows}  # every rep has a row, even with no accounts yet
    total = run(_mock_db.companies.count_documents({"signal_count": {"$gt": 0}}))
    assert sum(r["accounts"] for r in rows) == total
    for r in rows:
        assert r["contacted"] >= r["replied"] >= r["meetings"] >= r["won"]


def test_keep_awake_pings_the_public_address(monkeypatch):
    import httpx
    import ops
    calls = []

    async def fake_get(self, url, **kw):
        calls.append(url)
        return httpx.Response(200)
    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    monkeypatch.setenv("PUBLIC_API_URL", "https://api.example.test/")
    run(ops.keep_awake())
    assert calls == ["https://api.example.test/api/"]
    monkeypatch.setenv("KEEP_AWAKE", "0")
    run(ops.keep_awake())
    monkeypatch.setenv("KEEP_AWAKE", "1")
    monkeypatch.setenv("PUBLIC_API_URL", "http://127.0.0.1:8001")  # local preview: never ping
    run(ops.keep_awake())
    assert calls == ["https://api.example.test/api/"]


# --- Live intelligence: web research and people -----------------------------------------------

WEB = {
    "programs": [{"url": "https://www.acmemutual.example/news/content-platform", "title": "Acme Mutual consolidates its content platforms", "date": "2026-09-01",
                  "text": "Acme Mutual Insurance is retiring three legacy document systems by 2027 as part of a platform consolidation."}],
    "people": [{"url": "https://www.acmemutual.example/about/leadership", "title": "Leadership | Acme Mutual", "date": "",
                "text": "Dana Whitfield, Executive Vice President and Chief Information Officer, leads technology. Omar Haddad, Director of Records Management."}],
    "news": [{"url": "https://news.example/acme-acquires", "title": "Acme Mutual completes acquisition of Initrode Life", "date": "2026-08-15",
              "text": "The integration of Initrode Life's systems will run through 2027."}],
}


def _fake_web(monkeypatch):
    import radar_intel

    async def web_search(client_, angle, problems=None):
        objective = angle["objective"]
        kind = "people" if "lead IT" in objective else "news" if "Material news" in objective else "programs"
        return [dict(x) for x in WEB[kind]]
    monkeypatch.setenv("PARALLEL_API_KEY", "test-key")
    monkeypatch.setattr(radar_intel, "web_search", web_search)


def test_people_must_be_named_in_a_source():
    import radar_intel
    sources = [{"url": "https://x.example/a", "title": "A", "text": "Dana Whitfield, Executive Vice President and Chief Information Officer"}]
    kept = radar_intel.named_in_sources([{"name": "Dana Whitfield", "title": "EVP & CIO"}, {"name": "Invented Person", "title": "CTO"}], sources)
    assert [p["name"] for p in kept] == ["Dana Whitfield"] and kept[0]["source_url"] == "https://x.example/a"
    found = radar_intel.people_from_text(WEB["people"])
    assert {p["name"] for p in found} == {"Dana Whitfield", "Omar Haddad"}
    assert next(p for p in found if p["name"] == "Omar Haddad")["title"].startswith("Director of Records Management")
    assert next(p for p in found if p["name"] == "Dana Whitfield")["title"] == "Executive Vice President and Chief Information Officer"


def test_deep_dive_uses_live_web_sources_and_keeps_only_named_people(client, monkeypatch):
    import radar_research
    _fake_web(monkeypatch)

    async def model(system, user):
        assert "retiring three legacy document systems" in user and "Dana Whitfield" in user  # the model reads the pages, not just headlines
        n = next(int(x) for x in __import__("re").findall(r"\[(\d+)\] Leadership \| Acme Mutual", user))
        return (json.dumps({"score": 7, "verdict": "Ready for Outreach", "headline": "Acme is retiring legacy document systems [1].", "summary": "Platform consolidation [1].",
                            "trigger": "Retirement by 2027 [1].", "why_solix_wins": "One archive.", "first_touch": {"subject": "Retiring old systems", "body": "Hi Dana,\n\nShort note.\n\nBest,\nPat"},
                            "contacts": [{"name": "Dana Whitfield", "title": "EVP and CIO", "source": n}, {"name": "Made Up", "title": "CTO", "source": n}], "gaps": ""}), "test", "test-model")
    monkeypatch.setattr(radar_research, "_complete", model)
    acme = _company(client, "Acme Mutual")
    client.post(f"/api/admin/radar/accounts/{acme['id']}/research")
    run(radar_research.run_queue(only=None))
    r = run(_mock_db.radar_research.find_one({"company_id": acme["id"], "status": "draft"}, sort=[("created_at", -1)]))
    assert [c["name"] for c in r["contacts"]] == ["Dana Whitfield"]
    assert r["contacts"][0]["source_url"] == "https://www.acmemutual.example/about/leadership"
    kinds = {e["kind"] for e in r["evidence"]}
    assert {"web:programs", "web:people", "web:news"} <= kinds and all("excerpt" in e for e in r["evidence"])
    assert r["source_notes"] == []
    # Approving it puts the verified people on the account, with the page that named them.
    before = run(_mock_db.contacts.count_documents({"company_id": acme["id"]}))
    client.patch(f"/api/admin/radar/research/{r['id']}", json={"action": "approve"})
    dana = run(_mock_db.contacts.find_one({"company_id": acme["id"], "name": "Dana Whitfield"}))
    assert dana and dana["source"] == "research" and dana["source_url"] == "https://www.acmemutual.example/about/leadership"
    assert run(_mock_db.contacts.count_documents({"company_id": acme["id"]})) == before + 1


def test_find_people_merges_web_and_hunter_and_skips_known(client, monkeypatch):
    import radar_intel
    import radar_research
    acme = _company(client, "Acme Mutual")
    monkeypatch.delenv("PARALLEL_API_KEY", raising=False)
    monkeypatch.delenv("HUNTER_API_KEY", raising=False)
    assert client.post(f"/api/admin/radar/accounts/{acme['id']}/people").status_code == 422  # no live source configured
    _fake_web(monkeypatch)
    monkeypatch.setenv("HUNTER_API_KEY", "test-key")

    async def no_model(system, user):
        return None, None, None
    monkeypatch.setattr(radar_research, "_complete", no_model)  # the "Name, Title" rules still find people

    async def hunter(client_, domain, limit=10, company=None):
        return {"people": [{"name": "Priya Nair", "title": "VP Enterprise Architecture", "email": "priya.nair@acmemutual.example", "email_confidence": 94,
                            "email_status": "valid", "linkedin_url": None, "source_url": "https://www.acmemutual.example/blog/archive", "source_title": "acmemutual.example",
                            "via": "hunter"}], "pattern": "{first}.{last}"}
    monkeypatch.setattr(radar_intel, "hunter_people", hunter)
    out = client.post(f"/api/admin/radar/accounts/{acme['id']}/people").json()
    names = [p["name"] for p in out["people"]]
    assert "Omar Haddad" in names and "Priya Nair" in names and "Dana Whitfield" not in names  # Dana is already a contact
    assert out["email_pattern"] == "{first}.{last}" and out["sources_used"]["web_pages"] >= 1
    assert all(p["source_url"] for p in out["people"])
    assert next(p for p in out["people"] if p["name"] == "Priya Nair")["email"] == "priya.nair@acmemutual.example"


def test_search_names_drop_filing_suffixes():
    import radar_intel
    assert [radar_intel.search_name(n) for n in ("8X8 Inc /De/", "Cyabra, Inc.", "Varonis Systems Inc", "Data443 Risk Mitigation, Inc.", "Proofpoint")] == \
        ["8X8", "Cyabra", "Varonis Systems", "Data443 Risk Mitigation", "Proofpoint"]


def test_live_source_failures_are_explained_and_hunter_works_without_a_domain(monkeypatch):
    import httpx
    import radar_intel
    monkeypatch.setenv("PARALLEL_API_KEY", "test-key")
    monkeypatch.setenv("HUNTER_API_KEY", "test-key")
    seen = {}

    def handler(request):
        if request.url.host == "api.parallel.ai":
            seen["queries"] = json.loads(request.content)["search_queries"]
            return httpx.Response(401, json={"code": 16, "message": "Invalid API key (C.1)"})
        seen["hunter"] = dict(request.url.params)
        return httpx.Response(200, json={"data": {"domain": "varonis.example", "pattern": "{f}{last}", "emails": [
            {"value": "jdoe@varonis.example", "first_name": "Jane", "last_name": "Doe", "position": "Chief Information Officer", "confidence": 96,
             "sources": [{"uri": "https://varonis.example/about", "domain": "varonis.example"}], "verification": {"status": "valid"}}]}})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await radar_intel.find_people(c, {"name": "Varonis Systems Inc /De/", "domains": []})
    out = run(go())
    assert out["problems"] == ["Web search failed: Parallel returned 401: Invalid API key (C.1)"]
    assert all(len(q.split()) <= 6 and "Inc" not in q for q in seen["queries"])
    assert seen["hunter"]["company"] == "Varonis Systems" and "domain" not in seen["hunter"]
    assert seen["hunter"]["limit"] == "10"  # Hunter's free plan rejects more than 10
    assert out["hunter_domain"] == "varonis.example" and [p["name"] for p in out["people"]] == ["Jane Doe"]


LEADERSHIP_MD = """# Leadership

![photo](https://northwind.example/img/a.jpg)

### [Grace Okafor](https://northwind.example/team/grace)

Chief Information Officer

### Our Team

Chief Executive Officer office

**Tomas Lindqvist**

Vice President, Enterprise Architecture

We are a leading provider of storage."""


def test_firecrawl_reads_leadership_pages_and_caches(monkeypatch):
    import httpx
    import radar_intel
    import radar_research
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")
    monkeypatch.delenv("PARALLEL_API_KEY", raising=False)
    monkeypatch.delenv("HUNTER_API_KEY", raising=False)

    async def no_model(system, user):
        return None, None, None
    monkeypatch.setattr(radar_research, "_complete", no_model)  # the name-line / title-line rule finds them
    calls = []

    def handler(request):
        calls.append(request.url.path)
        assert request.headers["authorization"] == "Bearer fc-test"
        if request.url.path == "/v2/search":
            q = json.loads(request.content)["query"]
            assert "Inc" not in q
            return httpx.Response(200, json={"success": True, "data": {"web": [
                {"url": "https://northwind.example/about/leadership", "title": "Leadership | Northwind", "description": "Meet our leaders."},
                {"url": "https://news.example/northwind-q3", "title": "Northwind Q3 results", "description": "Revenue grew."}]}})
        assert json.loads(request.content)["url"] == "https://northwind.example/about/leadership"  # only the leadership page is read in full
        return httpx.Response(200, json={"success": True, "data": {"markdown": LEADERSHIP_MD, "metadata": {"title": "Leadership | Northwind"}}})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await radar_intel.find_people(c, {"name": "Northwind Data Inc", "domains": []})
    out = run(go())
    people = {p["name"]: p for p in out["people"]}
    assert set(people) == {"Grace Okafor", "Tomas Lindqvist"} and out["problems"] == []
    assert people["Grace Okafor"]["title"] == "Chief Information Officer"
    assert people["Grace Okafor"]["source_url"] == "https://northwind.example/about/leadership"
    assert calls.count("/v2/search") == 2 and calls.count("/v2/scrape") == 1
    run(go())  # the same search within a day comes from the cache: no new credits spent
    assert calls.count("/v2/search") == 2 and calls.count("/v2/scrape") == 1


def test_parallel_backs_up_firecrawl(monkeypatch):
    import httpx
    import radar_intel
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")
    monkeypatch.setenv("PARALLEL_API_KEY", "test-key")

    def handler(request):
        if request.url.host == "api.firecrawl.dev":
            return httpx.Response(402, json={"success": False, "error": "Insufficient credits to perform this request."})
        return httpx.Response(200, json={"results": [{"url": "https://contoso.example/news/archive", "title": "Contoso retires its archive", "excerpts": ["Done by 2027."]}]})

    async def go():
        problems = []
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await radar_intel.research_sources(c, {"name": "Contoso Unique Ltd", "domains": []}, ["programs"], problems), problems
    found, problems = run(go())
    assert [x["url"] for x in found] == ["https://contoso.example/news/archive"]
    assert problems == ["Web search failed: Firecrawl returned 402: Insufficient credits to perform this request."]


def test_find_email_tries_hunter_first(client, monkeypatch):
    import radar_intel
    acme = _company(client, "Acme Mutual")
    client.patch(f"/api/admin/radar/accounts/{acme['id']}", json={"domains": ["acmemutual.example"]})
    c = client.post("/api/admin/outbound/contacts", json={"name": "Omar Haddad", "title": "Director of Records Management", "company_id": acme["id"]}).json()

    async def finder(client_, domain, first, last):
        assert (domain, first, last) == ("acmemutual.example", "Omar", "Haddad")
        return {"email": "omar.haddad@acmemutual.example", "score": 91, "status": "valid", "source_url": "https://www.acmemutual.example/team"}
    monkeypatch.setattr(radar_intel, "hunter_email", finder)
    monkeypatch.setattr(outbound, "verify_email", _valid)
    r = client.post(f"/api/admin/outbound/contacts/{c['id']}/find-email").json()
    assert r["found"] == "omar.haddad@acmemutual.example"
    saved = run(_mock_db.contacts.find_one({"id": c["id"]}))
    assert saved["email_status"] == "valid" and saved["source_email"] == "hunter" and saved["email_source_url"] == "https://www.acmemutual.example/team"
