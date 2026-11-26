"""www.solix.com integration: the WordPress form intake (key, observe-only, visitor linking), the GTM tag, solix.com
page paths in scoring, and Sol's solix.com knowledge sync. In-process against an in-memory Mongo; the live site is
never called (its REST API is stubbed). Invented people only."""
import asyncio
import os

import pytest
from fastapi.testclient import TestClient


_loop = asyncio.new_event_loop()


def run(coro):
    return _loop.run_until_complete(coro)


@pytest.fixture(scope="module")
def app(isolated_app):
    return isolated_app


@pytest.fixture(scope="module")
def site_knowledge(isolated_app):
    import site_knowledge as sk  # reads database.db when it runs, so it uses this module's in-memory database
    return sk


KEY = "test-wp-intake-key"
URL = "/api/integrations/wordpress/submissions"


@pytest.fixture
def wp(app, monkeypatch):
    monkeypatch.setenv("WP_INTAKE_SECRET", KEY)
    monkeypatch.delenv("WP_INTAKE_ALERTS", raising=False)
    return TestClient(app[0].app)


# --- form intake --------------------------------------------------------------------------------------------------

def test_intake_needs_a_configured_key(app, monkeypatch):
    anon = TestClient(app[0].app)
    monkeypatch.delenv("WP_INTAKE_SECRET", raising=False)
    assert anon.post(URL, json={"type": "demo", "email": "a@example.com"}).status_code == 503
    monkeypatch.setenv("WP_INTAKE_SECRET", KEY)
    assert anon.post(URL, json={"type": "demo", "email": "a@example.com"}).status_code == 401
    assert anon.post(URL, json={"type": "demo", "email": "a@example.com"}, headers={"X-MarkHub-Key": "wrong"}).status_code == 401
    # The plugin's "Test connection": a right key with an empty body is a 422, and nothing is created.
    assert anon.post(URL, json={}, headers={"X-MarkHub-Key": KEY}).status_code == 422


def test_solixcom_leads_are_observed_without_a_second_alert_or_email(app, wp, monkeypatch):
    _, db = app
    import server
    calls = []

    async def fake_notify(doc):
        calls.append(("alert", doc["email"]))

    async def fake_deliver(doc, base):
        calls.append(("deliver", doc["email"]))

    monkeypatch.setattr(server, "notify_lead", fake_notify)
    monkeypatch.setattr(server, "deliver_for_submission", fake_deliver)
    r = wp.post(URL, headers={"X-MarkHub-Key": KEY}, json={
        "type": "download", "form": "gated-download", "email": "dana.reyes@example.org", "name": "Dana Reyes", "company": "Example Org",
        "resource": "Enterprise Ai A Fourth Generation Data Platform", "resource_slug": "enterprise-ai-a-fourth-generation-data-platform",
        "source_page": "https://www.solix.com/resources/lg/white-papers/enterprise-ai-a-fourth-generation-data-platform/",
        "client_ip": "198.51.100.20"})
    assert r.status_code == 201, r.text
    stored = run(db.submissions.find_one({"email": "dana.reyes@example.org"}))
    assert stored["source"] == "solix.com" and stored["form"] == "gated-download" and "client_ip" not in stored
    assert calls == []  # solix.com already alerted sales and emailed the white paper
    # A lead is still created and scored from it.
    assert run(db.leads.find_one({"email": "dana.reyes@example.org"})) is not None

    monkeypatch.setenv("WP_INTAKE_ALERTS", "1")
    wp.post(URL, headers={"X-MarkHub-Key": KEY}, json={"type": "demo", "form": "request-demo", "email": "sam.ortiz@example.org"})
    assert calls == [("alert", "sam.ortiz@example.org")]  # the alert can be switched on; the download email never is


def test_intake_links_the_lead_to_the_visitors_browsing(app, wp):
    _, db = app
    anon = TestClient(app[0].app)
    vid = "vis-solixcom-0001"
    beacon = {"visitor_id": vid, "session_id": "s1", "context": {"site": "www.solix.com", "landing": "/products/application-retirement-solution/"},
              "events": [{"type": "page_view", "path": "/products/application-retirement-solution/"},
                         {"type": "engaged", "path": "/products/application-retirement-solution/"}]}
    assert anon.post("/api/track", content=__import__("json").dumps(beacon), headers={"Content-Type": "text/plain", "X-Forwarded-For": "192.0.2.80"}).status_code == 204
    visitor = run(db.visitors.find_one({"id": vid}))
    assert visitor["product_scores"].get("application-retirement", 0) > 0
    r = wp.post(URL, headers={"X-MarkHub-Key": KEY}, json={"type": "demo", "form": "request-demo", "email": "lee.chen@example.net",
                                                           "visitor_id": vid, "source_page": "https://www.solix.com/products/request-demo/"})
    assert r.status_code == 201
    lead = run(db.leads.find_one({"email": "lee.chen@example.net"}))
    assert vid in (lead.get("visitor_ids") or [])
    assert (lead.get("product_scores") or {}).get("application-retirement", 0) > 0


# --- the tag ------------------------------------------------------------------------------------------------------

def test_tag_is_served_for_gtm(app):
    r = TestClient(app[0].app).get("/api/markhub.js")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/javascript")
    assert "markhub_visitor_id" in r.text and "markhub_consent" in r.text


# --- solix.com paths in scoring -----------------------------------------------------------------------------------

def test_solixcom_paths_map_to_products_and_industries():
    import scoring
    assert scoring.topics_for_path("/products/application-retirement-solution/") == ["application-retirement"]
    assert scoring.topics_for_path("/products/data-sense/?utm_source=x") == ["data-sense"]
    assert scoring.topics_for_path("/solutions/oracle-ebusiness-suite/") == ["oracle-oebs-archiving"]
    assert scoring.topics_for_path("/products/enterprise-edition") == ["enterprise-edition"]  # the site's own paths still work
    assert scoring.industry_for_path("/solutions/banking/") == "financial-services"
    assert scoring.industry_for_path("/industries/healthcare") == "healthcare"
    for slugs in scoring.SOLIXCOM_PATHS.values():
        assert set(slugs) <= scoring.PRODUCTS
    assert set(scoring.SOLIXCOM_INDUSTRIES.values()) <= scoring.INDUSTRIES


# --- Sol's solix.com knowledge ------------------------------------------------------------------------------------

SITE = "https://www.solix.com"
BODY = "<h2>Why retire</h2><p>" + "Legacy applications cost money to keep running. " * 20 + "</p><h2>How</h2><p>" + "Archive then decommission. " * 40 + "</p>"


def _item(path, title="A page", body=BODY, modified="2026-11-20T00:00:00"):
    return {"link": SITE + path, "title": {"rendered": title}, "content": {"rendered": body}, "excerpt": {"rendered": "<p>Summary.</p>"},
            "modified_gmt": modified}


def test_page_filters_and_chunks(site_knowledge):
    assert site_knowledge.to_doc(_item("/products/application-retirement-solution/answers/what-is-sap/"), SITE, "pages") is None
    assert site_knowledge.to_doc(_item("/privacy-policy/"), SITE, "pages") is None
    assert site_knowledge.to_doc(_item("/x/", body="<p>tiny</p>"), SITE, "pages") is None
    assert site_knowledge.to_doc({**_item("/x/"), "link": "https://www.einpresswire.com/article/1"}, SITE, "posts") is None  # off-site news link
    doc = site_knowledge.to_doc(_item("/products/application-retirement-solution/", "Application Retirement"), SITE, "pages")
    chunks = site_knowledge.chunks_for(doc)
    assert chunks[0]["url"] == SITE + "/products/application-retirement-solution/" and chunks[0]["kind"] == "solixcom"
    assert len(chunks) >= 2 and all(len(c["text"]) <= 2600 for c in chunks)
    twins = [{"url": SITE + "/resources/lg/white-papers/a/"}, {"url": SITE + "/resources/white-papers/a/"}, {"url": SITE + "/resources/lg/ebooks/only-gated/"}]
    assert [d["url"] for d in site_knowledge.dedupe(twins)] == [SITE + "/resources/white-papers/a/", SITE + "/resources/lg/ebooks/only-gated/"]


def test_solixcom_pages_shard_by_path():
    import sol_search
    assert sol_search.shard_of({"url": SITE + "/products/data-ask/", "kind": "solixcom"}) == "products"
    assert sol_search.shard_of({"url": SITE + "/resources/ebooks/x/", "kind": "solixcom"}) == "resources"


def test_sync_stores_pages_and_a_full_pass_drops_removed_ones(app, site_knowledge, monkeypatch):
    _, db = app
    import migrate
    monkeypatch.setenv("SOL_WORDPRESS_SITES", SITE)
    monkeypatch.setenv("SOL_WORDPRESS_TYPES", "pages,kb")
    pages = {"pages": [_item("/products/application-retirement-solution/", "Application Retirement"), _item("/company/about/", "About Solix")],
             "kb": [_item("/kb/hipaa/", "HIPAA")]}
    seen_urls = []

    async def fake_json(client, url):
        seen_urls.append(url)
        wp_type = url.split("/wp/v2/")[1].split("?")[0]
        return pages[wp_type] if "page=1&" in url else []

    monkeypatch.setattr(migrate, "_json", fake_json)
    result = run(site_knowledge.sync(full=True))
    assert result["status"] == "ok" and result["stored"] == 3 and result["complete"], result
    assert run(db.site_pages.count_documents({})) == 3
    titles = {c["title"].split(":")[0] for c in run(site_knowledge.chunks())}
    assert {"Application Retirement", "About Solix", "HIPAA"} <= titles

    # The next daily run only asks for changes.
    seen_urls.clear()
    run(site_knowledge.sync())
    assert all("modified_after=" in u for u in seen_urls)

    # A page unpublished on solix.com is dropped by the next full pass ...
    pages["pages"] = pages["pages"][:1]
    run(site_knowledge.sync(full=True))
    assert run(db.site_pages.find_one({"url": SITE + "/company/about/"})) is None
    # ... but never by a pass that couldn't read everything.
    pages["pages"] = []

    async def broken(client, url):
        return None

    monkeypatch.setattr(migrate, "_json", broken)
    result = run(site_knowledge.sync(full=True))
    assert result["complete"] is False and run(db.site_pages.count_documents({})) == 2


def test_sync_status_is_for_staff(app, monkeypatch):
    admin, _ = app
    monkeypatch.setenv("SOL_WORDPRESS_SITES", SITE)
    r = admin.get("/api/admin/sol/site-knowledge")
    assert r.status_code == 200 and r.json()["sites"] == [SITE]
    assert TestClient(admin.app).get("/api/admin/sol/site-knowledge").status_code == 401
