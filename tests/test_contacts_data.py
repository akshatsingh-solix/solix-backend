"""Finding the right people and their contact details: SEC officers, Prospeo search/enrich (email + mobile), published
phone numbers, Companies House matching, email-pattern learning, and honest usage counting. No live calls: every
provider answer below is a fixture."""
import os

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "solix_test")
os.environ.setdefault("JWT_SECRET", "test-secret")

import asyncio

import httpx
import pytest

import providers as pv
import radar_sources as rsrc

_loop = asyncio.new_event_loop()


def run(coro):
    return _loop.run_until_complete(coro)


def client_with(routes):
    """An httpx client whose responses come from `routes`: {substring of URL: (status, body)}."""
    def handler(request: httpx.Request):
        for frag, (status, body) in routes.items():
            if frag in str(request.url):
                if isinstance(body, (dict, list)):
                    return httpx.Response(status, json=body)
                return httpx.Response(status, content=body.encode() if isinstance(body, str) else body)
        return httpx.Response(404, json={})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_phones_on_a_page_are_labelled_and_tied_to_the_right_person():
    text = ("Head office: +44 (0)20 7946 0958. Media enquiries: Jane Doe on 020 7946 0123. Founded 1998-2002. "
            "Investor relations +1 212 555 0187")
    got = rsrc.phones_in(text, "United Kingdom", ["Jane Doe"], "https://acme.example/contact")
    assert [(p["number"], p["label"], p["person"]) for p in got] == [
        ("+44 20 7946 0958", "switchboard", None), ("+44 20 7946 0123", "direct", "Jane Doe"), ("+1 212-555-0187", "investors", None)]


def test_companies_house_matches_the_parent_not_a_lookalike():
    items = [{"title": "RS GROUP INTERNATIONAL LIMITED", "company_status": "active"}, {"title": "RS GROUP PLC", "company_status": "active"},
             {"title": "RS GROUP PLC", "company_status": "dissolved"}]
    assert rsrc.ch_match("RS Group plc", items)["title"] == "RS GROUP PLC"
    assert rsrc.ch_match("Barclays", [{"title": "BARCLAYS BANK UK PLC", "company_status": "active"}, {"title": "BARCLAYS PLC", "company_status": "active"}])["title"] == "BARCLAYS PLC"
    assert rsrc.ch_match("Acme", [{"title": "ACME WIDGETS LIMITED", "company_status": "active"}]) is None


def test_companies_house_explains_a_test_environment_key(monkeypatch):
    monkeypatch.setenv("COMPANIES_HOUSE_API_KEY", "sandbox-key")
    c = client_with({"search/companies": (401, {"error": "Invalid Authorization"})})
    with pytest.raises(httpx.HTTPStatusError) as e:
        run(rsrc._ch_company(c, "Tesco PLC"))
    assert "Live environment" in str(e.value) and "REST" in str(e.value)


def test_sec_insider_filings_give_officers_with_exact_titles(monkeypatch):
    rsrc._SEC_TICKERS.update({"by_key": {"acme mutual": 123}, "at": 1e12})
    form4 = """<ownershipDocument><reportingOwner><reportingOwnerId><rptOwnerName>WHITFIELD DANA M</rptOwnerName></reportingOwnerId>
      <reportingOwnerRelationship><isOfficer>1</isOfficer><officerTitle>EVP, Chief Information Officer</officerTitle></reportingOwnerRelationship></reportingOwner>
      <reportingOwner><reportingOwnerId><rptOwnerName>BIG FUND HOLDINGS LLC</rptOwnerName></reportingOwnerId>
      <reportingOwnerRelationship><isTenPercentOwner>1</isTenPercentOwner></reportingOwnerRelationship></reportingOwner></ownershipDocument>"""
    sub = {"filings": {"recent": {"form": ["4", "10-K"], "accessionNumber": ["0001-26-000001", "0001-26-000002"], "primaryDocument": ["xslF345X05/f4.xml", "k.htm"],
                                  "filingDate": ["2026-10-01", "2026-03-01"]}}}
    c = client_with({"submissions/CIK0000000123.json": (200, sub), "/123/000126000001/f4.xml": (200, form4)})
    people = run(rsrc._people_sec(c, "acmemutual.example", "Acme Mutual", "United States"))
    assert [(p["name"], p["title"]) for p in people] == [("Dana Whitfield", "EVP, Chief Information Officer")]
    assert people[0]["via"] == "sec_officers" and people[0]["source_url"].startswith("https://www.sec.gov/Archives/edgar/data/123/")


def test_prospeo_search_and_mobile_enrich(monkeypatch):
    monkeypatch.setenv("PROSPEO_API_KEY", "k")
    search = {"error": False, "results": [{"person": {"person_id": "p1", "full_name": "jordan example", "current_job_title": "Head of Records Management",
                                                     "linkedin_url": "https://www.linkedin.com/in/jordan-example"}}]}
    enrich = {"error": False, "free_enrichment": False, "person": {"full_name": "Jordan Example", "current_job_title": "Head of Records Management",
              "linkedin_url": "https://www.linkedin.com/in/jordan-example", "mobile": {"status": "VERIFIED", "revealed": True, "mobile_international": "+1 415-555-0142"},
              "email": {"status": "VERIFIED", "revealed": True, "email": "Jordan.Example@acmemutual.example"}, "last_job_change_detected_at": None},
              "company": {"domain": "acmemutual.example", "phone_hq": {"phone_hq_international": "+14155550100"}}}
    c = client_with({"search-person": (200, search), "enrich-person": (200, enrich)})
    got = run(rsrc._people_prospeo(c, "acmemutual.example", "Acme Mutual"))
    assert got.units == 1 and got.value[0]["name"] == "Jordan Example" and got.value[0]["title"] == "Head of Records Management"
    hit = run(rsrc._phone_prospeo(c, {"first_name": "Jordan", "last_name": "Example"}, {"domains": ["acmemutual.example"]}))
    assert hit.units == 10 and hit.value["phone"] == "+1 415-555-0142" and hit.value["type"] == "mobile" and hit.value["email"] == "jordan.example@acmemutual.example"
    none = client_with({"enrich-person": (400, {"error": True, "error_code": "NO_MATCH"})})
    assert run(rsrc._phone_prospeo(none, {"first_name": "A", "last_name": "B"}, {"domains": ["x.example"]})).units == 0  # no match, no charge
    broke = client_with({"enrich-person": (400, {"error": True, "error_code": "INSUFFICIENT_CREDITS"})})
    with pytest.raises(pv.OutOfCredits):
        run(rsrc._phone_prospeo(broke, {"first_name": "A", "last_name": "B"}, {"domains": ["x.example"]}))


def test_email_pattern_learning_orders_guesses():
    import outbound
    assert outbound.pattern_of("dana.whitfield@acme.example", "Dana", "Whitfield") == "{first}.{last}"
    assert outbound.pattern_of("dwhitfield@acme.example", "Dana", "Whitfield") == "{f}{last}"
    assert outbound.pattern_of("ceo@acme.example", "Dana", "Whitfield") is None
    assert outbound.guess_emails("Omar", "Haddad", "acme.example", "{f}{last}")[0] == "ohaddad@acme.example"
    assert outbound.guess_emails("Omar", "Haddad", "acme.example")[0] == "omar.haddad@acme.example"


def test_a_provider_without_its_own_allowance_spends_its_pool():
    assert pv.pool_monthly(pv.SPEC_BY_NAME["hunter_company"]) == 25  # Hunter's company lookups use Hunter's 25 searches
    assert pv.pool_monthly(pv.SPEC_BY_NAME["prospeo_people"]) == 75
    assert pv.pool_monthly(pv.SPEC_BY_NAME["wikidata"]) is None


def test_hunter_email_through_the_chain_is_counted_once(monkeypatch):
    import radar_intel
    calls = []

    async def fake_hunter(client, domain, first, last, *, charge=True):
        calls.append(charge)
        return {"email": "a.b@x.example", "score": 90, "status": "valid", "source_url": None}
    monkeypatch.setattr(radar_intel, "hunter_email", fake_hunter)
    run(rsrc._find_hunter(None, "x.example", "A", "B"))
    assert calls == [False]  # the chain's own charge is the only one


def test_phone_chain_has_backups_and_falls_back_to_the_switchboard(monkeypatch):
    names = [s.name for s in pv.SPECS if s.capability == "phone_find"]
    assert len(names) >= 5 and names[:2] == ["site_phones", "prospeo_mobile"]
    hit = rsrc._switchboard("(415) 555-0100", "https://www.sec.gov/x", "United States")
    assert hit["type"] == "switchboard" and hit["phone"] == "+1 415-555-0100"


def test_a_persons_number_is_never_the_switchboard(monkeypatch):
    async def board(client, person, company):
        return {"phone": "+1 415-555-0100", "type": "switchboard", "status": "published", "source_url": None}

    async def nothing(client, person, company):
        return None
    for n in rsrc.PERSONAL_PHONE:
        monkeypatch.setitem(rsrc.PHONE_ADAPTERS, n, nothing)
    monkeypatch.setitem(rsrc.PHONE_ADAPTERS, "site_phones", board)  # even if a personal source returned a main line
    assert set(rsrc.PERSONAL_PHONE).isdisjoint(rsrc.SWITCHBOARD_PHONE)
    assert all(pv.SPEC_BY_NAME[n].capability == "phone_find" for n in rsrc.PERSONAL_PHONE + rsrc.SWITCHBOARD_PHONE)


def test_apollo_webhook_prefers_the_mobile_and_records_do_not_call():
    payload = {"people": [{"phone_numbers": [
        {"raw_number": "+1 202-555-0100", "type_cd": "work_hq", "status_cd": "valid_number", "position": 0},
        {"raw_number": "+1 202-555-0142", "type_cd": "work_direct", "status_cd": "valid_number", "position": 1, "dnc_status_cd": "not_found"},
        {"raw_number": "+1 202-555-0116", "type_cd": "mobile", "status_cd": "valid_number", "position": 2, "dnc_status_cd": "dnc"}]}]}
    best = rsrc.apollo_best_number(payload)
    assert best["phone"] == "+1 202-555-0116" and best["type"] == "mobile" and best["dnc"] is True
    assert rsrc.apollo_best_number({"people": [{"phone_numbers": [{"raw_number": "+1 202-555-0100", "type_cd": "work_hq"}]}]}) is None  # company line only
