"""Intelligence: the weekly self-review's brief, what it learned, the tuning it made (with undo), and its settings."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import learning
from auth import get_current_admin, require_roles

router = APIRouter(prefix="/api/admin/intelligence", tags=["intelligence"], dependencies=[Depends(get_current_admin)])
admin_only = Depends(require_roles("admin"))


def _db():
    import database
    return database.db


@router.get("")
async def get_intelligence():
    latest = await learning.latest()
    log = await _db().tuning_log.find({}, {"_id": 0}).sort("at", -1).to_list(50)
    history = await _db().intelligence.find({}, {"_id": 0, "at": 1, "snapshot": 1}).sort("at", -1).to_list(12)
    return {"latest": latest, "settings": await learning.settings(), "tuning": log, "trend": list(reversed(history))}


@router.post("/review", dependencies=[admin_only])
async def run_review(user: dict = Depends(get_current_admin)):
    return await learning.review(by=user["email"])


class IntelSettings(BaseModel):
    autotune: bool | None = None
    min_sends: int | None = None


@router.put("/settings", dependencies=[admin_only])
async def put_settings(body: IntelSettings):
    vals = body.model_dump(exclude_none=True)
    if "min_sends" in vals and not 3 <= vals["min_sends"] <= 500:
        raise HTTPException(status_code=422, detail="min_sends must be between 3 and 500")
    return await learning.save_settings(vals)


@router.post("/apply", dependencies=[admin_only])
async def apply_proposals(user: dict = Depends(get_current_admin)):
    """Apply what the latest review suggested (when autotune is off)."""
    latest = await learning.latest()
    if not latest or not latest.get("proposals") or latest.get("applied"):
        return {"applied": 0}
    n = await learning.apply(latest["proposals"], by=user["email"], review_id=latest["id"])
    await _db().intelligence.update_one({"id": latest["id"]}, {"$set": {"applied": n}})
    return {"applied": n}


@router.post("/tuning/{change_id}/revert", dependencies=[admin_only])
async def revert_change(change_id: str, user: dict = Depends(get_current_admin)):
    return await learning.revert(change_id, user["email"])
