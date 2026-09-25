"""Offline tests for Sol: retrieval, provider failover, streamed tool calls, chat endpoint."""
import asyncio
import json
import os

import httpx
import pytest

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "solix_test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("ADMIN_EMAIL", "admin@example.com")
os.environ.setdefault("ADMIN_PASSWORD", "AdminPass!1")

import llm  # noqa: E402
import sol_search  # noqa: E402


def run_sync(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def collect(agen):
    async def go():
        return [e async for e in agen]
    return run_sync(go())


def sse_body(*chunks):
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"


def text_chunk(t):
    return {"choices": [{"delta": {"content": t}}]}


# ---------- retrieval ----------
def test_knowledge_is_generated_and_loaded():
    assert len(sol_search.index().chunks) > 50
    urls = {c["url"] for c in sol_search.index().chunks}
    assert "/products/application-retirement" in urls and "/careers" in urls


def test_search_finds_the_right_pages():
    assert sol_search.search("Can you retire SAP ECC and keep the data?", 3)[0]["url"] == "/products/sap-archiving"
    assert sol_search.search("are you hiring engineers?", 1)[0]["url"] == "/careers"
    assert sol_search.search("GDPR DSAR automation", 1)[0]["id"].startswith(("product:consumer-data-privacy", "resource:consumer-data-privacy"))


def test_small_talk_retrieves_nothing():
    assert sol_search.search("hi there", 5) == []


def test_format_context_respects_budget():
    ctx = sol_search.format_context(sol_search.search("archiving cost reduction", 10), max_chars=1500)
    assert 0 < len(ctx) <= 1500 + 20 and "(page: /" in ctx


# ---------- provider failover ----------
def provider(name, models=("m",)):
    return llm.Provider(name, f"https://{name}.test/v1", "k", list(models))


def test_failover_skips_rate_limited_provider_and_cools_it_down():
    llm._cooldown.clear()
    seen = []

    def handler(request):
        seen.append(request.url.host)
        if request.url.host == "a.test":
            return httpx.Response(429, text="rate limited")
        return httpx.Response(200, text=sse_body(text_chunk("Hello "), text_chunk("there")))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    events = collect(llm.stream_completion([{"role": "user", "content": "hi"}], providers=[provider("a"), provider("b")], client=client))
    assert "".join(e["text"] for e in events if e["type"] == "text") == "Hello there"
    assert events[-1] == {"type": "meta", "provider": "b", "model": "m"}
    assert not llm._available(("a", "m"))

    # Next turn goes straight to the healthy provider.
    seen.clear()
    collect(llm.stream_completion([{"role": "user", "content": "hi"}], providers=[provider("a"), provider("b")], client=client))
    assert seen == ["b.test"]
    llm._cooldown.clear()


def test_all_providers_failing_raises():
    llm._cooldown.clear()
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503, text="down")))
    with pytest.raises(llm.ProviderError):
        collect(llm.stream_completion([{"role": "user", "content": "hi"}], providers=[provider("a"), provider("b")], client=client))
    llm._cooldown.clear()


def test_no_provider_configured_raises():
    with pytest.raises(llm.ProviderError):
        collect(llm.stream_completion([], providers=[]))


def test_streamed_tool_call_fragments_are_assembled():
    llm._cooldown.clear()
    body = sse_body(
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "search_site", "arguments": '{"que'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'ry": "SAP"}'}}]}}]},
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=body)))
    events = collect(llm.stream_completion([], tools=[{}], providers=[provider("a")], client=client))
    assert events[0] == {"type": "tool_calls", "calls": [{"id": "c1", "name": "search_site", "arguments": {"query": "SAP"}}]}


def test_tool_calls_without_index_are_kept_apart():
    llm._cooldown.clear()
    body = sse_body({"choices": [{"delta": {"tool_calls": [
        {"id": "x", "function": {"name": "search_site", "arguments": '{"query": "a"}'}},
        {"id": "y", "function": {"name": "search_site", "arguments": '{"query": "b"}'}},
    ]}}]})
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=body)))
    calls = collect(llm.stream_completion([], tools=[{}], providers=[provider("a")], client=client))[0]["calls"]
    assert [c["arguments"]["query"] for c in calls] == ["a", "b"]


def test_configured_providers_order_and_openrouter_model_fallback(monkeypatch):
    for k in ("GEMINI_API_KEY", "GROQ_API_KEY", "MISTRAL_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY", "SOL_CUSTOM_BASE_URL", "SOL_PROVIDER_ORDER", "SOL_OPENROUTER_MODELS", "CHAT_MODELS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "x")
    assert llm.configured_providers()[0].models == llm.DEFAULT_OPENROUTER_MODELS.split(",")
    # The Render service's existing CHAT_MODELS keeps working.
    monkeypatch.setenv("CHAT_MODELS", "a:free,b:free")
    monkeypatch.setenv("GROQ_API_KEY", "y")
    ps = llm.configured_providers()
    assert [p.name for p in ps] == ["groq", "openrouter"]
    assert ps[1].models == ["a:free", "b:free"]
    monkeypatch.setenv("SOL_PROVIDER_ORDER", "openrouter,groq")
    assert [p.name for p in llm.configured_providers()] == ["openrouter", "groq"]


def test_groq_key_alone_is_enough(monkeypatch):
    for k in ("GEMINI_API_KEY", "MISTRAL_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY", "SOL_CUSTOM_BASE_URL", "SOL_PROVIDER_ORDER", "SOL_GROQ_MODELS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "gsk")
    [groq] = llm.configured_providers()
    assert groq.base_url == "https://api.groq.com/openai/v1" and groq.models == ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]


def test_openai_key_still_supported(monkeypatch):
    for k in ("GEMINI_API_KEY", "GROQ_API_KEY", "MISTRAL_API_KEY", "OPENROUTER_API_KEY", "SOL_CUSTOM_BASE_URL", "SOL_PROVIDER_ORDER"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o-mini")
    assert [(p.name, p.models) for p in llm.configured_providers()] == [("openai", ["gpt-4o-mini"])]


def test_empty_reply_falls_through_to_next_model():
    llm._cooldown.clear()

    def handler(request):
        if json.loads(request.content)["model"] == "thinker":
            return httpx.Response(200, text="data: [DONE]\n\n")
        return httpx.Response(200, text=sse_body(text_chunk("Answer")))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    events = collect(llm.stream_completion([], providers=[provider("a", ("thinker", "talker"))], client=client))
    assert events[0] == {"type": "text", "text": "Answer"} and events[-1]["model"] == "talker"
    llm._cooldown.clear()


# ---------- chat endpoint ----------
@pytest.fixture
def chat_app(monkeypatch):
    """The full API on an in-memory Mongo, with one fake provider configured."""
    from mongomock_motor import AsyncMongoMockClient
    import database
    import accounts, admin, auth, cache, chat, content, delivery, emailer, events, intent, leads_admin, migrate, server, site_settings  # noqa: E401

    fake_db = AsyncMongoMockClient()["solix_test"]
    monkeypatch.setattr(database, "db", fake_db)
    for m in (accounts, admin, auth, chat, content, delivery, emailer, events, intent, leads_admin, migrate, server, site_settings):
        monkeypatch.setattr(m, "db", fake_db, raising=False)
    cache.clear()
    monkeypatch.setattr(chat, "notify_lead", lambda doc: asyncio.sleep(0))
    monkeypatch.setattr(chat, "configured_providers", lambda: [provider("a")])
    chat._hits.clear()
    # Each test starts from the bundled knowledge and re-reads the CMS.
    monkeypatch.setattr(sol_search, "_index", None)
    monkeypatch.setattr(sol_search, "_cms_checked", None)
    return chat, server.app, fake_db


def parse_sse(text):
    return [json.loads(line[5:]) for line in text.split("\n") if line.startswith("data:")]


def test_chat_books_demo_through_tool_and_streams_answer(chat_app, monkeypatch):
    from fastapi.testclient import TestClient
    chat, app, fake_db = chat_app
    seen_messages = []

    async def fake_stream(messages, tools=None, **kw):
        seen_messages.append([dict(m) for m in messages])
        if len(seen_messages) == 1:
            yield {"type": "tool_calls", "calls": [{"id": "t1", "name": "create_demo_request", "arguments": {"name": "Ada Lovelace", "email": "ada@acme.com", "company": "Acme"}}]}
        else:
            yield {"type": "text", "text": "Thanks Ada, "}
            yield {"type": "text", "text": "you're booked."}
        yield {"type": "meta", "provider": "a", "model": "m"}

    monkeypatch.setattr(chat, "stream_completion", fake_stream)
    r = TestClient(app).post("/api/chat/stream", json={"session_id": "sess-123", "message": "Yes please book the SAP archiving demo", "language": "fr", "page": "/products/sap-archiving"})
    events = parse_sse(r.text)
    assert any(e.get("event") == "demo_booked" and e["company"] == "Acme" for e in events)
    assert "".join(e.get("delta", "") for e in events) == "Thanks Ada, you're booked."
    assert events[-1] == {"done": True}

    system = seen_messages[0][0]["content"]
    assert "Reply in French" in system and "/products/sap-archiving" in system and "Site knowledge" in system
    # Round two carries the tool result back to the model.
    assert seen_messages[1][-1]["role"] == "tool" and json.loads(seen_messages[1][-1]["content"])["ok"] is True

    async def check():
        sub = await fake_db.submissions.find_one({"source": "chat"})
        msgs = await fake_db.chat_messages.find({"session_id": "sess-123"}).to_list(10)
        return sub, msgs
    sub, msgs = run_sync(check())
    assert sub["type"] == "demo" and sub["email"] == "ada@acme.com"
    # Chat bookings are scored like any other lead.
    assert run_sync(fake_db.leads.find_one({"email": "ada@acme.com"})) is not None
    assert [m["role"] for m in msgs] == ["user", "assistant"] and msgs[1]["provider"] == "a"


def test_chat_rejects_invalid_email_without_saving(chat_app):
    chat, _, fake_db = chat_app
    out = run_sync(chat.create_demo_request("s", {"name": "Bo", "email": "not-an-email", "company": "Acme"}))
    assert out["ok"] is False
    assert run_sync(fake_db.submissions.count_documents({})) == 0


def test_chat_reports_error_when_every_provider_fails(chat_app, monkeypatch):
    from fastapi.testclient import TestClient
    chat, app, _ = chat_app

    async def failing(messages, tools=None, **kw):
        raise llm.ProviderError("all down")
        yield  # pragma: no cover

    monkeypatch.setattr(chat, "stream_completion", failing)
    events = parse_sse(TestClient(app).post("/api/chat/stream", json={"session_id": "sess-err", "message": "hello"}).text)
    assert events == [{"error": "The concierge is temporarily unavailable. Please try again."}]


def test_chat_rate_limits_a_session(chat_app, monkeypatch):
    from fastapi.testclient import TestClient
    chat, app, _ = chat_app
    monkeypatch.setitem(chat.LIMITS, "session", (2, 300))

    async def quick(messages, tools=None, **kw):
        yield {"type": "text", "text": "ok"}

    monkeypatch.setattr(chat, "stream_completion", quick)
    client = TestClient(app)
    codes = [client.post("/api/chat/stream", json={"session_id": "sess-rl", "message": "hi"}).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_chat_503_without_providers(chat_app, monkeypatch):
    from fastapi.testclient import TestClient
    chat, app, _ = chat_app
    monkeypatch.setattr(chat, "configured_providers", lambda: [])
    assert TestClient(app).post("/api/chat/stream", json={"session_id": "sess-none", "message": "hi"}).status_code == 503


def test_admin_lists_conversations_with_leads(monkeypatch):
    mongomock_motor = pytest.importorskip("mongomock_motor")
    import admin

    fake_db = mongomock_motor.AsyncMongoMockClient()["t"]
    monkeypatch.setattr(admin, "db", fake_db)

    async def seed_and_list():
        await fake_db.chat_messages.insert_many([
            {"session_id": "s1", "role": "user", "content": "Retire SAP?", "created_at": "2026-09-01T10:00:00", "page": "/products"},
            {"session_id": "s1", "role": "assistant", "content": "Yes.", "created_at": "2026-09-01T10:00:05", "model": "gemini-2.5-flash"},
            {"session_id": "s2", "role": "user", "content": "Jobs?", "created_at": "2026-09-02T10:00:00"},
        ])
        await fake_db.submissions.insert_one({"source": "chat", "source_page": "chat:s1", "type": "demo"})
        return await admin.list_chats(page=1, page_size=25, q=None), await admin.get_chat("s1")

    listing, transcript = run_sync(seed_and_list())
    assert listing["total"] == 2
    assert [i["session_id"] for i in listing["items"]] == ["s2", "s1"]
    s1 = listing["items"][1]
    assert s1["first_question"] == "Retire SAP?" and s1["lead"] == "demo" and s1["models"] == ["gemini-2.5-flash"] and s1["messages"] == 2
    assert [m["role"] for m in transcript] == ["user", "assistant"]


# ---------- token budget (free-tier per-minute caps) ----------
def test_fit_history_keeps_newest_turns_within_budget():
    import chat
    history = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"turn {i} " + "x" * 400} for i in range(16)]
    kept = chat._fit_history(history, 500)
    assert kept and sum(chat._tokens(m["content"]) for m in kept) <= 500
    assert kept[-1]["content"].startswith("turn 15")
    assert kept[0]["role"] == "user"


def test_fit_history_with_no_room_is_empty():
    import chat
    assert chat._fit_history([{"role": "user", "content": "hi"}], 0) == []


def test_long_conversation_request_stays_under_budget(chat_app, monkeypatch):
    from fastapi.testclient import TestClient
    chat, app, fake_db = chat_app
    sent = []

    async def capture(messages, tools=None, **kw):
        sent.append((messages, tools, kw))
        yield {"type": "text", "text": "ok"}

    monkeypatch.setattr(chat, "stream_completion", capture)

    async def seed():
        await fake_db.chat_messages.insert_many([
            {"id": str(i), "session_id": "sess-long", "role": "user" if i % 2 == 0 else "assistant",
             "content": f"message {i} " + "archiving retirement governance " * 45, "created_at": f"2026-09-01T10:{i:02d}:00"}
            for i in range(16)])
    run_sync(seed())

    TestClient(app).post("/api/chat/stream", json={"session_id": "sess-long", "message": "What about SAP ECC retirement for manufacturing?", "page": "/products/sap-archiving"})
    messages, tools, kw = sent[0]
    total = sum(chat._tokens(m["content"]) for m in messages) + chat._tokens(json.dumps(tools))
    assert total <= chat.INPUT_TOKEN_BUDGET
    assert kw["max_tokens"] == chat.MAX_OUTPUT_TOKENS
    # Newest history survives, oldest is dropped, and the latest question is last.
    assert "message 15" in messages[-2]["content"] and not any("message 0 " in m["content"] for m in messages)
    assert messages[-1]["content"].startswith("What about SAP ECC")


# ---------- published CMS content ----------
def test_cms_chunks_split_sections_and_hide_gated_bodies():
    open_doc = {"slug": "dsar-guide", "title": "DSAR Field Guide", "type": "blog", "summary": "How to answer DSARs fast.",
                "body": "Intro text.\n\n## Step one\nFind every system holding personal data.\n![chart](x.png)\n## Step two\nAutomate the response."}
    chunks = sol_search.cms_chunks(open_doc)
    assert [c["title"] for c in chunks] == ["DSAR Field Guide", "DSAR Field Guide: DSAR Field Guide", "DSAR Field Guide: Step one", "DSAR Field Guide: Step two"]
    assert all(c["url"] == "/resources/dsar-guide" for c in chunks) and "x.png" not in "".join(c["text"] for c in chunks)

    gated = sol_search.cms_chunks({**open_doc, "slug": "secret", "gated": True, "body": "## Section\nPaid-for secret detail."})
    assert len(gated) == 1 and "secret detail" not in gated[0]["text"] and "gated download" in gated[0]["text"]


def test_published_cms_content_is_answerable_and_drafts_are_not(chat_app, monkeypatch):
    from fastapi.testclient import TestClient
    chat, app, fake_db = chat_app
    seen = []

    async def capture(messages, tools=None, **kw):
        seen.append(messages[0]["content"])
        yield {"type": "text", "text": "ok"}

    monkeypatch.setattr(chat, "stream_completion", capture)
    run_sync(fake_db.content.insert_many([
        {"id": "c1", "slug": "zebra-vault-launch", "title": "Zebravault Launch", "type": "news", "status": "published", "publish_at": "2026-01-01T00:00:00+00:00",
         "summary": "Zebravault is the new immutable archive tier.", "body": "## Pricing model\nZebravault is billed per terabyte."},
        {"id": "c2", "slug": "draft-thing", "title": "Quokkaplan Draft", "type": "blog", "status": "draft", "publish_at": None, "summary": "Quokkaplan secret", "body": ""},
    ]))
    with TestClient(app) as client:
        client.post("/api/chat/stream", json={"session_id": "sess-cms1", "message": "What is Zebravault and how is it billed?"})
        client.post("/api/chat/stream", json={"session_id": "sess-cms2", "message": "Tell me about Quokkaplan"})
    assert "/resources/zebra-vault-launch" in seen[0] and "billed per terabyte" in seen[0]
    assert "Quokkaplan secret" not in seen[1]


def test_cms_item_replaces_builtin_article_at_same_address():
    builtin = next(c for c in sol_search.bundled_chunks() if c["kind"] == "article")
    slug = builtin["url"].rsplit("/", 1)[1]

    class FakeContent:
        def find(self, *a, **k):
            class Cursor:
                async def to_list(self, n):
                    return [{"slug": slug, "title": "Rewritten", "type": "blog", "summary": "New version.", "body": ""}]
            return Cursor()

    class FakeDb:
        content = FakeContent()

    run_sync(sol_search.refresh_cms(FakeDb(), force=True))
    at_url = [c for c in sol_search.index().chunks if c["url"] == builtin["url"]]
    assert [c["id"] for c in at_url] == [f"cms:{slug}"]
    sol_search._index = None


# ---------- open conversation ----------
def test_comparison_questions_bring_the_differentiators(chat_app, monkeypatch):
    from fastapi.testclient import TestClient
    chat, app, _ = chat_app
    seen = []

    async def capture(messages, tools=None, **kw):
        seen.append((messages[0]["content"], kw))
        yield {"type": "text", "text": "ok"}

    monkeypatch.setattr(chat, "stream_completion", capture)
    with TestClient(app) as client:
        client.post("/api/chat/stream", json={"session_id": "sess-cmp1", "message": "how is it better than others?"})
        client.post("/api/chat/stream", json={"session_id": "sess-open", "message": "tell me a joke"})
    comparison, small_talk = seen[0][0], seen[1][0]
    assert "(page: /platform#why-solix)" in comparison and "not a portfolio of acquisitions" in comparison
    # Nothing on the site matches small talk: Sol is told to use its own knowledge, not to deflect.
    assert "answer from your own knowledge" in small_talk and "(page: /" not in small_talk.split("Site knowledge for this turn:")[1]
    assert seen[0][1]["temperature"] == chat.TEMPERATURE


def test_temperature_reaches_the_provider():
    llm._cooldown.clear()
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, text=sse_body(text_chunk("hi")))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    collect(llm.stream_completion([], temperature=0.7, providers=[provider("a")], client=client))
    assert bodies[0]["temperature"] == 0.7


def test_prompt_allows_general_knowledge_but_guards_solix_facts():
    import knowledge
    p = knowledge.CONCIERGE_SYSTEM_PROMPT
    assert "Use it freely for anything general" in p
    assert "Solix-specific facts" in p and "Never invent Solix numbers" in p
