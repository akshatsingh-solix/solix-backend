import os
import re
import json
import uuid
import asyncio
import logging
from typing import List, Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, ConfigDict

from database import db, now_iso
from knowledge import CONCIERGE_SYSTEM_PROMPT
from emailer import notify_lead
from intent import record_submission

logger = logging.getLogger("solix.chat")
router = APIRouter(prefix="/api/chat", tags=["chat"])

try:
    from openai import AsyncOpenAI
except ImportError:
    AsyncOpenAI = None

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
# None when the openai package isn't installed or no key is set - the
# concierge endpoint degrades to a clean 503 rather than the whole API
# failing to start.
_client = AsyncOpenAI(api_key=OPENAI_API_KEY) if (AsyncOpenAI is not None and OPENAI_API_KEY) else None

HISTORY_LIMIT = 24
MAX_TOOL_ROUNDS = 3
EMAIL_RX = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

DEMO_TOOL = {
    "type": "function",
    "function": {
        "name": "create_demo_request",
        "description": "Save a demo request for the visitor. Call this ONLY after the visitor has given their full name, work email and company AND confirmed they want a demo. Never invent values.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Visitor's full name"},
                "email": {"type": "string", "description": "Visitor's work email address"},
                "company": {"type": "string", "description": "Visitor's company"},
                "interest": {"type": "string", "description": "Product or solution of interest, if mentioned"},
                "notes": {"type": "string", "description": "One-sentence summary of what the visitor wants to see or solve"},
            },
            "required": ["name", "email", "company"],
        },
    },
}


LANGUAGE_NAMES = {"es": "Spanish", "fr": "French", "de": "German"}


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=6, max_length=80)
    message: str = Field(min_length=1, max_length=2000)
    # The site language the visitor has selected (en/es/fr/de).
    language: str = Field(default="en", max_length=8)


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    session_id: str
    role: Literal["user", "assistant"]
    content: str
    created_at: str


async def save_message(session_id: str, role: str, content: str) -> None:
    await db.chat_messages.insert_one({"id": str(uuid.uuid4()), "session_id": session_id, "role": role, "content": content, "created_at": now_iso()})


def sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


async def create_demo_request(session_id: str, args: dict) -> dict:
    email = (args.get("email") or "").strip()
    name = (args.get("name") or "").strip()
    company = (args.get("company") or "").strip()
    if not EMAIL_RX.match(email):
        return {"ok": False, "error": "The email address looks invalid. Ask the visitor to re-enter their work email."}
    if len(name) < 2 or len(company) < 2:
        return {"ok": False, "error": "Name and company are required. Ask the visitor for the missing detail."}
    doc = {
        "id": str(uuid.uuid4()),
        "type": "demo",
        "email": email,
        "name": name,
        "company": company,
        "interest": (args.get("interest") or None),
        "message": (args.get("notes") or None),
        "source": "chat",
        "source_page": f"chat:{session_id}",
        "created_at": now_iso(),
    }
    await db.submissions.insert_one(dict(doc))
    asyncio.create_task(notify_lead(doc))
    try:
        await record_submission(doc)
    except Exception:
        logger.exception("lead scoring failed for chat booking %s", doc["id"])
    logger.info("chat demo request saved %s", doc["id"])
    return {"ok": True, "submission_id": doc["id"], "message": "Demo request saved. A Solix expert will reach out within one business day."}


@router.get("/{session_id}", response_model=List[ChatMessage])
async def get_chat_history(session_id: str):
    docs = await db.chat_messages.find({"session_id": session_id}, {"_id": 0}).sort("created_at", 1).to_list(200)
    return [ChatMessage(**d) for d in docs]


@router.delete("/{session_id}", status_code=204)
async def clear_chat_history(session_id: str):
    await db.chat_messages.delete_many({"session_id": session_id})
    return None


@router.post("/stream")
async def chat_stream(req: ChatRequest):
    if _client is None:
        raise HTTPException(status_code=503, detail="AI concierge is not configured")

    history = await db.chat_messages.find({"session_id": req.session_id}, {"_id": 0, "role": 1, "content": 1}).sort("created_at", 1).to_list(HISTORY_LIMIT)
    system_prompt = CONCIERGE_SYSTEM_PROMPT
    if req.language in LANGUAGE_NAMES:
        name = LANGUAGE_NAMES[req.language]
        system_prompt += f"\n\nThe visitor is browsing the site in {name}. Reply in {name} unless they write to you in another language."
    messages = [{"role": "system", "content": system_prompt}] + [{"role": m["role"], "content": m["content"]} for m in history] + [{"role": "user", "content": req.message}]

    async def generate():
        await save_message(req.session_id, "user", req.message)
        full = ""
        try:
            for _ in range(MAX_TOOL_ROUNDS):
                stream = await _client.chat.completions.create(
                    model=OPENAI_MODEL,
                    messages=messages,
                    tools=[DEMO_TOOL],
                    tool_choice="auto",
                    stream=True,
                )

                text_chunk = ""
                tool_calls: dict[int, dict] = {}
                finish_reason = None

                async for event in stream:
                    choice = event.choices[0]
                    delta = choice.delta
                    if delta.content:
                        text_chunk += delta.content
                        yield sse({"delta": delta.content})
                    if delta.tool_calls:
                        for tc_delta in delta.tool_calls:
                            slot = tool_calls.setdefault(tc_delta.index, {"id": None, "name": None, "arguments": ""})
                            if tc_delta.id:
                                slot["id"] = tc_delta.id
                            if tc_delta.function and tc_delta.function.name:
                                slot["name"] = tc_delta.function.name
                            if tc_delta.function and tc_delta.function.arguments:
                                slot["arguments"] += tc_delta.function.arguments
                    if choice.finish_reason:
                        finish_reason = choice.finish_reason

                full += text_chunk

                if finish_reason != "tool_calls" or not tool_calls:
                    break

                messages.append({
                    "role": "assistant",
                    "content": text_chunk or None,
                    "tool_calls": [
                        {"id": tc["id"], "type": "function", "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                        for tc in tool_calls.values()
                    ],
                })

                for tc in tool_calls.values():
                    try:
                        args = json.loads(tc["arguments"] or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    if tc["name"] == "create_demo_request":
                        outcome = await create_demo_request(req.session_id, args)
                        if outcome.get("ok"):
                            yield sse({"event": "demo_booked", "submission_id": outcome["submission_id"], "name": args.get("name"), "email": args.get("email"), "company": args.get("company")})
                    else:
                        outcome = {"ok": False, "error": f"Unknown tool {tc['name']}"}
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": json.dumps(outcome)})
        except Exception:
            logger.exception("chat stream failed")
            yield sse({"error": "The concierge is temporarily unavailable. Please try again."})
            return
        if full:
            await save_message(req.session_id, "assistant", full)
        yield sse({"done": True})

    return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
