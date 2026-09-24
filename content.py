"""Content publishing (CMS) for the website's Resources hub.

Editors create blogs, white papers, datasheets, case studies, webinars and
marketing material in the admin; published items appear on the site within
seconds (the site also bakes a snapshot in at build time, so pages never
depend on this API being awake). Files (PDFs, images, decks) are stored in
MongoDB and served with immutable caching. Gated files are only released
against a download-form submission, and every publish can optionally
trigger a static rebuild of the site.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from typing import List, Literal, Optional

import httpx
from bson import Binary
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

import cache
import scoring
from auth import get_current_admin, require_roles
from database import db, now_iso

logger = logging.getLogger("solix.content")
public = APIRouter(prefix="/api", tags=["content"])
admin = APIRouter(prefix="/api/admin", tags=["content-admin"], dependencies=[Depends(get_current_admin)])
can_edit = Depends(require_roles("admin", "editor"))

CONTENT_TYPES = ("blog", "whitepaper", "datasheet", "casestudy", "ebook", "webinar", "podcast", "leadership", "event", "brief", "collateral")
Status = Literal["draft", "scheduled", "published", "archived"]
SLUG_RX = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_FILE = 10 * 1024 * 1024
LIST_FIELDS = {"_id": 0, "body": 0}


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:100] or uuid.uuid4().hex[:8]


def read_minutes(body: str) -> int:
    return max(1, round(len(re.findall(r"\w+", body or "")) / 220))


class ContentIn(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    type: Literal[CONTENT_TYPES]  # type: ignore[valid-type]
    slug: Optional[str] = Field(default=None, max_length=120)
    summary: str = Field(default="", max_length=600)
    body: str = Field(default="", max_length=100_000)
    tag: Optional[str] = Field(default=None, max_length=60)
    products: List[str] = Field(default_factory=list, max_length=10)
    industries: List[str] = Field(default_factory=list, max_length=10)
    author: Optional[str] = Field(default=None, max_length=120)
    author_role: Optional[str] = Field(default=None, max_length=160)
    cover_image: Optional[str] = Field(default=None, max_length=500)
    file_id: Optional[str] = Field(default=None, max_length=64)
    gated: bool = False
    event_date: Optional[str] = Field(default=None, max_length=40)
    video_url: Optional[str] = Field(default=None, max_length=500)
    seo_title: Optional[str] = Field(default=None, max_length=120)
    seo_description: Optional[str] = Field(default=None, max_length=300)


class PublishIn(BaseModel):
    publish_at: Optional[str] = None


def _is_live(doc: dict, now: Optional[str] = None) -> bool:
    now = now or now_iso()
    return doc.get("status") in ("published", "scheduled") and (doc.get("publish_at") or "") <= now


def _signed(file_id: str, ttl: int = 3600) -> str:
    exp = int(time.time()) + ttl
    sig = hmac.new(os.environ["JWT_SECRET"].encode(), f"{file_id}.{exp}".encode(), hashlib.sha256).digest()
    return f"{exp}.{base64.urlsafe_b64encode(sig[:18]).decode()}"


def _check_signed(file_id: str, token: str) -> bool:
    try:
        exp_s, sig = token.split(".", 1)
        if int(exp_s) < time.time():
            return False
        good = hmac.new(os.environ["JWT_SECRET"].encode(), f"{file_id}.{exp_s}".encode(), hashlib.sha256).digest()
        return hmac.compare_digest(base64.urlsafe_b64encode(good[:18]).decode(), sig)
    except (ValueError, KeyError):
        return False


def file_url(f: dict, token: Optional[str] = None) -> str:
    base = f"/api/files/{f['id']}/{f['name']}"
    return f"{base}?t={token}" if token else base


async def _public_view(doc: dict, full: bool) -> dict:
    out = {k: doc.get(k) for k in (
        "id", "slug", "type", "title", "summary", "tag", "products", "industries", "author", "author_role",
        "cover_image", "gated", "event_date", "video_url", "seo_title", "seo_description", "read_minutes",
        "published_at", "publish_at", "updated_at",
    )}
    out["date"] = (doc.get("publish_at") or doc.get("published_at") or "")[:10]
    if full:
        out["body"] = doc.get("body") or ""
    if doc.get("file_id"):
        f = await db.files.find_one({"id": doc["file_id"]}, {"_id": 0, "data": 0})
        if f:
            out["file"] = {"name": f["name"], "size": f["size"], "content_type": f["content_type"], "gated": bool(doc.get("gated"))}
            if not doc.get("gated"):
                out["file"]["url"] = file_url(f)
    return out


async def published(full: bool) -> list:
    async def produce():
        now = now_iso()
        docs = await db.content.find({"status": {"$in": ["published", "scheduled"]}, "publish_at": {"$lte": now}}, {"_id": 0} if full else LIST_FIELDS).sort("publish_at", -1).to_list(1000)
        return [await _public_view(d, full) for d in docs]
    # Short TTL so scheduled items go live on time without a cron.
    return await cache.memo("content", f"published:{full}", 30, produce)


# --- Public -------------------------------------------------------------------

@public.get("/content")
async def list_content(request: Request, type: Optional[str] = None, product: Optional[str] = None, full: bool = False, limit: int = Query(200, ge=1, le=1000)):
    items = await published(full)
    if type:
        items = [i for i in items if i["type"] == type]
    if product:
        items = [i for i in items if product in (i.get("products") or [])]
    return cache.cached_json(request, {"items": items[:limit]}, max_age=60, swr=86400)


@public.get("/content/{slug}")
async def get_content(slug: str, request: Request):
    items = await published(True)
    doc = next((i for i in items if i["slug"] == slug), None)
    if not doc:
        raise HTTPException(status_code=404, detail="Not found")
    return cache.cached_json(request, doc, max_age=60, swr=86400)


class UnlockIn(BaseModel):
    submission_id: str = Field(min_length=8, max_length=64)


@public.post("/content/{slug}/unlock")
async def unlock(slug: str, body: UnlockIn):
    """Release a gated file to someone who just submitted the download form."""
    doc = await db.content.find_one({"slug": slug}, {"_id": 0})
    if not doc or not _is_live(doc) or not doc.get("file_id"):
        raise HTTPException(status_code=404, detail="Not found")
    sub = await db.submissions.find_one({"id": body.submission_id, "type": "download"}, {"_id": 0})
    if not sub:
        raise HTTPException(status_code=403, detail="Please fill in the form to download this file.")
    age = datetime.now(timezone.utc) - datetime.fromisoformat(sub["created_at"])
    if age.total_seconds() > 24 * 3600:
        raise HTTPException(status_code=403, detail="This download link has expired. Please fill in the form again.")
    f = await db.files.find_one({"id": doc["file_id"]}, {"_id": 0, "data": 0})
    if not f:
        raise HTTPException(status_code=404, detail="File not found")
    return {"url": file_url(f, _signed(f["id"], 24 * 3600))}


@public.get("/files/{file_id}/{name}")
async def serve_file(file_id: str, name: str, request: Request, t: Optional[str] = None):
    f = await db.files.find_one({"id": file_id}, {"_id": 0})
    if not f:
        raise HTTPException(status_code=404, detail="Not found")
    allowed = f["kind"] == "image" or (t and _check_signed(file_id, t))
    if not allowed:
        # Documents are public only when attached to live, non-gated content.
        allowed = bool(await db.content.find_one({"file_id": file_id, "gated": {"$ne": True}, "status": {"$in": ["published", "scheduled"]}, "publish_at": {"$lte": now_iso()}}, {"_id": 1}))
    if not allowed:
        raise HTTPException(status_code=403, detail="This file requires a download form.")
    etag = f'"{f["sha256"][:24]}"'
    headers = {
        "ETag": etag, "X-Content-Type-Options": "nosniff",
        "Cache-Control": ("private" if t else "public") + ", max-age=31536000, immutable",
        "Content-Disposition": ("inline" if f["kind"] in ("image", "pdf") else "attachment") + f'; filename="{f["name"]}"',
    }
    if cache.etag_matches(request, etag):
        return Response(status_code=304, headers=headers)
    return Response(content=bytes(f["data"]), media_type=f["content_type"], headers=headers)


# --- Admin --------------------------------------------------------------------

async def _trigger_rebuild(reason: str) -> None:
    """Optional: ask GitHub Actions to rebuild the static site so the baked-in
    content snapshot is fresh. Needs GITHUB_DEPLOY_TOKEN; skipped otherwise."""
    token, repo = os.environ.get("GITHUB_DEPLOY_TOKEN"), os.environ.get("GITHUB_SITE_REPO", "akshatsingh-solix/Website")
    if not token:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(f"https://api.github.com/repos/{repo}/dispatches", headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}, json={"event_type": "content-published", "client_payload": {"reason": reason[:100]}})
    except httpx.HTTPError:
        logger.warning("site rebuild trigger failed")


def _changed(reason: str) -> None:
    cache.bump("content")
    asyncio.create_task(_trigger_rebuild(reason))


async def _validate(body: ContentIn, current_id: Optional[str] = None) -> dict:
    data = body.model_dump()
    data["slug"] = slugify(data["slug"] or data["title"])
    if not SLUG_RX.match(data["slug"]):
        raise HTTPException(status_code=422, detail="Slug may only contain lowercase letters, numbers and dashes.")
    clash = await db.content.find_one({"slug": data["slug"], "id": {"$ne": current_id}}, {"_id": 0, "id": 1})
    if clash:
        raise HTTPException(status_code=409, detail="Another item already uses this URL slug.")
    data["products"] = [p for p in dict.fromkeys(data["products"]) if p in scoring.PRODUCTS]
    data["industries"] = [i for i in dict.fromkeys(data["industries"]) if i in scoring.INDUSTRIES]
    if data["file_id"] and not await db.files.find_one({"id": data["file_id"]}, {"_id": 1}):
        raise HTTPException(status_code=422, detail="Attached file not found. Upload it again.")
    for key in ("cover_image", "video_url"):
        url = data.get(key)
        if url and not (url.startswith("https://") or url.startswith("/")):
            raise HTTPException(status_code=422, detail=f"{key.replace('_', ' ').capitalize()} must be an https:// link or a site path.")
    data["read_minutes"] = read_minutes(data["body"])
    return data


async def _snapshot(doc: dict, user: dict) -> None:
    await db.content_versions.insert_one({"content_id": doc["id"], "version": doc.get("version", 1), "saved_at": now_iso(), "saved_by": user.get("email"), "doc": {k: v for k, v in doc.items() if k != "_id"}})


@admin.get("/content")
async def admin_list(status: Optional[str] = None, type: Optional[str] = None, q: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100)):
    query: dict = {}
    if status and status != "all":
        query["status"] = status
    else:
        query["status"] = {"$ne": "archived"}
    if type and type != "all":
        query["type"] = type
    if q:
        rx = {"$regex": re.escape(q.strip()), "$options": "i"}
        query["$or"] = [{"title": rx}, {"summary": rx}, {"slug": rx}, {"tag": rx}]
    total = await db.content.count_documents(query)
    items = await db.content.find(query, LIST_FIELDS).sort("updated_at", -1).skip((page - 1) * page_size).limit(page_size).to_list(page_size)
    # Engagement for the last 30 days, from tracked events.
    since = datetime.fromtimestamp(time.time() - 30 * 86400, tz=timezone.utc).isoformat()
    for it in items:
        path = f"/resources/{it['slug']}"
        it["views_30d"] = await db.events.count_documents({"type": "resource_view", "path": path, "at": {"$gte": since}})
        it["downloads_30d"] = await db.events.count_documents({"type": "resource_download", "path": path, "at": {"$gte": since}})
        it["live"] = _is_live(it)
    return {"items": items, "total": total, "page": page, "page_size": page_size, "types": CONTENT_TYPES}


@admin.post("/content", status_code=201, dependencies=[can_edit])
async def admin_create(body: ContentIn, user: dict = Depends(get_current_admin)):
    data = await _validate(body)
    now = now_iso()
    doc = {**data, "id": str(uuid.uuid4()), "status": "draft", "version": 1, "created_at": now, "updated_at": now, "created_by": user["email"], "updated_by": user["email"], "publish_at": None, "published_at": None}
    await db.content.insert_one(dict(doc))
    doc.pop("_id", None)
    return doc


@admin.get("/content/{content_id}")
async def admin_get(content_id: str):
    doc = await db.content.find_one({"id": content_id}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Not found")
    if doc.get("file_id"):
        f = await db.files.find_one({"id": doc["file_id"]}, {"_id": 0, "data": 0})
        doc["file"] = {**f, "url": file_url(f, _signed(f["id"]))} if f else None
    doc["live"] = _is_live(doc)
    return doc


@admin.put("/content/{content_id}", dependencies=[can_edit])
async def admin_update(content_id: str, body: ContentIn, user: dict = Depends(get_current_admin)):
    doc = await db.content.find_one({"id": content_id}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Not found")
    data = await _validate(body, content_id)
    await _snapshot(doc, user)
    update = {**data, "version": doc.get("version", 1) + 1, "updated_at": now_iso(), "updated_by": user["email"]}
    await db.content.update_one({"id": content_id}, {"$set": update})
    if _is_live(doc):
        _changed(f"updated {data['slug']}")
    return await admin_get(content_id)


@admin.post("/content/{content_id}/publish", dependencies=[can_edit])
async def admin_publish(content_id: str, body: PublishIn, user: dict = Depends(get_current_admin)):
    doc = await db.content.find_one({"id": content_id}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Not found")
    now = now_iso()
    at = now
    if body.publish_at:
        try:
            at = datetime.fromisoformat(body.publish_at.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid publish date")
    status = "scheduled" if at > now else "published"
    await db.content.update_one({"id": content_id}, {"$set": {"status": status, "publish_at": at, "published_at": doc.get("published_at") or at, "updated_at": now, "updated_by": user["email"]}})
    _changed(f"published {doc['slug']}")
    return await admin_get(content_id)


@admin.post("/content/{content_id}/unpublish", dependencies=[can_edit])
async def admin_unpublish(content_id: str, user: dict = Depends(get_current_admin)):
    res = await db.content.update_one({"id": content_id}, {"$set": {"status": "draft", "updated_at": now_iso(), "updated_by": user["email"]}})
    if not res.matched_count:
        raise HTTPException(status_code=404, detail="Not found")
    _changed(f"unpublished {content_id}")
    return await admin_get(content_id)


@admin.delete("/content/{content_id}", status_code=204, dependencies=[can_edit])
async def admin_archive(content_id: str, user: dict = Depends(get_current_admin)):
    res = await db.content.update_one({"id": content_id}, {"$set": {"status": "archived", "updated_at": now_iso(), "updated_by": user["email"]}})
    if not res.matched_count:
        raise HTTPException(status_code=404, detail="Not found")
    _changed(f"archived {content_id}")


@admin.get("/content/{content_id}/versions")
async def admin_versions(content_id: str):
    docs = await db.content_versions.find({"content_id": content_id}, {"_id": 0, "doc.body": 0}).sort("saved_at", -1).to_list(20)
    return [{"version": d["version"], "saved_at": d["saved_at"], "saved_by": d.get("saved_by"), "title": d["doc"].get("title"), "status": d["doc"].get("status")} for d in docs]


@admin.post("/content/{content_id}/versions/{version}/restore", dependencies=[can_edit])
async def admin_restore(content_id: str, version: int, user: dict = Depends(get_current_admin)):
    snap = await db.content_versions.find_one({"content_id": content_id, "version": version}, {"_id": 0})
    current = await db.content.find_one({"id": content_id}, {"_id": 0})
    if not snap or not current:
        raise HTTPException(status_code=404, detail="Version not found")
    await _snapshot(current, user)
    keep = {k: snap["doc"].get(k) for k in ContentIn.model_fields}
    keep["read_minutes"] = read_minutes(keep.get("body") or "")
    await db.content.update_one({"id": content_id}, {"$set": {**keep, "version": current.get("version", 1) + 1, "updated_at": now_iso(), "updated_by": user["email"]}})
    if _is_live(current):
        _changed(f"restored {content_id}")
    return await admin_get(content_id)


# Files -------------------------------------------------------------------------

SIGNATURES = [
    (b"%PDF", "application/pdf", "pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png", "image"),
    (b"\xff\xd8\xff", "image/jpeg", "image"),
    (b"GIF87a", "image/gif", "image"),
    (b"GIF89a", "image/gif", "image"),
]
OFFICE = {
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def sniff(data: bytes, name: str) -> tuple[str, str]:
    for sig, ctype, kind in SIGNATURES:
        if data.startswith(sig):
            return ctype, kind
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "image"
    ext = os.path.splitext(name.lower())[1]
    if data.startswith(b"PK\x03\x04") and ext in OFFICE:
        return OFFICE[ext], "document"
    raise HTTPException(status_code=415, detail="Upload a PDF, PNG, JPEG, WebP, GIF, PPTX, DOCX or XLSX file.")


@admin.post("/files", status_code=201, dependencies=[can_edit])
async def upload_file(file: UploadFile = File(...), user: dict = Depends(get_current_admin)):
    data = await file.read(MAX_FILE + 1)
    if len(data) > MAX_FILE:
        raise HTTPException(status_code=413, detail="Files must be 10 MB or smaller.")
    if not data:
        raise HTTPException(status_code=422, detail="The file is empty.")
    raw_name = os.path.basename(file.filename or "file")
    ctype, kind = sniff(data, raw_name)
    stem, ext = os.path.splitext(raw_name)
    name = (slugify(stem) or "file") + ext.lower()
    sha = hashlib.sha256(data).hexdigest()
    existing = await db.files.find_one({"sha256": sha}, {"_id": 0, "data": 0})
    if existing:
        return {**existing, "url": file_url(existing, _signed(existing["id"]) if existing["kind"] != "image" else None), "public_url": file_url(existing)}
    doc = {"id": uuid.uuid4().hex, "name": name, "content_type": ctype, "kind": kind, "size": len(data), "sha256": sha, "data": Binary(data), "created_at": now_iso(), "created_by": user["email"]}
    await db.files.insert_one(dict(doc))
    doc.pop("data")
    doc.pop("_id", None)
    return {**doc, "url": file_url(doc, _signed(doc["id"]) if kind != "image" else None), "public_url": file_url(doc)}


@admin.get("/files")
async def list_files(kind: Optional[str] = None, limit: int = Query(50, le=200)):
    query = {"kind": kind} if kind else {}
    docs = await db.files.find(query, {"_id": 0, "data": 0}).sort("created_at", -1).to_list(limit)
    return [{**d, "url": file_url(d, _signed(d["id"]) if d["kind"] != "image" else None), "public_url": file_url(d)} for d in docs]
