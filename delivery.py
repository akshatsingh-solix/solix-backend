"""Gated asset delivery: when a visitor fills in the download form for a gated
white paper, datasheet, eBook or other asset, email them their copy.

The email carries a signed download link (valid for `link_days`) and, when the
transport supports it and the file is small enough, the file itself as an
attachment. Built-in site articles (which have no file) get a link that
reopens the full article. Every attempt is logged in `deliveries`, shown in
Admin > Settings and on the lead's timeline, and can be resent.
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, EmailStr, Field

import emailer
from auth import get_current_admin, require_roles
from content import _check_signed, _is_live, _signed, file_url
import database
from database import now_iso

logger = logging.getLogger("solix.delivery")
public = APIRouter(prefix="/api", tags=["delivery"])
admin = APIRouter(prefix="/api/admin", tags=["delivery-admin"], dependencies=[Depends(get_current_admin)])

DEFAULTS = {
    "enabled": True,
    "subject": "Your copy of {title}",
    "message": "Thanks for your interest in {title}. Your copy is ready below. The link stays valid for {days} days.",
    "attach": True,
    "attach_max_mb": 5,
    "link_days": 7,
}
DEDUPE_MINUTES = 10


def site_url() -> str:
    return (os.environ.get("SITE_URL") or "https://akshatsingh-solix.github.io/Website").rstrip("/")


def api_base(request: Optional[Request] = None) -> str:
    """Absolute https origin of this API, for links in emails."""
    env = os.environ.get("PUBLIC_API_URL")
    if env:
        return env.rstrip("/")
    if request is None:
        return ""
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.url.netloc
    return f"{proto}://{host}"


async def get_settings() -> dict:
    doc = await database.db.settings.find_one({"key": "delivery"}, {"_id": 0}) or {}
    return {**DEFAULTS, **{k: doc[k] for k in DEFAULTS if k in doc}}


def _fill(template: str, **values) -> str:
    out = template
    for k, v in values.items():
        out = out.replace("{" + k + "}", str(v))
    return out


def access_token(slug: str, days: int) -> str:
    return _signed(f"access:{slug}", days * 86400)


def delivery_html(*, name: str, title: str, message: str, file_name: Optional[str], download_url: Optional[str], page_url: Optional[str], attached: bool) -> str:
    greeting = f"Hi {escape(name.split()[0])}," if name else "Hello,"
    button = ""
    if download_url:
        button = (f'<a href="{escape(download_url)}" style="display:inline-block;background:#EE2424;color:#ffffff;text-decoration:none;'
                  f'font-weight:600;padding:12px 22px;border-radius:999px;font-size:15px">Download your copy</a>')
        if file_name:
            button += f'<p style="margin:10px 0 0;color:#64748b;font-size:12px">File: {escape(file_name)}</p>'
    elif page_url:
        button = (f'<a href="{escape(page_url)}" style="display:inline-block;background:#EE2424;color:#ffffff;text-decoration:none;'
                  f'font-weight:600;padding:12px 22px;border-radius:999px;font-size:15px">Read the full content</a>')
    extra = '<p style="margin:16px 0 0;color:#475569;font-size:14px">We have also attached the file to this email.</p>' if attached else ""
    page = (f'<p style="margin:16px 0 0;color:#475569;font-size:14px">You can also <a href="{escape(page_url)}" style="color:#0072AD">view it on our website</a>.</p>'
            if page_url and download_url else "")
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f1f5f9;padding:24px 0">'
        '<tr><td align="center"><table role="presentation" width="600" cellpadding="0" cellspacing="0" '
        'style="background:#ffffff;border-radius:12px;font-family:Arial,Helvetica,sans-serif;max-width:600px;width:100%">'
        '<tr><td style="background:#0D192D;padding:20px 28px;border-radius:12px 12px 0 0">'
        f'<span style="color:#FF4D4D;font-size:11px;letter-spacing:2px;text-transform:uppercase">{escape(emailer.EMAIL_FROM_NAME)}</span>'
        f'<h1 style="margin:8px 0 0;color:#ffffff;font-size:22px;font-weight:600;line-height:1.3">{escape(title)}</h1></td></tr>'
        f'<tr><td style="padding:24px 28px;color:#0f172a;font-size:15px;line-height:1.6"><p style="margin:0 0 12px">{greeting}</p>'
        f'<p style="margin:0 0 20px">{escape(message)}</p>{button}{extra}{page}'
        f'<p style="margin:28px 0 0;color:#64748b;font-size:12px;line-height:1.5">You are receiving this because you requested this resource on our website. '
        'We never ask for passwords or payment details by email.</p></td></tr></table></td></tr></table>'
    )


async def _resolve(slug: Optional[str], fallback_title: Optional[str]) -> dict:
    """Find what to deliver for a slug: a live CMS item (maybe with a file) or a built-in page."""
    doc = await database.db.content.find_one({"slug": slug}, {"_id": 0}) if slug else None
    if doc and not _is_live(doc):
        doc = None
    f = await database.db.files.find_one({"id": doc["file_id"]}, {"_id": 0, "data": 0}) if doc and doc.get("file_id") else None
    return {"doc": doc, "file": f, "title": (doc or {}).get("title") or fallback_title or "your resource"}


async def deliver(sub: dict, *, base: str, resend_of: Optional[str] = None, force: bool = False) -> dict:
    """Email the asset behind a download submission and log the attempt."""
    settings = await get_settings()
    slug = sub.get("resource_slug")
    target = await _resolve(slug, sub.get("resource"))
    record = {
        "id": str(uuid.uuid4()), "submission_id": sub.get("id"), "email": (sub.get("email") or "").lower(), "name": sub.get("name"),
        "slug": slug, "title": target["title"], "created_at": now_iso(), "attached": False, "resend_of": resend_of,
    }

    async def done(status: str, detail: Optional[str] = None, **more) -> dict:
        record.update(status=status, detail=detail, **more)
        await database.db.deliveries.insert_one(dict(record))
        record.pop("_id", None)
        return record

    if not settings["enabled"] and not force:
        return await done("skipped", "Automatic delivery is turned off")
    if not emailer.email_provider():
        return await done("skipped", "No email transport configured")
    if not force and slug:
        since = (datetime.now(timezone.utc) - timedelta(minutes=DEDUPE_MINUTES)).isoformat()
        if await database.db.deliveries.find_one({"email": record["email"], "slug": slug, "status": "sent", "created_at": {"$gte": since}}, {"_id": 1}):
            return await done("skipped", "Already sent in the last few minutes")

    days = int(settings["link_days"])
    page_url = None
    if slug:
        page_url = f"{site_url()}/resources/{slug}?access={access_token(slug, days)}"
    download_url, attachments, f = None, [], target["file"]
    if f:
        if not base.startswith("https://"):
            return await done("failed", "PUBLIC_API_URL must be an https:// address to put download links in email")
        download_url = base + file_url(f, _signed(f["id"], days * 86400))
        limit = float(settings["attach_max_mb"]) * 1024 * 1024
        if settings["attach"] and emailer.supports_attachments() and f["size"] <= limit:
            full = await database.db.files.find_one({"id": f["id"]}, {"_id": 0, "data": 1})
            if full:
                attachments = [{"name": f["name"], "content_type": f["content_type"], "data": bytes(full["data"])}]
    if not download_url and not page_url:
        return await done("skipped", "Nothing to deliver for this form (no resource given)")

    subject = _fill(settings["subject"], title=target["title"], days=days)[:200]
    message = _fill(settings["message"], title=target["title"], days=days, name=sub.get("name") or "")
    html = delivery_html(name=sub.get("name") or "", title=target["title"], message=message, file_name=f["name"] if f else None,
                         download_url=download_url, page_url=page_url, attached=bool(attachments))
    try:
        email_id = await emailer.send_email(to=record["email"], subject=subject, html=html, attachments=attachments)
    except Exception as exc:
        logger.exception("asset delivery failed")
        return await done("failed", str(exc)[:300])
    return await done("sent", None, email_id=email_id, attached=bool(attachments), provider=emailer.email_provider())


async def deliver_for_submission(sub: dict, base: str) -> None:
    if sub.get("type") != "download":
        return
    try:
        await deliver(sub, base=base)
    except Exception:
        logger.exception("asset delivery crashed for submission %s", sub.get("id"))


# --- Public: verify a "reopen the full article" link --------------------------

@public.get("/content-access/{slug}")
async def verify_access(slug: str, t: str = Query(min_length=8, max_length=200)):
    """Check a link from a delivery email; for a gated CMS file, also return a fresh signed download link."""
    if not _check_signed(f"access:{slug}", t):
        return {"ok": False}
    target = await _resolve(slug, None)
    f = target["file"]
    return {"ok": True, "url": file_url(f, _signed(f["id"], 24 * 3600)) if f else None}


# --- Admin ----------------------------------------------------------------------

class DeliverySettings(BaseModel):
    enabled: bool = True
    subject: str = Field(min_length=3, max_length=200)
    message: str = Field(min_length=3, max_length=1000)
    attach: bool = True
    attach_max_mb: float = Field(ge=0.5, le=10)
    link_days: int = Field(ge=1, le=90)


@admin.get("/delivery")
async def get_delivery():
    return {**await get_settings(), "provider": emailer.email_provider(), "attachments_supported": emailer.supports_attachments(),
            "site_url": site_url(), "api_url": os.environ.get("PUBLIC_API_URL") or None}


@admin.put("/delivery", dependencies=[Depends(require_roles("admin"))])
async def put_delivery(body: DeliverySettings):
    await database.db.settings.update_one({"key": "delivery"}, {"$set": {**body.model_dump(), "updated_at": now_iso()}}, upsert=True)
    return await get_delivery()


@admin.get("/deliveries")
async def list_deliveries(status: Optional[str] = None, limit: int = Query(50, le=200)):
    query = {"status": status} if status else {}
    return await database.db.deliveries.find(query, {"_id": 0}).sort("created_at", -1).to_list(limit)


class TestDelivery(BaseModel):
    email: EmailStr
    slug: Optional[str] = Field(default=None, max_length=120)


@admin.post("/deliveries/test", dependencies=[Depends(require_roles("admin", "editor"))])
async def test_delivery(body: TestDelivery, request: Request, user: dict = Depends(get_current_admin)):
    """Send yourself the email a visitor would get for an asset (the latest gated item by default)."""
    slug = body.slug
    if not slug:
        latest = await database.db.content.find({"gated": True, "file_id": {"$ne": None}, "status": {"$in": ["published", "scheduled"]}}, {"_id": 0, "slug": 1}).sort("publish_at", -1).to_list(1)
        slug = latest[0]["slug"] if latest else None
    if not slug:
        raise HTTPException(status_code=422, detail="Publish a gated item with a file first, or name one to test with.")
    sub = {"id": f"test-{int(time.time())}", "type": "download", "email": str(body.email), "name": user.get("name") or "", "resource_slug": slug}
    return await deliver(sub, base=api_base(request), force=True)


@admin.post("/deliveries/{delivery_id}/resend", dependencies=[Depends(require_roles("admin", "sales", "editor"))])
async def resend(delivery_id: str, request: Request):
    old = await database.db.deliveries.find_one({"id": delivery_id}, {"_id": 0})
    if not old:
        raise HTTPException(status_code=404, detail="Delivery not found")
    sub = {"id": old.get("submission_id"), "type": "download", "email": old["email"], "name": old.get("name"), "resource_slug": old.get("slug"), "resource": old.get("title")}
    return await deliver(sub, base=api_base(request), resend_of=delivery_id, force=True)
