"""Offline unit tests for seo.py: Semrush parsing, topic scoring, AI mention detection."""
import asyncio
import os
from datetime import datetime, timedelta, timezone

import httpx
import pytest

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "solix_test")

import seo  # noqa: E402


def run_sync(coro):
    """Run a coroutine on a private loop; asyncio.run() would unset the
    thread's default loop and break other tests sharing this worker."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_parse_semrush_csv_maps_by_position():
    rows = seo.parse_semrush_csv("Keyword;Position;Search Volume\ndata archiving;3;1900\n", ["Ph", "Po", "Nq"])
    assert rows == [{"Ph": "data archiving", "Po": "3", "Nq": "1900"}]


def test_parse_semrush_csv_nothing_found_is_empty():
    assert seo.parse_semrush_csv("ERROR 50 :: NOTHING FOUND", ["Ph"]) == []


def test_parse_semrush_csv_raises_on_units():
    with pytest.raises(seo.SemrushError):
        seo.parse_semrush_csv("ERROR 132 :: API UNITS BALANCE IS ZERO", ["Ph"])
    assert seo.semrush_http_error(seo.SemrushError("ERROR 132 :: API UNITS BALANCE IS ZERO")).status_code == 402


def test_keyword_normalises_traffic_and_features():
    kw = seo._keyword({"Ph": "x", "Po": "2", "Pp": "5", "Nq": "100", "Tr": "12.5", "Td": "0.1,1.00", "Fk": "11,21", "Fp": "11", "In": "1"}, 1000)
    assert kw["traffic"] == 125 and kw["prev_position"] == 5
    assert kw["serp"] == [11, 21] and kw["owned"] == [11] and kw["trend"] == [0.1, 1.0]


def test_history_sorted_and_dated():
    h = seo._history([{"Dt": "20260815", "Ot": "10", "Or": "5"}, {"Dt": "20260715", "Ot": "8", "Or": "4"}])
    assert [x["date"] for x in h] == ["2026-07-15", "2026-08-15"]


def test_mentions_ranked_by_first_appearance():
    found = seo._mentions("Consider Informatica, then Solix. Solix handles SAP.", {"solix.com": "Solix", "informatica.com": "Informatica", "veritas.com": "Veritas"})
    assert [(f["brand"], f["rank"], f["count"]) for f in found] == [("Informatica", 1, 1), ("Solix", 2, 2)]


def test_root_domain():
    assert seo._root("https://www.solix.com/products") == "solix.com"
    assert seo._root("https://news.bbc.co.uk/x") == "bbc.co.uk"


def test_clean_domain():
    assert seo._clean_domain("https://WWW.Solix.com/path") == "solix.com"


def test_gather_topic_scores_momentum_with_mocked_sources():
    now = datetime.now(timezone.utc)
    recent = int((now - timedelta(days=1)).timestamp())
    old = int((now - timedelta(days=15)).timestamp())
    rss = f"""<rss><channel>
      <item><title>EU AI Act enforcement starts for general purpose models</title><link>https://n/1</link><pubDate>{(now - timedelta(days=2)).strftime('%a, %d %b %Y %H:%M:%S GMT')}</pubDate><source>Reuters</source></item>
      <item><title>EU AI Act guidance published</title><link>https://n/2</link><pubDate>{(now - timedelta(days=20)).strftime('%a, %d %b %Y %H:%M:%S GMT')}</pubDate><source>FT</source></item>
    </channel></rss>"""

    def handler(req: httpx.Request):
        host = req.url.host
        if host == "news.google.com":
            return httpx.Response(200, content=rss.encode())
        if host == "hn.algolia.com":
            return httpx.Response(200, json={"hits": [
                {"objectID": "1", "title": "AI Act enforcement and open models", "created_at_i": recent, "points": 120, "num_comments": 40},
                {"objectID": "2", "title": "Older thread", "created_at_i": old, "points": 5, "num_comments": 1},
            ]})
        return httpx.Response(429)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await seo.gather_topic(c, "AI Act")

    t = run_sync(run())
    assert t["mentions_30d"] == 4 and t["mentions_7d"] == 2
    assert t["voices"] == {"news": 2, "practitioners": 2}
    assert t["failed_sources"] == []  # reddit non-200 degrades to empty, not a failure
    assert "enforcement" in t["rising_terms"]
    assert t["top"][0]["source"] == "Hacker News"


def _openrouter(handler):
    async def run(**kw):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await seo.ask_openrouter(c, "nvidia/nemotron-3.5-lightning:free", "best archiving vendors?", **kw)
    return run


def test_openrouter_answer_and_citations(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    seen = {}

    def handler(req):
        import json
        seen.update(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "Solix and Informatica.", "annotations": [
            {"type": "url_citation", "url_citation": {"url": "https://www.solix.com/x"}}]}}]})

    res = run_sync(_openrouter(handler)(web=True))
    assert res["answer"] == "Solix and Informatica." and res["cited"] == ["https://www.solix.com/x"]
    assert seen["plugins"] == [{"id": "web", "max_results": 5}] and seen["model"].endswith(":free")


def test_openrouter_no_web_by_default_and_rate_limit(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    res = run_sync(_openrouter(lambda req: httpx.Response(429, json={"error": {"message": "limit"}}))(web=False))
    assert "50 requests a day" in res["error"]


def test_ai_provider_prefers_openrouter(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    monkeypatch.delenv("OPENROUTER_MODELS", raising=False)
    monkeypatch.delenv("OPENROUTER_WEB", raising=False)
    p = seo.ai_provider()
    assert p == {"name": "openrouter", "models": ["nvidia/nemotron-3.5-lightning:free"], "web": False}
    monkeypatch.delenv("OPENROUTER_API_KEY")
    assert seo.ai_provider()["name"] == "anthropic"


def test_role_check_blocks_viewers():
    check = seo._roles("admin")
    with pytest.raises(seo.HTTPException) as e:
        run_sync(check(user={"role": "viewer"}))
    assert e.value.status_code == 403
    assert run_sync(check(user={"role": "admin"}))["role"] == "admin"


def _mock_client(handler, monkeypatch):
    real = httpx.AsyncClient

    def factory(*a, **kw):
        kw.pop("transport", None)
        return real(*a, transport=httpx.MockTransport(handler), **kw)
    monkeypatch.setattr(seo.httpx, "AsyncClient", factory)


def test_newsmcp_events_become_topic_items(monkeypatch):
    monkeypatch.setenv("NEWSMCP_API_KEY", "nk")
    seen = {}

    def handler(req):
        seen["key"], seen["q"] = req.headers.get("x-api-key"), req.url.params.get("q")
        return httpx.Response(200, json={"events": [{"headline": "Informatica adds AI governance", "first_seen": "2026-09-20T10:00:00", "newsrooms": 6,
                                                     "content_type": "analysis", "one_liner": "Analysts weigh in.", "entities": [{"name": "Informatica"}],
                                                     "sources": ["https://example.com/a"]}]})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await seo._newsmcp(c, "AI governance")
    items = run_sync(go())
    assert seen == {"key": "nk", "q": '"AI governance"'}
    assert items[0]["kind"] == "analysts" and items[0]["url"] == "https://example.com/a" and items[0]["entities"] == ["Informatica"]


def test_parse_json_object_tolerates_prose():
    assert seo.parse_json_object('Sure!\n```json\n{"summary": "x", "debates": []}\n```') == {"summary": "x", "debates": []}
    assert seo.parse_json_object("no json here") is None


def test_deep_dive_end_to_end(monkeypatch):
    from mongomock_motor import AsyncMongoMockClient
    import json as _json

    monkeypatch.setattr(seo, "db", AsyncMongoMockClient()["t"])
    monkeypatch.setenv("PARALLEL_API_KEY", "pk")
    monkeypatch.setenv("OPENROUTER_API_KEY", "ok")
    monkeypatch.delenv("NEWSMCP_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_MODELS", raising=False)
    article = "Analysts at Example Research say 62% of enterprises will retire legacy apps by 2027. " * 10
    calls = []

    def handler(req):
        host, path = req.url.host, req.url.path
        calls.append(f"{host}{path}")
        if host == "api.parallel.ai" and path.endswith("/search"):
            return httpx.Response(200, json={"results": [{"url": "https://analyst.example/report", "title": "Retirement outlook", "publish_date": "2026-09-20", "excerpts": ["short"]}]})
        if host == "api.parallel.ai" and path.endswith("/extract"):
            assert _json.loads(req.content)["urls"] == ["https://analyst.example/report"]
            return httpx.Response(200, json={"results": [{"url": "https://analyst.example/report", "title": "Retirement outlook", "full_content": article}], "errors": []})
        if host == "openrouter.ai":
            body = _json.loads(req.content)
            assert "[1] Retirement outlook" in body["messages"][1]["content"]
            return httpx.Response(200, json={"choices": [{"message": {"content": _json.dumps({
                "summary": "Retirement is accelerating.", "sentiment": "positive",
                "expert_views": [{"who": "Example Research", "view": "Most will retire apps by 2027", "source": 1}],
                "key_stats": [{"stat": "62% by 2027", "source": 1}], "debates": [], "buyer_questions": ["How long does retirement take?"],
                "competitor_moves": [], "content_angles": [{"title": "Retire before 2027", "angle": "x", "format": "guide"}]})}}]})
        return httpx.Response(503)  # news feeds unavailable in this test

    _mock_client(handler, monkeypatch)
    data = run_sync(seo.deep_dive(seo.DeepDiveBody(topic="application retirement")))
    assert data["model"] == "nvidia/nemotron-3.5-lightning:free"
    assert data["sources"][0]["n"] == 1 and data["sources"][0]["chars"] == len(article.strip())
    assert data["insight"]["key_stats"][0]["url"] == "https://analyst.example/report"
    assert data["insight"]["buyer_questions"] == ["How long does retirement take?"]
    # Second call is served from the 24-hour cache.
    before = len(calls)
    assert run_sync(seo.deep_dive(seo.DeepDiveBody(topic="Application Retirement")))["generated_at"] == data["generated_at"]
    assert len(calls) == before


def test_deep_dive_without_keys_explains(monkeypatch):
    from mongomock_motor import AsyncMongoMockClient

    monkeypatch.setattr(seo, "db", AsyncMongoMockClient()["t"])
    for k in ("PARALLEL_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "NEWSMCP_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    _mock_client(lambda req: httpx.Response(503), monkeypatch)
    data = run_sync(seo.deep_dive(seo.DeepDiveBody(topic="data sovereignty")))
    assert data["insight"] is None and any("PARALLEL_API_KEY" in n for n in data["notes"])
