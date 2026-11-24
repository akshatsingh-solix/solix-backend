"""Seamless.AI contact research (spec: contact-verify; MarkHub's 12 Nov decision to explore Seamless for enrichment).

Flow (Seamless public API): research is asynchronous. POST /contacts/research with direct inputs (an email, a
LinkedIn URL, or name plus company/domain) answers 202 with request ids; GET /contacts/research/poll returns the
researched contact. MarkHub polls 2-5 s apart, a few times, then leaves the rest to the next run.

What we keep, and why so little (Seamless Public API Terms; the CEO's 17 Oct data-minimisation directive):
only the fields the gates need (work email + its validation, title, LinkedIn URL, phone if asked) are written,
each tagged `vendor: seamless` in `vendor_fields`, so `purge_vendor()` can remove exactly Seamless-sourced data
if the contract ends. Seamless's do-not-email flag goes straight to the suppression list.

Needs SEAMLESS_API_KEY (a "Public API licence" on the Seamless account: without it calls fail 422
missingApiAccess). Credit cost per call isn't consistent across Seamless's own docs: the X-PublicAPI-Credits
header of each response is logged so the real cost is measured on the live account.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

import httpx

import database
from database import now_iso

logger = logging.getLogger("solix.seamless")
BASE = os.environ.get("SEAMLESS_API_URL", "https://api.seamless.ai/api/client/v1").rstrip("/")
VALID = {"valid", "verified", "deliverable"}


def ready() -> bool:
    return bool(os.environ.get("SEAMLESS_API_KEY"))


async def _req(client: httpx.AsyncClient, method: str, path: str, **kw) -> dict:
    r = await client.request(method, f"{BASE}{path}", headers={"Token": os.environ["SEAMLESS_API_KEY"]}, **kw)
    credits = r.headers.get("X-PublicAPI-Credits")
    if credits:
        await database.db.vendor_usage.insert_one({"vendor": "seamless", "path": path, "credits_header": credits, "at": now_iso()})
    if r.status_code == 422 and "missingApiAccess" in r.text:
        raise RuntimeError("Seamless: this account has no Public API licence (missingApiAccess)")
    if r.status_code == 429:
        raise RuntimeError("Seamless: rate limited (about 60 requests a minute per endpoint)")
    if r.status_code >= 400:
        raise RuntimeError(f"Seamless {r.status_code}: {r.text[:200]}")
    return r.json() if r.content else {}


def _inputs(contact: dict, company: Optional[dict]) -> dict:
    if contact.get("linkedin_url"):
        return {"linkedInUrl": contact["linkedin_url"]}
    if contact.get("email"):
        return {"email": contact["email"]}
    dom = (company or {}).get("domain") or ((company or {}).get("domains") or [None])[0]
    return {"name": contact.get("name"), "companyName": (company or {}).get("name"), **({"domain": dom} if dom else {})}


async def research(contact: dict, company: Optional[dict] = None, *, polls: int = 5) -> dict:
    """Research one person; returns the minimal field set, or {"pending": request_id} if Seamless is still working."""
    if not ready():
        raise RuntimeError("SEAMLESS_API_KEY is not set")
    async with httpx.AsyncClient(timeout=30) as client:
        started = await _req(client, "POST", "/contacts/research", json={"contacts": [_inputs(contact, company)]})
        ids = started.get("requestIds") or [started.get("requestId")]
        ids = [i for i in ids if i]
        if not ids:
            return {}
        for attempt in range(polls):
            await asyncio.sleep(3 if attempt else 2)
            res = await _req(client, "GET", "/contacts/research/poll", params={"requestIds": ",".join(ids)})
            items = res.get("data") or res.get("contacts") or []
            done = [x for x in items if (x.get("status") or "").lower() in ("complete", "completed", "done", "duplicate", "contact-already-researched")]
            if done:
                return _minimal(done[0].get("contact") or done[0])
    return {"pending": ids[0]}


def _minimal(c: dict) -> dict:
    emails = c.get("emails") or ([{"email": c.get("email"), "validation": c.get("emailValidation")}] if c.get("email") else [])
    best = next((e for e in emails if (e.get("validation") or e.get("status") or "").lower() in VALID), emails[0] if emails else {})
    return {"email": (best.get("email") or "").lower() or None, "email_validation": (best.get("validation") or best.get("status") or "").lower() or None,
            "title": c.get("title") or c.get("jobTitle"), "linkedin_url": c.get("linkedInUrl") or c.get("linkedinUrl"),
            "do_not_email": bool(c.get("doNotEmail") or c.get("do_not_email")), "seamless_id": c.get("id") or c.get("contactId")}


async def apply(contact: dict, found: dict, by: str) -> dict:
    """Write what Seamless found onto the contact, tagged by vendor; honour its do-not-email flag."""
    import compliance
    if not found or found.get("pending"):
        return contact
    sets, tagged = {}, []
    for k in ("email", "title", "linkedin_url"):
        if found.get(k) and not contact.get(k):
            sets[k] = found[k]
            tagged.append(k)
    if found.get("email") and (found.get("email_validation") in VALID) and (contact.get("email") in (None, found["email"])):
        sets.update({"email_status": "valid", "verify_detail": "Validated by Seamless.AI", "verified_at": now_iso(), "source_email": "seamless"})
    if found.get("seamless_id"):
        sets["vendor_ids"] = {**(contact.get("vendor_ids") or {}), "seamless": found["seamless_id"]}
    if tagged:
        sets["vendor_fields"] = {**(contact.get("vendor_fields") or {}), **{k: "seamless" for k in tagged}}
    sets["updated_at"] = now_iso()
    await database.db.contacts.update_one({"id": contact["id"]}, {"$set": sets})
    email = sets.get("email") or contact.get("email")
    if found.get("do_not_email") and email:
        await compliance.suppress(email, "dnc", source="seamless", by=by)
    return {**contact, **sets}


async def purge_vendor(vendor: str = "seamless") -> int:
    """Remove every field a vendor supplied (contract ended, or its terms require deletion)."""
    n = 0
    async for c in database.db.contacts.find({"vendor_fields": {"$exists": True}}, {"_id": 0, "id": 1, "vendor_fields": 1}):
        fields = [k for k, v in (c.get("vendor_fields") or {}).items() if v == vendor]
        if fields:
            await database.db.contacts.update_one({"id": c["id"]}, {"$unset": {k: "" for k in fields}, "$set": {
                "vendor_fields": {k: v for k, v in c["vendor_fields"].items() if v != vendor}, "updated_at": now_iso()}})
            n += 1
    return n
