"""Tests for the Radar's free data chains (providers.py, radar_sources.py, radar_visitors.py and the new
collectors in radar_run.py). Every provider is mocked with httpx.MockTransport: nothing leaves the process.

Fixtures use invented companies and documentation-style data only."""
import os

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "solix_test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("ADMIN_EMAIL", "admin@example.com")
os.environ.setdefault("ADMIN_PASSWORD", "AdminPass!1")
os.environ["OPS_LOOP"] = "0"

import asyncio
import json
import sys
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient

import database
import server  # noqa: F401  (imports every router)

import providers as pv
import radar_model as rm
import radar_run
import radar_service as rs
import radar_sources as srcs
import radar_visitors
from providers import Billed, Skip, Spec

PATCHED = ("server", "intent", "leads_admin", "admin", "auth", "emailer", "accounts", "delivery", "chat", "chat_leads", "content", "events", "press", "site_settings", "seo", "knowledge")
ALL_KEYS = sorted({v for s in pv.SPECS for v in s.env} | {"JINA_API_KEY", "CERTSPOTTER_API_KEY", "OTX_API_KEY"})
_loop = asyncio.new_event_loop()


def run(coro):
    return _loop.run_until_complete(coro)


def _bind_memory_db():
    if getattr(database.db, "client", None) is database.client:
        database.db = AsyncMongoMockClient()["solix_test"]
    for name in PATCHED:
        mod = sys.modules.get(name)
        if mod is not None and hasattr(mod, "db"):
            mod.db = database.db
    return database.db


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """No provider key from the environment or another test: each test sets exactly the ones it means."""
    for k in ALL_KEYS:
        monkeypatch.delenv(k, raising=False)
    db = _bind_memory_db()
    run(db.provider_usage.delete_many({}))
    run(db.web_cache.delete_many({}))
    run(db.ip_cache.delete_many({}))
    run(db.settings.delete_many({"key": "providers"}))
    import cache
    cache.clear()
    yield


@pytest.fixture(scope="module")
def client():
    _bind_memory_db()
    with TestClient(server.app) as c:
        token = c.post("/api/auth/login", json={"email": os.environ["ADMIN_EMAIL"], "password": os.environ["ADMIN_PASSWORD"]}).json()["access_token"]
        c.headers.update({"Authorization": f"Bearer {token}"})
        yield c


def mock(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _status_error(code):
    req = httpx.Request("GET", "https://provider.example/x")
    return httpx.HTTPStatusError(f"HTTP {code}", request=req, response=httpx.Response(code, request=req, json={"message": "nope"}))


# --- The failover engine ------------------------------------------------------------------------------

def _registry(monkeypatch, *specs):
    reg = list(specs)
    monkeypatch.setattr(pv, "SPECS", reg)
    monkeypatch.setattr(pv, "SPEC_BY_NAME", {s.name: s for s in reg})
    monkeypatch.setitem(pv.CAPABILITIES, "t", {"label": "Test", "why": "tests"})


def test_chain_fails_over_rests_providers_and_respects_allowances(monkeypatch):
    for k in ("A_KEY", "B_KEY", "C_KEY"):
        monkeypatch.setenv(k, "x")
    _registry(monkeypatch, Spec("a", "t", "A", ("A_KEY",)), Spec("b", "t", "B", ("B_KEY",), monthly=2), Spec("c", "t", "C", ("C_KEY",)), Spec("d", "t", "D", ("D_KEY",)))
    calls = []

    def answer(name, exc=None):
        async def call():
            calls.append(name)
            if exc:
                raise exc
            return [name]
        return call
    fns = {"a": answer("a", _status_error(429)), "b": answer("b"), "c": answer("c"), "d": answer("d")}
    assert run(pv.first("t", fns)) == (["b"], "b") and calls == ["a", "b"]
    assert run(pv.first("t", fns)) == (["b"], "b") and calls == ["a", "b", "b"]  # a rests after its 429
    assert run(pv.first("t", fns)) == (["c"], "c")  # b has used its free allowance (2 a month)
    cap = next(c for c in run(pv.status())["capabilities"] if c["key"] == "t")
    rows = {r["name"]: r for r in cap["providers"]}
    assert rows["a"]["reason"] == "rate limited" and rows["a"]["resting_until"]
    assert rows["b"]["left_month"] == 0 and rows["b"]["used_month"] == 2 and not rows["b"]["ready"]
    assert rows["d"]["configured"] is False and rows["d"]["reason"] == "not set up"
    usage = run(database.db.provider_usage.find_one({"pool": "b"}))
    assert usage["units"] == 2  # counted in the database, so a restart or the GitHub runner sees it too


def test_out_of_credits_rests_for_hours_and_skip_is_not_a_failure(monkeypatch):
    monkeypatch.setenv("A_KEY", "x")
    monkeypatch.setenv("B_KEY", "x")
    _registry(monkeypatch, Spec("a", "t", "A", ("A_KEY",)), Spec("b", "t", "B", ("B_KEY",)))

    async def broke():
        raise _status_error(402)

    async def not_mine():
        raise Skip()

    async def ok():
        return Billed(["b"], 3)
    assert run(pv.first("t", {"a": broke, "b": ok})) == (["b"], "b")
    assert pv.cooling(pv.SPEC_BY_NAME["a"]) == "out of credits"
    assert run(pv.usage("b"))["units"] == 3  # an adapter can bill per record returned
    pv.reset()
    assert run(pv.first("t", {"a": not_mine, "b": ok}))[1] == "b" and pv.cooling(pv.SPEC_BY_NAME["a"]) is None


def test_scheduled_work_is_paced_but_people_are_only_capped(monkeypatch):
    monkeypatch.setenv("A_KEY", "x")
    _registry(monkeypatch, Spec("a", "t", "A", ("A_KEY",), monthly=300))
    assert pv.pace(300, 0, datetime(2026, 11, 1, tzinfo=timezone.utc)) == 10
    assert pv.pace(300, 290, datetime(2026, 11, 30, tzinfo=timezone.utc)) == 10
    assert pv.pace(100, 100, datetime(2026, 11, 15, tzinfo=timezone.utc)) == 1
    spec = pv.SPEC_BY_NAME["a"]
    monkeypatch.setattr(pv, "_now", lambda: datetime(2026, 11, 11, 12, tzinfo=timezone.utc))
    pv._usage["a"] = {"month": "2026-11", "units": 200, "days": {"11": 200}, "loaded": 1e18}  # 200 of 300 spent today: over a 1/20 share
    assert run(pv.headroom(spec, bulk=True))[1].startswith("paced")
    assert run(pv.headroom(spec, bulk=False)) == (True, "")


# --- Web search, news ------------------------------------------------------------------------------

def test_search_moves_to_the_next_engine_and_caches(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tv")
    monkeypatch.setenv("SERPER_API_KEY", "sp")
    hosts = []

    def handler(request):
        hosts.append(request.url.host)
        if request.url.host == "api.tavily.com":
            return httpx.Response(429, json={"detail": "rate limit"})
        assert request.headers["x-api-key"] == "sp" and json.loads(request.content)["q"] == '"Contoso" archive'
        return httpx.Response(200, json={"organic": [{"title": "Contoso retires <b>legacy</b> apps", "link": "https://contoso.example/news", "snippet": "By 2027.", "date": "Oct 2, 2026"}]})

    async def go():
        async with mock(handler) as c:
            return await srcs.search(c, '"Contoso" archive', limit=5), await srcs.search(c, '"Contoso" archive', limit=5)
    first, again = run(go())
    assert first == again == [{"url": "https://contoso.example/news", "title": "Contoso retires legacy apps", "date": "2026-10-02", "text": "By 2027."}]
    assert hosts == ["api.tavily.com", "google.serper.dev"]  # Tavily rate limited, Serper answered; the repeat came from the cache


def test_news_falls_back_from_google_to_bing(monkeypatch):
    import seo
    monkeypatch.setenv("RADAR_KEYLESS_SOURCES", "on")

    async def nothing(client, q):
        return []
    monkeypatch.setattr(seo, "_news", nothing)
    rss = """<?xml version="1.0"?><rss><channel><item><title>Contoso Mutual to retire mainframe archive</title>
    <link>http://www.bing.com/news/apiclick.aspx?ref=FexRss&amp;url=https%3a%2f%2fnews.example%2fcontoso-archive&amp;c=1</link>
    <pubDate>Mon, 02 Nov 2026 10:00:00 GMT</pubDate></item></channel></rss>"""
    hosts = []

    def handler(request):
        hosts.append(request.url.host)
        return httpx.Response(200, content=rss.encode(), headers={"content-type": "application/rss+xml"})

    async def go():
        async with mock(handler) as c:
            return await srcs.news(c, '"Contoso Mutual"')
    items = run(go())
    assert items == [{"title": "Contoso Mutual to retire mainframe archive", "url": "https://news.example/contoso-archive", "source": "news.example", "at": "2026-11-02T10:00:00+00:00"}]
    assert hosts == ["www.bing.com"]


# --- Job postings across the market --------------------------------------------------------------------

def test_market_jobs_classify_size_filter_and_phrase_matches(monkeypatch):
    monkeypatch.setenv("THEIRSTACK_API_KEY", "ts")
    monkeypatch.setenv("ADZUNA_APP_ID", "id")
    monkeypatch.setenv("ADZUNA_APP_KEY", "key")
    monkeypatch.setattr(radar_run, "job_terms", lambda settings: ["Documentum"])

    def handler(request):
        if request.url.host == "api.theirstack.com":
            body = json.loads(request.content)
            assert body["job_description_contains_or"] == ["Documentum"] and body["min_employee_count"] == 200
            return httpx.Response(200, json={"data": [
                {"id": 1, "job_title": "ECM Platform Engineer", "url": "https://jobs.example/1", "date_posted": "2026-11-01", "company": "Contoso Mutual", "company_domain": "contoso.example",
                 "description": "You will administer OpenText Documentum D2 and xPlore.", "country_code": "US", "company_object": {"employee_count": 2500}},
                {"id": 2, "job_title": "Developer", "url": "https://jobs.example/2", "date_posted": "2026-11-01", "company": "Tiny Shop", "description": "Documentum experience.",
                 "country_code": "US", "company_object": {"employee_count": 40}}]})
        assert request.url.params["what_phrase"] == "Documentum"
        return httpx.Response(200, json={"results": [{"id": "9", "title": "Records Systems Analyst", "description": "Support our content platforms and retention...",
                                                      "redirect_url": "https://adzuna.example/9", "created": "2026-11-02T08:00:00Z", "company": {"display_name": "Fabrikam Health"},
                                                      "location": {"display_name": "Austin, Travis County", "area": ["US", "Texas", "Travis County", "Austin"]}}]})

    async def go():
        async with mock(handler) as c:
            return await radar_run.market_jobs(c, {"countries": ["United States"], "min_employees": 200, "jobs_per_term": 2}, {"notes": [], "counts": {}})
    sigs = {s["company"]: s for s in run(go())}
    assert set(sigs) == {"Contoso Mutual", "Fabrikam Health"}  # the 40-person company is below mid-market
    assert sigs["Contoso Mutual"]["type"] == "competitor_job" and sigs["Contoso Mutual"]["employees"] == 2500 and "Documentum D2" in sigs["Contoso Mutual"]["evidence_text"]
    fab = sigs["Fabrikam Health"]
    assert fab["competitor_key"] == "opentext-documentum" and fab["evidence_text"].startswith("Listed by adzuna for “Documentum”")
    assert fab["state"] == "Texas" and fab["role_strength"] == "Strong"
    assert run(pv.usage("theirstack"))["units"] == 2  # billed per job returned


def test_ats_xray_finds_boards_and_adds_them_to_the_daily_scan(client, monkeypatch):
    async def boards(client_, term, problems=None):
        return [{"ats": "greenhouse", "board": "contosomutual", "found_url": "https://boards.greenhouse.io/contosomutual/jobs/77", "found_title": "x"}]
    monkeypatch.setattr(srcs, "ats_xray", boards)

    def handler(request):
        if request.url.path == "/v1/boards/contosomutual":
            return httpx.Response(200, json={"name": "Contoso Mutual"})
        return httpx.Response(200, json={"jobs": [{"id": 77, "title": "Documentum Administrator II", "absolute_url": "https://boards.greenhouse.io/contosomutual/jobs/77",
                                                   "first_published": datetime.now(timezone.utc).date().isoformat(), "location": {"name": "Hartford, CT"},
                                                   "content": "&lt;p&gt;Run OpenText Documentum.&lt;/p&gt;"}]})
    before = run(rs.radar_settings())["targets"]
    try:
        async def go():
            settings = {**(await rs.radar_settings()), "xray_terms_per_run": 1}
            async with mock(handler) as c:
                return await radar_run.xray_boards(c, settings, {"notes": [], "counts": {}})
        sigs = run(go())
        assert [(s["company"], s["type"], s["source"]) for s in sigs] == [("Contoso Mutual", "competitor_job", "greenhouse")]
        added = [t for t in run(rs.radar_settings())["targets"] if t.get("source") == "ats_xray"]
        assert added and added[0]["board"] == "contosomutual" and added[0]["company"] == "Contoso Mutual"
        assert run(go()) == []  # already a target: read by the daily ATS scan from now on, not re-added
    finally:
        run(rs.save_radar_settings({"targets": before}))


def test_ats_urls_are_recognised():
    assert srcs.ats_from_url("https://boards.greenhouse.io/acme/jobs/1") == {"ats": "greenhouse", "board": "acme"}
    assert srcs.ats_from_url("https://jobs.eu.lever.co/acme/abc") == {"ats": "lever", "board": "acme", "region": "eu"}
    assert srcs.ats_from_url("https://jobs.smartrecruiters.com/Acme/123")["ats"] == "smartrecruiters"
    assert srcs.ats_from_url("https://acme.wd5.myworkdayjobs.com/en-US/External/job/X")["workday_url"] == "https://acme.wd5.myworkdayjobs.com/External"
    assert srcs.ats_from_url("https://www.acme.example/careers") is None


# --- Company size, HQ, domain: mid-market vs enterprise -----------------------------------------------------

WD_ENTITY = {"claims": {"P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q4830453"}}}}],
                        "P1128": [{"mainsnak": {"datavalue": {"value": {"amount": "+12000", "unit": "1"}}}}],
                        "P856": [{"mainsnak": {"datavalue": {"value": "https://www.contoso.example/"}}}],
                        "P17": [{"mainsnak": {"datavalue": {"value": {"id": "Q30"}}}}], "P452": [{"mainsnak": {"datavalue": {"value": {"id": "Q43183"}}}}]},
             "labels": {"en": {"value": "Contoso Holdings"}}, "descriptions": {"en": {"value": "American insurance company"}}}


def _firmo_handler(request):
    if request.url.host == "www.wikidata.org":
        p = request.url.params
        if p["action"] == "wbsearchentities":
            return httpx.Response(200, json={"search": [{"id": "Q99"}]})
        if p.get("props") == "labels":
            return httpx.Response(200, json={"entities": {"Q30": {"labels": {"en": {"value": "United States of America"}}}, "Q43183": {"labels": {"en": {"value": "insurance"}}}}})
        return httpx.Response(200, json={"entities": {"Q99": WD_ENTITY}})
    if request.url.host == "api.gleif.org":
        return httpx.Response(200, json={"data": [{"id": "LEI0000000000000TEST", "attributes": {"entity": {
            "legalName": {"name": "Contoso Holdings, Inc."}, "status": "ACTIVE",
            "headquartersAddress": {"city": "San Francisco", "region": "US-CA", "country": "US", "postalCode": "94105"}}}}]})
    return httpx.Response(404, json={})


def test_enrichment_sizes_locates_and_routes_an_account(client, monkeypatch):
    monkeypatch.setenv("RADAR_KEYLESS_SOURCES", "on")
    monkeypatch.setitem(srcs.FIRMO_ADAPTERS, "sec", lambda *a: _skip())
    c = run(rs.get_or_create_company("Contoso Holdings", source="manual"))

    async def go():
        async with mock(_firmo_handler) as http:
            return await radar_run.enrich_company(http, c)
    sets = run(go())
    assert sets["employees"] == 12000 and sets["industry"] == "insurance"
    assert sets["hq"]["state"] == "CA" and sets["hq"]["zip"] == "94105"
    acct = run(rs.recompute(c["id"]))
    assert acct["segment"] == "enterprise" and acct["domains"] == ["contoso.example"]
    assert acct["territory"] == "USA" and acct["route_rep"] == "Hameed Hemmat"  # Northern California by HQ ZIP
    assert {s["via"] for s in sets["firmographics"]["sources"]} == {"wikidata", "gleif"}
    # A person's value is never overwritten.
    client.patch(f"/api/admin/radar/accounts/{c['id']}", json={"employees": 450})
    assert run(rs.recompute(c["id"]))["segment"] == "mid_market"
    seg = client.get("/api/admin/radar/accounts", params={"segment": "mid_market", "q": "Contoso Holdings"}).json()["items"]
    assert [x["id"] for x in seg] == [c["id"]]


async def _skip():
    raise Skip()


def test_segments_from_headcount_or_revenue():
    assert rm.segment_of(1000) == "enterprise" and rm.segment_of(999) == "mid_market" and rm.segment_of(199) == "smb"
    assert rm.segment_of(None, "$1B-$10B") == "enterprise" and rm.segment_of(None, "$250M") == "mid_market" and rm.segment_of() is None
    assert rm.segment_of(300, cfg={"mid_market_min": 500}) == "smb"
    assert srcs.employees_of("1001-5000") == 1001 and srcs.employees_of("10K+") == 10000 and srcs.employees_of(None) is None


# --- People who work there now -------------------------------------------------------------------------------

def test_people_merge_across_hunter_linkedin_xray_and_companies_house(monkeypatch):
    import radar_intel
    import radar_research
    monkeypatch.setenv("SERPER_API_KEY", "sp")
    monkeypatch.setenv("HUNTER_API_KEY", "hk")
    monkeypatch.setenv("COMPANIES_HOUSE_API_KEY", "ch")

    async def no_model(system, user):
        return None, None, None
    monkeypatch.setattr(radar_research, "_complete", no_model)

    def handler(request):
        host = request.url.host
        if host == "google.serper.dev":
            q = json.loads(request.content)["q"]
            if q.startswith("site:linkedin.com/in"):
                return httpx.Response(200, json={"organic": [
                    {"title": "Dana Whitfield - Chief Information Officer - Acme Mutual | LinkedIn", "link": "https://www.linkedin.com/in/dana-whitfield-0001"},
                    {"title": "Former Person - Director of IT - Initrode | LinkedIn", "link": "https://www.linkedin.com/in/former-person"}]})
            return httpx.Response(200, json={"organic": []})
        if host == "api.hunter.io":
            return httpx.Response(200, json={"data": {"domain": "acmemutual.example", "pattern": "{first}.{last}", "emails": [
                {"value": "dana.whitfield@acmemutual.example", "first_name": "Dana", "last_name": "Whitfield", "position": "CIO", "confidence": 95,
                 "sources": [{"uri": "https://acmemutual.example/about", "still_on_page": True}]}]}})
        if request.url.path.endswith("/officers"):
            return httpx.Response(200, json={"items": [{"name": "WHITFIELD, Dana Marie", "officer_role": "director", "occupation": "Chief Information Officer"},
                                                       {"name": "GONE, Old", "officer_role": "director", "resigned_on": "2024-01-01"}]})
        return httpx.Response(200, json={"items": [{"title": "ACME MUTUAL LIMITED", "company_number": "01234567", "company_status": "active"}]})

    async def go():
        async with mock(handler) as c:
            return await radar_intel.find_people(c, {"name": "Acme Mutual", "domains": ["acmemutual.example"]})
    out = run(go())
    assert [p["name"] for p in out["people"]] == ["Dana Whitfield"]  # one person, three sources; the ex-employee and the resigned director are out
    dana = out["people"][0]
    assert dana["email"] == "dana.whitfield@acmemutual.example" and dana["linkedin_url"] == "https://www.linkedin.com/in/dana-whitfield-0001"
    assert dana["current"] is True and set(dana["also_via"]) == {"linkedin_xray", "companies_house"}
    assert out["sources_used"]["linkedin_xray"] == 1 and out["sources_used"]["other"] == 1


# --- Email verification and finding ------------------------------------------------------------------------------

def test_verification_moves_on_when_a_verifier_runs_out(monkeypatch):
    monkeypatch.setenv("MAILBOXVALIDATOR_API_KEY", "m")
    monkeypatch.setenv("ZEROBOUNCE_API_KEY", "z")

    def handler(request):
        if request.url.host == "api.mailboxvalidator.com":
            return httpx.Response(200, json={"error": {"error_code": 10004, "error_message": "Insufficient credits."}})
        return httpx.Response(200, json={"status": "valid", "sub_status": ""})

    async def go():
        async with mock(handler) as c:
            return await srcs.verify_email(c, "omar.haddad@acmemutual.example")
    assert run(go()) == {"status": "valid", "detail": "ZeroBounce: valid"}
    assert pv.cooling(pv.SPEC_BY_NAME["mailboxvalidator"]) == "out of credits"


def test_email_finder_chain_after_hunter(monkeypatch):
    monkeypatch.setenv("TOMBA_KEY", "k")
    monkeypatch.setenv("TOMBA_SECRET", "s")

    def handler(request):
        assert request.headers["x-tomba-key"] == "k" and request.url.params["first_name"] == "Omar"
        return httpx.Response(200, json={"data": {"email": "Omar.Haddad@acmemutual.example", "score": 88, "verification": {"status": "valid"},
                                                  "sources": [{"uri": "https://acmemutual.example/team"}]}})

    async def go():
        async with mock(handler) as c:
            return await srcs.find_email(c, "acmemutual.example", "Omar", "Haddad", skip={"hunter_find"})
    hit, via = run(go())
    assert via == "tomba_find" and hit["email"] == "omar.haddad@acmemutual.example" and hit["source_url"] == "https://acmemutual.example/team"


# --- Website visitors -> organisations ---------------------------------------------------------------------------

def test_visitor_networks_skip_isps_and_never_store_the_address(monkeypatch):
    monkeypatch.setenv("IPINFO_TOKEN", "t")
    monkeypatch.setenv("RADAR_KEYLESS_SOURCES", "on")
    hosts = []

    def handler(request):
        hosts.append(request.url.host)
        if request.url.host == "api.ipinfo.io":
            return httpx.Response(200, json={"ip": "8.8.4.4", "asn": "AS7922", "as_name": "Comcast Cable Communications, LLC", "as_domain": "comcast.net", "country": "United States"})
        return httpx.Response(200, json={"name": "CONTOSO-NET", "country": "US", "entities": [{"roles": ["registrant"], "vcardArray": ["vcard", [
            ["fn", {}, "text", "Contoso Mutual Insurance Company"], ["email", {}, "text", "noc@contoso.example"]]]}]})

    async def go():
        async with mock(handler) as c:
            return await srcs.ip_company(c, "8.8.4.4"), await srcs.ip_company(c, "8.8.4.4")
    org, again = run(go())
    assert org == again and org["domain"] == "contoso.example" and org["type"] == "business" and org["via"] == "rdap"
    assert hosts == ["api.ipinfo.io", "rdap.org"]  # the ISP answer was not accepted; the repeat came from the cache
    cached = run(database.db.ip_cache.find_one({}, {"_id": 0}))
    assert "8.8.4.4" not in json.dumps(cached) and cached["key"] == srcs.ip_hash("8.8.4.4")
    assert run(srcs.ip_company(httpx.AsyncClient(), "10.0.0.1")) is None and run(srcs.ip_company(httpx.AsyncClient(), "testclient")) is None


def test_tracking_starts_identification_with_the_forwarded_address(client, monkeypatch):
    seen = []
    monkeypatch.setattr(radar_visitors, "schedule", lambda vid, ip, checked=None: seen.append((vid, ip, checked)))
    r = client.post("/api/track", content=json.dumps({"visitor_id": "visitor-abc-123", "session_id": "s1", "events": [{"type": "page_view", "path": "/products/application-retirement"}]}),
                    headers={"X-Forwarded-For": "8.8.4.4, 10.0.0.2", "Content-Type": "text/plain"})
    assert r.status_code == 204 and seen == [("visitor-abc-123", "8.8.4.4", None)]


def test_identified_visits_become_an_account_signal(client, monkeypatch):
    async def org(client_, ip):
        return {"org": "Northwind Traders", "domain": "northwind.example", "type": "business", "country": "United States", "via": "ipapi_is"}
    monkeypatch.setattr(srcs, "ip_company", org)
    now = datetime.now(timezone.utc).isoformat()
    db = database.db
    for vid in ("v-north-1", "v-north-2"):
        run(db.visitors.update_one({"id": vid}, {"$set": {"id": vid, "last_seen": now, "pages_count": 4, "sessions": 1, "product_scores": {"application-retirement": 9}}}, upsert=True))
        assert run(radar_visitors.identify(vid, "8.8.4.4"))["domain"] == "northwind.example"
    run(db.visitors.insert_one({"id": "v-isp", "last_seen": now, "org": {"org": "Some ISP", "domain": "isp.example", "type": "isp"}}))
    run(db.visitors.insert_one({"id": "v-self", "last_seen": now, "org": {"org": "Solix", "domain": "solix.com", "type": "business"}}))
    stored = run(db.visitors.find_one({"id": "v-north-1"}, {"_id": 0}))
    assert stored["org"]["domain"] == "northwind.example" and "8.8.4.4" not in json.dumps(stored)
    sigs = run(radar_visitors.signals(30))
    assert [s["domain"] for s in sigs] == ["northwind.example"]
    assert sigs[0]["type"] == "visit" and "2 visitors" in sigs[0]["title"] and "Application Retirement" in sigs[0]["evidence_text"]
    saved = run(rs.radar_settings())["collectors"]
    try:
        run(rs.save_radar_settings({"collectors": {k: k == "visitors" for k in saved}}))
        rr = {"id": "test-run", "counts": {}, "notes": []}
        touched = run(radar_run.collect(rr))
        run(rs.recompute_many(touched))
    finally:
        run(rs.save_radar_settings({"collectors": saved}))
    acct = client.get("/api/admin/radar/accounts", params={"q": "northwind.example", "tab": "website"}).json()["items"]
    assert acct and "visit" in acct[0]["signal_types"] and acct[0]["domains"] == ["northwind.example"]


# --- Admin: the Data sources screen ------------------------------------------------------------------------------

def test_data_sources_screen_lists_every_chain_and_can_switch_one_off(client):
    st = client.get("/api/admin/radar/providers").json()
    caps = {c["key"]: c for c in st["capabilities"]}
    assert set(caps) == set(pv.CAPABILITIES)
    assert all(len(c["providers"]) >= 4 for c in caps.values())  # at least four sources behind every Radar function
    assert all(len(c["providers"]) >= 5 for k, c in caps.items() if k != "public_sector")
    st = client.put("/api/admin/radar/providers", json={"disabled": ["tavily"], "order": {"web_search": ["serper", "tavily"]}}).json()
    web = next(c for c in st["capabilities"] if c["key"] == "web_search")["providers"]
    assert web[0]["name"] == "serper" and next(p for p in web if p["name"] == "tavily")["reason"] == "switched off"
    client.put("/api/admin/radar/providers", json={"disabled": [], "order": {}})
    assert client.post("/api/admin/radar/providers/tavily/test").json()["detail"].startswith("Not set up: TAVILY_API_KEY")
    assert client.post("/api/admin/radar/providers/nope/test").json() == {"ok": False, "detail": "Unknown provider"}
    meta = client.get("/api/admin/radar/meta").json()
    assert meta["live_sources"]["chains"]["web_search"] == 0 and {s["key"] for s in meta["segments"]} == {"enterprise", "mid_market", "smb"}
