"""Site-wide settings editors change from the admin without a code deploy:
the announcement bar at the top of every page and the SOLIXEmpower promo.

The site reads GET /api/site at runtime (cached for a minute) and falls back
to its built-in defaults when the API is unreachable.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

import cache
from auth import get_current_admin, require_roles
import database
from database import now_iso

public = APIRouter(prefix="/api", tags=["site"])
admin = APIRouter(prefix="/api/admin", tags=["site-admin"], dependencies=[Depends(get_current_admin)])


class Announcement(BaseModel):
    enabled: bool = True
    badge: str = Field(default="New", max_length=20)
    text: str = Field(default="", max_length=160)
    link_label: str = Field(default="", max_length=60)
    link_url: str = Field(default="", max_length=500)


class SiteSettings(BaseModel):
    # None means "use the site's built-in announcement".
    announcement: Optional[Announcement] = None
    empower_promo: bool = True


async def load() -> dict:
    doc = await database.db.settings.find_one({"key": "site"}, {"_id": 0}) or {}
    return SiteSettings(**{k: doc.get(k) for k in SiteSettings.model_fields if k in doc}).model_dump()


@public.get("/site")
async def get_site(request: Request):
    data = await cache.memo("site", "settings", 60, load)
    return cache.cached_json(request, data, max_age=60, swr=3600)


@admin.get("/site")
async def admin_get_site():
    return await load()


@admin.put("/site", dependencies=[Depends(require_roles("admin", "editor"))])
async def admin_put_site(body: SiteSettings, user: dict = Depends(get_current_admin)):
    a = body.announcement
    if a and a.link_url and not (a.link_url.startswith("https://") or a.link_url.startswith("/")):
        raise HTTPException(status_code=422, detail="The announcement link must be an https:// address or a site path like /products.")
    await database.db.settings.update_one({"key": "site"}, {"$set": {**body.model_dump(), "updated_at": now_iso(), "updated_by": user["email"]}}, upsert=True)
    cache.bump("site")
    return await load()
