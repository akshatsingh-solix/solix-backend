"""Phases 2-5: data accuracy (data_quality.py), product fit (product_fit.py), persona messaging (messaging.py) and the
weekly self-review (learning.py). In-process against an in-memory Mongo; invented companies and people only."""
import os

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "solix_test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("ADMIN_EMAIL", "admin@example.com")
os.environ.setdefault("ADMIN_PASSWORD", "AdminPass!1")
os.environ["OPS_LOOP"] = "0"
for k in ("GROQ_API_KEY", "GEMINI_API_KEY", "MISTRAL_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY", "SOL_CUSTOM_BASE_URL"):
    os.environ.pop(k, None)

import asyncio
import sys
import uuid

import pytest
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient

import cache
import data_quality as dq
import database
import learning
import messaging
import product_fit
import server  # noqa: F401

PATCHED = ("server", "intent", "leads_admin", "admin", "auth", "emailer", "accounts", "delivery", "chat", "chat_leads", "content", "events", "press", "site_settings", "seo", "knowledge")
_loop = asyncio.new_event_loop()


def run(coro):
    return _loop.run_until_complete(coro)


@pytest.fixture(scope="module")
def client():
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


def obs(value, via, match="domain"):
    return dq.observation(value, via, match)


def _company(name, **fields):
    import radar_model as rm
    import radar_service as rs

    async def go():
        c = await rs.get_or_create_company(name, domain=name.lower().replace(" ", "") + ".example", source="manual")
        if fields:
            await database.db.companies.update_one({"id": c["id"]}, {"$set": fields})
        await database.db.company_signals.insert_one({"id": str(uuid.uuid4()), "dedupe_key": str(uuid.uuid4()), "company_id": c["id"], "type": "problem_job", "title": "Records Retention Lead",
                                                      "problem_term": "retention", "role_strength": "Strong", "posted_at": "2026-11-01", "first_seen_at": "2026-11-01"})
        cache.bump("radar_ctx")
        await rs.recompute(c["id"])
        return c["id"]
    return run(go())


# --- Phase 2: accuracy -------------------------------------------------------------------------------------------

def test_agreeing_sources_verify_and_conflicts_are_flagged():
    two = dq.field_view("industry", [obs("Commercial Banking", "sec"), obs("Banking", "wikidata")])
    assert two["status"] == "verified" and two["p"] >= dq.TARGET
    one = dq.field_view("industry", [obs("Banking", "pdl_company")])
    assert one["status"] in ("unverified", "likely") and one["p"] < dq.TARGET
    split = dq.field_view("industry", [obs("Banking", "pdl_company"), obs("Grocery retail", "hunter_company")])
    assert split["status"] == "conflicting" and split["conflicts"]
    assert dq.field_view("country", [obs("US", "sec"), obs("United States", "gleif")])["status"] == "verified"
    assert dq.field_view("revenue", [obs(2_880_000_000, "sec"), obs(3_000_000_000, "wikidata")])["status"] == "verified"  # within 2x is the same company


def test_measured_accuracy_lowers_trust():
    good = dq.trust_of("wikidata", {})
    bad = dq.trust_of("wikidata", {"industry:wikidata": {"n": 60, "correct": 30}}, "industry")
    assert bad < good - 0.2  # 50% right in 60 checks outweighs the prior
    assert dq.trust_of("wikidata", {"industry:wikidata": {"n": 2, "correct": 0}}, "industry") > 0.7  # two checks barely move it
    lo, hi = dq.wilson(98, 100)
    assert lo < 0.98 < hi  # 98 of 100 doesn't prove 98%


def test_account_quality_confirm_correct_and_report(client):
    cid = _company("Tallowind Bank", facts={"industry": [obs("Banking", "pdl_company")], "country": [obs("United States", "sec"), obs("US", "gleif")]},
                   size_candidates=[{"value": 4200, "via": "pdl_company", "match": "domain", "confidence": "medium", "at": "2026-10-01"}])
    d = client.get(f"/api/admin/radar/accounts/{cid}").json()
    q = d["quality"]
    assert q["fields"]["country"]["status"] == "verified" and q["fields"]["industry"]["status"] != "verified" and not q["meets_target"]
    assert d["account"]["data_quality"]["score"] == q["score"] and d["account"]["industry_slug"] == "financial-services"
    # A person confirms the industry: verified, and People Data Labs is scored right.
    r = client.post(f"/api/admin/radar/accounts/{cid}/confirm", json={"fields": ["industry", "employees"]})
    assert r.status_code == 200, r.text
    assert r.json()["quality"]["fields"]["industry"]["status"] == "verified" and r.json()["quality"]["fields"]["employees"]["status"] == "verified"
    rows = run(database.db.accuracy_log.find({"company_id": cid}).to_list(50))
    assert any(x["via"] == "pdl_company" and x["field"] == "industry" and x["correct"] for x in rows)
    assert any(x["via"] == "shown" and x.get("shown") is True for x in rows)
    # A person corrects the country: the sources that said otherwise are scored wrong.
    client.patch(f"/api/admin/radar/accounts/{cid}", json={"hq": {"country": "Canada"}})
    rows = run(database.db.accuracy_log.find({"company_id": cid, "field": "country"}).to_list(50))
    assert rows and all(not x["correct"] for x in rows if x["via"] in ("sec", "gleif"))
    rep = client.get("/api/admin/radar/accuracy").json()
    assert rep["overall"]["n"] >= 3 and rep["target"] == 0.98 and any(s["via"] == "pdl_company" for s in rep["sources"])
    assert rep["accounts"]["total"] >= 1
    audit = client.get("/api/admin/radar/accuracy/audit", params={"n": 50}).json()["items"]
    assert not any(a["company_id"] == cid and a["field"] in ("industry", "country", "employees") for a in audit)  # confirmed values aren't re-audited
    assert client.get("/api/admin/radar/accounts", params={"industry": "financial-services"}).json()["total"] >= 1


def test_outreach_gate_blocks_conflicting_facts(client):
    import compliance
    cid = _company("Splitfact Holdings", facts={"industry": [obs("Banking", "pdl_company"), obs("Grocery retail", "hunter_company")]})
    company = run(database.db.companies.find_one({"id": cid}, {"_id": 0}))
    assert "industry" in company["data_quality"]["conflicting"]
    g = run(compliance.gate({"email": "pat@splitfact.example", "email_status": "valid", "name": "Pat", "title": "CIO", "lawful_basis": "legitimate_interest"}, company))
    assert any("Sources disagree on the industry" in r for r in g["reasons"])
    g = run(compliance.gate({"email": "pat@splitfact.example", "email_status": "valid", "name": "Pat", "title": "CIO", "lawful_basis": "legitimate_interest"}, company,
                            {**run(compliance.outbound_settings()), "data_gate": "off"}))
    assert not any("Sources disagree" in r for r in g["reasons"]) and any("Sources disagree" in w for w in g["warnings"])


# --- Phase 3: product fit ----------------------------------------------------------------------------------------

def test_product_fit_leads_with_displacement_and_never_repitches_customers():
    fit = product_fit.recommend({"industry": "Insurance", "segment": "enterprise", "competitor_keys": ["opentext-infoarchive"]},
                                [{"type": "competitor_job", "competitor_key": "opentext-infoarchive", "title": "Archive engineer to decommission legacy policy systems"}])
    lead = fit["plays"][0]
    assert lead["kind"] == "lead" and lead["product"] in ("application-retirement", "enterprise-archiving")
    assert any("OpenText InfoArchive" in r for r in lead["reasons"]) and fit["summary"].startswith("Lead with")
    cust = product_fit.recommend({"industry": "Commercial Banking", "is_customer": True, "products_owned": ["email-archiving"], "segment": "enterprise"}, [])
    assert all(p["product"] != "email-archiving" for p in cust["plays"])
    assert {p["kind"] for p in cust["plays"]} <= {"up_sell", "cross_sell"} and any(p["kind"] == "up_sell" for p in cust["plays"])
    tuned = product_fit.recommend({"industry": "Insurance"}, [], weights={"industry:insurance:mainframe-archiving": 1.5})
    assert tuned["plays"][0]["product"] == "mainframe-archiving"  # learned weights move the ranking


def test_products_owned_and_fit_on_the_account(client):
    cid = _company("Orchardline Insurance", industry="Insurance", is_customer=True)
    r = client.patch(f"/api/admin/radar/accounts/{cid}", json={"products_owned": ["application-retirement", "not-a-product"]})
    assert r.status_code == 200 and r.json()["products_owned"] == ["application-retirement"]
    d = client.get(f"/api/admin/radar/accounts/{cid}").json()
    assert d["fit"]["customer"] and all(p["product"] != "application-retirement" for p in d["fit"]["plays"])
    assert d["account"]["pitch_product"] and d["account"]["pitch_summary"]


# --- Phase 4: persona messaging --------------------------------------------------------------------------------

def test_brief_and_opener_follow_persona_and_never_name_competitors():
    co = {"name": "Acme Mutual Insurance", "industry": "Insurance", "competitor_keys": ["opentext-infoarchive"]}
    sig = {"type": "competitor_job", "competitor_key": "opentext-infoarchive", "title": "InfoArchive administrator"}
    cio = messaging.brief({"title": "Chief Information Officer"}, co, signal=sig)
    rec = messaging.brief({"title": "Records Manager"}, co, signal=sig)
    assert cio["persona"] == "it_exec" and cio["seniority"] == "executive" and rec["persona"] == "compliance"
    assert cio["pain"] != rec["pain"] and cio["explore_cta"] != rec["explore_cta"]
    for b in (cio, rec):
        text = messaging.rule_opener(b)
        assert "InfoArchive" not in text and "Acme Mutual" in text
        assert messaging.check(text, b)["ok"]
    bad = messaging.check("I hope this finds you well. Can we book a 30-minute demo call? It's a game-changer.", cio)
    assert not bad["ok"] and any("stock phrase" in i for i in bad["issues"]) and any("meeting" in i for i in bad["issues"])


def test_contact_brief_endpoint(client):
    cid = _company("Brightwater Health", industry="Hospitals and Health Care")
    c = client.post("/api/admin/outbound/contacts", json={"name": "Robin Example", "title": "VP Information Technology", "email": "robin@brightwaterhealth.example",
                                                         "company_id": cid})
    assert c.status_code == 201, c.text
    d = client.get(f"/api/admin/outbound/contacts/{c.json()['id']}/brief").json()
    assert d["brief"]["persona"] == "it_exec" and d["brief"]["industry"] == "healthcare"
    assert d["steps"][0]["intent"] == "exploration" and d["steps"][2]["intent"] == "buying"
    assert "demo" not in d["steps"][0]["body"].lower() and "Brightwater" in d["opener"]
    assert d["quality"]["score"] >= 60


# --- Phase 5: learning -------------------------------------------------------------------------------------------

def _rows(industry, product, n, pos):
    return [{"industry_slug": industry, "pitch_product": product, "positive": i < pos, "replied": i < pos} for i in range(n)]


def test_proposals_are_bounded_and_need_evidence():
    rows = _rows("insurance", "mainframe-archiving", 20, 8) + _rows("retail", "database-archiving", 40, 1) + _rows("energy", "file-archiving", 5, 5)
    props = {p["key"]: p for p in learning.proposals(rows, {})}
    up, down = props["industry:insurance:mainframe-archiving"], props["industry:retail:database-archiving"]
    assert up["after"] == 1.2 and down["after"] == 0.8  # at most 0.2 a week
    assert "industry:energy:file-archiving" not in props  # 5 sends isn't enough
    again = {p["key"]: p for p in learning.proposals(rows, {"industry:insurance:mainframe-archiving": 1.5})}
    assert "industry:insurance:mainframe-archiving" not in again or again["industry:insurance:mainframe-archiving"]["after"] <= 1.5


def test_weekly_review_tunes_logs_and_reverts(client):
    db = database.db
    for i in range(10):
        eid = str(uuid.uuid4())
        run(db.enrollments.insert_one({"id": eid, "contact_id": f"learn-c{i}", "status": "active", "created_at": "2026-11-10T00:00:00+00:00", "persona": "compliance",
                                       "industry_slug": "insurance", "pitch_product": "mainframe-archiving", "opener_quality": 85, "opener_provider": "rules"}))
        if i < 6:
            run(db.reply_log.insert_one({"enrollment_id": eid, "contact_id": f"learn-c{i}", "label": "interested", "received_at": "2026-11-12T00:00:00+00:00"}))
    for i in range(20):
        run(db.enrollments.insert_one({"id": str(uuid.uuid4()), "contact_id": f"learn-d{i}", "status": "active", "created_at": "2026-11-10T00:00:00+00:00", "persona": "it_exec",
                                       "industry_slug": "retail", "pitch_product": "database-archiving", "opener_quality": 50, "opener_provider": "rules"}))
    r = client.post("/api/admin/intelligence/review")
    assert r.status_code == 200, r.text
    rev = r.json()
    assert rev["applied"] >= 1 and rev["insights"] and rev["learned"]["outcomes"] >= 30
    assert any(i["kind"] == "win" and "compliance" in i["title"] for i in rev["insights"])
    import radar_service as rs
    w = run(rs.radar_settings())["product_weights"]
    assert w["industry:insurance:mainframe-archiving"] == 1.2
    intel = client.get("/api/admin/intelligence").json()
    change = next(t for t in intel["tuning"] if t["key"] == "industry:insurance:mainframe-archiving")
    assert client.post(f"/api/admin/intelligence/tuning/{change['id']}/revert").json()["reverted"]
    assert "industry:insurance:mainframe-archiving" not in run(rs.radar_settings())["product_weights"]
    # Autotune off: the next review proposes, it doesn't apply.
    client.put("/api/admin/intelligence/settings", json={"autotune": False})
    rev2 = client.post("/api/admin/intelligence/review").json()
    assert rev2["applied"] == 0 and rev2["proposals"]
    assert any(i["title"].startswith("Suggested:") for i in rev2["insights"])
    assert client.post("/api/admin/intelligence/apply").json()["applied"] >= 1
    client.put("/api/admin/intelligence/settings", json={"autotune": True})
    # Leave the shared in-memory database as other modules expect it.
    run(db.enrollments.delete_many({"contact_id": {"$regex": "^learn-"}}))
    run(db.reply_log.delete_many({"contact_id": {"$regex": "^learn-"}}))
    run(rs.save_radar_settings({"product_weights": {}}))
