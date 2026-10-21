"""EchoLocate outbound: contacts -> gated enrollment -> personalised sequence ->
send -> reply handling -> lead and CRM writeback -> learning.

Flow
----
1. A contact (from a Radar deep dive, a CSV, or typed in) is verified.
2. Enrolling them in a sequence runs the compliance gate, then writes a
   signal-grounded opener (never a template, never naming the competitor).
3. With approval on (the default, human-on-the-loop), it waits in the
   approval queue; approved enrollments are sent by the sender tick within
   the daily cap and send window - through SMTP, Instantly or dry-run.
4. Replies (Instantly webhook or logged by hand) are classified into six
   labels. A positive reply stops every other touch, becomes an MQL lead in the
   existing funnel, and is pushed to the AE, Slack and Digiprise.
5. Bounce rate over the threshold pauses all sending; the watchdog alerts.
6. Weekly, once 20+ outcomes exist, signal-type weights are re-learned.
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Dict, List, Literal, Optional

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

import compliance
import database
import ops
import radar_model as rm
import radar_service as rs
import scoring
from auth import get_current_admin, require_roles
from database import now_iso

logger = logging.getLogger("solix.outbound")
router = APIRouter(prefix="/api/admin/outbound", tags=["outbound"], dependencies=[Depends(get_current_admin)])
can_work = Depends(require_roles("admin", "sales"))
admin_only = Depends(require_roles("admin"))

INSTANTLY = "https://api.instantly.ai/api/v2"
REPLY_LABELS = ["interested", "meeting", "objection", "referral", "forward", "ooo", "not_interested", "unsubscribe", "neutral"]
POSITIVE = {"interested", "meeting"}
ACTIVE = ["pending_approval", "scheduled", "active", "paused"]
TARGET_PERSONAS = re.compile(r"\b(cio|chief information|chief data|cdo|chief technology|cto|vp|vice president|svp|evp|head of|director)\b.*\b(it|information|infrastructure|application|data|technology|engineering|architecture|records|compliance|digital)\b|"
                             r"\b(cio|cdo|cto|data architect|enterprise architect)\b|\bchief (information|data|technology|digital|architecture) officer\b", re.I)


def _db():
    return database.db


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- Contacts --------------------------------------------------------------------------------

def persona_of(title: Optional[str]) -> str:
    return "target" if TARGET_PERSONAS.search(title or "") else "other"


def seniority_of(title: Optional[str]) -> str:
    t = title or ""
    if scoring.SENIOR_RX.search(t):
        return "senior"
    if scoring.MANAGER_RX.search(t):
        return "manager"
    return "individual"


def new_contact_doc(data: dict, company: Optional[dict]) -> dict:
    name = (data.get("name") or " ".join(filter(None, [data.get("first_name"), data.get("last_name")]))).strip()
    parts = name.split()
    now = now_iso()
    return {
        "id": str(uuid.uuid4()), "company_id": (company or {}).get("id") or data.get("company_id"), "company": (company or {}).get("name") or data.get("company"),
        "name": name, "first_name": data.get("first_name") or (parts[0] if parts else ""), "last_name": data.get("last_name") or (" ".join(parts[1:]) if len(parts) > 1 else ""),
        "title": data.get("title"), "persona": persona_of(data.get("title")), "seniority": seniority_of(data.get("title")),
        "email": (data.get("email") or "").strip().lower() or None, "email_status": "unchecked" if data.get("email") else "missing", "verified_at": None,
        "linkedin_url": data.get("linkedin_url"), "phone": data.get("phone"), "country": data.get("country") or ((company or {}).get("hq") or {}).get("country"),
        "source": data.get("source") or "manual", "source_url": data.get("source_url"), "source_collected_at": now,
        "lawful_basis": data.get("lawful_basis") or "legitimate_interest", "suppressed": None, "notes": data.get("notes") or "", "created_at": now, "updated_at": now,
    }


async def verify_email(email: str) -> dict:
    """Syntax, MX lookup, then Reacher (self-hosted, free) when REACHER_URL is set."""
    email = (email or "").strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", email):
        return {"status": "invalid", "detail": "Not an email address"}
    domain = email.split("@")[1]
    try:
        import dns.asyncresolver
        import dns.exception
        r = dns.asyncresolver.Resolver()
        r.lifetime = 8
        mx = await r.resolve(domain, "MX")
        if not len(mx):
            return {"status": "invalid", "detail": "Domain has no mail server"}
    except Exception:  # noqa: BLE001 - NXDOMAIN, no answer, timeout
        return {"status": "invalid", "detail": "Domain has no mail server"}
    url = os.environ.get("REACHER_URL")
    if not url:
        return {"status": "mx_only", "detail": "Mail server exists; mailbox not checked (set REACHER_URL to verify mailboxes)"}
    headers = {"Authorization": os.environ["REACHER_TOKEN"]} if os.environ.get("REACHER_TOKEN") else {}
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            res = (await client.post(f"{url.rstrip('/')}/v0/check_email", json={"to_email": email}, headers=headers)).json()
    except (httpx.HTTPError, ValueError) as e:
        return {"status": "unknown", "detail": f"Verifier unavailable: {type(e).__name__}"}
    reach = res.get("is_reachable")
    smtp = res.get("smtp") or {}
    status = {"safe": "valid", "invalid": "invalid", "risky": "catch_all" if smtp.get("is_catch_all") else "risky"}.get(reach, "unknown")
    return {"status": status, "detail": f"Reacher: {reach}"}


async def set_verification(contact: dict) -> dict:
    v = await verify_email(contact.get("email"))
    await _db().contacts.update_one({"id": contact["id"]}, {"$set": {"email_status": v["status"], "verify_detail": v["detail"], "verified_at": now_iso()}})
    return {**contact, "email_status": v["status"], "verify_detail": v["detail"]}


def guess_emails(first: str, last: str, domain: str) -> List[str]:
    f, l = re.sub(r"[^a-z]", "", (first or "").lower()), re.sub(r"[^a-z]", "", (last or "").lower())
    if not (f and domain):
        return []
    pats = [f"{f}.{l}", f"{f[0]}{l}", f"{f}{l}", f"{f}", f"{f}_{l}", f"{f[0]}.{l}", f"{l}.{f}"] if l else [f]
    return [f"{p}@{domain}" for p in dict.fromkeys(pats)]


# --- Sequences ---------------------------------------------------------------------------------

DEFAULT_SEQUENCES = [
    {"name": "EU 3-touch (application retirement)", "region": "eu", "steps": [
        {"day": 0, "subject_a": "{company}'s legacy applications", "subject_b": "Retiring what {company} no longer runs",
         "body_a": "Hi {first_name},\n\n{opener}\n\nSolix retires legacy applications into one compliant, searchable archive with retention and legal hold, so the old platforms can be switched off and their cost removed.\n\nWould a 15-minute call in the next two weeks be useful?\n\nBest,\n{rep_name}",
         "body_b": "Hi {first_name},\n\n{opener}\n\nWe help teams keep the records and switch off the systems: one governed archive, retention rules applied, and the licences gone.\n\nWorth a short conversation?\n\nBest,\n{rep_name}"},
        {"day": 4, "subject_a": "Re: {company}'s legacy applications", "subject_b": "Re: Retiring what {company} no longer runs",
         "body_a": "Hi {first_name},\n\nA different angle: most retirement programmes stall on one question - what happens to the records the old system still holds. That's the part Solix takes off your plate, with the data still searchable for audits and AI work.\n\nOpen to comparing notes?\n\nBest,\n{rep_name}",
         "body_b": "Hi {first_name},\n\nOne data point: retiring an application into an archive typically removes its licence and infrastructure cost in the first year, while auditors can still search everything.\n\nShall I send a short overview?\n\nBest,\n{rep_name}"},
        {"day": 9, "subject_a": "Closing the loop", "subject_b": "Closing the loop",
         "body_a": "Hi {first_name},\n\nI may have the wrong person or the wrong moment. If someone else owns application retirement or archiving at {company}, I'd be grateful for a pointer - otherwise I'll leave it here.\n\nBest,\n{rep_name}",
         "body_b": "Hi {first_name},\n\nI'll stop here. If retiring legacy systems comes up later, I'm happy to help - or to talk to whoever owns it at {company}.\n\nBest,\n{rep_name}"},
    ]},
    {"name": "US 4-touch (competitive displacement)", "region": "us", "steps": [
        {"day": 0, "subject_a": "{company}: archive and retirement", "subject_b": "A question about {company}'s archive",
         "body_a": "Hi {first_name},\n\n{opener}\n\nSolix gives you one governed archive for retired applications, content and email, with retention and legal hold built in.\n\nWorth 15 minutes?\n\nBest,\n{rep_name}",
         "body_b": "Hi {first_name},\n\n{opener}\n\nIf consolidating archives and retiring old platforms is on the roadmap, we may be able to help.\n\nOpen to a short call?\n\nBest,\n{rep_name}"},
        {"day": 3, "subject_a": "Re: {company}: archive and retirement", "subject_b": "Re: A question about {company}'s archive",
         "body_a": "Hi {first_name},\n\nTeams usually come to us when keeping an old platform alive only for its records costs more than it should. We move the records, apply retention, and the platform goes.\n\nShall I share how others planned it?\n\nBest,\n{rep_name}",
         "body_b": "Hi {first_name},\n\nQuick follow-up with one idea: a single searchable archive also gives AI projects governed access to historical data.\n\nInterested?\n\nBest,\n{rep_name}"},
        {"day": 7, "subject_a": "Case in point", "subject_b": "Case in point",
         "body_a": "Hi {first_name},\n\nA similar enterprise retired dozens of legacy applications into Solix and kept every record searchable for auditors.\n\nHappy to walk you through it.\n\nBest,\n{rep_name}",
         "body_b": "Hi {first_name},\n\nOne customer story you might find relevant: they consolidated several archives into Solix and cut the run cost of retired systems.\n\nWant the details?\n\nBest,\n{rep_name}"},
        {"day": 12, "subject_a": "Should I close your file?", "subject_b": "Should I close your file?",
         "body_a": "Hi {first_name},\n\nI haven't heard back, so I'll assume the timing isn't right. If someone else at {company} owns archiving or retirement, a pointer would be appreciated.\n\nBest,\n{rep_name}",
         "body_b": "Hi {first_name},\n\nLast note from me. If this becomes a priority, just reply and I'll pick it up.\n\nBest,\n{rep_name}"},
    ]},
]


async def ensure_default_sequences() -> None:
    if await _db().sequences.count_documents({}) == 0:
        for s in DEFAULT_SEQUENCES:
            await _db().sequences.insert_one({"id": str(uuid.uuid4()), **s, "active": True, "provider": None, "instantly_campaign_id": None, "ab_winner": None,
                                              "created_by": "system", "created_at": now_iso(), "updated_at": now_iso()})


def _fill(template: str, ctx: dict) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: str(ctx.get(m.group(1), m.group(0))), template or "")


# --- Personalisation ----------------------------------------------------------------------------

OPENER_SYSTEM = ("You write the first two sentences of a B2B cold email for Solix Technologies (archiving, application retirement, data governance). "
                 "Ground them ONLY in the evidence given about the prospect's company. Never name any software vendor or competitor product. Never mention pricing. "
                 "No greeting, no sign-off, no flattery, no questions. Maximum 2 sentences, under 45 words. Reply with the two sentences only.")


async def write_opener(contact: dict, company: Optional[dict]) -> dict:
    """A signal-grounded opener. Falls back to the approved deep dive, then to a neutral line."""
    company = company or {}
    research = await _db().radar_research.find_one({"company_id": company.get("id"), "status": "approved"}, {"_id": 0}, sort=[("created_at", -1)]) if company.get("id") else None
    sig = None
    if company.get("id"):
        sigs = await _db().company_signals.find({"company_id": company["id"]}, {"_id": 0}).to_list(200)
        sigs.sort(key=lambda s: (rm.STRENGTH_RANK.get(s.get("role_strength") or "Unrated", 3), str(s.get("posted_at") or "")), reverse=False)
        sig = sigs[0] if sigs else None
    evidence = []
    if research:
        evidence.append(f"Research headline: {research.get('headline')}\nTrigger: {research.get('trigger')}")
    if sig:
        evidence.append(f"Signal ({sig['type'].replace('_', ' ')}): {sig.get('title') or ''} - {sig.get('evidence_text') or ''}")
    opener, provider = None, "rules"
    if evidence:
        import llm
        if llm.configured_providers():
            text = []
            try:
                async for ev in llm.stream_completion([{"role": "system", "content": OPENER_SYSTEM},
                                                       {"role": "user", "content": f"Prospect: {contact.get('title') or ''} at {company.get('name') or contact.get('company')}\n\n" + "\n\n".join(evidence)}],
                                                      max_tokens=160, temperature=0.4):
                    if ev["type"] == "text":
                        text.append(ev["text"])
                    elif ev["type"] == "meta":
                        provider = ev.get("provider")
                opener = re.sub(r"\s+", " ", "".join(text)).strip().strip('"')
            except llm.ProviderError:
                opener = None
    if opener and (rm.find_competitors(opener) or len(opener.split()) > 70 or re.search(r"\[\d+\]", opener)):
        opener, provider = None, "rules"
    if not opener and research and (research.get("first_touch") or {}).get("body"):
        body = research["first_touch"]["body"]
        paras = [p.strip() for p in body.split("\n\n") if p.strip() and not p.strip().lower().startswith(("hi ", "best", "regards"))]
        if paras and not rm.find_competitors(paras[0]):
            opener, provider = paras[0], "deep_dive"
    if not opener:
        what = "archiving, records retention and retiring legacy applications" if sig else "keeping records from older systems"
        opener, provider = f"I noticed {company.get('name') or 'your team'} is investing in {what}.", "rules"
    return {"opener": opener, "provider": provider, "signal_id": (sig or {}).get("id"), "research_id": (research or {}).get("id")}


def render_steps(sequence: dict, variant: str, ctx: dict) -> List[dict]:
    v = variant.lower()
    return [{"day": int(s.get("day") or 0), "subject": _fill(s.get(f"subject_{v}") or s.get("subject_a") or "", ctx), "body": _fill(s.get(f"body_{v}") or s.get("body_a") or "", ctx)}
            for s in sequence.get("steps") or []]


def footer_html(email: str, settings: dict) -> str:
    unsub = compliance.unsubscribe_url(email)
    parts = []
    if settings.get("ai_disclosure"):
        parts.append(escape(settings["ai_disclosure"]))
    parts.append(f"{escape(settings.get('sender_name') or 'Solix Technologies')}, {escape(settings.get('postal_address') or '')}")
    parts.append(f'Not relevant? <a href="{escape(unsub)}">Unsubscribe</a> and we won\'t email you again.')
    return "<p style='color:#778;font-size:12px;margin-top:24px'>" + "<br>".join(parts) + "</p>"


def body_html(text: str) -> str:
    return "".join(f"<p>{escape(p).replace(chr(10), '<br>')}</p>" for p in (text or "").split("\n\n"))


# --- Enrollment ----------------------------------------------------------------------------------

async def _variant(sequence: dict) -> str:
    if sequence.get("ab_winner") in ("A", "B"):
        return sequence["ab_winner"]
    a = await _db().enrollments.count_documents({"sequence_id": sequence["id"], "variant": "A"})
    b = await _db().enrollments.count_documents({"sequence_id": sequence["id"], "variant": "B"})
    return "A" if a <= b else "B"


async def enroll(contact: dict, sequence: dict, by: str, *, force_variant: Optional[str] = None) -> dict:
    company = await _db().companies.find_one({"id": contact.get("company_id")}, {"_id": 0}) if contact.get("company_id") else None
    settings = await compliance.outbound_settings()
    g = await compliance.gate(contact, company, settings)
    if not g["ok"]:
        raise HTTPException(status_code=422, detail={"message": "This contact can't be enrolled yet.", "reasons": g["reasons"], "warnings": g["warnings"]})
    seq_region = sequence.get("region") or "global"
    if seq_region not in ("global", g["region"]):
        # Copy, opt-out wording and the LIA it relies on are per region.
        raise HTTPException(status_code=422, detail={"message": "Wrong sequence for this region.", "warnings": g["warnings"],
                                                     "reasons": [f"'{sequence['name']}' is written for {seq_region.upper()} recipients; this contact is in {g['region'].upper()}"]})
    if len(sequence.get("steps") or []) > int(settings.get("max_touches") or 4):
        raise HTTPException(status_code=422, detail=f"Sequences may have at most {settings.get('max_touches')} touches.")
    op = await write_opener(contact, company)
    variant = force_variant or await _variant(sequence)
    rep_name = (company or {}).get("owner_name") or settings.get("sender_name") or "Solix"
    ctx = {"first_name": contact.get("first_name") or "there", "last_name": contact.get("last_name") or "", "company": (company or {}).get("name") or contact.get("company") or "your team",
           "title": contact.get("title") or "", "opener": op["opener"], "rep_name": rep_name}
    steps = render_steps(sequence, variant, ctx)
    now = now_iso()
    doc = {
        "id": str(uuid.uuid4()), "contact_id": contact["id"], "company_id": contact.get("company_id"), "sequence_id": sequence["id"], "sequence_name": sequence["name"],
        "email": contact["email"], "contact_name": contact.get("name"), "company_name": ctx["company"], "variant": variant, "opener": op["opener"], "opener_provider": op["provider"],
        "signal_id": op["signal_id"], "research_id": op["research_id"], "steps": steps, "step_index": 0, "channel": "email",
        "provider": sequence.get("provider") or settings.get("provider"), "status": "pending_approval" if settings.get("approval_required") else "scheduled",
        "next_send_at": now, "gate": g, "created_by": by, "created_at": now, "updated_at": now, "history": [{"at": now, "by": by, "action": "enrolled", "detail": f"Variant {variant}"}],
    }
    await _db().enrollments.insert_one(dict(doc))
    if company:
        await rs.add_history(company["id"], by, "outbound", f"{contact.get('name')} enrolled in {sequence['name']}")
    doc.pop("_id", None)
    return doc


async def _set_status(enr: dict, status: str, by: str, detail: str = "", extra: Optional[dict] = None) -> None:
    await _db().enrollments.update_one({"id": enr["id"]}, {"$set": {"status": status, "updated_at": now_iso(), **(extra or {})},
                                                           "$push": {"history": {"$each": [{"at": now_iso(), "by": by, "action": status, "detail": detail}], "$slice": -50}}})


async def stop_contact(contact_id: str, status: str, reason: str, by: str = "system", keep: Optional[str] = None) -> int:
    """Cross-channel stop: a reply on any channel ends every other touch for this person."""
    n = 0
    async for e in _db().enrollments.find({"contact_id": contact_id, "status": {"$in": ACTIVE}, **({"id": {"$ne": keep}} if keep else {})}, {"_id": 0, "id": 1}):
        await _set_status(e, status, by, reason)
        n += 1
    return n


# --- Sending -------------------------------------------------------------------------------------

async def instantly_request(method: str, path: str, **kw) -> dict:
    key = os.environ.get("INSTANTLY_API_KEY")
    if not key:
        raise RuntimeError("INSTANTLY_API_KEY is not set")
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.request(method, f"{INSTANTLY}{path}", headers={"Authorization": f"Bearer {key}"}, **kw)
    if r.status_code >= 400:
        raise RuntimeError(f"Instantly {r.status_code}: {r.text[:200]}")
    return r.json() if r.content else {}


async def instantly_block(email: str) -> None:
    await instantly_request("POST", "/block-lists-entries", json={"bl_value": email})


async def instantly_delete_lead(email: str) -> None:
    found = await instantly_request("POST", "/leads/list", json={"search": email, "limit": 10})
    for lead in found.get("items") or []:
        if (lead.get("email") or "").lower() == email.lower():
            await instantly_request("DELETE", f"/leads/{lead['id']}")


async def _send_instantly(enr: dict, contact: dict, sequence: dict, settings: dict) -> str:
    campaign = sequence.get("instantly_campaign_id") or settings.get("instantly_campaign_id")
    if not campaign:
        raise RuntimeError("No Instantly campaign is set for this sequence")
    custom = {"opener": enr["opener"], "rep_name": (enr["steps"][0]["body"].rsplit("\n", 1)[-1] if enr["steps"] else ""), "solix_enrollment": enr["id"],
              "unsubscribe_url": compliance.unsubscribe_url(contact["email"])}
    for i, s in enumerate(enr["steps"], 1):
        custom[f"subject_{i}"], custom[f"body_{i}"] = s["subject"], s["body"]
    res = await instantly_request("POST", "/leads", json={"campaign": campaign, "email": contact["email"], "first_name": contact.get("first_name"), "last_name": contact.get("last_name"),
                                                          "company_name": enr.get("company_name"), "job_title": contact.get("title"), "personalization": enr["opener"],
                                                          "custom_variables": custom, "skip_if_in_workspace": True})
    return res.get("id") or "instantly"


async def send_step(enr: dict, settings: dict) -> str:
    contact = await _db().contacts.find_one({"id": enr["contact_id"]}, {"_id": 0})
    company = await _db().companies.find_one({"id": enr.get("company_id")}, {"_id": 0}) if enr.get("company_id") else None
    if not contact:
        await _set_status(enr, "stopped", "system", "Contact deleted")
        return "stopped"
    # Re-check suppression and exclusions at send time: they can change after enrollment.
    sup = await compliance.is_suppressed(contact.get("email"))
    blocked = sup or (company or {}).get("excluded_reason") or ((company or {}).get("status") in ("not_a_fit", "meeting_booked", "opportunity", "won", "lost"))
    if blocked:
        await _set_status(enr, "stopped", "system", f"Blocked at send time: {sup or (company or {}).get('excluded_reason') or (company or {}).get('status')}")
        return "stopped"
    step = enr["steps"][enr["step_index"]]
    provider = enr.get("provider") or settings.get("provider") or "dry_run"
    sequence = await _db().sequences.find_one({"id": enr["sequence_id"]}, {"_id": 0}) or {}
    send = {"id": str(uuid.uuid4()), "enrollment_id": enr["id"], "contact_id": contact["id"], "company_id": enr.get("company_id"), "sequence_id": enr["sequence_id"],
            "variant": enr["variant"], "step": enr["step_index"] + 1, "email": contact["email"], "subject": step["subject"], "provider": provider, "sent_at": now_iso()}
    if provider == "instantly":
        send["provider_ref"] = await _send_instantly(enr, contact, sequence, settings)
        send["status"] = "handed_off"
        await _db().sends.insert_one(dict(send))
        await _set_status(enr, "active", "system", "Handed to Instantly (it sends every step)", {"step_index": len(enr["steps"]), "handed_off_at": now_iso()})
    else:
        if provider == "smtp":
            from emailer import send_email
            unsub = compliance.unsubscribe_url(contact["email"])
            html = body_html(step["body"]) + footer_html(contact["email"], settings)
            send["provider_ref"] = await send_email(to=contact["email"], subject=step["subject"], html=html,
                                                    headers={"List-Unsubscribe": f"<{unsub}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"})
            send["status"] = "sent"
        else:
            send["status"] = "simulated"
        await _db().sends.insert_one(dict(send))
        nxt = enr["step_index"] + 1
        if nxt >= len(enr["steps"]):
            await _set_status(enr, "completed", "system", f"Step {nxt} sent; sequence finished", {"step_index": nxt})
        else:
            started = datetime.fromisoformat(enr.get("first_sent_at") or now_iso())
            due = (started + timedelta(days=enr["steps"][nxt]["day"])).isoformat()
            await _set_status(enr, "active", "system", f"Step {nxt} sent", {"step_index": nxt, "next_send_at": due, "first_sent_at": enr.get("first_sent_at") or send["sent_at"]})
    if company and company.get("status") in rm.SYSTEM_STATUSES:
        await _db().companies.update_one({"id": company["id"]}, {"$set": {"status": "contacted"}})
        await rs.add_history(company["id"], "system", "outbound", f"First email {'handed to Instantly' if provider == 'instantly' else provider} for {contact.get('name')}")
    return send["status"]


async def bounce_check(settings: dict) -> Optional[float]:
    since = (_now() - timedelta(days=7)).isoformat()
    sent = await _db().sends.count_documents({"sent_at": {"$gte": since}, "status": {"$in": ["sent", "handed_off", "delivered"]}})
    if sent < 20:
        return None
    bounced = await _db().sends.count_documents({"sent_at": {"$gte": since}, "bounced": True})
    pct = round(100 * bounced / sent, 2)
    if pct > float(settings.get("bounce_pause_pct") or 3) and not settings.get("paused"):
        await compliance.save_outbound_settings({"paused": True, "pause_reason": f"Bounce rate {pct}% over the last 7 days is above {settings.get('bounce_pause_pct')}%"})
        await ops.alert("bounce_pause", f"Outbound paused automatically: bounce rate {pct}% (7 days, {sent} sends). Check list quality and domain health before resuming.", severity="fail", dedupe_hours=1)
    return pct


def _in_window(settings: dict, now: datetime) -> bool:
    days = settings.get("send_days") or [0, 1, 2, 3, 4]
    start, end = (settings.get("send_hours_utc") or [8, 17])[:2]
    return now.weekday() in days and start <= now.hour < end


async def tick() -> dict:
    """The sender: resumes paused enrollments that are due, then sends what's due within the cap."""
    settings = await compliance.outbound_settings()
    counts = {"sent": 0, "stopped": 0, "errors": 0, "resumed": 0}
    now = _now()
    async for e in _db().enrollments.find({"status": "paused", "resume_at": {"$lte": now.isoformat()}}, {"_id": 0, "id": 1}):
        await _set_status(e, "active", "system", "Resumed after pause", {"next_send_at": now.isoformat(), "resume_at": None})
        counts["resumed"] += 1
    if now.weekday() == 0 and now.hour >= 6:
        state = await _db().settings.find_one({"key": "learning"}, {"_id": 0}) or {}
        if (state.get("last_run") or "")[:10] != now.date().isoformat():
            try:
                await learn()
            except Exception:  # noqa: BLE001
                logger.exception("weekly learning failed")
    await _db().ops_runs.delete_many({"job": "outbound_tick", "started_at": {"$lt": (now - timedelta(days=3)).isoformat()}})
    async with ops.run("outbound_tick") as run:
        await bounce_check(settings)
        settings = await compliance.outbound_settings()
        if not settings.get("sending_enabled") or settings.get("paused") or not _in_window(settings, now):
            run["counts"] = {**counts, "skipped": "off" if not settings.get("sending_enabled") else "paused" if settings.get("paused") else "outside window"}
            return run["counts"]
        today = now.date().isoformat()
        sent_today = await _db().sends.count_documents({"sent_at": {"$gte": today}})
        room = max(0, int(settings.get("daily_cap") or 0) - sent_today)
        if room == 0:
            run["counts"] = {**counts, "skipped": "daily cap reached"}
            return run["counts"]
        due = await _db().enrollments.find({"status": {"$in": ["scheduled", "active"]}, "next_send_at": {"$lte": now.isoformat()}, "provider": {"$ne": "instantly"}}, {"_id": 0}).sort("next_send_at", 1).to_list(room)
        due += await _db().enrollments.find({"status": "scheduled", "provider": "instantly"}, {"_id": 0}).to_list(max(0, room - len(due)))
        for e in due[:room]:
            if e["step_index"] >= len(e["steps"]):
                continue
            try:
                result = await send_step(e, settings)
                counts["stopped" if result == "stopped" else "sent"] += 1
            except Exception as exc:  # noqa: BLE001 - one bad address must not stop the batch
                counts["errors"] += 1
                await _set_status(e, e["status"], "system", f"Send failed: {str(exc)[:200]}", {"next_send_at": (now + timedelta(hours=2)).isoformat(), "last_error": str(exc)[:300]})
        run["counts"] = counts
    return counts


# --- Replies ---------------------------------------------------------------------------------------

_KW = [
    ("unsubscribe", re.compile(r"\b(unsubscribe|remove me|take me off|stop (emailing|contacting)|opt[ -]?out|do not (email|contact))\b", re.I)),
    ("ooo", re.compile(r"\b(out of (the )?office|on (annual )?leave|on vacation|limited access to email|away until|return(ing)? on)\b", re.I)),
    # Without a model these rules are the whole classifier, so a clear "no" or "later" is checked before
    # anything that proposes time, and a proposed time beats a general show of interest.
    ("not_interested", re.compile(r"\b(not interested|no thanks|no thank you|not a priority|we('re| are) (all )?set|already (have|use) a solution|"
                                  r"please don'?t (follow up|email|contact|reach out))\b", re.I)),
    ("objection", re.compile(r"\b(not (right )?now|maybe later|circle back|(next|early|later this) (year|quarter)|after (q[1-4]|the new year))\b", re.I)),
    ("meeting", re.compile(r"\b(booked|calendar invite|send (me |over )?(an? )?(invite|calendar)|let'?s meet|(schedule|set up|book|arrange|grab) (a |the |some )?(call|meeting|time|chat)|"
                           r"find (a |some )?(time|\d+ ?min(ute)?s?)|can we (talk|chat|meet|speak|connect)|how about (mon|tue|wed|thu|fri)\w*|works for me|"
                           r"(free|available) (on|next|this|tomorrow))\b", re.I)),
    ("forward", re.compile(r"\b(forward(ed|ing)? (this|your (email|note)) to|cc'?(ing|ed) (my colleague|\w+)|looping in)\b", re.I)),
    ("referral", re.compile(r"\b(the right person (is|would be)|you should (speak|talk) (to|with)|reach out to|contact (my colleague|\w+ \w+) (at|who))\b", re.I)),
    ("interested", re.compile(r"\b(interested|tell me more|send (me )?(more|details|info)|sounds (good|interesting)|happy to (chat|talk)|let'?s (talk|chat)|open to|timely|"
                              r"keen to|would (like|love) to (learn|know|hear|see)|worth a (chat|conversation|call|look)|curious)\b", re.I)),
    ("objection", re.compile(r"\b(budget|too busy|timing)\b", re.I)),
]
CLASSIFY_SYSTEM = ("Classify this reply to a B2B sales email. Labels: interested (wants to learn more), meeting (agrees a time or books), objection (engaged but not now / concern), "
                   "referral (points to another person), forward (has forwarded or cc'd a colleague), ooo (auto-reply / away), not_interested (clear no), unsubscribe (asks to stop), neutral. "
                   'Reply with JSON only: {"label": "...", "reason": "one short sentence", "resume_on": "YYYY-MM-DD or null"}')


async def classify(text: str) -> dict:
    t = text or ""
    for label, rx in _KW[:2]:
        if rx.search(t):
            return {"label": label, "reason": "Matched a clear phrase", "by": "rules"}
    import llm
    if llm.configured_providers():
        out = []
        try:
            async for ev in llm.stream_completion([{"role": "system", "content": CLASSIFY_SYSTEM}, {"role": "user", "content": t[:4000]}], max_tokens=150, temperature=0):
                if ev["type"] == "text":
                    out.append(ev["text"])
            raw = "".join(out)
            data = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
            if data.get("label") in REPLY_LABELS:
                return {"label": data["label"], "reason": data.get("reason") or "", "resume_on": data.get("resume_on"), "by": "model"}
        except Exception:  # noqa: BLE001
            pass
    for label, rx in _KW[2:]:
        if rx.search(t):
            return {"label": label, "reason": "Matched a phrase", "by": "rules"}
    return {"label": "neutral", "reason": "No clear intent", "by": "rules"}


async def promote_to_lead(company: Optional[dict], person: dict, *, reason: str, by: str, stage: str = "mql", source: str = "radar") -> dict:
    """Create or update the lead in the existing funnel and move it to `stage` (forward only)."""
    import intent
    email = person["email"].strip().lower()
    now = now_iso()
    lead = await _db().leads.find_one({"email": email}, {"_id": 0})
    if not lead:
        lead = {"id": str(uuid.uuid4()), "email": email, "created_at": now, "stage": "lead", "visitor_ids": [], "direct_scores": {}, "direct_scored_at": now,
                "submission_types": [], "submissions_count": 0, "owner": (company or {}).get("owner"), "notes": "", "tags": [source],
                "stage_history": [{"stage": "lead", "at": now, "by": by, "reason": reason}], "first_touch": {"outbound": True, "source": source, "at": now}, "source": source}
        await _db().leads.insert_one(dict(lead))
    sets = {k: v for k, v in {"name": person.get("name"), "job_title": person.get("job_title") or person.get("title"), "company": (company or {}).get("name"),
                              "country": ((company or {}).get("hq") or {}).get("country")}.items() if v and not lead.get(k)}
    if company:
        sets["company_id"] = company["id"]
        slugs = [s for k in (company.get("competitor_keys") or [])[:2] for s in (rm.COMPETITOR_BY_KEY.get(k) or {}).get("slugs", [])][:3]
        direct = dict(lead.get("direct_scores") or {})
        scoring.add_points(direct, slugs, 20)
        sets.update({"direct_scores": direct, "direct_scored_at": now})
    if not lead.get("owner") and (company or {}).get("owner"):
        sets["owner"] = company["owner"]
    sets["tags"] = sorted(set(lead.get("tags") or []) | {source})
    sets["last_activity_at"] = now
    await _db().leads.update_one({"id": lead["id"]}, {"$set": sets})
    lead = await intent.rollup_lead(lead["id"]) or {**lead, **sets}
    order = scoring.STAGES
    if stage in order and order.index(stage) > order.index(lead.get("stage") or "lead") and lead.get("stage") not in ("lost", "disqualified"):
        upd = {"stage": stage, "stage_reason": reason, "stage_changed_at": now,
               "stage_history": ((lead.get("stage_history") or []) + [{"stage": stage, "at": now, "by": by, "reason": reason}])[-50:]}
        if not lead.get("mql_at"):
            upd.update({"mql_at": now, "mql_line": lead.get("primary_line"), "mql_product": lead.get("primary_product")})
        if stage in ("sql", "opportunity", "won") and not lead.get("sql_at"):
            upd["sql_at"] = now
        await _db().leads.update_one({"id": lead["id"]}, {"$set": upd})
        lead.update(upd)
    await _db().contacts.update_many({"email": email}, {"$set": {"lead_id": lead["id"]}})
    if company:
        await _db().companies.update_one({"id": company["id"]}, {"$addToSet": {"lead_ids": lead["id"]}})
        await rs.add_history(company["id"], by, "lead", f"{email} → {stage.upper()} ({reason})")
        await rs.recompute(company["id"])
    await ops.digiprise("lead.upserted", {"lead_id": lead["id"], "email": email, "name": lead.get("name"), "company": lead.get("company"), "stage": lead.get("stage"),
                                          "owner": lead.get("owner"), "reason": reason, "account_id": (company or {}).get("id"), "account_score": (company or {}).get("score")},
                        key=f"dp-lead:{lead['id']}:{lead.get('stage')}")
    return lead


INBOUND_ACTIONS = {"demo": "requested a demo", "trial": "started a trial", "contact": "contacted sales", "event": "registered for an event",
                   "download": "downloaded gated content", "chat": "talked to Sol", "partner": "applied to partner"}


async def attach_lead(contact: dict) -> dict:
    """Point a contact at the website lead with the same email, so both pages show one person."""
    lead = await _db().leads.find_one({"email": contact.get("email")}, {"_id": 0, "id": 1}) if contact.get("email") else None
    if (lead or {}).get("id") != contact.get("lead_id"):
        await _db().contacts.update_one({"id": contact["id"]}, {"$set": {"lead_id": (lead or {}).get("id")}})
        contact = {**contact, "lead_id": (lead or {}).get("id")}
    return contact


async def on_inbound(lead: dict, stype: str) -> int:
    """Someone in outbound engaged on the website: stop emailing them and hand them to the owner."""
    email = (lead.get("email") or "").lower()
    if not email:
        return 0
    await _db().contacts.update_many({"email": email}, {"$set": {"lead_id": lead["id"]}})
    action = INBOUND_ACTIONS.get(stype)
    if not action:  # a newsletter sign-up isn't a reason to stop
        return 0
    stopped = 0
    async for c in _db().contacts.find({"email": email}, {"_id": 0, "id": 1, "name": 1, "company_id": 1}):
        n = await stop_contact(c["id"], "stopped", f"Engaged inbound: {action}")
        if n and c.get("company_id"):
            await rs.add_history(c["company_id"], "system", "inbound", f"{c.get('name') or email} {action}; outbound stopped")
        stopped += n
    if stopped:
        await ops.notify("pipeline", f"{lead.get('name') or email} {action} while in an outbound sequence, so the sequence stopped. Owner: {lead.get('owner') or 'unassigned'}.",
                         event="inbound_handoff", key=f"handoff:{lead['id']}:{stype}:{now_iso()[:10]}")
    return stopped


async def handle_reply(contact: dict, text: str, *, source: str, label: Optional[str] = None, by: str = "system", meta: Optional[dict] = None) -> dict:
    cls = {"label": label, "reason": "Set by a person", "by": by} if label else await classify(text)
    label = cls["label"]
    company = await _db().companies.find_one({"id": contact.get("company_id")}, {"_id": 0}) if contact.get("company_id") else None
    enr = await _db().enrollments.find_one({"contact_id": contact["id"]}, {"_id": 0, "steps": 0}, sort=[("created_at", -1)])
    rec = {"id": str(uuid.uuid4()), "contact_id": contact["id"], "company_id": contact.get("company_id"), "email": contact.get("email"), "enrollment_id": (enr or {}).get("id"),
           "sequence_id": (enr or {}).get("sequence_id"), "variant": (enr or {}).get("variant"), "step": (enr or {}).get("step_index"), "label": label, "reason": cls.get("reason"),
           "classified_by": cls.get("by"), "text": (text or "")[:5000], "source": source, "meta": meta or {}, "received_at": now_iso()}
    await _db().reply_log.insert_one(dict(rec))
    owner = (company or {}).get("owner")
    who = f"{contact.get('name') or contact.get('email')} ({contact.get('title') or 'unknown title'}) at {(company or {}).get('name') or contact.get('company') or 'unknown company'}"
    if label in POSITIVE:
        await stop_contact(contact["id"], "replied", f"Replied: {label}")
        lead = await promote_to_lead(company, {"email": contact["email"], "name": contact.get("name"), "title": contact.get("title")}, reason=f"Positive reply ({label}) to outbound",
                                     by="echolocate", stage="sql" if label == "meeting" else "mql", source="outbound")
        if company:
            target = "meeting_booked" if label == "meeting" else "replied"
            if rs.STATUS_ORDER.index(target) > (rs.STATUS_ORDER.index(company["status"]) if company.get("status") in rs.STATUS_ORDER else -1):
                await _db().companies.update_one({"id": company["id"]}, {"$set": {"status": target}})
        await ops.notify("replies", f":large_green_circle: {label.title()} reply from {who}. Lead {lead.get('stage', '').upper()}. Owner: {owner or 'unassigned'}.", event="reply_positive")
        if owner:
            html = (f"<p><b>{escape(who)}</b> replied ({escape(label)}).</p><blockquote>{escape((text or '')[:1500])}</blockquote>"
                    f"<p>Account score {round((company or {}).get('score') or 0)}; signal: {escape(', '.join((company or {}).get('competitors') or []) or 'see Radar')}. "
                    f"Sequence: {escape((enr or {}).get('sequence_name') or '-')} step {(enr or {}).get('step_index') or '-'}.</p><p>Suggested next step: reply within 2 hours and offer two times.</p>")
            await ops.enqueue("email", "reply_alert", {"to": owner, "subject": f"[Solix] {label.title()} reply: {who}"[:150], "html": html, "reply_to": contact.get("email")})
    elif label in ("not_interested", "unsubscribe"):
        await compliance.suppress(contact["email"], "unsubscribed" if label == "unsubscribe" else "not_interested", source=f"reply:{source}", by="echolocate")
        await ops.notify("replies", f"{label.replace('_', ' ').title()} reply from {who}; suppressed.", event="reply_negative")
    elif label == "objection":
        resume = (_now() + timedelta(days=90)).isoformat()
        async for e in _db().enrollments.find({"contact_id": contact["id"], "status": {"$in": ACTIVE}}, {"_id": 0, "id": 1}):
            await _set_status(e, "paused", "echolocate", "Objection: re-engage in 90 days", {"resume_at": resume})
        await ops.notify("replies", f"Objection from {who}; paused for 90 days.", event="reply_objection")
    elif label == "ooo":
        resume = cls.get("resume_on") or (_now() + timedelta(days=7)).date().isoformat()
        async for e in _db().enrollments.find({"contact_id": contact["id"], "status": {"$in": ["scheduled", "active"]}}, {"_id": 0, "id": 1}):
            await _set_status(e, "paused", "echolocate", f"Out of office until {resume}", {"resume_at": f"{resume}T08:00:00+00:00" if len(resume) == 10 else resume})
    elif label in ("referral", "forward"):
        new = 0
        for em in set(re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", text or "")) - {contact.get("email")}:
            if not await _db().contacts.find_one({"email": em.lower()}, {"_id": 1}):
                ref = new_contact_doc({"email": em, "name": em.split("@")[0].replace(".", " ").title(), "source": "referral",
                                       "source_url": None, "notes": f"Referred by {contact.get('email')}"}, company)
                await _db().contacts.insert_one(dict(ref))
                await attach_lead(ref)
                new += 1
        await stop_contact(contact["id"], "replied", f"Replied: {label}")
        await ops.notify("replies", f"{label.title()} from {who}; {new} new contact(s) added.", event="reply_referral")
    if company:
        await rs.add_history(company["id"], "echolocate", "reply", f"{contact.get('name') or contact.get('email')}: {label}")
    await ops.digiprise("reply.received", {"email": contact.get("email"), "label": label, "excerpt": (text or "")[:300], "account_id": (company or {}).get("id")}, key=f"dp-reply:{rec['id']}")
    return rec


async def on_account_status(company: dict, user: dict) -> None:
    if company.get("status") in ("not_a_fit", "meeting_booked", "won"):
        async for c in _db().contacts.find({"company_id": company["id"]}, {"_id": 0, "id": 1}):
            await stop_contact(c["id"], "stopped", f"Account marked {rm.STATUS_LABELS.get(company['status'])} by {user.get('email')}")


# --- Learning --------------------------------------------------------------------------------------

async def learn() -> dict:
    """Weekly re-weight of signal types from real outcomes. Needs 20+ closed outcomes, else keeps the defaults."""
    async with ops.run("learn") as run:
        positive = {"replied", "meeting_booked", "opportunity", "won"}
        closed = await _db().companies.find({"status": {"$in": list(positive | {"contacted", "not_a_fit", "lost"})}}, {"_id": 0, "status": 1, "signal_types": 1}).to_list(20000)
        outcomes = closed  # contacted-without-reply counts as a negative outcome
        n = len(outcomes)
        run["counts"]["outcomes"] = n
        if n < 20:
            run["counts"]["changed"] = 0
            run["notes"].append("fewer than 20 outcomes; weights unchanged")
            await _db().settings.update_one({"key": "learning"}, {"$set": {"last_run": now_iso(), "outcomes": n}}, upsert=True)
            return run["counts"]
        overall = sum(1 for c in outcomes if c["status"] in positive) / n or 0.01
        weights = {}
        for t in rm.SIGNAL_TYPES:
            with_t = [c for c in outcomes if t in (c.get("signal_types") or [])]
            if len(with_t) < 5:
                continue
            rate = sum(1 for c in with_t if c["status"] in positive) / len(with_t)
            weights[t] = round(max(0.5, min(2.0, rate / overall)) * rm.TYPE_WEIGHTS.get(t, 0.5), 3)
        await rs.save_radar_settings({"weights": weights})
        await _db().settings.update_one({"key": "learning"}, {"$set": {"last_run": now_iso(), "outcomes": n, "weights": weights}}, upsert=True)
        run["counts"]["changed"] = len(weights)
        await rs.recompute_all()
    return run["counts"]


async def ab_update() -> None:
    settings = await compliance.outbound_settings()
    min_sends = int(settings.get("ab_min_sends") or 50)
    async for seq in _db().sequences.find({"ab_winner": None}, {"_id": 0, "id": 1}):
        stats = {}
        for v in ("A", "B"):
            sends = await _db().sends.count_documents({"sequence_id": seq["id"], "variant": v, "step": 1})
            pos = await _db().reply_log.count_documents({"sequence_id": seq["id"], "variant": v, "label": {"$in": list(POSITIVE)}})
            stats[v] = (sends, pos / sends if sends else 0)
        if stats["A"][0] >= min_sends and stats["B"][0] >= min_sends and stats["A"][1] != stats["B"][1]:
            winner = "A" if stats["A"][1] > stats["B"][1] else "B"
            await _db().sequences.update_one({"id": seq["id"]}, {"$set": {"ab_winner": winner, "ab_decided_at": now_iso(), "ab_stats": {k: {"sends": s, "positive_rate": round(r * 100, 2)} for k, (s, r) in stats.items()}}})
            await ops.notify("pipeline", f"A/B test decided: variant {winner} wins on positive reply rate.", event="ab_winner")


# --- KPIs ---------------------------------------------------------------------------------------------

async def kpis(days: int = 30) -> dict:
    db = _db()
    since = (_now() - timedelta(days=days)).isoformat()
    week = (_now() - timedelta(days=7)).isoformat()
    sends = await db.sends.find({"sent_at": {"$gte": since}}, {"_id": 0, "sequence_id": 1, "variant": 1, "bounced": 1, "status": 1, "step": 1}).to_list(100000)
    replies = await db.reply_log.find({"received_at": {"$gte": since}}, {"_id": 0, "label": 1, "sequence_id": 1, "variant": 1, "received_at": 1}).to_list(100000)
    delivered = [s for s in sends if s.get("status") in ("sent", "handed_off", "delivered", "simulated")]
    bounced = sum(1 for s in sends if s.get("bounced"))
    positive = [r for r in replies if r["label"] in POSITIVE]
    by_seq: Dict[str, dict] = {}
    names = {s["id"]: s["name"] async for s in db.sequences.find({}, {"_id": 0, "id": 1, "name": 1})}
    for s in delivered:
        row = by_seq.setdefault(f"{s['sequence_id']}|{s.get('variant')}", {"sequence": names.get(s["sequence_id"], "?"), "variant": s.get("variant"), "sends": 0, "replies": 0, "positive": 0})
        row["sends"] += 1
    for r in replies:
        row = by_seq.setdefault(f"{r.get('sequence_id')}|{r.get('variant')}", {"sequence": names.get(r.get("sequence_id"), "Logged by hand"), "variant": r.get("variant"), "sends": 0, "replies": 0, "positive": 0})
        row["replies"] += 1
        row["positive"] += r["label"] in POSITIVE
    runs = await db.ops_runs.find({"job": "radar_daily", "status": "ok"}, {"_id": 0, "started_at": 1}).sort("started_at", -1).to_list(60)
    days_ok = sorted({r["started_at"][:10] for r in runs}, reverse=True)
    streak, d = 0, _now().date()
    for i in range(0, 60):
        day = (d - timedelta(days=i)).isoformat()
        if day in days_ok:
            streak += 1
        elif i > 0:
            break
    status = await db.enrollments.aggregate([{"$group": {"_id": "$status", "n": {"$sum": 1}}}]).to_list(None)
    return {
        "window_days": days, "sends": len(delivered), "bounced": bounced, "bounce_rate": round(100 * bounced / len(delivered), 2) if delivered else None,
        "replies": len(replies), "reply_rate": round(100 * len(replies) / len(delivered), 2) if delivered else None, "positive": len(positive),
        "positive_rate": round(100 * len(positive) / len(delivered), 2) if delivered else None, "meetings": sum(1 for r in replies if r["label"] == "meeting"),
        "unsubscribes": sum(1 for r in replies if r["label"] == "unsubscribe"), "qualified_replies_7d": sum(1 for r in positive if r["received_at"] >= week),
        "by_sequence": sorted(by_seq.values(), key=lambda r: -r["sends"]), "enrollments": {s["_id"]: s["n"] for s in status},
        "dod": {"autonomous_streak_days": streak, "target_streak": 7, "bounce_ok": (bounced / len(delivered) < 0.03) if delivered else None,
                "qualified_reply_this_week": any(r["received_at"] >= week for r in positive)},
        "contacts": await db.contacts.count_documents({}), "suppressed": await db.suppression.count_documents({}),
        "pending_approval": await db.enrollments.count_documents({"status": "pending_approval"}),
    }


# --- API ---------------------------------------------------------------------------------------------

class ContactIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: Optional[str] = Field(default=None, max_length=200)
    title: Optional[str] = Field(default=None, max_length=200)
    company_id: Optional[str] = None
    company: Optional[str] = Field(default=None, max_length=200)
    domain: Optional[str] = Field(default=None, max_length=200)
    country: Optional[str] = Field(default=None, max_length=80)
    linkedin_url: Optional[str] = Field(default=None, max_length=300)
    phone: Optional[str] = Field(default=None, max_length=40)
    source: Optional[str] = Field(default="manual", max_length=40)
    source_url: Optional[str] = Field(default=None, max_length=1000)
    lawful_basis: Optional[str] = Field(default="legitimate_interest", max_length=40)
    notes: Optional[str] = Field(default=None, max_length=2000)


async def _company_for(body: dict) -> Optional[dict]:
    if body.get("company_id"):
        return await _db().companies.find_one({"id": body["company_id"]}, {"_id": 0})
    if body.get("company") or body.get("domain") or body.get("email"):
        dom = body.get("domain") or (rm.domain_of(body.get("email")) if body.get("email") and rm.domain_of(body["email"]) not in scoring.FREE_EMAIL_DOMAINS else None)
        if body.get("company"):
            return await rs.get_or_create_company(body["company"], domain=dom, source="contact")
        if dom:
            return await _db().companies.find_one({"domains": dom}, {"_id": 0})
    return None


@router.get("/contacts")
async def list_contacts(q: Optional[str] = None, company_id: Optional[str] = None, status: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(50, le=200)):
    query: dict = {}
    if q:
        rx = {"$regex": re.escape(q.strip()), "$options": "i"}
        query["$or"] = [{"name": rx}, {"email": rx}, {"company": rx}, {"title": rx}]
    if company_id:
        query["company_id"] = company_id
    if status:
        query["email_status"] = status
    total = await _db().contacts.count_documents(query)
    items = await _db().contacts.find(query, {"_id": 0}).sort("created_at", -1).skip((page - 1) * page_size).limit(page_size).to_list(page_size)
    return {"items": items, "total": total}


@router.post("/contacts", status_code=201, dependencies=[can_work])
async def create_contact(body: ContactIn, user: dict = Depends(get_current_admin)):
    data = body.model_dump()
    if data.get("email") and await _db().contacts.find_one({"email": data["email"].strip().lower()}, {"_id": 1}):
        raise HTTPException(status_code=409, detail="A contact with this email already exists")
    company = await _company_for(data)
    doc = new_contact_doc(data, company)
    await _db().contacts.insert_one(dict(doc))
    doc = await attach_lead(doc)
    if doc["email"]:
        doc = await set_verification(doc)
    if company:
        await rs.add_history(company["id"], user["email"], "contact", f"Contact added: {doc['name']}")
    doc.pop("_id", None)
    return doc


class ContactPatch(BaseModel):
    name: Optional[str] = Field(default=None, max_length=120)
    email: Optional[str] = Field(default=None, max_length=200)
    title: Optional[str] = Field(default=None, max_length=200)
    country: Optional[str] = Field(default=None, max_length=80)
    linkedin_url: Optional[str] = Field(default=None, max_length=300)
    lawful_basis: Optional[str] = Field(default=None, max_length=40)
    notes: Optional[str] = Field(default=None, max_length=2000)
    company_id: Optional[str] = None
    mark_verified: Optional[bool] = None


@router.patch("/contacts/{contact_id}", dependencies=[can_work])
async def patch_contact(contact_id: str, body: ContactPatch, user: dict = Depends(get_current_admin)):
    c = await _db().contacts.find_one({"id": contact_id}, {"_id": 0})
    if not c:
        raise HTTPException(status_code=404, detail="Contact not found")
    sets = body.model_dump(exclude_none=True)
    if sets.pop("mark_verified", None):
        if not c.get("email"):
            raise HTTPException(status_code=422, detail="Add an email address first")
        if c.get("email_status") == "invalid":
            raise HTTPException(status_code=409, detail="This address bounced or failed verification; it can't be marked verified")
        sets.update({"email_status": "valid", "verify_detail": f"Marked verified by {user['email']}", "verified_at": now_iso()})
    if "email" in sets:
        sets["email"] = sets["email"].strip().lower() or None
        sets["email_status"] = "unchecked" if sets["email"] else "missing"
    if "title" in sets:
        sets.update({"persona": persona_of(sets["title"]), "seniority": seniority_of(sets["title"])})
    if "name" in sets:
        parts = sets["name"].split()
        sets.update({"first_name": parts[0] if parts else "", "last_name": " ".join(parts[1:])})
    sets["updated_at"] = now_iso()
    await _db().contacts.update_one({"id": contact_id}, {"$set": sets})
    c = await _db().contacts.find_one({"id": contact_id}, {"_id": 0})
    if "email" in sets:
        c = await attach_lead(c)
        if c.get("email"):
            c = await set_verification(c)
    return c


@router.delete("/contacts/{contact_id}", status_code=204, dependencies=[admin_only])
async def delete_contact(contact_id: str):
    await stop_contact(contact_id, "stopped", "Contact deleted")
    await _db().contacts.delete_one({"id": contact_id})


@router.post("/contacts/{contact_id}/verify", dependencies=[can_work])
async def verify_contact(contact_id: str):
    c = await _db().contacts.find_one({"id": contact_id}, {"_id": 0})
    if not c or not c.get("email"):
        raise HTTPException(status_code=422, detail="This contact has no email address to verify")
    return await set_verification(c)


@router.post("/contacts/{contact_id}/find-email", dependencies=[can_work])
async def find_email(contact_id: str):
    """Try common address patterns on the account's domain; keep the first one the verifier accepts."""
    c = await _db().contacts.find_one({"id": contact_id}, {"_id": 0})
    if not c:
        raise HTTPException(status_code=404, detail="Contact not found")
    company = await _db().companies.find_one({"id": c.get("company_id")}, {"_id": 0}) if c.get("company_id") else None
    domain = ((company or {}).get("domains") or [None])[0]
    if not domain:
        raise HTTPException(status_code=422, detail="Add the account's web domain first (Account → Details)")
    tried = []
    for cand in guess_emails(c.get("first_name"), c.get("last_name"), domain)[:6]:
        v = await verify_email(cand)
        tried.append({"email": cand, **v})
        if v["status"] == "valid":
            await _db().contacts.update_one({"id": contact_id}, {"$set": {"email": cand, "email_status": "valid", "verify_detail": v["detail"], "verified_at": now_iso(), "source_email": "pattern+verify"}})
            return {"found": cand, "tried": tried}
        if v["status"] == "mx_only":
            break  # without a mailbox verifier every pattern looks the same; don't guess
    return {"found": None, "tried": tried}


class ImportIn(BaseModel):
    csv: str = Field(min_length=5, max_length=2_000_000)
    source: str = Field(default="csv_import", max_length=40)


@router.post("/contacts/import", dependencies=[can_work])
async def import_contacts(body: ImportIn, user: dict = Depends(get_current_admin)):
    import csv
    import io
    rows = list(csv.DictReader(io.StringIO(body.csv)))
    counts = {"created": 0, "skipped": 0, "invalid": 0}
    for r in rows[:5000]:
        r = {(k or "").strip().lower().replace(" ", "_"): (v or "").strip() for k, v in r.items()}
        name = r.get("name") or " ".join(filter(None, [r.get("first_name"), r.get("last_name")]))
        email = (r.get("email") or "").lower()
        if not name or (email and await _db().contacts.find_one({"email": email}, {"_id": 1})):
            counts["skipped"] += 1
            continue
        data = {"name": name, "email": email or None, "title": r.get("title") or r.get("job_title"), "company": r.get("company") or r.get("account"), "domain": r.get("domain") or r.get("website"),
                "country": r.get("country"), "linkedin_url": r.get("linkedin_url") or r.get("linkedin"), "source": body.source, "source_url": r.get("source_url"), "first_name": r.get("first_name"), "last_name": r.get("last_name")}
        company = await _company_for(data)
        doc = new_contact_doc(data, company)
        await _db().contacts.insert_one(dict(doc))
        await attach_lead(doc)
        counts["created"] += 1
    return counts


class SequenceIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    region: str = Field(default="global", pattern="^(eu|uk|us|global)$")
    steps: List[Dict[str, object]] = Field(min_length=1, max_length=8)
    active: bool = True
    provider: Optional[str] = Field(default=None, pattern="^(dry_run|smtp|instantly)$")
    instantly_campaign_id: Optional[str] = Field(default=None, max_length=80)


def _clean_steps(steps: list) -> list:
    out = []
    for s in steps:
        step = {"day": int(s.get("day") or 0)}
        for k in ("subject_a", "body_a", "subject_b", "body_b"):
            step[k] = str(s.get(k) or "")[:5000]
        if not step["subject_a"] or not step["body_a"]:
            raise HTTPException(status_code=422, detail="Every step needs a subject and body for variant A")
        if rm.find_competitors(step["subject_a"] + step["body_a"] + step["subject_b"] + step["body_b"]):
            raise HTTPException(status_code=422, detail="Templates must not name a competitor (rep rule).")
        out.append(step)
    return sorted(out, key=lambda s: s["day"])


@router.get("/sequences")
async def list_sequences():
    await ensure_default_sequences()
    seqs = await _db().sequences.find({}, {"_id": 0}).sort("created_at", 1).to_list(200)
    for s in seqs:
        s["enrolled"] = await _db().enrollments.count_documents({"sequence_id": s["id"]})
    return seqs


@router.post("/sequences", status_code=201, dependencies=[can_work])
async def create_sequence(body: SequenceIn, user: dict = Depends(get_current_admin)):
    doc = {"id": str(uuid.uuid4()), **body.model_dump(), "steps": _clean_steps(body.steps), "ab_winner": None, "created_by": user["email"], "created_at": now_iso(), "updated_at": now_iso()}
    await _db().sequences.insert_one(dict(doc))
    doc.pop("_id", None)
    return doc


@router.put("/sequences/{seq_id}", dependencies=[can_work])
async def update_sequence(seq_id: str, body: SequenceIn):
    values = {**body.model_dump(), "steps": _clean_steps(body.steps), "updated_at": now_iso()}
    res = await _db().sequences.update_one({"id": seq_id}, {"$set": values})
    if not res.matched_count:
        raise HTTPException(status_code=404, detail="Sequence not found")
    return await _db().sequences.find_one({"id": seq_id}, {"_id": 0})


class EnrollIn(BaseModel):
    contact_ids: List[str] = Field(min_length=1, max_length=500)
    sequence_id: str
    variant: Optional[Literal["A", "B"]] = None


@router.post("/enrollments", dependencies=[can_work])
async def create_enrollments(body: EnrollIn, user: dict = Depends(get_current_admin)):
    seq = await _db().sequences.find_one({"id": body.sequence_id, "active": True}, {"_id": 0})
    if not seq:
        raise HTTPException(status_code=404, detail="Sequence not found or inactive")
    created, blocked = [], []
    for cid in body.contact_ids:
        c = await _db().contacts.find_one({"id": cid}, {"_id": 0})
        if not c:
            continue
        try:
            created.append(await enroll(c, seq, user["email"], force_variant=body.variant))
        except HTTPException as e:
            d = e.detail if isinstance(e.detail, dict) else {"reasons": [str(e.detail)]}
            blocked.append({"contact_id": cid, "name": c.get("name"), "reasons": d.get("reasons", [])})
    return {"created": created, "blocked": blocked}


@router.get("/enrollments")
async def list_enrollments(status: Optional[str] = None, company_id: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(50, le=200)):
    q: dict = {}
    if status:
        q["status"] = {"$in": status.split(",")}
    if company_id:
        q["company_id"] = company_id
    total = await _db().enrollments.count_documents(q)
    items = await _db().enrollments.find(q, {"_id": 0}).sort("created_at", -1).skip((page - 1) * page_size).limit(page_size).to_list(page_size)
    return {"items": items, "total": total}


class EnrollmentPatch(BaseModel):
    action: Literal["approve", "reject", "pause", "resume", "stop", "edit"]
    steps: Optional[List[Dict[str, object]]] = None
    opener: Optional[str] = Field(default=None, max_length=600)
    reason: Optional[str] = Field(default=None, max_length=300)


@router.patch("/enrollments/{enr_id}", dependencies=[can_work])
async def patch_enrollment(enr_id: str, body: EnrollmentPatch, user: dict = Depends(get_current_admin)):
    e = await _db().enrollments.find_one({"id": enr_id}, {"_id": 0})
    if not e:
        raise HTTPException(status_code=404, detail="Enrollment not found")
    if body.action == "edit":
        sets: dict = {}
        if body.steps is not None:
            steps = [{"day": int(s.get("day") or 0), "subject": str(s.get("subject") or "")[:300], "body": str(s.get("body") or "")[:5000]} for s in body.steps]
            if any(rm.find_competitors(s["subject"] + s["body"]) for s in steps):
                raise HTTPException(status_code=422, detail="The email names a competitor. Talk about the problem instead.")
            sets["steps"] = steps
        if body.opener is not None:
            if rm.find_competitors(body.opener):
                raise HTTPException(status_code=422, detail="The opener names a competitor.")
            sets["opener"] = body.opener
        await _set_status(e, e["status"], user["email"], "Edited", sets)
    elif body.action == "approve":
        if e["status"] != "pending_approval":
            raise HTTPException(status_code=409, detail="Only enrollments waiting for approval can be approved")
        c = await _db().contacts.find_one({"id": e["contact_id"]}, {"_id": 0}) or {}
        company = await _db().companies.find_one({"id": e.get("company_id")}, {"_id": 0}) if e.get("company_id") else None
        g = await compliance.gate(c, company, enrollment_id=e["id"])
        if not g["ok"]:
            raise HTTPException(status_code=422, detail={"message": "No longer passes the gate", "reasons": g["reasons"]})
        await _set_status(e, "scheduled", user["email"], "Approved", {"approved_by": user["email"], "approved_at": now_iso(), "next_send_at": now_iso()})
    elif body.action == "reject":
        await _set_status(e, "stopped", user["email"], f"Rejected: {body.reason or 'no reason given'}")
    elif body.action == "pause":
        await _set_status(e, "paused", user["email"], body.reason or "Paused by hand", {"resume_at": None})
    elif body.action == "resume":
        await _set_status(e, "active" if e.get("first_sent_at") else "scheduled", user["email"], "Resumed", {"next_send_at": now_iso(), "resume_at": None})
    elif body.action == "stop":
        await _set_status(e, "stopped", user["email"], body.reason or "Stopped by hand")
    return await _db().enrollments.find_one({"id": enr_id}, {"_id": 0})


class ReplyIn(BaseModel):
    contact_id: Optional[str] = None
    email: Optional[str] = Field(default=None, max_length=200)
    text: str = Field(min_length=1, max_length=10000)
    label: Optional[str] = None


@router.post("/replies", dependencies=[can_work])
async def log_reply(body: ReplyIn, user: dict = Depends(get_current_admin)):
    c = await _db().contacts.find_one({"id": body.contact_id}, {"_id": 0}) if body.contact_id else await _db().contacts.find_one({"email": (body.email or "").lower()}, {"_id": 0})
    if not c:
        raise HTTPException(status_code=404, detail="No contact with that email; add them first")
    if body.label and body.label not in REPLY_LABELS:
        raise HTTPException(status_code=422, detail="Unknown label")
    return await handle_reply(c, body.text, source="manual", label=body.label, by=user["email"])


@router.get("/replies")
async def list_replies(label: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(50, le=200)):
    q = {"label": label} if label else {}
    total = await _db().reply_log.count_documents(q)
    items = await _db().reply_log.find(q, {"_id": 0}).sort("received_at", -1).skip((page - 1) * page_size).limit(page_size).to_list(page_size)
    names = {c["id"]: c async for c in _db().contacts.find({"id": {"$in": [i["contact_id"] for i in items]}}, {"_id": 0, "id": 1, "name": 1, "company": 1, "title": 1, "lead_id": 1})}
    for i in items:
        i["contact"] = names.get(i["contact_id"])
    return {"items": items, "total": total}


@router.get("/sends")
async def list_sends(page: int = Query(1, ge=1), page_size: int = Query(50, le=200)):
    total = await _db().sends.count_documents({})
    items = await _db().sends.find({}, {"_id": 0}).sort("sent_at", -1).skip((page - 1) * page_size).limit(page_size).to_list(page_size)
    return {"items": items, "total": total}


@router.get("/kpis")
async def get_kpis(days: int = Query(30, ge=1, le=365)):
    return await kpis(days)


@router.post("/tick", dependencies=[admin_only])
async def run_tick():
    out = await tick()
    await ab_update()
    return out


@router.post("/learn", dependencies=[admin_only])
async def run_learn():
    return await learn()


@router.post("/instantly/connect", dependencies=[admin_only])
async def connect_instantly(request: Request):
    """Register this API as an Instantly webhook (all events) with the shared-secret header."""
    secret = os.environ.get("INSTANTLY_WEBHOOK_SECRET")
    base = (os.environ.get("PUBLIC_API_URL") or "").rstrip("/")
    if not (os.environ.get("INSTANTLY_API_KEY") and secret and base):
        raise HTTPException(status_code=422, detail="Set INSTANTLY_API_KEY, INSTANTLY_WEBHOOK_SECRET and PUBLIC_API_URL on the server first.")
    try:
        res = await instantly_request("POST", "/webhooks", json={"name": "Solix EchoLocate", "target_hook_url": f"{base}/api/outbound/webhooks/instantly", "event_type": "all_events",
                                                                 "headers": {"X-Solix-Webhook-Secret": secret}})
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"connected": True, "webhook_id": res.get("id")}


# --- Instantly webhook (public, secret header) ------------------------------------------------------------

webhooks = APIRouter(prefix="/api/outbound/webhooks", tags=["outbound-webhooks"])


@webhooks.post("/instantly")
async def instantly_webhook(request: Request, x_solix_webhook_secret: Optional[str] = Header(default=None)):
    import hmac
    secret = os.environ.get("INSTANTLY_WEBHOOK_SECRET")
    if not secret or not x_solix_webhook_secret or not hmac.compare_digest(secret, x_solix_webhook_secret):
        raise HTTPException(status_code=401, detail="Invalid webhook secret")
    try:
        p = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    event = p.get("event_type") or p.get("event") or ""
    email = (p.get("lead_email") or p.get("email") or (p.get("lead") or {}).get("email") or "").strip().lower()
    await _db().webhook_events.insert_one({"id": str(uuid.uuid4()), "source": "instantly", "event": event, "email_hash": compliance.email_hash(email) if email else None,
                                           "received_at": now_iso(), "campaign": p.get("campaign_id") or p.get("campaign")})
    contact = await _db().contacts.find_one({"email": email}, {"_id": 0}) if email else None
    if not contact:
        if event in ("lead_unsubscribed",) and email:
            await compliance.suppress(email, "unsubscribed", source="instantly", by="instantly")
        return {"ok": True, "matched": False}
    enr = await _db().enrollments.find_one({"contact_id": contact["id"]}, {"_id": 0}, sort=[("created_at", -1)])
    if event == "email_sent" and enr:
        await _db().sends.insert_one({"id": str(uuid.uuid4()), "enrollment_id": enr["id"], "contact_id": contact["id"], "company_id": enr.get("company_id"), "sequence_id": enr["sequence_id"],
                                      "variant": enr.get("variant"), "step": p.get("step") or p.get("email_sequence_step"), "email": email, "subject": p.get("email_subject"),
                                      "provider": "instantly", "status": "delivered", "sent_at": now_iso()})
    elif event == "email_bounced":
        await _db().sends.update_many({"contact_id": contact["id"]}, {"$set": {"bounced": True}})
        await _db().contacts.update_one({"id": contact["id"]}, {"$set": {"email_status": "invalid", "verify_detail": "Hard bounce"}})
        await compliance.suppress(email, "bounced", source="instantly", by="instantly")
    elif event in ("reply_received", "lead_interested", "lead_meeting_booked", "lead_not_interested", "lead_out_of_office", "lead_wrong_person", "lead_unsubscribed"):
        text = p.get("reply_text") or p.get("reply_text_snippet") or ""
        label = {"lead_interested": "interested", "lead_meeting_booked": "meeting", "lead_not_interested": "not_interested", "lead_out_of_office": "ooo",
                 "lead_wrong_person": "referral", "lead_unsubscribed": "unsubscribe"}.get(event)
        if event == "reply_received" or label:
            if label and await _db().reply_log.find_one({"contact_id": contact["id"], "label": label, "received_at": {"$gte": (_now() - timedelta(minutes=10)).isoformat()}}, {"_id": 1}):
                return {"ok": True, "duplicate": True}
            await handle_reply(contact, text, source="instantly", label=label, meta={"event": event, "unibox_url": p.get("unibox_url")})
    elif event in ("email_opened", "email_link_clicked") and enr:
        await _db().enrollments.update_one({"id": enr["id"]}, {"$inc": {"opens" if event == "email_opened" else "clicks": 1}})
    return {"ok": True, "matched": True}
