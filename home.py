"""MarkHub home: a dashboard shaped for who is looking (GET /api/admin/home).

The admin assigns each user a dashboard (Settings > Access), or it follows their role and seniority:

    executive   the leadership overview (the existing reports dashboard, built in the browser)
    manager     the team: rep scorecard, coverage gaps (unassigned and unrouted accounts), sizes to check
    rep         my day: my accounts with a next step, my leads, my replies
    marketing   inbound: leads by channel, form submissions, content published
    compliance  suppression, signed LIAs, sending health

Every view only counts what the user may see: a rep's view is their own book, and a feature they have no
access to is left out.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends

import database
import permissions
from auth import get_current_admin

router = APIRouter(prefix="/api/admin/home", tags=["home"])
CARD = {"_id": 0, "id": 1, "name": 1, "score": 1, "strength": 1, "status": 1, "next_action": 1, "segment": 1, "size_status": 1, "surge": 1,
        "latest_signal_at": 1, "owner_name": 1, "primary_product": 1}


def _db():
    return database.db


def _ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


async def _rep(user: dict, access: dict) -> dict:
    email = (user.get("email") or "").lower()
    out: dict = {}
    if permissions.RANK[access.get("radar", "none")] >= 1:
        mine = await _db().companies.find({"owner": email}, CARD).sort("score", -1).to_list(500)
        out["accounts"] = {"total": len(mine), "by_status": dict(Counter(c.get("status") or "new" for c in mine)),
                           "surging": [c for c in mine if c.get("surge")][:6],
                           "todo": [c for c in mine if c.get("next_action")][:12],
                           "check_size": sum(1 for c in mine if c.get("size_status") in ("doubtful", "unverified"))}
        ids = [c["id"] for c in mine]
        if ids and permissions.RANK[access.get("outbound", "none")] >= 1:
            names = {c["id"]: c.get("name") for c in mine}
            replies = await _db().reply_log.find({"company_id": {"$in": ids}, "received_at": {"$gte": _ago(14)}},
                                                 {"_id": 0, "text": 0, "meta": 0}).sort("received_at", -1).to_list(10)
            out["replies"] = [{**r, "company_name": names.get(r.get("company_id"))} for r in replies]
            out["active_sequences"] = await _db().enrollments.count_documents({"company_id": {"$in": ids}, "status": {"$in": ["active", "pending_approval"]}})
    if permissions.RANK[access.get("leads", "none")] >= 1:
        leads = await _db().leads.find({"owner": email}, {"_id": 0, "id": 1, "name": 1, "email": 1, "company": 1, "stage": 1, "score": 1, "last_activity_at": 1}) \
            .sort("last_activity_at", -1).to_list(500)
        out["leads"] = {"total": len(leads), "by_stage": dict(Counter(l.get("stage") or "lead" for l in leads)), "recent": leads[:8]}
    return out


async def _manager(user: dict, access: dict) -> dict:
    out: dict = {}
    if permissions.RANK[access.get("radar", "none")] >= 1:
        import radar_service as rs
        db = _db()
        out["reps"] = await rs.rep_scorecard(30)
        out["coverage"] = {
            "unassigned_strong": await db.companies.count_documents({"strength": "Strong", "owner": {"$in": [None, ""]}, "excluded_reason": None}),
            "needs_routing": await db.companies.count_documents({"route_basis": "needs_routing"}),
            "sizes_to_check": await db.companies.count_documents({"size_status": {"$in": ["doubtful", "unverified"]}}),
            "ready_not_contacted": await db.companies.count_documents({"status": "ready"}),
        }
        brief = await db.intelligence.find_one({}, {"_id": 0, "at": 1, "insights": 1}, sort=[("at", -1)])
        out["brief"] = brief
        segs = await db.companies.aggregate([{"$group": {"_id": "$segment", "n": {"$sum": 1}}}]).to_list(10)
        out["segments"] = {(s["_id"] or "unknown"): s["n"] for s in segs}
    if permissions.RANK[access.get("leads", "none")] >= 1:
        stages = await _db().leads.aggregate([{"$group": {"_id": "$stage", "n": {"$sum": 1}}}]).to_list(20)
        out["pipeline"] = {(s["_id"] or "lead"): s["n"] for s in stages}
    return out


async def _marketing(user: dict, access: dict) -> dict:
    out: dict = {}
    since = _ago(30)
    if permissions.RANK[access.get("leads", "none")] >= 1:
        rows = await _db().leads.find({"created_at": {"$gte": since}}, {"_id": 0, "channel": 1, "stage": 1}).to_list(100000)
        out["new_leads"] = {"total": len(rows), "by_channel": dict(Counter(r.get("channel") or "direct" for r in rows)),
                            "mqls": sum(1 for r in rows if r.get("stage") not in (None, "lead", "disqualified"))}
    if permissions.RANK[access.get("inbox", "none")] >= 1:
        subs = await _db().submissions.find({"created_at": {"$gte": since}}, {"_id": 0, "type": 1}).to_list(100000)
        out["submissions"] = dict(Counter(s.get("type") or "other" for s in subs))
    if permissions.RANK[access.get("content", "none")] >= 1:
        out["content"] = {"published_30d": await _db().content.count_documents({"status": {"$in": ["published", "scheduled"]}, "publish_at": {"$gte": since}}),
                          "drafts": await _db().content.count_documents({"status": "draft"})}
    return out


async def _compliance(user: dict, access: dict) -> dict:
    if permissions.RANK[access.get("compliance", "none")] < 1:
        return {}
    db = _db()
    lia = (await db.settings.find_one({"key": "lia"}, {"_id": 0}) or {}).get("records") or []
    sends = await db.sends.find({"sent_at": {"$gte": _ago(7)}}, {"_id": 0, "bounced": 1}).to_list(100000)
    return {"suppressed": await db.suppression.count_documents({}), "erasures_30d": await db.erasure_requests.count_documents({"created_at": {"$gte": _ago(30)}}),
            "lias": [{"region": r.get("region"), "signed": bool(r.get("signed_at")) and not r.get("revoked"), "signed_at": r.get("signed_at")} for r in lia],
            "sends_7d": len(sends), "bounce_rate_7d": round(100 * sum(1 for s in sends if s.get("bounced")) / len(sends), 1) if sends else None}


BUILDERS = {"rep": _rep, "manager": _manager, "marketing": _marketing, "compliance": _compliance}


@router.get("")
async def home(user: dict = Depends(get_current_admin), view: str = ""):
    prof = await permissions.profile(user)
    variant = view if view in permissions.DASHBOARDS and (user.get("role") == "admin" or view == prof["dashboard"]) else prof["dashboard"]
    builder = BUILDERS.get(variant)
    return {"dashboard": variant, "dashboards": permissions.DASHBOARDS if user.get("role") == "admin" else {variant: permissions.DASHBOARDS[variant]},
            "data": await builder(user, prof["access"]) if builder else None}
