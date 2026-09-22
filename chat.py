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

logger = logging.getLogger("solix.chat")
router = APIRouter(prefix="/api/chat", tags=["chat"])

try:
    # Only available inside Emergent's build image. On any other host this
    # import fails, so the concierge degrades to a 503 instead of the whole
    # API failing to start.
    from emergentintegrations.llm.chat import LlmChat, UserMessage, TextDelta, StreamDone
except ImportError:
    LlmChat = UserMessage = TextDelta = StreamDone = None

LLM_KEY = os.environ.get("EMERGENT_LLM_KEY") if LlmChat is not None else None
CHAT_MODEL = ("openai", "gpt-5.4-mini")
HISTORY_LIMIT = 24
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


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=6, max_length=80)
    message: str = Field(min_length=1, max_length=2000)


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
    if not LLM_KEY:
        raise HTTPException(status_code=503, detail="AI concierge is not configured")

    history = await db.chat_messages.find({"session_id": req.session_id}, {"_id": 0, "role": 1, "content": 1}).sort("created_at", 1).to_list(HISTORY_LIMIT)
    initial = [{"role": "system", "content": CONCIERGE_SYSTEM_PROMPT}] + [{"role": m["role"], "content": m["content"]} for m in history]

    chat = (
        LlmChat(api_key=LLM_KEY, session_id=req.session_id, system_message=CONCIERGE_SYSTEM_PROMPT, initial_messages=initial)
        .with_model(*CHAT_MODEL)
        .with_tools([DEMO_TOOL], tool_choice="auto")
    )

    async def run_turn(user_message):
        text = ""
        pending = None
        async for event in chat.stream_message(user_message):
            if isinstance(event, TextDelta):
                text += event.content
                yield sse({"delta": event.content}), None
            elif isinstance(event, StreamDone):
                pending = event.tool_calls
        yield None, (text, pending)

    async def generate():
        await save_message(req.session_id, "user", req.message)
        full = ""
        try:
            user_message = UserMessage(text=req.message)
            for _ in range(3):
                result = None
                async for chunk, done in run_turn(user_message):
                    if chunk:
                        yield chunk
                    if done:
                        result = done
                text, tool_calls = result
                full += text
                if not tool_calls:
                    break
                for tc in tool_calls:
                    if tc.name == "create_demo_request":
                        outcome = await create_demo_request(req.session_id, tc.arguments)
                        if outcome.get("ok"):
                            yield sse({"event": "demo_booked", "submission_id": outcome["submission_id"], "name": tc.arguments.get("name"), "email": tc.arguments.get("email"), "company": tc.arguments.get("company")})
                    else:
                        outcome = {"ok": False, "error": f"Unknown tool {tc.name}"}
                    chat.add_tool_result(tc.id, json.dumps(outcome))
                user_message = None
        except Exception:
            logger.exception("chat stream failed")
            yield sse({"error": "The concierge is temporarily unavailable. Please try again."})
            return
        if full:
            await save_message(req.session_id, "assistant", full)
        yield sse({"done": True})

    return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
