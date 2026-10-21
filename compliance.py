"""EchoLocate compliance: the rules every outbound message must pass.

* Suppression list - unsubscribes, bounces, not-interested replies, rep DNCs and
  GDPR erasures. Erasures keep only a SHA-256 of the address, so the person is
  blocked forever without their address being stored.
* Exclusion gate - Germany and Poland (UWG §7 / PKE Art. 398) are blocked for
  outbound, as are existing customers, accounts in a live deal, personal email
  domains and suppressed people.
* Confidence gate - nothing is sent without a verified business email, a name,
  a title, a real signal on the account, an account score over the floor, a
  lawful basis and a signed Legitimate Interest Assessment for the region.
* LIA register - outbound to a region stays off until compliance signs its LIA.
* Erasure - GDPR Art. 17: one request deletes the person from every collection
  and queues deletion in Instantly and Digiprise.
* One-click unsubscribe (RFC 8058) on every email, processed immediately.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import uuid
from typing import List, Optional, Set, Tuple

from fastapi import APIRouter, Depends, Form, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

import database
import ops
import radar_model as rm
import scoring
from auth import get_current_admin, require_roles
from database import now_iso

router = APIRouter(prefix="/api/admin/compliance", tags=["compliance"], dependencies=[Depends(get_current_admin)])
public = APIRouter(prefix="/api/outbound", tags=["outbound-public"])
can_comply = Depends(require_roles("admin", "compliance"))
can_suppress = Depends(require_roles("admin", "compliance", "sales"))

REASONS = ("unsubscribed", "bounced", "not_interested", "dnc", "gdpr_erasure", "complaint", "manual")
REGIONS = ("eu", "uk", "us", "global")
DEFAULT_OUTBOUND = {
    "sending_enabled": False, "paused": False, "pause_reason": None, "approval_required": True, "provider": "dry_run",
    "instantly_campaign_id": None, "blocked_countries": ["Germany", "Poland"], "postal_address": "", "sender_name": "Solix Technologies",
    "daily_cap": 50, "send_days": [0, 1, 2, 3, 4], "send_hours_utc": [8, 17], "min_account_score": 30, "require_verified_email": True,
    "max_touches": 4, "ai_disclosure": "", "bounce_pause_pct": 3.0, "ab_min_sends": 50,
}


def _db():
    return database.db


def email_hash(email: str) -> str:
    return hashlib.sha256((email or "").strip().lower().encode()).hexdigest()


def region_of(country: Optional[str]) -> str:
    c = rm.country_norm(country)
    if c in ("United Kingdom", "England", "Scotland", "Wales", "Northern Ireland"):
        return "uk"
    if rm.COUNTRY_TO_REGION.get(c) == "Europe & UK":
        return "eu"
    if c in ("United States", "Canada", "Puerto Rico"):
        return "us"
    return "global"


def recipient_location(contact: dict, company: Optional[dict]) -> Tuple[Set[str], str, bool]:
    """(countries, region, explicit). Contact country, then account HQ; without either, the account's signal
    countries and routed territory stand in, so a Radar account with no HQ on file still lands in the right region."""
    company = company or {}
    explicit = contact.get("country") or (company.get("hq") or {}).get("country")
    if explicit:
        return {rm.country_norm(explicit)}, region_of(explicit), True
    countries = {rm.country_norm(c) for c in company.get("countries") or [] if c}
    territory = company.get("territory")
    if territory in ("USA", "Canada"):
        region = "us"
    elif territory == "Europe & UK":
        in_europe = {c for c in countries if region_of(c) in ("eu", "uk")}
        region = "uk" if in_europe and all(region_of(c) == "uk" for c in in_europe) else "eu"
    else:
        region = "global"
    return countries, region, False


async def outbound_settings() -> dict:
    doc = await _db().settings.find_one({"key": "outbound"}, {"_id": 0}) or {}
    return {**DEFAULT_OUTBOUND, **{k: v for k, v in doc.items() if k in DEFAULT_OUTBOUND}}


async def save_outbound_settings(values: dict) -> dict:
    clean = {k: v for k, v in values.items() if k in DEFAULT_OUTBOUND}
    await _db().settings.update_one({"key": "outbound"}, {"$set": {**clean, "updated_at": now_iso()}}, upsert=True)
    return await outbound_settings()


# --- Suppression ------------------------------------------------------------------------------

async def is_suppressed(email: Optional[str]) -> Optional[str]:
    if not email:
        return None
    doc = await _db().suppression.find_one({"$or": [{"email_hash": email_hash(email)}, {"domain": rm.domain_of(email)}]}, {"_id": 0, "reason": 1})
    return doc["reason"] if doc else None


async def suppress(email: str, reason: str, *, source: str, by: str, keep_email: bool = True) -> dict:
    email = (email or "").strip().lower()
    if reason not in REASONS:
        reason = "manual"
    doc = {"email_hash": email_hash(email), "email": email if keep_email else None, "reason": reason, "source": source, "added_by": by, "added_at": now_iso()}
    await _db().suppression.update_one({"email_hash": doc["email_hash"]}, {"$set": doc}, upsert=True)
    if email:  # sales sees it on the lead too
        note = {"at": now_iso(), "by": by, "action": "suppressed", "detail": f"Do not contact: {reason.replace('_', ' ')} ({source})"}
        await _db().leads.update_many({"email": email}, {"$set": {"do_not_contact": reason}, "$push": {"history": {"$each": [note], "$slice": -50}}})
    ids = [c["id"] async for c in _db().contacts.find({"email": email}, {"_id": 0, "id": 1})] if email else []
    if ids:
        await _db().contacts.update_many({"id": {"$in": ids}}, {"$set": {"suppressed": reason, "updated_at": now_iso()}})
        await _db().enrollments.update_many({"contact_id": {"$in": ids}, "status": {"$in": ["pending_approval", "scheduled", "active", "paused"]}},
                                            {"$set": {"status": "stopped", "stop_reason": f"Suppressed: {reason}", "updated_at": now_iso()}})
    if os.environ.get("INSTANTLY_API_KEY") and email:
        await ops.enqueue("instantly_block", "suppression", {"email": email}, key=f"block:{doc['email_hash']}")
    await ops.digiprise("contact.suppressed", {"email_hash": doc["email_hash"], "reason": reason, "email": email if keep_email else None}, key=f"dp-supp:{doc['email_hash']}:{reason}")
    return doc


# --- Gates ---------------------------------------------------------------------------------------

async def lia_signed(region: str) -> bool:
    doc = await _db().settings.find_one({"key": "lia"}, {"_id": 0}) or {}
    return any(r.get("signed_at") and r.get("region") in (region, "global") and not r.get("revoked") for r in doc.get("records") or [])


async def gate(contact: dict, company: Optional[dict], settings: Optional[dict] = None, *, enrollment_id: Optional[str] = None) -> dict:
    """Exclusion + confidence gate. `ok` only when there are no blocking reasons."""
    settings = settings or await outbound_settings()
    reasons: List[str] = []
    warnings: List[str] = []
    email = (contact.get("email") or "").strip().lower()
    if not email or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", email):
        reasons.append("No valid email address")
    else:
        sup = await is_suppressed(email)
        if sup:
            reasons.append(f"On the suppression list ({sup.replace('_', ' ')})")
        if rm.domain_of(email) in scoring.FREE_EMAIL_DOMAINS:
            reasons.append("Personal email address: business addresses only")
        lead = await _db().leads.find_one({"email": email}, {"_id": 0, "stage": 1, "owner": 1})
        owned = f", owner {lead['owner']}" if (lead or {}).get("owner") else ""
        if (lead or {}).get("stage") in ("sql", "opportunity", "won"):
            reasons.append(f"Already in the sales pipeline ({lead['stage'].upper()}{owned})")
        elif (lead or {}).get("stage") in ("mql", "sal") and owned:
            warnings.append(f"Inbound {lead['stage'].upper()}{owned}: check with them before sequencing")
    status = contact.get("email_status") or "unchecked"
    if status == "invalid":
        reasons.append("Email address is invalid or has bounced")
    elif status != "valid" and settings.get("require_verified_email"):
        reasons.append(f"Email not verified ({status.replace('_', ' ')}): verify it, mark it verified, or allow unverified addresses in Compliance")
    elif status != "valid":
        warnings.append(f"Mailbox not verified ({status.replace('_', ' ')})")
    if not contact.get("name"):
        reasons.append("No contact name")
    if not contact.get("title"):
        reasons.append("No job title")
    if not contact.get("lawful_basis"):
        reasons.append("No lawful basis recorded")
    countries, region, explicit = recipient_location(contact, company)
    blocked = {rm.country_norm(c) for c in settings.get("blocked_countries") or []}
    if countries & blocked:
        where = ", ".join(sorted(countries & blocked))
        reasons.append(f"Outbound blocked in {where} (prior consent required)" if explicit else
                       f"Account has signals in {where}: set the contact's country to confirm where they are")
    if not company:
        reasons.append("Not linked to an account")
    else:
        if company.get("excluded_reason"):
            reasons.append(f"Account excluded: {company['excluded_reason']}")
        if not (company.get("signal_count") or 0) and "website" not in (company.get("signal_types") or []):
            reasons.append("Account has no intent signal")
        if (company.get("score") or 0) < float(settings.get("min_account_score") or 0):
            reasons.append(f"Account score {round(company.get('score') or 0)} is under {settings.get('min_account_score')}")
        if company.get("status") in ("not_a_fit", "won", "lost", "meeting_booked", "opportunity"):
            reasons.append(f"Account status is {rm.STATUS_LABELS.get(company['status'], company['status'])}")
        if not company.get("research_id"):
            warnings.append("No deep dive on this account yet")
    if not await lia_signed(region):
        reasons.append(f"No signed Legitimate Interest Assessment for region '{region}'")
    if contact.get("id"):
        other = await _db().enrollments.find_one({"contact_id": contact["id"], "status": {"$in": ["pending_approval", "scheduled", "active", "paused"]},
                                                  **({"id": {"$ne": enrollment_id}} if enrollment_id else {})}, {"_id": 0, "sequence_name": 1})
        if other:
            reasons.append(f"Already in an active sequence ({other.get('sequence_name') or 'another sequence'})")
    if contact.get("persona") != "target":
        warnings.append("Title is outside the target personas")
    return {"ok": not reasons, "reasons": reasons, "warnings": warnings, "region": region}


# --- Unsubscribe ----------------------------------------------------------------------------------

def _secret() -> bytes:
    return (os.environ.get("UNSUBSCRIBE_SECRET") or os.environ.get("JWT_SECRET") or "dev-secret").encode()


def unsubscribe_token(email: str) -> str:
    e = base64.urlsafe_b64encode(email.strip().lower().encode()).decode().rstrip("=")
    sig = hmac.new(_secret(), f"unsub|{e}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{e}.{sig}"


def unsubscribe_url(email: str) -> str:
    base = (os.environ.get("PUBLIC_API_URL") or "").rstrip("/")
    return f"{base}/api/outbound/unsubscribe?t={unsubscribe_token(email)}"


def _email_from_token(t: str) -> Optional[str]:
    try:
        e, sig = t.rsplit(".", 1)
    except ValueError:
        return None
    good = hmac.new(_secret(), f"unsub|{e}".encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(good, sig):
        return None
    try:
        return base64.urlsafe_b64decode(e + "=" * (-len(e) % 4)).decode()
    except (ValueError, UnicodeDecodeError):
        return None


_PAGE = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Solix email preferences</title>
<body style="font-family:Arial,sans-serif;max-width:520px;margin:60px auto;padding:0 16px;color:#14222c"><h1 style="font-size:22px">{title}</h1><p>{body}</p></body>"""


@public.get("/unsubscribe", response_class=HTMLResponse)
async def unsubscribe_page(t: str = Query(..., max_length=400)):
    if not _email_from_token(t):
        return HTMLResponse(_PAGE.format(title="This link isn't valid", body="Reply to the email with 'unsubscribe' and we'll remove you straight away."), status_code=400)
    form = f'<form method="post"><input type="hidden" name="t" value="{t}"><button style="padding:10px 18px;font-size:15px">Unsubscribe me</button></form>'
    return HTMLResponse(_PAGE.format(title="Stop emails from Solix?", body=f"Press the button and you won't get further outreach emails from us. {form}"))


@public.post("/unsubscribe", response_class=HTMLResponse)
async def unsubscribe(t: str = Form(...)):
    """One-click (RFC 8058) and the confirmation button both land here."""
    email = _email_from_token(t)
    if not email:
        raise HTTPException(status_code=400, detail="Invalid unsubscribe link")
    await suppress(email, "unsubscribed", source="unsubscribe_link", by="recipient")
    await ops.notify("replies", "A recipient unsubscribed via the email link.", event="unsubscribe")
    return HTMLResponse(_PAGE.format(title="You're unsubscribed", body="You won't receive further outreach emails from Solix. This took effect immediately."))


# --- Erasure (GDPR Art. 17) -------------------------------------------------------------------------

async def erase(email: str, by: str) -> dict:
    db = _db()
    email = email.strip().lower()
    counts = {}
    contacts = [c async for c in db.contacts.find({"email": email}, {"_id": 0, "id": 1, "name": 1, "company_id": 1})]
    contact_ids = [c["id"] for c in contacts]
    lead_ids = [l["id"] async for l in db.leads.find({"email": email}, {"_id": 0, "id": 1})]
    visitor_ids = [v["id"] async for v in db.visitors.find({"$or": [{"email": email}, {"lead_id": {"$in": lead_ids}}]}, {"_id": 0, "id": 1})]
    chat_sessions = [s["source_page"][5:] async for s in db.submissions.find({"email": email, "source": "chat", "source_page": {"$regex": "^chat:"}}, {"_id": 0, "source_page": 1})]
    ops_ = [
        ("contacts", db.contacts.delete_many({"id": {"$in": contact_ids}})),
        ("enrollments", db.enrollments.delete_many({"contact_id": {"$in": contact_ids}})),
        ("sends", db.sends.delete_many({"$or": [{"contact_id": {"$in": contact_ids}}, {"email": email}]})),
        ("replies", db.reply_log.delete_many({"$or": [{"contact_id": {"$in": contact_ids}}, {"email": email}]})),
        ("leads", db.leads.delete_many({"id": {"$in": lead_ids}})),
        ("submissions", db.submissions.delete_many({"email": email})),
        ("visitors", db.visitors.delete_many({"id": {"$in": visitor_ids}})),
        ("events", db.events.delete_many({"visitor_id": {"$in": visitor_ids}})),
        ("chat_messages", db.chat_messages.delete_many({"session_id": {"$in": chat_sessions}})),
        ("trial_accounts", db.accounts.delete_many({"email": email})),
        ("event_registrations", db.event_registrations.delete_many({"email": email})),
        ("deliveries", db.deliveries.delete_many({"email": email})),
        ("notifications", db.notifications.delete_many({"lead_email": email})),
    ]
    for name, op in ops_:
        res = await op
        counts[name] = res.deleted_count
    await db.companies.update_many({"lead_ids": {"$in": lead_ids}}, {"$pull": {"lead_ids": {"$in": lead_ids}}})
    # Account history and queued Slack/Digiprise/email jobs mention the person too.
    rx = {"$regex": re.escape(email), "$options": "i"}
    res = await db.companies.update_many({"history.detail": rx}, {"$pull": {"history": {"detail": rx}}})
    scrubbed = res.modified_count
    for c in contacts:
        if c.get("name") and c.get("company_id"):
            res = await db.companies.update_one({"id": c["company_id"]}, {"$pull": {"history": {"detail": {"$regex": re.escape(c["name"])}}}})
            scrubbed += res.modified_count
    counts["account_history"] = scrubbed
    res = await db.ops_outbox.delete_many({"$or": [{"payload.email": email}, {"payload.reply_to": email}, {"payload.text": rx}, {"payload.excerpt": rx}],
                                           "target": {"$nin": ["instantly_delete", "instantly_block"]}})
    counts["queued_messages"] = res.deleted_count
    await suppress(email, "gdpr_erasure", source="erasure", by=by, keep_email=False)
    await db.suppression.update_one({"email_hash": email_hash(email)}, {"$set": {"email": None}})
    if os.environ.get("INSTANTLY_API_KEY"):
        await ops.enqueue("instantly_delete", "erasure", {"email": email}, key=f"erase:{email_hash(email)}")
    await ops.digiprise("contact.erased", {"email_hash": email_hash(email)}, key=f"dp-erase:{email_hash(email)}")
    rec = {"id": str(uuid.uuid4()), "email_hash": email_hash(email), "requested_by": by, "completed_at": now_iso(), "counts": counts}
    await db.erasure_requests.insert_one(dict(rec))
    return rec


# --- API ----------------------------------------------------------------------------------------------

class OutboundSettingsIn(BaseModel):
    sending_enabled: Optional[bool] = None
    paused: Optional[bool] = None
    approval_required: Optional[bool] = None
    provider: Optional[str] = Field(default=None, pattern="^(dry_run|smtp|instantly)$")
    instantly_campaign_id: Optional[str] = Field(default=None, max_length=80)
    blocked_countries: Optional[List[str]] = None
    postal_address: Optional[str] = Field(default=None, max_length=300)
    sender_name: Optional[str] = Field(default=None, max_length=120)
    daily_cap: Optional[int] = Field(default=None, ge=0, le=5000)
    send_days: Optional[List[int]] = None
    send_hours_utc: Optional[List[int]] = Field(default=None, min_length=2, max_length=2)
    min_account_score: Optional[float] = Field(default=None, ge=0, le=1000)
    require_verified_email: Optional[bool] = None
    max_touches: Optional[int] = Field(default=None, ge=1, le=8)
    ai_disclosure: Optional[str] = Field(default=None, max_length=300)
    bounce_pause_pct: Optional[float] = Field(default=None, ge=0.5, le=20)
    ab_min_sends: Optional[int] = Field(default=None, ge=10, le=1000)


@router.get("/settings")
async def get_settings():
    s = await outbound_settings()
    doc = await _db().settings.find_one({"key": "lia"}, {"_id": 0}) or {}
    return {**s, "lia_regions": sorted({r["region"] for r in doc.get("records") or [] if r.get("signed_at") and not r.get("revoked")}),
            "transport": {"smtp_or_resend": bool(os.environ.get("SMTP_HOST") or os.environ.get("RESEND_API_KEY")), "instantly": bool(os.environ.get("INSTANTLY_API_KEY")),
                          "public_api_url": bool(os.environ.get("PUBLIC_API_URL"))}}


@router.put("/settings", dependencies=[Depends(require_roles("admin"))])
async def put_settings(body: OutboundSettingsIn, user: dict = Depends(get_current_admin)):
    values = body.model_dump(exclude_none=True)
    current = await outbound_settings()
    if values.get("sending_enabled") and not current.get("sending_enabled"):
        if values.get("provider", current["provider"]) != "dry_run" and not (values.get("postal_address") or current.get("postal_address")):
            raise HTTPException(status_code=422, detail="Add the Solix postal address first: PECR and CAN-SPAM require it in every email.")
        if values.get("provider", current["provider"]) != "dry_run" and not os.environ.get("PUBLIC_API_URL"):
            raise HTTPException(status_code=422, detail="Set PUBLIC_API_URL on the server so emails can carry a working unsubscribe link.")
    if values.get("paused") is False:
        values["pause_reason"] = None
    saved = await save_outbound_settings(values)
    await ops.notify("watchdog", f"Outbound settings changed by {user['email']}: sending {'on' if saved['sending_enabled'] else 'off'}, {'paused' if saved['paused'] else 'running'}, provider {saved['provider']}.", event="settings")
    return saved


@router.get("/suppression", dependencies=[can_suppress])
async def list_suppression(q: Optional[str] = None, reason: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(50, le=200)):
    query: dict = {}
    if q:
        query["$or"] = [{"email": {"$regex": re.escape(q.strip().lower())}}, {"email_hash": email_hash(q)}]
    if reason:
        query["reason"] = reason
    total = await _db().suppression.count_documents(query)
    items = await _db().suppression.find(query, {"_id": 0}).sort("added_at", -1).skip((page - 1) * page_size).limit(page_size).to_list(page_size)
    return {"items": items, "total": total}


class SuppressIn(BaseModel):
    emails: List[str] = Field(min_length=1, max_length=5000)
    reason: str = "dnc"


@router.post("/suppression", dependencies=[can_suppress])
async def add_suppression(body: SuppressIn, user: dict = Depends(get_current_admin)):
    n = 0
    for raw in body.emails:
        for e in re.findall(r"[^\s,;<>\"']+@[^\s,;<>\"']+\.[a-z]{2,}", raw.lower()):
            await suppress(e, body.reason if body.reason in REASONS else "dnc", source="admin", by=user["email"])
            n += 1
    return {"added": n}


@router.delete("/suppression/{hash_}", dependencies=[can_comply])
async def remove_suppression(hash_: str, user: dict = Depends(get_current_admin)):
    doc = await _db().suppression.find_one({"email_hash": hash_}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Not on the list")
    if doc["reason"] in ("gdpr_erasure", "unsubscribed", "complaint"):
        raise HTTPException(status_code=409, detail="Unsubscribes, complaints and erasures can't be lifted from the admin.")
    await _db().suppression.delete_one({"email_hash": hash_})
    if doc.get("email"):
        note = {"at": now_iso(), "by": user["email"], "action": "unsuppressed", "detail": "Removed from the do-not-contact list"}
        await _db().leads.update_many({"email": doc["email"]}, {"$unset": {"do_not_contact": ""}, "$push": {"history": {"$each": [note], "$slice": -50}}})
        await _db().contacts.update_many({"email": doc["email"]}, {"$set": {"suppressed": None}})
    await ops.notify("watchdog", f"{user['email']} removed a {doc['reason']} suppression.", event="unsuppress")
    return {"removed": True}


class LiaIn(BaseModel):
    region: str = Field(pattern="^(eu|uk|us|global)$")
    campaign_type: str = Field(min_length=2, max_length=120)
    purpose: str = Field(min_length=10, max_length=3000)
    necessity: str = Field(min_length=10, max_length=3000)
    balancing: str = Field(min_length=10, max_length=3000)
    safeguards: Optional[str] = Field(default=None, max_length=3000)


@router.get("/lia")
async def list_lia():
    return (await _db().settings.find_one({"key": "lia"}, {"_id": 0}) or {}).get("records", [])


@router.post("/lia", status_code=201, dependencies=[can_comply])
async def create_lia(body: LiaIn, user: dict = Depends(get_current_admin)):
    rec = {"id": str(uuid.uuid4()), **body.model_dump(), "created_by": user["email"], "created_at": now_iso(), "signed_by": None, "signed_at": None}
    await _db().settings.update_one({"key": "lia"}, {"$push": {"records": rec}}, upsert=True)
    return rec


@router.post("/lia/{lia_id}/sign", dependencies=[can_comply])
async def sign_lia(lia_id: str, user: dict = Depends(get_current_admin)):
    doc = await _db().settings.find_one({"key": "lia"}, {"_id": 0}) or {}
    recs = doc.get("records") or []
    for r in recs:
        if r["id"] == lia_id:
            r.update({"signed_by": user["email"], "signed_at": now_iso(), "revoked": False})
            break
    else:
        raise HTTPException(status_code=404, detail="LIA not found")
    await _db().settings.update_one({"key": "lia"}, {"$set": {"records": recs}})
    await ops.notify("watchdog", f"LIA for region {r['region']} ({r['campaign_type']}) signed by {user['email']}.", event="lia")
    return r


@router.post("/lia/{lia_id}/revoke", dependencies=[can_comply])
async def revoke_lia(lia_id: str, user: dict = Depends(get_current_admin)):
    doc = await _db().settings.find_one({"key": "lia"}, {"_id": 0}) or {}
    recs = doc.get("records") or []
    hit = next((r for r in recs if r["id"] == lia_id), None)
    if not hit:
        raise HTTPException(status_code=404, detail="LIA not found")
    hit.update({"revoked": True, "revoked_by": user["email"], "revoked_at": now_iso()})
    await _db().settings.update_one({"key": "lia"}, {"$set": {"records": recs}})
    return hit


class ErasureIn(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    confirm: bool = False


@router.post("/erasure", dependencies=[can_comply])
async def erasure(body: ErasureIn, user: dict = Depends(get_current_admin)):
    if not body.confirm:
        raise HTTPException(status_code=422, detail="Erasure deletes this person everywhere and can't be undone. Send confirm: true.")
    return await erase(body.email, user["email"])


@router.get("/erasure", dependencies=[can_comply])
async def erasure_log(limit: int = Query(50, le=200)):
    return await _db().erasure_requests.find({}, {"_id": 0}).sort("completed_at", -1).to_list(limit)


@router.get("/gate/{contact_id}")
async def gate_check(contact_id: str):
    c = await _db().contacts.find_one({"id": contact_id}, {"_id": 0})
    if not c:
        raise HTTPException(status_code=404, detail="Contact not found")
    company = await _db().companies.find_one({"id": c.get("company_id")}, {"_id": 0}) if c.get("company_id") else None
    return await gate(c, company)
