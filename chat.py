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
import sol_search

logger = logging.getLogger("solix.chat")
router = APIRouter(prefix="/api/chat", tags=["chat"])

HISTORY_LIMIT = 16
HISTORY_CHARS = 1500
# Everything sent per request (instructions, tools, site knowledge, history) is
# kept under this many tokens, so one message fits well inside free tiers'
# per-minute caps (Groq: 8K tokens/min) and each day's quota stretches further.
INPUT_TOKEN_BUDGET = int(os.environ.get("SOL_INPUT_TOKEN_BUDGET", "5000"))
MAX_OUTPUT_TOKENS = int(os.environ.get("SOL_MAX_OUTPUT_TOKENS", "900"))
# Higher is more conversational and varied; lower is more literal.
TEMPERATURE = float(os.environ.get("SOL_TEMPERATURE", "0.6"))
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
    hits = sol_search.search(message, 6)
    # Short follow-ups ("and for healthcare?") lean on the previous question.
    if len(message.split()) < 8 and last_user:
        hits += sol_search.search(f"{last_user} {message}", 4)
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
    """Which AI providers are configured (names only), for health checks."""
    return {"ai": bool(configured_providers()), "providers": [p.name for p in configured_providers()], "knowledge_chunks": len(sol_search.index().chunks)}


@router.get("/{session_id}", response_model=List[ChatMessage])
async def get_chat_history(session_id: str):
    docs = await db.chat_messages.find({"session_id": session_id}, {"_id": 0}).sort("created_at", 1).to_list(200)
    return [ChatMessage(**d) for d in docs]


@router.delete("/{session_id}", status_code=204)
async def clear_chat_history(session_id: str):
    await db.chat_messages.delete_many({"session_id": session_id})
    return None


@router.post("/stream")
async def chat_stream(req: ChatRequest, request: Request):
    if _rate_limited("session", req.session_id) or _rate_limited("ip", _client_ip(request)):
        raise HTTPException(status_code=429, detail="Too many messages. Please wait a moment.")
    # Contact details are saved from the message itself before any model runs,
    # so a lead is never lost to a model that didn't notice, or wasn't up.
    captured = {}
    try:
        found = chat_leads.extract_contact(req.message)
        if found:
            captured = await chat_leads.capture(req.session_id, found, trusted=False, page=req.page, language=req.language, visitor_id=req.visitor_id)
    except Exception:
        logger.exception("chat lead capture failed")
    if not configured_providers():
        raise HTTPException(status_code=503, detail="AI concierge is not configured")

    recent = await db.chat_messages.find({"session_id": req.session_id}, {"_id": 0, "role": 1, "content": 1}).sort("created_at", -1).to_list(HISTORY_LIMIT)
    history = [{"role": m["role"], "content": m["content"][:HISTORY_CHARS]} for m in reversed(recent)]

    await sol_search.refresh_cms(db)
    lang = LANGUAGE_NAMES.get(req.language.split("-")[0].lower(), "English")
    context = sol_search.format_context(_retrieve(req.message, history, req.page), max_chars=CONTEXT_CHARS)
    visitor = f"The visitor is on page {req.page}" + (f' ("{req.page_title}")' if req.page_title else "") + "." if req.page else "Page unknown."
    contact = await chat_leads.get_contact(req.session_id) or {}
    known = ", ".join(f"{k.replace('_', ' ')}: {contact[k]}" for k in chat_leads.CONTACT_FIELDS if contact.get(k))
    known_line = (f"Visitor details already saved: {known}. Don't ask for these again; use their name naturally."
                  if known else "No contact details saved yet.")
    turn_system = (
        f"{visitor} {known_line} Reply in {lang} unless the visitor writes in another language; keep product names and page paths as they are.\n\n"
        f"Site knowledge for this turn:\n{context or '(no Solix pages match this message; answer from your own knowledge, and use search_site only if it asks about Solix specifics)'}"
    )
    # One leading system message: some providers reject system turns mid-conversation.
    system = f"{CONCIERGE_SYSTEM_PROMPT}\n\n## This turn\n{turn_system}"
    history = _fit_history(history, INPUT_TOKEN_BUDGET - _tokens(system) - _tokens(json.dumps(TOOLS)) - _tokens(req.message))
    messages = [{"role": "system", "content": system}, *history, {"role": "user", "content": req.message}]

    async def generate():
        await save_message(req.session_id, "user", req.message, page=req.page, language=req.language)
        full, meta, tools_used = "", {}, []
        if captured:
            yield sse({"event": "lead_captured", "fields": sorted(captured)})
        try:
            for round_no in range(MAX_TOOL_ROUNDS):
                text, calls = "", []
                # The last round gets no tools so the model must answer.
                async for event in stream_completion(messages, TOOLS if round_no < MAX_TOOL_ROUNDS - 1 else None, max_tokens=MAX_OUTPUT_TOKENS, temperature=TEMPERATURE):
                    if event["type"] == "text":
                        text += event["text"]
                        yield sse({"delta": event["text"]})
                    elif event["type"] == "tool_calls":
                        calls = event["calls"]
                    elif event["type"] == "meta":
                        meta = {"provider": event["provider"], "model": event["model"]}
                full += text
                if not calls:
                    break
                messages.append({"role": "assistant", "content": text or None, "tool_calls": [
                    {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}} for c in calls]})
                for c in calls:
                    tools_used.append(c["name"])
                    if c["name"] == "search_site":
                        hits = sol_search.search(str(c["arguments"].get("query", "")), 4)
                        outcome = {"results": [{"title": h["title"], "page": h["url"], "text": h["text"][:800]} for h in hits]} if hits else {"results": [], "note": "Nothing on the site matches."}
                    elif c["name"] == "save_visitor_details":
                        saved = await chat_leads.capture(req.session_id, c["arguments"], trusted=True, page=req.page, language=req.language, visitor_id=req.visitor_id)
                        outcome = {"ok": True, "saved": sorted(saved)} if saved else {"ok": True, "note": "Nothing new to save."}
                        if saved:
                            yield sse({"event": "lead_captured", "fields": sorted(saved)})
                    elif c["name"] == "create_demo_request":
                        outcome = await create_demo_request(req.session_id, c["arguments"])
                        if outcome.get("ok"):
                            yield sse({"event": "demo_booked", "submission_id": outcome["submission_id"], "name": c["arguments"].get("name"), "email": c["arguments"].get("email"), "company": c["arguments"].get("company")})
                    elif c["name"] == "request_expert_contact":
                        outcome = await request_expert_contact(req.session_id, c["arguments"])
                        if outcome.get("ok"):
                            yield sse({"event": "expert_requested", "submission_id": outcome["submission_id"], "name": c["arguments"].get("name"), "email": c["arguments"].get("email")})
                    else:
                        outcome = {"ok": False, "error": f"Unknown tool {c['name']}"}
                    messages.append({"role": "tool", "tool_call_id": c["id"], "name": c["name"], "content": json.dumps(outcome)})
                if text and not text.endswith(("\n", " ")):
                    full += "\n\n"
                    yield sse({"delta": "\n\n"})
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
            await save_message(req.session_id, "assistant", full, tools=tools_used or None, **meta)
        yield sse({"done": True})

    return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
