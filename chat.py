import os
import re
import json
import time
import uuid
import asyncio
import logging
from collections import defaultdict, deque
from typing import List, Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, ConfigDict

from database import db, now_iso
from knowledge import CONCIERGE_SYSTEM_PROMPT, LANGUAGE_NAMES
from emailer import notify_lead
from intent import record_submission
import chat_leads
from llm import ProviderError, configured_providers, stream_completion
import sol_config
import sol_guard
import sol_learning
import sol_search
import internal_traffic

logger = logging.getLogger("solix.chat")
router = APIRouter(prefix="/api/chat", tags=["chat"])

HISTORY_LIMIT = 16
HISTORY_CHARS = 1500
# Everything sent per request (instructions, tools, site knowledge, history) is
# kept under this many tokens, so one message fits well inside free tiers'
# per-minute caps (Groq: 8K tokens/min) and each day's quota stretches further.
INPUT_TOKEN_BUDGET = int(os.environ.get("SOL_INPUT_TOKEN_BUDGET", "5000"))
# Temperature, answer length and the rest are set in Admin > Conversations > Control (sol_config.py).
# Answers being written at once across all visitors; past this a visitor gets a quick "busy" instead of a slow queue.
MAX_CONCURRENT_STREAMS = int(os.environ.get("SOL_MAX_CONCURRENT_STREAMS", "80"))
# "How is it better", "vs Informatica", "why choose you": always bring the differentiators.
COMPARISON_RX = re.compile(r"\b(better|best|vs\.?|versus|compare[sd]?|comparison|competitor|competition|alternative|differen\w*|unique|stand out|why (choose|solix|you|pick|should))\b", re.I)
DIFFERENTIATOR_IDS = ("platform:why-solix", "platform:it-leaders")
CONTEXT_CHARS = 4000
MAX_TOOL_ROUNDS = 4
EMAIL_RX = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_site",
            "description": "Search the Solix website (products, solutions, industries, articles, case studies, newsroom, careers, partners, company) and return matching excerpts with their page paths.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Keywords to search for, e.g. 'SAP ECC retirement' or 'HIPAA archiving'"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_visitor_details",
            "description": "Save contact or company details the visitor has shared (name, email, phone, company, job title, what they're interested in). Call it as soon as any such detail appears in the conversation, without asking for confirmation, then carry on the conversation normally. Never invent values.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "email": {"type": "string"},
                    "phone": {"type": "string"},
                    "company": {"type": "string"},
                    "job_title": {"type": "string"},
                    "interest": {"type": "string", "description": "Product, solution or problem they care about"},
                    "notes": {"type": "string", "description": "One line on their situation or need, useful for sales follow-up"},
                    "problem": {"type": "string", "description": "Their problem in one line, in their terms"},
                    "industry": {"type": "string"},
                    "systems": {"type": "string", "description": "Systems or data sources involved, comma separated (e.g. SAP ECC, mainframe)"},
                    "goals": {"type": "string", "description": "What they want: cut cost, compliance, retire apps, migrate, privacy, AI (comma separated)"},
                    "timeline": {"type": "string"},
                    "data_volume": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_demo_request",
            "description": "Book a demo or pricing conversation. Needs the visitor's email; include name and company if known. Call once the visitor has said they want it. Never invent values.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Visitor's full name"},
                    "email": {"type": "string", "description": "Visitor's work email address"},
                    "company": {"type": "string", "description": "Visitor's company"},
                    "interest": {"type": "string", "description": "Product or solution of interest, if mentioned"},
                    "notes": {"type": "string", "description": "One-sentence summary of what the visitor wants to see or solve"},
                },
                "required": ["email"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_expert_contact",
            "description": "Pass a visitor's question to a Solix expert who will reply by email. Needs their email and question; include name if known. Call once the visitor has asked for it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "email": {"type": "string"},
                    "company": {"type": "string"},
                    "question": {"type": "string", "description": "The visitor's question or request, in their words"},
                },
                "required": ["email", "question"],
            },
        },
    },
]


PROFILE_ARGS = ("problem", "industry", "systems", "goals", "timeline", "data_volume")


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=6, max_length=80)
    message: str = Field(min_length=1, max_length=2000)
    language: str = Field(default="en", max_length=8)
    page: Optional[str] = Field(default=None, max_length=300)
    page_title: Optional[str] = Field(default=None, max_length=200)
    # Links the chat lead to this visitor's browsing (only sent with analytics consent).
    visitor_id: Optional[str] = Field(default=None, max_length=64)


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    session_id: str
    role: Literal["user", "assistant"]
    content: str
    created_at: str


# --- abuse protection: per-session and per-IP sliding windows (in memory; one instance) ---
_hits = defaultdict(deque)
LIMITS = {"session": (20, 300), "ip": (60, 3600)}  # (messages, seconds)
_daily: dict = {}  # (utc date, ip) -> messages today
_inflight = 0


def _daily_limited(ip: str, limit: int) -> bool:
    day = time.strftime("%Y-%m-%d", time.gmtime())
    if len(_daily) > 50000:
        for k in [k for k in _daily if k[0] != day]:
            _daily.pop(k, None)
    n = _daily.get((day, ip), 0) + 1
    _daily[(day, ip)] = n
    return n > limit


def _rate_limited(kind: str, key: str) -> bool:
    limit, window = LIMITS[kind]
    q, now = _hits[(kind, key)], time.monotonic()
    while q and q[0] < now - window:
        q.popleft()
    if len(q) >= limit:
        return True
    q.append(now)
    return False


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() or (request.client.host if request.client else "unknown")


async def save_message(session_id: str, role: str, content: str, **meta) -> None:
    if internal_traffic.session_internal(session_id):  # staff chatting with Sol: kept out of chat metrics
        meta["internal"] = True
    await db.chat_messages.insert_one({"id": str(uuid.uuid4()), "session_id": session_id, "role": role, "content": content, "created_at": now_iso(), **meta})


def sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


def _clean(args: dict, key: str) -> str:
    return str(args.get(key) or "").strip()


async def _save_lead(session_id: str, kind: str, args: dict, message: Optional[str]) -> dict:
    doc = {
        "id": str(uuid.uuid4()),
        "type": kind,
        "email": _clean(args, "email"),
        "name": _clean(args, "name") or None,
        "company": _clean(args, "company") or None,
        "phone": _clean(args, "phone") or None,
        "job_title": _clean(args, "job_title") or None,
        "interest": _clean(args, "interest") or None,
        "message": message,
        "source": "chat",
        "source_page": f"chat:{session_id}",
        "created_at": now_iso(),
    }
    await internal_traffic.stamp(doc, session_id=session_id)
    await db.submissions.insert_one(dict(doc))
    asyncio.create_task(notify_lead(doc))
    try:
        await record_submission(doc)
    except Exception:
        logger.exception("lead scoring failed for chat %s %s", kind, doc["id"])
    logger.info("chat %s request saved %s", kind, doc["id"])
    return doc


async def _fill_from_chat(session_id: str, args: dict) -> dict:
    """Bookings reuse whatever the visitor already shared earlier in the chat."""
    known = await chat_leads.get_contact(session_id) or {}
    return {**{k: known.get(k) for k in ("name", "company", "phone", "job_title") if known.get(k)}, **{k: v for k, v in args.items() if v}}


async def create_demo_request(session_id: str, args: dict) -> dict:
    args = await _fill_from_chat(session_id, args)
    email = _clean(args, "email")
    if not EMAIL_RX.match(email):
        return {"ok": False, "error": "The email address is missing or looks invalid. Ask the visitor for their work email."}
    await chat_leads.capture(session_id, args, trusted=True, alert=False)
    doc = await _save_lead(session_id, "demo", args, _clean(args, "notes") or None)
    return {"ok": True, "submission_id": doc["id"], "message": "Demo request saved. A Solix expert will reach out within one business day."}


async def request_expert_contact(session_id: str, args: dict) -> dict:
    args = await _fill_from_chat(session_id, args)
    email, question = _clean(args, "email"), _clean(args, "question")
    if not EMAIL_RX.match(email):
        return {"ok": False, "error": "The email address is missing or looks invalid. Ask the visitor for it."}
    if len(question) < 3:
        return {"ok": False, "error": "The question is missing. Ask the visitor what they'd like the expert to answer."}
    await chat_leads.capture(session_id, args, trusted=True, alert=False)
    doc = await _save_lead(session_id, "contact", args, question)
    return {"ok": True, "submission_id": doc["id"], "message": "Question passed to a Solix expert, who will reply by email within one business day."}


# --- Sol's tools in the shared registry (tool_registry.py): routed, scoped, validated before they run ----------

import tool_registry as tr  # noqa: E402


def _schema(name: str) -> dict:
    return next(t["function"] for t in TOOLS if t["function"]["name"] == name)


async def _t_search(args: dict, ctx: tr.Ctx) -> dict:
    found = sol_search.search(str(args.get("query", ""))[:200], 4)
    return {"results": [{"title": h["title"], "page": h["url"], "text": h["text"][:800]} for h in found] if found else [],
            **({} if found else {"note": "Nothing on the site matches."}), "_score": max((h["score"] for h in found), default=0.0)}


async def _t_save(args: dict, ctx: tr.Ctx) -> dict:
    req = ctx.extra["req"]
    saved = await chat_leads.capture(req.session_id, {k: v for k, v in args.items() if k not in PROFILE_ARGS}, trusted=True, page=req.page,
                                     language=req.language, visitor_id=req.visitor_id)
    situation = {k: args[k] for k in PROFILE_ARGS if args.get(k)}
    if situation:
        await sol_learning.update_profile(req.session_id, situation, req.visitor_id)
    out = {"ok": True, "saved": sorted(saved) + sorted(situation)} if (saved or situation) else {"ok": True, "note": "Nothing new to save."}
    if saved:
        out["_events"] = [{"event": "lead_captured", "fields": sorted(saved)}]
    return out


async def _v_save(args: dict, ctx: tr.Ctx) -> dict:
    if not any(str(v).strip() for v in args.values() if v is not None):
        return {"problems": ["Nothing to save: call it only when the visitor has shared a detail."]}
    if args.get("email") and not EMAIL_RX.match(str(args["email"]).strip()):
        return {"problems": ["That email address looks invalid; ask the visitor to check it."]}
    return {"preview": {"fields": sorted(k for k, v in args.items() if v)}}


async def _v_contact(args: dict, ctx: tr.Ctx, *, need_question: bool = False) -> dict:
    filled = await _fill_from_chat(ctx.extra["req"].session_id, dict(args))
    problems = []
    if not EMAIL_RX.match(_clean(filled, "email")):
        problems.append("The email address is missing or looks invalid. Ask the visitor for their work email.")
    if need_question and len(_clean(filled, "question")) < 3:
        problems.append("The question is missing. Ask the visitor what they'd like the expert to answer.")
    return {"problems": problems, "preview": {"email_on_file": bool(_clean(filled, "email"))}}


async def _t_demo(args: dict, ctx: tr.Ctx) -> dict:
    out = await create_demo_request(ctx.extra["req"].session_id, args)
    if out.get("ok"):
        out["_events"] = [{"event": "demo_booked", "submission_id": out["submission_id"], "name": args.get("name"), "email": args.get("email"), "company": args.get("company")}]
    return out


async def _t_expert(args: dict, ctx: tr.Ctx) -> dict:
    out = await request_expert_contact(ctx.extra["req"].session_id, args)
    if out.get("ok"):
        out["_events"] = [{"event": "expert_requested", "submission_id": out["submission_id"], "name": args.get("name"), "email": args.get("email")}]
    return out


async def _v_expert(args: dict, ctx: tr.Ctx) -> dict:
    return await _v_contact(args, ctx, need_question=True)


SOL = frozenset({"sol"})
tr.REGISTRY.register(tr.Tool(**{**_schema("search_site"), "parameters": {**_schema("search_site")["parameters"], "properties": {"query": {"type": "string", "maxLength": 300, "description": "Keywords to search for, e.g. 'SAP ECC retirement' or 'HIPAA archiving'"}}},
                                "access": "read", "handler": _t_search, "surfaces": SOL, "core": True,
                                "examples": ("what does solix offer for sap archiving", "do you have a case study for healthcare", "which page covers application retirement")}))
tr.REGISTRY.register(tr.Tool(**_schema("save_visitor_details"), access="write", handler=_t_save, validate=_v_save, surfaces=SOL, confirm=False,
                             examples=("my name is jane and I work at acme", "you can reach me at jane@acme.com", "I'm the CIO at a bank", "we run SAP ECC and a mainframe",
                                       "our goal is to cut storage cost by next quarter", "call me on +1 555 0100", "I'm with a hospital group", "we have about 200 TB")))
tr.REGISTRY.register(tr.Tool(**_schema("create_demo_request"), access="write", handler=_t_demo, validate=_v_contact, surfaces=SOL, confirm=False,
                             examples=("book a demo", "can I see a demo", "yes please set up the demo", "I'd like a pricing call", "schedule a walkthrough for my team",
                                       "let's get a meeting on the calendar", "show me the product live")))
tr.REGISTRY.register(tr.Tool(**_schema("request_expert_contact"), access="write", handler=_t_expert, validate=_v_expert, surfaces=SOL, confirm=False,
                             examples=("can someone from sales email me", "I want to talk to a human", "pass this to an expert", "I have a support issue",
                                       "partnership enquiry, who do I speak to", "have a specialist answer this by email")))


def _tokens(text: str) -> int:
    """Rough token count (about 4 characters per token for English)."""
    return len(text) // 4 + 4


def _fit_history(history: List[dict], room: int) -> List[dict]:
    """The most recent turns that fit in `room` tokens, oldest first."""
    kept = []
    for m in reversed(history):
        room -= _tokens(m["content"])
        if room < 0:
            break
        kept.append(m)
    kept.reverse()
    # Start on a visitor turn; some providers reject a leading assistant message.
    while kept and kept[0]["role"] != "user":
        kept.pop(0)
    return kept


def _retrieve(message: str, history: List[dict], page: Optional[str]) -> List[dict]:
    """Top excerpts for this turn, plus the page the visitor is reading."""
    last_user = next((m["content"] for m in reversed(history) if m["role"] == "user"), "")
    hits = sol_search.hybrid_search(message, 6)
    # Short follow-ups ("and for healthcare?") lean on the previous question.
    if len(message.split()) < 8 and last_user:
        hits += sol_search.hybrid_search(f"{last_user} {message}", 4)
    if page and page not in ("/", ""):
        hits = [c for c in sol_search.index().chunks if c["url"] == page.split("?")[0]][:2] + hits
    if COMPARISON_RX.search(message):
        hits = [c for c in sol_search.index().chunks if c["id"] in DIFFERENTIATOR_IDS] + hits
    seen, per_url, out = set(), defaultdict(int), []
    for h in hits:
        if h["id"] in seen or per_url[h["url"]] >= 3:
            continue
        seen.add(h["id"])
        per_url[h["url"]] += 1
        out.append(h)
    return out[:7]


def _sources(message: str) -> List[dict]:
    """Pages worth showing as 'Related' chips: only clearly relevant ones."""
    hits = sol_search.search(message, 8)
    if not hits or hits[0]["score"] < 6:
        return []
    top, out, urls = hits[0]["score"], [], set()
    for h in hits:
        if h["score"] < top * 0.6 or h["url"] in urls:
            continue
        urls.add(h["url"])
        out.append({"title": h["title"].split(":")[0], "url": h["url"]})
    return out[:3]


@router.get("/status")
async def chat_status():
    """Whether AI answers are on. Which providers and models answer is admin-only (sol_admin.py)."""
    cfg = await sol_config.load()
    return {"ai": bool(cfg.enabled and sol_config.route(cfg, configured_providers()))}


@router.post("/capture")
async def chat_capture(req: ChatRequest, request: Request):
    """Contact capture only, for the widget's offline concierge (no model involved)."""
    ip = _client_ip(request)
    if _rate_limited("session", req.session_id) or _rate_limited("ip", ip):
        raise HTTPException(status_code=429, detail="Too many messages. Please wait a moment.")
    await internal_traffic.check_session(req.session_id, ip=ip, visitor_id=req.visitor_id)
    found = chat_leads.extract_contact(req.message)
    saved = await chat_leads.capture(req.session_id, found, trusted=False, page=req.page, language=req.language, visitor_id=req.visitor_id) if found else {}
    return {"fields": sorted(saved)}


@router.get("/{session_id}", response_model=List[ChatMessage])
async def get_chat_history(session_id: str):
    docs = await db.chat_messages.find({"session_id": session_id}, {"_id": 0}).sort("created_at", 1).to_list(200)
    return [ChatMessage(**d) for d in docs]


@router.delete("/{session_id}", status_code=204)
async def clear_chat_history(session_id: str):
    await db.chat_messages.delete_many({"session_id": session_id})
    return None


def _stream_text(text: str, size: int = 28):
    """A ready answer (cache or guard) sent in small pieces, so the widget renders it like a live one."""
    for i in range(0, len(text), size):
        yield sse({"delta": text[i:i + size]})


@router.post("/stream")
async def chat_stream(req: ChatRequest, request: Request):
    received = time.monotonic()  # prep_ms: how long a visitor waits before any model is asked (the widget times out)
    ip = _client_ip(request)
    if _rate_limited("session", req.session_id) or _rate_limited("ip", ip):
        raise HTTPException(status_code=429, detail="Too many messages. Please wait a moment.")
    cfg = await sol_config.load()
    if _daily_limited(ip, cfg.daily_ip_limit):
        raise HTTPException(status_code=429, detail="You've reached today's chat limit. Please contact us at /contact and a Solix expert will help.")
    # Staff testing Sol (internal network, a browser marked internal, or an internal email shared earlier).
    internal = await internal_traffic.check_session(req.session_id, ip=ip, visitor_id=req.visitor_id)
    # Contact details are saved from the message itself before any model runs,
    # so a lead is never lost to a model that didn't notice, or wasn't up.
    captured = {}
    try:
        found = chat_leads.extract_contact(req.message)
        if found:
            captured = await chat_leads.capture(req.session_id, found, trusted=False, page=req.page, language=req.language, visitor_id=req.visitor_id)
    except Exception:
        logger.exception("chat lead capture failed")
    # What the visitor's situation is, read from every message (no model needed).
    prior = {}
    try:
        profile = prior = await sol_learning.get_profile(req.session_id, req.visitor_id)
        seen = sol_learning.read_message(req.message)
        if seen or (profile.get("returning") and not profile.get("session_id")):
            profile = await sol_learning.update_profile(req.session_id, seen, req.visitor_id, profile)
    except Exception:
        logger.exception("visitor profile update failed")
        profile = {}

    # The protection layer: coding, jailbreaks, key probes and free-LLM use never reach a model.
    verdict = sol_guard.screen(req.message, block_code=cfg.block_code)
    if verdict:
        logger.info("Sol guard blocked a %s message in %s", verdict.kind, req.session_id)

        async def guarded():
            await save_message(req.session_id, "user", req.message, page=req.page, language=req.language, guard=verdict.kind)
            if captured:
                yield sse({"event": "lead_captured", "fields": sorted(captured)})
            for piece in _stream_text(verdict.reply):
                yield piece
            await save_message(req.session_id, "assistant", verdict.reply, provider="guard", model=verdict.kind)
            yield sse({"done": True})
        return StreamingResponse(guarded(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    routed = sol_config.route(cfg, configured_providers()) if cfg.enabled else []
    if not routed:
        raise HTTPException(status_code=503, detail="AI concierge is not configured")
    if _inflight >= MAX_CONCURRENT_STREAMS:
        raise HTTPException(status_code=503, detail="Sol is busy right now. Please try again in a few seconds.")

    recent = await db.chat_messages.find({"session_id": req.session_id}, {"_id": 0, "role": 1, "content": 1}).sort("created_at", -1).to_list(HISTORY_LIMIT)
    history = [{"role": m["role"], "content": m["content"][:HISTORY_CHARS]} for m in reversed(recent)]

    await sol_search.ensure_fresh(db)
    contact = await chat_leads.get_contact(req.session_id) or {}
    # What this message alone says is part of the question itself; anything known from before makes it personal.
    personal = bool(history or contact or sol_learning.describe_profile(prior))

    # A first, impersonal question answered before against the same knowledge: instant, free, identical.
    hit = None
    if cfg.answer_cache and not personal:
        try:
            hit = await sol_learning.cached_answer(req.message, req.language)
        except Exception:
            logger.exception("answer cache lookup failed")
    if hit:
        async def from_cache():
            await save_message(req.session_id, "user", req.message, page=req.page, language=req.language)
            if captured:
                yield sse({"event": "lead_captured", "fields": sorted(captured)})
            for piece in _stream_text(hit["answer"]):
                yield piece
            if hit.get("sources"):
                yield sse({"event": "sources", "sources": hit["sources"]})
            await save_message(req.session_id, "assistant", hit["answer"], provider="cache", model=hit.get("model"))
            yield sse({"done": True})
        return StreamingResponse(from_cache(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    lang = LANGUAGE_NAMES.get(req.language.split("-")[0].lower(), "English")
    hits = _retrieve(req.message, history, req.page)
    top_score = max((h.get("score", 0) for h in hits), default=0.0)
    context = sol_search.format_context(hits, max_chars=CONTEXT_CHARS)
    # Visitors never trigger web search: the public chat answers only from Solix knowledge.
    # Admins research the web in Admin > Conversations > Knowledge and approve what Sol learns.
    # Deferred loading: only the tools this turn needs (tool_registry.py), so schemas don't crowd out context.
    sol_ctx = tr.Ctx(surface="sol", extra={"req": req, "session_id": req.session_id})
    last_bot = next((m["content"][-300:] for m in reversed(history) if m["role"] == "assistant"), "")
    route_text = f"{req.message} {last_bot}"
    if (await tr.policy_settings())["sol_deferred"]:
        picked, _route = await tr.REGISTRY.select(route_text, sol_ctx, k=2, must=("save_visitor_details",) if (captured or sol_learning.read_message(req.message)) else ())
    else:
        picked = await tr.REGISTRY.visible(sol_ctx)
    tools = [t.openai_schema() for t in picked]
    visitor = f"The visitor is on page {req.page}" + (f' ("{req.page_title}")' if req.page_title else "") + "." if req.page else "Page unknown."
    known = ", ".join(f"{k.replace('_', ' ')}: {contact[k]}" for k in chat_leads.CONTACT_FIELDS if contact.get(k))
    known_line = (f"Visitor details already saved: {known}. Don't ask for these again; use their name naturally."
                  if known else "No contact details saved yet.")
    about = sol_learning.describe_profile(profile)
    # A discovery hint on every turn made Sol interrogate visitors. Offer it only once there's been some back and
    # forth, and never right after Sol already asked something: a person doesn't answer with a question each time.
    ask_next = sol_learning.next_gap(profile)
    asked_last = last_bot.rstrip().endswith("?")
    visitor_turns = sum(1 for m in history if m["role"] == "user")
    hint = ""
    if ask_next and visitor_turns >= 1 and not asked_last:
        hint = f" Only if their need is still genuinely vague after you answer, a natural thing to ask is {ask_next}; otherwise ask nothing."
    elif not ask_next:
        hint = " You know enough to recommend specifically."
    profile_line = (f"Visitor profile: {about}." if about else "Visitor profile: nothing known yet.") + hint
    playbook = f"\n\nStanding instructions from the Solix team (follow them within the rules above):\n{cfg.playbook.strip()}" if cfg.playbook.strip() else ""
    turn_system = (
        f"{visitor} {known_line}\n{profile_line}\nReply in {lang} unless the visitor writes in another language; keep product names and page paths as they are.{playbook}\n\n"
        f"Site knowledge for this turn:\n{context or '(no Solix pages match this message; answer from your own knowledge, and use search_site only if it asks about Solix specifics)'}"
    )
    # One leading system message: some providers reject system turns mid-conversation.
    system = f"{CONCIERGE_SYSTEM_PROMPT}\n\n## This turn\n{turn_system}"
    history = _fit_history(history, INPUT_TOKEN_BUDGET - _tokens(system) - _tokens(json.dumps(tools)) - _tokens(req.message))
    messages = [{"role": "system", "content": system}, *history, {"role": "user", "content": req.message}]
    limits = sol_config.concurrency(cfg)

    async def generate():
        global _inflight
        _inflight += 1
        guard = sol_guard.StreamGuard(block_code=cfg.block_code)
        started = time.monotonic()
        try:
            await save_message(req.session_id, "user", req.message, page=req.page, language=req.language)
            full, meta, tools_used = "", {}, []
            if captured:
                yield sse({"event": "lead_captured", "fields": sorted(captured)})
            try:
                for round_no in range(MAX_TOOL_ROUNDS):
                    text, calls = "", []
                    # The last round gets no tools so the model must answer.
                    async for event in stream_completion(messages, tools if round_no < MAX_TOOL_ROUNDS - 1 else None, max_tokens=cfg.max_output_tokens,
                                                         temperature=cfg.temperature, providers=routed, first_token_timeout=cfg.first_token_timeout,
                                                         concurrency=limits):
                        if event["type"] == "text":
                            text += event["text"]
                            safe = guard.feed(event["text"])
                            if safe:
                                full += safe
                                yield sse({"delta": safe})
                        elif event["type"] == "tool_calls":
                            calls = event["calls"]
                        elif event["type"] == "meta":
                            meta = {"provider": event["provider"], "model": event["model"]}
                    if guard.stopped:
                        break
                    if not calls:
                        break
                    messages.append({"role": "assistant", "content": text or None, "tool_calls": [
                        {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}} for c in calls]})
                    for c in calls:
                        tools_used.append(c["name"])
                        args = c["arguments"] if isinstance(c["arguments"], dict) else {}
                        try:
                            outcome = await tr.REGISTRY.execute(c["name"], args, sol_ctx)
                        except tr.PolicyError as e:
                            outcome = {"ok": False, "error": str(e)}
                        if not isinstance(outcome, dict):
                            outcome = {"result": outcome}
                        best_score[0] = max(best_score[0], outcome.pop("_score", 0.0) or 0.0)
                        for ev in outcome.pop("_events", []):
                            yield sse(ev)
                        messages.append({"role": "tool", "tool_call_id": c["id"], "name": c["name"], "content": json.dumps(outcome)})
                    if text and not text.endswith(("\n", " ")):
                        guard.feed("\n\n")
                rest = guard.flush()
                if rest:
                    full += rest
                    yield sse({"delta": rest})
            except ProviderError as e:
                logger.error("Sol could not answer: %s", e)
                yield sse({"error": "The concierge is temporarily unavailable. Please try again."})
                return
            except Exception:
                logger.exception("chat stream failed")
                yield sse({"error": "The concierge is temporarily unavailable. Please try again."})
                return
            sources = _sources(req.message)
            if sources:
                yield sse({"event": "sources", "sources": sources})
            if full:
                await save_message(req.session_id, "assistant", full, tools=tools_used or None, guard=guard.events or None,
                                   latency_ms=round((time.monotonic() - started) * 1000), prep_ms=round((started - received) * 1000),
                                   top_score=top_score, **meta)
            yield sse({"done": True})
            await _after_answer(full, meta, tools_used, guard, sources)
        finally:
            _inflight -= 1

    best_score = [top_score]

    async def _after_answer(full, meta, tools_used, guard, sources):
        """Learning and caching; never allowed to break a finished answer."""
        try:
            # The team's own test questions don't count as gaps in what visitors ask.
            if cfg.learning and full and max(best_score) < sol_learning.GAP_SCORE and not (internal or internal_traffic.session_internal(req.session_id)):
                await sol_learning.log_gap(req.message, full, round(max(best_score), 2), req.session_id)
            cacheable = cfg.answer_cache and full and not personal and not guard.events and meta.get("provider") and len(full) > 60 and not (
                set(tools_used) - {"search_site"}) and not captured
            if cacheable:
                await sol_learning.store_answer(req.message, req.language, full, sources, meta["provider"], meta["model"], cfg.cache_hours)
        except Exception:
            logger.exception("Sol learning/caching after an answer failed")

    return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
