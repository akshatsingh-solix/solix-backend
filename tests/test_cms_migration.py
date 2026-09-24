"""In-process tests for built-in content takeover, gated asset delivery emails,
website migration and site settings (mongomock-motor, mocked HTTP and email)."""
import os

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "solix_test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("ADMIN_EMAIL", "admin@example.com")
os.environ.setdefault("ADMIN_PASSWORD", "AdminPass!1")

import time  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from mongomock_motor import AsyncMongoMockClient  # noqa: E402

import database  # noqa: E402
import accounts, admin, auth, cache, chat, content, delivery, emailer, events, intent, leads_admin, migrate, server, site_settings  # noqa: E402,E401

MODULES = (accounts, admin, auth, chat, content, delivery, emailer, events, intent, leads_admin, migrate, server, site_settings)
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


@pytest.fixture()
def client(monkeypatch):
    mock = AsyncMongoMockClient()["solix_test"]
    previous = {m: getattr(m, "db", None) for m in MODULES}
    previous_database = database.db
    database.db = mock
    for m in MODULES:
        m.db = mock
    cache.clear()
    with TestClient(server.app) as c:
        yield c
    database.db = previous_database
    for m, db in previous.items():
        m.db = db


@pytest.fixture()
def outbox(monkeypatch):
    """Capture emails instead of sending them; SMTP-like transport with attachments."""
    sent = []

    async def fake_send(**kw):
        emailer._assert_safe_email(kw["subject"], kw["html"])
        sent.append(kw)
        return f"msg-{len(sent)}"

    monkeypatch.setattr(emailer, "send_email", fake_send)
    monkeypatch.setattr(emailer, "email_provider", lambda: "smtp")
    monkeypatch.setattr(emailer, "supports_attachments", lambda: True)
    monkeypatch.setenv("PUBLIC_API_URL", "https://api.example.com")
    monkeypatch.setenv("SITE_URL", "https://www.example.com/site")
    return sent


def staff(client):
    r = client.post("/api/auth/login", json={"email": "admin@example.com", "password": "AdminPass!1"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def wait_for(fn, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        value = fn()
        if value:
            return value
        time.sleep(0.05)
    return fn()


# --- Built-in content under CMS management ---------------------------------------

BUILTIN = [
    {"title": "Build an enterprise archive in the cloud", "slug": "build-enterprise-archive-in-the-cloud", "type": "whitepaper", "summary": "Reference architecture.", "body": "## Why\n\nText.", "gated": True, "date": "2025-03-10"},
    {"title": "Solix launches Enterprise Edition", "slug": "enterprise-edition-launch", "type": "news", "summary": "Launch.", "body": "Body.", "tag": "Product", "date": "2026-06-02"},
    {"title": "Summit next year", "slug": "summit-next-year", "type": "event", "summary": "An upcoming event.", "event_date": "Oct 2099", "date": "2099-10-01"},
]


def test_import_builtin_keeps_dates_and_withdrawn_hides_them(client):
    h = staff(client)
    r = client.post("/api/admin/content/import-builtin", json={"items": BUILTIN}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json() == {"created": 3, "updated": 0, "skipped": 0, "failed": []}
    again = client.post("/api/admin/content/import-builtin", json={"items": BUILTIN}, headers=h).json()
    assert again["skipped"] == 3 and again["created"] == 0

    listed = client.get("/api/admin/content", params={"origin": "builtin"}, headers=h).json()
    assert listed["total"] == 3 and listed["origin_counts"]["builtin"] == 3
    # An upcoming event that's already on the site stays live (not "scheduled").
    event = next(i for i in listed["items"] if i["slug"] == "summit-next-year")
    assert event["status"] == "published" and event["original_date"].startswith("2099-10-01")
    item = next(i for i in listed["items"] if i["slug"] == "enterprise-edition-launch")
    assert item["status"] == "published" and item["publish_at"].startswith("2026-06-02") and item["origin"] == "builtin"

    public = client.get("/api/content").json()
    assert {i["slug"] for i in public["items"]} == {"build-enterprise-archive-in-the-cloud", "enterprise-edition-launch", "summit-next-year"}
    assert public["withdrawn"] == []

    client.post(f"/api/admin/content/{item['id']}/unpublish", headers=h)
    cache.clear()
    public = client.get("/api/content").json()
    assert public["withdrawn"] == ["enterprise-edition-launch"]
    assert "enterprise-edition-launch" not in {i["slug"] for i in public["items"]}

    # Content written in the CMS never counts as withdrawn.
    new = client.post("/api/admin/content", json={"title": "Fresh post", "type": "blog"}, headers=h).json()
    assert new["origin"] == "cms"
    cache.clear()
    assert client.get("/api/content").json()["withdrawn"] == ["enterprise-edition-launch"]
    assert client.get("/api/admin/content", params={"origin": "cms"}, headers=h).json()["total"] == 1


# --- Gated asset delivery --------------------------------------------------------------

def _gated_whitepaper(client, h, slug="data-lake-guide"):
    f = client.post("/api/admin/files", files={"file": ("guide.pdf", PDF, "application/pdf")}, headers=h).json()
    doc = client.post("/api/admin/content", json={"title": "The data lake guide", "type": "whitepaper", "slug": slug, "file_id": f["id"], "gated": True}, headers=h).json()
    client.post(f"/api/admin/content/{doc['id']}/publish", json={}, headers=h)
    return doc


def _deliveries(client, h, n=1):
    return wait_for(lambda: (lambda d: d if len(d) >= n else None)(client.get("/api/admin/deliveries", headers=h).json()))


def test_download_form_emails_the_gated_file(client, outbox):
    h = staff(client)
    _gated_whitepaper(client, h)
    r = client.post("/api/submissions", json={"type": "download", "email": "Ana@Corp.com", "name": "Ana Silva", "resource": "The data lake guide", "resource_slug": "data-lake-guide"})
    assert r.status_code == 201
    log = _deliveries(client, h)
    assert log[0]["status"] == "sent" and log[0]["attached"] is True and log[0]["email"] == "ana@corp.com"
    mail = outbox[0]
    assert mail["to"] == "ana@corp.com" and mail["subject"] == "Your copy of The data lake guide"
    assert mail["attachments"][0]["name"] == "guide.pdf" and mail["attachments"][0]["data"] == PDF
    assert "Hi Ana," in mail["html"]
    # The signed link in the email downloads the gated file.
    link = mail["html"].split('href="https://api.example.com')[1].split('"')[0].replace("&amp;", "&")
    got = client.get(link)
    assert got.status_code == 200 and got.content == PDF
    assert client.get(link.split("?")[0]).status_code == 403
    # The "view on the website" link unlocks the page and hands it a fresh file link.
    page = mail["html"].split("view it on our website")[0].rsplit('href="', 1)[1].split('"')[0].replace("&amp;", "&")
    access = client.get("/api/content-access/data-lake-guide", params={"t": page.split("access=")[1]}).json()
    assert access["ok"] and client.get(access["url"]).content == PDF

    # A second submit moments later doesn't send a duplicate; resend does.
    client.post("/api/submissions", json={"type": "download", "email": "ana@corp.com", "resource_slug": "data-lake-guide"})
    log = _deliveries(client, h, 2)
    assert log[0]["status"] == "skipped" and "Already sent" in log[0]["detail"]
    again = client.post(f"/api/admin/deliveries/{log[-1]['id']}/resend", headers=h).json()
    assert again["status"] == "sent" and again["resend_of"] == log[-1]["id"] and len(outbox) == 2

    # It shows on the lead's timeline.
    lead = client.get("/api/admin/leads", params={"q": "ana@corp.com"}, headers=h).json()["items"][0]
    timeline = client.get(f"/api/admin/leads/{lead['id']}", headers=h).json()["timeline"]
    assert any(t["kind"] == "email" and t["type"] == "asset_sent" for t in timeline)


def test_builtin_article_gets_a_reopen_link_and_settings_apply(client, outbox):
    h = staff(client)
    client.put("/api/admin/delivery", json={"enabled": True, "subject": "Here is {title}", "message": "Enjoy {title}.", "attach": False, "attach_max_mb": 5, "link_days": 3}, headers=h)
    client.post("/api/submissions", json={"type": "download", "email": "li@corp.com", "resource": "Archive-first S/4HANA", "resource_slug": "archive-first-fastest-path-to-s4hana"})
    log = _deliveries(client, h)
    assert log[0]["status"] == "sent" and log[0]["attached"] is False
    mail = outbox[0]
    assert mail["subject"] == "Here is Archive-first S/4HANA" and not mail["attachments"]
    href = mail["html"].split('href="')[1].split('"')[0].replace("&amp;", "&")
    assert href.startswith("https://www.example.com/site/resources/archive-first-fastest-path-to-s4hana?access=")
    token = href.split("access=")[1]
    assert client.get("/api/content-access/archive-first-fastest-path-to-s4hana", params={"t": token}).json() == {"ok": True, "url": None}
    assert client.get("/api/content-access/another-slug", params={"t": token}).json() == {"ok": False}

    # Turned off: nothing is sent, the attempt is still logged.
    client.put("/api/admin/delivery", json={"enabled": False, "subject": "x {title}", "message": "Your file.", "attach": True, "attach_max_mb": 5, "link_days": 7}, headers=h)
    client.post("/api/submissions", json={"type": "download", "email": "li2@corp.com", "resource_slug": "archive-first-fastest-path-to-s4hana"})
    log = _deliveries(client, h, 2)
    assert log[0]["status"] == "skipped" and len(outbox) == 1
    # Other form types never trigger a delivery.
    client.post("/api/submissions", json={"type": "demo", "email": "x@corp.com"})
    time.sleep(0.2)
    assert len(client.get("/api/admin/deliveries", headers=h).json()) == 2


def test_delivery_without_transport_is_logged_as_skipped(client, monkeypatch):
    monkeypatch.setattr(emailer, "email_provider", lambda: None)
    h = staff(client)
    client.post("/api/submissions", json={"type": "download", "email": "a@corp.com", "resource_slug": "x-guide"})
    log = _deliveries(client, h)
    assert log[0]["status"] == "skipped" and "No email transport" in log[0]["detail"]
    info = client.get("/api/admin/delivery", headers=h).json()
    assert info["provider"] is None and info["enabled"] is True


def test_test_delivery_uses_latest_gated_item(client, outbox):
    h = staff(client)
    assert client.post("/api/admin/deliveries/test", json={"email": "me@corp.com"}, headers=h).status_code == 422
    _gated_whitepaper(client, h)
    r = client.post("/api/admin/deliveries/test", json={"email": "me@corp.com"}, headers=h)
    assert r.status_code == 200 and r.json()["status"] == "sent" and outbox[0]["to"] == "me@corp.com"


def test_smtp_transport_sends_html_and_attachments(monkeypatch):
    captured = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            captured["host"] = (host, port)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, context=None):
            captured["tls"] = True

        def login(self, user, password):
            captured["login"] = user

        def send_message(self, msg):
            captured["msg"] = msg

    import smtplib
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    for k, v in {"SMTP_HOST": "smtp.example.com", "SMTP_PORT": "587", "SMTP_USER": "mailer@example.com", "SMTP_PASSWORD": "pw", "EMAIL_FROM": "hello@example.com"}.items():
        monkeypatch.setenv(k, v)
    assert emailer.email_provider() == "smtp" and emailer.supports_attachments()
    emailer._send_smtp(emailer._smtp_config(), "x@corp.com", "Hi", '<p>Hello <a href="https://example.com/a">there</a></p>', None, [{"name": "a.pdf", "content_type": "application/pdf", "data": PDF}])
    msg = captured["msg"]
    assert captured["host"] == ("smtp.example.com", 587) and captured["tls"] and captured["login"] == "mailer@example.com"
    assert msg["To"] == "x@corp.com" and "hello@example.com" in msg["From"]
    parts = [p for p in msg.walk() if p.get_filename()]
    assert parts[0].get_filename() == "a.pdf" and parts[0].get_payload(decode=True) == PDF


# --- Website migration ------------------------------------------------------------------

WP_POSTS = [
    {
        "id": 1, "slug": "cloud-archiving-guide", "link": "https://old.example.com/blog/cloud-archiving-guide/", "date_gmt": "2019-04-02T10:00:00",
        "title": {"rendered": "Cloud Archiving &#8211; The Guide"}, "excerpt": {"rendered": "<p>How to archive in the cloud [&hellip;]</p>"},
        "content": {"rendered": '<h2>Why archive</h2><p>Because <strong>costs</strong> grow. Read the <a href="/files/guide.pdf">PDF</a>.</p>'
                    '<p><img src="https://old.example.com/img/chart.png" alt="Chart"></p><script>alert(1)</script>'
                    '<div class="share-buttons">Share this</div><table><tr><td>Tier</td><td>Cost</td></tr></table>'},
        "_embedded": {"wp:featuredmedia": [{"source_url": "https://old.example.com/img/cover.png"}], "author": [{"name": "Jane Doe"}], "wp:term": [[{"name": "White Papers"}]]},
    },
    {
        "id": 2, "slug": "sap-archiving-news", "link": "https://old.example.com/news/sap-archiving-news/", "date_gmt": "2020-01-15T08:00:00",
        "title": {"rendered": "Solix announces SAP Archiving update"}, "excerpt": {"rendered": "<p>News.</p>"}, "content": {"rendered": "<p>Press text about sap archiving.</p>" * 5},
        "_embedded": {},
    },
]

PAGE = """<!doctype html><html><head><title>Data governance explained | Old Site</title>
<meta property="og:title" content="Data governance explained"><meta name="description" content="What governance means.">
<meta property="article:published_time" content="2018-07-01T12:00:00Z"><meta property="og:image" content="https://old.example.com/img/cover.png">
</head><body><nav>menu</nav><article><h1>Data governance explained</h1><p>{p}</p><h3>Details</h3><ul><li>One</li><li>Two</li></ul></article><footer>foot</footer></body></html>"""


def mock_site(extra=None):
    routes = {
        "/wp-json/wp/v2/types": (200, "application/json", b'{"post":{"name":"Posts","rest_base":"posts"},"page":{"name":"Pages","rest_base":"pages"},"attachment":{"name":"Media","rest_base":"media"}}'),
        "/files/guide.pdf": (200, "application/pdf", PDF),
        "/img/chart.png": (200, "image/png", PNG),
        "/img/cover.png": (200, "image/png", PNG + b"cover"),
        "/sitemap_index.xml": (200, "application/xml", b'<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><sitemap><loc>https://old.example.com/post-sitemap.xml</loc></sitemap></sitemapindex>'),
        "/post-sitemap.xml": (200, "application/xml", b'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://old.example.com/blog/governance-explained/</loc><lastmod>2018-07-02</lastmod></url><url><loc>https://old.example.com/careers/</loc></url></urlset>'),
        "/blog/governance-explained/": (200, "text/html; charset=utf-8", PAGE.replace("{p}", "Governance is about knowing your data. " * 20).encode()),
        "/feed/": (200, "application/rss+xml", b'<?xml version="1.0"?><rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><item><title>Podcast: AI on archives</title><link>https://old.example.com/podcast/ai-archives/</link><pubDate>Tue, 02 Jun 2020 09:00:00 GMT</pubDate><category>Podcast</category><content:encoded><![CDATA[' + b"<p>Long episode notes. </p>" * 60 + b"]]></content:encoded></item></channel></rss>"),
        "/old": (301, "text/html", b""),
        **(extra or {}),
    }

    def handler(request: httpx.Request):
        path = request.url.path
        if path.startswith("/wp-json/wp/v2/posts"):
            page = int(request.url.params.get("page", "1"))
            return httpx.Response(200, json=WP_POSTS if page == 1 else [])
        if path == "/old":
            return httpx.Response(301, headers={"location": "http://127.0.0.1/admin"})
        if path in routes:
            status, ctype, body = routes[path]
            return httpx.Response(status, headers={"content-type": ctype}, content=body)
        return httpx.Response(404)

    return handler


@pytest.fixture()
def web(monkeypatch):
    monkeypatch.setattr(migrate, "_public_host", lambda host: host != "127.0.0.1")
    monkeypatch.setattr(migrate, "new_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(mock_site()), headers={"User-Agent": migrate.UA}))


def _run(client, h, body):
    r = client.post("/api/admin/migrations", json=body, headers=h)
    assert r.status_code == 201, r.text
    job_id = r.json()["id"]
    return wait_for(lambda: (lambda j: j if j["status"] not in ("queued", "running") else None)(client.get(f"/api/admin/migrations/{job_id}", headers=h).json()), 10)


def test_wordpress_preview_then_migrate_with_media(client, web):
    h = staff(client)
    body = {"source": "wordpress", "url": "https://old.example.com", "status": "published"}
    p = client.post("/api/admin/migrations/preview", json=body, headers=h)
    assert p.status_code == 200, p.text
    prev = p.json()
    assert prev["found"] == 2 and prev["types"] == {"whitepaper": 1, "news": 1}
    assert {t["rest_base"] for t in prev["wp_types_available"]} == {"posts", "pages"}
    first = prev["items"][0]
    assert first["title"] == "Cloud Archiving – The Guide" and first["type"] == "whitepaper" and first["has_file"] and not first["exists"]
    assert client.get("/api/admin/content", headers=h).json()["total"] == 0  # preview saves nothing

    job = _run(client, h, body)
    assert job["status"] == "done", job["log"]
    assert job["counts"]["created"] == 2 and job["counts"]["failed"] == 0
    items = {i["slug"]: i for i in client.get("/api/admin/content", params={"origin": "import"}, headers=h).json()["items"]}
    guide = client.get(f"/api/admin/content/{items['cloud-archiving-guide']['id']}", headers=h).json()
    assert guide["type"] == "whitepaper" and guide["status"] == "published" and guide["publish_at"].startswith("2019-04-02")
    assert guide["author"] == "Jane Doe" and guide["tag"] == "White Papers" and guide["source_url"] == "https://old.example.com/blog/cloud-archiving-guide/"
    assert guide["cover_image"].startswith("/api/files/") and guide["file"]["content_type"] == "application/pdf"
    assert "## Why archive" in guide["body"] and "**costs**" in guide["body"] and "alert(1)" not in guide["body"] and "Share this" not in guide["body"]
    assert "![Chart](/api/files/" in guide["body"] and "- Tier · Cost" in guide["body"]
    assert guide["summary"] == "How to archive in the cloud"
    news = items["sap-archiving-news"]
    assert news["type"] == "news" and "sap-archiving" in news["products"]

    # Running it again skips what's already there.
    again = _run(client, h, body)
    assert again["counts"]["skipped"] == 2 and again["counts"]["created"] == 0

    # Redirect map for the old server.
    csv_text = client.get("/api/admin/migrations/redirects", headers=h).text
    assert "https://old.example.com/news/sap-archiving-news/," in csv_text and "/newsroom/sap-archiving-news" in csv_text
    assert "/resources/cloud-archiving-guide" in csv_text
    nginx = client.get("/api/admin/migrations/redirects", params={"format": "nginx"}, headers=h).text
    assert "location = /blog/cloud-archiving-guide/ { return 301" in nginx
    assert len(client.get("/api/admin/migrations", headers=h).json()) == 2


def test_sitemap_rss_urls_and_file_sources(client, web):
    h = staff(client)
    job = _run(client, h, {"source": "sitemap", "url": "https://old.example.com/sitemap_index.xml", "include": "/blog/", "status": "draft", "mirror_media": False})
    assert job["counts"] == {"found": 1, "processed": 1, "created": 1, "updated": 0, "skipped": 0, "failed": 0}, job["log"]
    doc = client.get("/api/admin/content", params={"origin": "import"}, headers=h).json()["items"][0]
    assert doc["title"] == "Data governance explained" and doc["status"] == "draft" and doc["slug"] == "governance-explained"
    full = client.get(f"/api/admin/content/{doc['id']}", headers=h).json()
    assert full["original_date"].startswith("2018-07-01") and "### Details" in full["body"] and "- One" in full["body"] and "menu" not in full["body"]
    assert full["cover_image"] == "https://old.example.com/img/cover.png"

    job = _run(client, h, {"source": "rss", "url": "https://old.example.com/feed/", "status": "published", "mirror_media": False})
    assert job["counts"]["created"] == 1, job["log"]
    pod = [i for i in client.get("/api/admin/content", params={"type": "podcast"}, headers=h).json()["items"]]
    assert pod and pod[0]["publish_at"].startswith("2020-06-02")

    job = _run(client, h, {"source": "urls", "urls": ["https://old.example.com/missing/", "https://old.example.com/blog/governance-explained/"], "on_conflict": "update", "mirror_media": False})
    assert job["counts"]["failed"] == 1 and job["counts"]["updated"] == 1
    assert any("HTTP 404" in entry["msg"] for entry in client.get(f"/api/admin/migrations/{job['id']}", headers=h).json()["log"])

    records = [
        {"Title": "Q3 press release", "Type": "Press release", "Date": "2017-10-01", "Body": "## Headline\n\nText.", "URL": "https://old.example.com/press/q3"},
        {"title": "Retail datasheet", "type": "Datasheet", "summary": "One pager", "body_html": "<p>Retail <em>data</em>.</p>"},
        {"summary": "no title, no url"},
    ]
    job = _run(client, h, {"source": "file", "records": records, "status": "published"})
    assert job["counts"]["created"] == 2 and job["counts"]["found"] == 2, job["log"]
    news = client.get("/api/admin/content", params={"type": "news"}, headers=h).json()["items"][0]
    assert news["title"] == "Q3 press release" and news["publish_at"].startswith("2017-10-01")


def test_migration_refuses_private_addresses_and_parallel_jobs(client, web):
    h = staff(client)
    r = client.post("/api/admin/migrations/preview", json={"source": "sitemap", "url": "http://127.0.0.1/sitemap.xml"}, headers=h)
    assert r.status_code == 422 and "public internet" in r.json()["detail"]
    r = client.post("/api/admin/migrations/preview", json={"source": "sitemap", "url": "file:///etc/passwd"}, headers=h)
    assert r.status_code == 422
    # A redirect to a private address is refused too.
    job = _run(client, h, {"source": "urls", "urls": ["https://old.example.com/old"]})
    assert job["counts"]["failed"] == 1
    assert client.post("/api/admin/migrations", json={"source": "wordpress"}, headers=h).status_code == 422
    # Only staff who can edit content may migrate.
    assert client.post("/api/admin/migrations/preview", json={"source": "urls", "urls": ["https://old.example.com/x"]}).status_code == 401


def test_extract_and_convert_helpers():
    page = migrate.extract_page(PAGE.replace("{p}", "Body text here. " * 30), "https://old.example.com/blog/x/")
    assert page["title"] == "Data governance explained" and page["summary"] == "What governance means."
    assert page["date"] == "2018-07-01T12:00:00Z"
    html, video = migrate.clean_html('<p>Hi</p><iframe src="https://www.youtube.com/embed/abc123"></iframe><img src="/a.png">', "https://old.example.com/p/")
    assert video == "https://www.youtube.com/watch?v=abc123" and 'src="https://old.example.com/a.png"' in html
    md = migrate.to_markdown("<h1>Top</h1><h4>Small</h4><p>Text <img src='https://x.example.com/i.png' alt='I'> more</p>")
    assert md.startswith("## Top") and "### Small" in md and "\n\n![I](https://x.example.com/i.png)\n\n" in md
    assert migrate.detect_type("/resources/white-paper-x") == "whitepaper" and migrate.detect_type("/blog/x") == "blog"
    assert migrate.normalise_type("Case Study") == "casestudy" and migrate.normalise_type("nonsense") is None


# --- Site settings -----------------------------------------------------------------------------

def test_site_settings_announcement(client):
    h = staff(client)
    assert client.get("/api/site").json() == {"announcement": None, "empower_promo": True}
    body = {"announcement": {"enabled": True, "badge": "Live", "text": "Empower 2026 keynotes are streaming", "link_label": "Watch", "link_url": "/newsroom"}, "empower_promo": False}
    r = client.put("/api/admin/site", json=body, headers=h)
    assert r.status_code == 200 and r.json()["announcement"]["badge"] == "Live"
    assert client.get("/api/site").json()["empower_promo"] is False
    bad = {**body, "announcement": {**body["announcement"], "link_url": "javascript:alert(1)"}}
    assert client.put("/api/admin/site", json=bad, headers=h).status_code == 422
    assert client.put("/api/admin/site", json=body).status_code == 401
