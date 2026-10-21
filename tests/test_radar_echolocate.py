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
