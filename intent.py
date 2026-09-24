"""Visitor intent tracking and lead rollup.

POST /api/track receives batched behaviour events from the website (sent with
navigator.sendBeacon only after the visitor accepts analytics cookies). Each
anonymous visitor accumulates decayed product scores. When the visitor
identifies themselves through any form, the visitor is linked to a lead
(one per email) and the lead is re-scored and staged - see scoring.py.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response

import scoring
from database import db, now_iso

logger = logging.getLogger("solix.intent")
router = APIRouter(prefix="/api", tags=["intent"])

MAX_BODY = 32 * 1024
MAX_EVENTS = 50
RATE_WINDOW, RATE_MAX = 60, 60  # batches per IP per minute
SEEN_KEEP = 300
_hits: dict[str, deque] = defaultdict(deque)


def _rate_limited(ip: str) -> bool:
    now = time.monotonic()
    q = _hits[ip]
    while q and now - q[0] > RATE_WINDOW:
        q.popleft()
    if len(q) >= RATE_MAX:
        return True
    q.append(now)
    if len(_hits) > 5000:  # keep memory bounded on a small instance
        for k in list(_hits)[:1000]:
            _hits.pop(k, None)
    return False


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")


def _s(v, n: int) -> Optional[str]:
    return str(v)[:n] if v not in (None, "") else None


async def get_scoring_settings() -> dict:
    doc = await db.settings.find_one({"key": "scoring"}, {"_id": 0}) or {}
    return {**scoring.DEFAULT_SETTINGS, **{k: doc[k] for k in scoring.DEFAULT_SETTINGS if k in doc}}


def _touch(ctx: dict, landing: Optional[str]) -> dict:
    return {
        "utm_source": _s(ctx.get("utm_source"), 80), "utm_medium": _s(ctx.get("utm_medium"), 80),
        "utm_campaign": _s(ctx.get("utm_campaign"), 120), "utm_term": _s(ctx.get("utm_term"), 120),
        "utm_content": _s(ctx.get("utm_content"), 120), "referrer": _s(ctx.get("referrer"), 300),
        "landing": _s(landing or ctx.get("landing"), 300), "at": now_iso(),
    }


@router.post("/track", status_code=204)
async def track(request: Request):
    """Batched behaviour events. Body is JSON sent as text/plain (sendBeacon)."""
    if _rate_limited(_client_ip(request)):
        raise HTTPException(status_code=429, detail="Too many requests")
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise HTTPException(status_code=413, detail="Payload too large")
    try:
        body = json.loads(raw or b"{}")
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Invalid JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid payload")
    vid = _s(body.get("visitor_id"), 64)
    sid = _s(body.get("session_id"), 64)
    events = body.get("events") if isinstance(body.get("events"), list) else []
    if not vid or len(vid) < 8 or not events:
        return Response(status_code=204)
    await ingest(vid, sid or "none", events[:MAX_EVENTS], body.get("context") if isinstance(body.get("context"), dict) else {})
    return Response(status_code=204)


async def ingest(vid: str, sid: str, events: list, ctx: dict) -> dict:
    settings = await get_scoring_settings()
    now = now_iso()
    visitor = await db.visitors.find_one({"id": vid}, {"_id": 0})
    new_visitor = visitor is None
    if new_visitor:
        visitor = {
            "id": vid, "created_at": now, "sessions": 0, "last_session": None, "product_scores": {}, "scored_at": now,
            "industries": {}, "events_count": 0, "pages_count": 0, "first_touch": _touch(ctx, None), "seen": [],
            "lang": _s(ctx.get("lang"), 8), "lead_id": None, "candidate": False,
        }
    scores = scoring.decay_scores(visitor.get("product_scores") or {}, visitor.get("scored_at"), settings["half_life_days"])
    seen = deque(visitor.get("seen") or [], maxlen=SEEN_KEEP)
    if visitor.get("last_session") != sid:
        visitor["sessions"] = (visitor.get("sessions") or 0) + 1
        visitor["last_session"] = sid
        visitor["last_touch"] = _touch(ctx, None)
    docs = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        etype = _s(ev.get("type"), 32)
        if etype not in scoring.EVENT_TYPES:
            continue
        path = _s(ev.get("path"), 300) or ""
        meta = ev.get("meta") if isinstance(ev.get("meta"), dict) else {}
        meta = {k: _s(v, 200) for k, v in list(meta.items())[:8] if isinstance(k, str)}
        server_topics = scoring.topics_for_path(path)
        client_topics = scoring.clean_topics(ev.get("topics"))
        if etype in ("page_view", "engaged", "deep_scroll"):
            topics = server_topics or (client_topics if path.startswith("/resources/") else [])
        elif etype == "pricing_intent":
            topics = client_topics or server_topics or ([max(scores, key=scores.get)] if scores else [])
        else:
            topics = client_topics or server_topics
        # Score each (session, type, path/topic) once, so reloads and repeat clicks don't inflate intent.
        key = f"{sid}|{etype}|{path}|{','.join(topics)}|{meta.get('q') or ''}"
        points = 0.0
        if topics and key not in seen:
            points = scoring.EVENT_WEIGHTS.get(etype, 0)
            scoring.add_points(scores, topics, points)
            seen.append(key)
        if etype == "page_view":
            visitor["pages_count"] = (visitor.get("pages_count") or 0) + 1
            ind = scoring.industry_for_path(path)
            if ind:
                visitor.setdefault("industries", {})[ind] = visitor["industries"].get(ind, 0) + 1
            if path.rstrip("/").endswith("/careers"):
                visitor["candidate"] = True
        docs.append({
            "id": str(uuid.uuid4()), "visitor_id": vid, "session_id": sid, "type": etype, "path": path,
            "topics": topics, "points": points, "meta": meta, "at": now, "at_dt": datetime.now(timezone.utc),
        })
    if docs:
        await db.events.insert_many(docs)
    visitor.update({
        "product_scores": scores, "scored_at": now, "last_seen": now, "seen": list(seen),
        "events_count": (visitor.get("events_count") or 0) + len(docs),
    })
    await db.visitors.update_one({"id": vid}, {"$set": visitor}, upsert=True)
    if visitor.get("lead_id"):
        await rollup_lead(visitor["lead_id"], settings)
    return visitor


# --- Leads --------------------------------------------------------------------

async def rollup_lead(lead_id: str, settings: Optional[dict] = None) -> Optional[dict]:
    """Recompute a lead's scores from its own form activity and every linked visitor."""
    settings = settings or await get_scoring_settings()
    lead = await db.leads.find_one({"id": lead_id}, {"_id": 0})
    if not lead:
        return None
    hl = settings["half_life_days"]
    direct = scoring.decay_scores(lead.get("direct_scores") or {}, lead.get("direct_scored_at"), hl)
    scores = dict(direct)
    industries: dict = {}
    pages = sessions = 0
    last_seen = lead.get("last_activity_at")
    for vid in lead.get("visitor_ids") or []:
        v = await db.visitors.find_one({"id": vid}, {"_id": 0, "product_scores": 1, "scored_at": 1, "industries": 1, "pages_count": 1, "sessions": 1, "last_seen": 1})
        if not v:
            continue
        for k, val in scoring.decay_scores(v.get("product_scores") or {}, v.get("scored_at"), hl).items():
            scores[k] = round(scores.get(k, 0) + val, 2)
        for k, n in (v.get("industries") or {}).items():
            industries[k] = industries.get(k, 0) + n
        pages += v.get("pages_count") or 0
        sessions += v.get("sessions") or 0
        if v.get("last_seen") and (not last_seen or v["last_seen"] > last_seen):
            last_seen = v["last_seen"]
    fit, fit_breakdown = scoring.fit_score(lead)
    summary = scoring.summarize(scores, fit)
    update = {
        "direct_scores": direct, "direct_scored_at": now_iso(), "product_scores": scores, "fit_score": fit,
        "fit_breakdown": fit_breakdown, **summary, "pages_viewed": pages, "sessions": sessions,
        "industry": lead.get("industry") or (max(industries, key=industries.get) if industries else None),
        "industries": industries, "last_activity_at": last_seen, "channel": scoring.channel_for(lead.get("first_touch")),
        "rescored_at": now_iso(),
    }
    merged = {**lead, **update}
    stage, reason = scoring.evaluate_stage(merged, settings)
    if stage != lead.get("stage"):
        update["stage"] = stage
        update["stage_reason"] = reason
        update["stage_changed_at"] = now_iso()
        if stage == "mql" and not lead.get("mql_at"):
            update["mql_at"] = now_iso()
            update["mql_line"] = summary["primary_line"]
            update["mql_product"] = summary["primary_product"]
        history = (lead.get("stage_history") or []) + [{"stage": stage, "at": now_iso(), "by": "system", "reason": reason}]
        update["stage_history"] = history[-50:]
    await db.leads.update_one({"id": lead_id}, {"$set": update})
    return {**lead, **update}


PROFILE_FIELDS = ("name", "company", "job_title", "phone", "country", "company_size")


async def record_submission(sub: dict, *, visitor_id: Optional[str] = None, topics: Optional[list] = None, extra: Optional[dict] = None) -> Optional[dict]:
    """Create/update the lead behind a form submission, sign-up or chat booking."""
    stype = sub.get("type")
    email = (sub.get("email") or "").strip().lower()
    if not email or stype == "career":
        return None
    settings = await get_scoring_settings()
    now = now_iso()
    lead = await db.leads.find_one({"email": email}, {"_id": 0})
    if not lead:
        lead = {
            "id": str(uuid.uuid4()), "email": email, "created_at": now, "stage": "lead", "visitor_ids": [],
            "direct_scores": {}, "direct_scored_at": now, "submission_types": [], "submissions_count": 0,
            "owner": None, "notes": "", "tags": [], "stage_history": [{"stage": "lead", "at": now, "by": "system", "reason": f"First form: {stype}"}],
            "first_touch": None, "source": sub.get("source") or "web",
        }
        await db.leads.insert_one(dict(lead))
    profile = {**(extra or {}), **{k: sub.get(k) for k in PROFILE_FIELDS if sub.get(k)}}
    changes = {k: _s(v, 200) for k, v in profile.items() if k in PROFILE_FIELDS + ("language",) and v}
    wanted = scoring.clean_topics((topics or []) + [sub.get("interest") or ""])
    if stype == "trial" and "enterprise-content-services" not in wanted:
        wanted.append("enterprise-content-services")
    direct = scoring.decay_scores(lead.get("direct_scores") or {}, lead.get("direct_scored_at"), settings["half_life_days"])
    scoring.add_points(direct, wanted, scoring.SUBMISSION_WEIGHTS.get(stype, 0))
    types = list(lead.get("submission_types") or [])
    if stype not in types:
        types.append(stype)
    tags = list(lead.get("tags") or [])
    if stype == "partner" and "partner" not in tags:
        tags.append("partner")
    visitor_ids = list(lead.get("visitor_ids") or [])
    first_touch = lead.get("first_touch")
    if visitor_id:
        v = await db.visitors.find_one({"id": visitor_id}, {"_id": 0, "first_touch": 1, "lead_id": 1})
        if v is not None:
            if visitor_id not in visitor_ids:
                visitor_ids.append(visitor_id)
            await db.visitors.update_one({"id": visitor_id}, {"$set": {"lead_id": lead["id"], "email": email}})
            first_touch = first_touch or v.get("first_touch")
    if not first_touch:
        first_touch = {"chat": True, "at": now} if sub.get("source") == "chat" else {"landing": sub.get("source_page"), "at": now}
    await db.leads.update_one({"id": lead["id"]}, {"$set": {
        **changes, "direct_scores": direct, "direct_scored_at": now, "submission_types": types, "tags": tags,
        "visitor_ids": visitor_ids[-20:], "first_touch": first_touch, "last_activity_at": now, "updated_at": now,
        "last_submission_type": stype, "submissions_count": (lead.get("submissions_count") or 0) + 1,
    }})
    result = await rollup_lead(lead["id"], settings)
    if result and sub.get("id"):
        await db.submissions.update_one({"id": sub["id"]}, {"$set": {
            "lead_id": lead["id"], "lead_stage": result.get("stage"), "primary_line": result.get("primary_line"),
            "primary_product": result.get("primary_product"), "lead_score": result.get("score"),
        }})
    return result


async def rescore_all(limit: int = 5000) -> int:
    """Apply decay to every recently active lead (run periodically)."""
    settings = await get_scoring_settings()
    n = 0
    async for lead in db.leads.find({}, {"_id": 0, "id": 1}).sort("last_activity_at", -1).limit(limit):
        await rollup_lead(lead["id"], settings)
        n += 1
    return n
