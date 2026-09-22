import csv
import io
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Literal

from fastapi import APIRouter, Depends, Query, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, EmailStr, Field

from database import db, now_iso
from auth import get_current_admin
from emailer import ALERT_TYPES, get_alert_recipient

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(get_current_admin)])

CSV_FIELDS = ["id", "created_at", "type", "status", "owner", "name", "email", "company", "job_title", "phone", "interest", "role", "resource", "message", "source", "source_page", "notes", "updated_at"]
STATUSES = ("new", "contacted", "qualified")


def _query(type: Optional[str], q: Optional[str], status: Optional[str] = None, owner: Optional[str] = None) -> dict:
    clauses = []
    if type and type != "all":
        clauses.append({"type": type})
    if status and status != "all":
        clauses.append({"$or": [{"status": {"$exists": False}}, {"status": "new"}]} if status == "new" else {"status": status})
    if owner and owner != "all":
        clauses.append({"$or": [{"owner": {"$exists": False}}, {"owner": None}, {"owner": ""}]} if owner == "unassigned" else {"owner": owner})
    if q:
        rx = {"$regex": q.strip(), "$options": "i"}
        clauses.append({"$or": [{"name": rx}, {"email": rx}, {"company": rx}, {"message": rx}, {"interest": rx}, {"role": rx}, {"resource": rx}, {"notes": rx}, {"owner": rx}]})
    return {"$and": clauses} if clauses else {}


@router.get("/stats")
async def stats():
    total = await db.submissions.count_documents({})
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    last_7 = await db.submissions.count_documents({"created_at": {"$gte": week_ago}})
    pipeline = [{"$group": {"_id": "$type", "count": {"$sum": 1}}}]
    by_type = {r["_id"]: r["count"] async for r in db.submissions.aggregate(pipeline)}
    chat_leads = await db.submissions.count_documents({"source": "chat"})
    alerts_sent = await db.notifications.count_documents({"status": "sent"})
    by_status = {}
    async for r in db.submissions.aggregate([{"$group": {"_id": "$status", "count": {"$sum": 1}}}]):
        key = r["_id"] or "new"
        by_status[key] = by_status.get(key, 0) + r["count"]
    by_owner = {}
    async for r in db.submissions.aggregate([{"$group": {"_id": "$owner", "count": {"$sum": 1}}}]):
        key = r["_id"] or "unassigned"
        by_owner[key] = by_owner.get(key, 0) + r["count"]
    return {"total": total, "last_7_days": last_7, "by_type": by_type, "by_status": by_status, "by_owner": by_owner, "chat_leads": chat_leads, "alerts_sent": alerts_sent}


@router.get("/submissions")
async def list_submissions(type: Optional[str] = None, q: Optional[str] = None, status: Optional[str] = None, owner: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=200)):
    query = _query(type, q, status, owner)
    total = await db.submissions.count_documents(query)
    cursor = db.submissions.find(query, {"_id": 0}).sort("created_at", -1).skip((page - 1) * page_size).limit(page_size)
    items = await cursor.to_list(page_size)
    for it in items:
        it.setdefault("status", "new")
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/submissions/export")
async def export_submissions(type: Optional[str] = None, q: Optional[str] = None, status: Optional[str] = None, owner: Optional[str] = None):
    docs = await db.submissions.find(_query(type, q, status, owner), {"_id": 0}).sort("created_at", -1).to_list(10000)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for d in docs:
        d.setdefault("status", "new")
        writer.writerow({k: d.get(k, "") for k in CSV_FIELDS})
    buf.seek(0)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="solix-leads-{stamp}.csv"'})


@router.delete("/submissions/{submission_id}", status_code=204)
async def delete_submission(submission_id: str):
    res = await db.submissions.delete_one({"id": submission_id})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Submission not found")
    return None


class LeadUpdate(BaseModel):
    status: Optional[Literal["new", "contacted", "qualified"]] = None
    notes: Optional[str] = Field(default=None, max_length=4000)
    owner: Optional[str] = Field(default=None, max_length=200)


@router.patch("/submissions/{submission_id}")
async def update_submission(submission_id: str, body: LeadUpdate):
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    if "owner" in body.model_fields_set:
        changes["owner"] = body.owner.strip() or None
    if not changes:
        raise HTTPException(status_code=400, detail="Nothing to update")
    changes["updated_at"] = now_iso()
    res = await db.submissions.find_one_and_update({"id": submission_id}, {"$set": changes}, projection={"_id": 0}, return_document=True)
    if not res:
        raise HTTPException(status_code=404, detail="Submission not found")
    res.setdefault("status", "new")
    return res


class TeamMember(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: EmailStr


class Team(BaseModel):
    members: List[TeamMember] = Field(max_length=100)


@router.get("/team")
async def get_team():
    doc = await db.settings.find_one({"key": "team"}, {"_id": 0})
    return {"members": (doc or {}).get("members", [])}


@router.put("/team")
async def put_team(body: Team):
    seen, members = set(), []
    for m in body.members:
        email = m.email.lower()
        if email not in seen:
            seen.add(email)
            members.append({"name": m.name.strip(), "email": email})
    await db.settings.update_one({"key": "team"}, {"$set": {"members": members, "updated_at": now_iso()}}, upsert=True)
    return {"members": members}


class AlertSettings(BaseModel):
    alert_email: Optional[EmailStr] = None


@router.get("/settings")
async def get_settings():
    return {"alert_email": await get_alert_recipient(), "alert_types": sorted(ALERT_TYPES)}


@router.put("/settings")
async def update_settings(body: AlertSettings):
    await db.settings.update_one({"key": "alerts"}, {"$set": {"alert_email": body.alert_email, "updated_at": now_iso()}}, upsert=True)
    return {"alert_email": await get_alert_recipient(), "alert_types": sorted(ALERT_TYPES)}


@router.get("/notifications")
async def list_notifications(limit: int = Query(20, le=100)) -> List[dict]:
    return await db.notifications.find({}, {"_id": 0}).sort("created_at", -1).to_list(limit)
