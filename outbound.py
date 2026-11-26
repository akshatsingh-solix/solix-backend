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

import asyncio
import hashlib
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Dict, List, Literal, Optional

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

import cache
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
webhooks = APIRouter(prefix="/api/outbound/webhooks", tags=["outbound-webhooks"])  # public: providers call these (secret or signed URL)
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
    """Syntax, MX lookup, then the mailbox-verification chain (radar_sources.verify_email): a self-hosted Reacher, then
    MailboxValidator, ZeroBounce, Abstract, Hunter and Tomba's free allowances - whichever are set up and have checks left."""
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
    import radar_sources
    if not radar_sources.mailbox_verifier_ready():
        return {"status": "mx_only", "detail": "Mail server exists; mailbox not checked (set REACHER_URL or a verification key, see docs/radar-free-sources.md)"}
    async with httpx.AsyncClient(timeout=60) as client:
        res = await radar_sources.verify_email(client, email)
    if not res:
        return {"status": "unknown", "detail": "No verifier gave a definite answer (unavailable, out of free checks, or the server greylisted us)"}
    return res


async def score_email_source(contact: dict, correct: bool, how: str) -> None:
    """Measured accuracy of whoever supplied this address (data_quality.py): a verification, bounce or reply is a check."""
    import data_quality as dq
    via = contact.get("source_email") or contact.get("source") or "unknown"
    await dq.log("email", "email", via, correct, how=how, contact_id=contact.get("id"), company_id=contact.get("company_id"))


async def set_verification(contact: dict) -> dict:
    v = await verify_email(contact.get("email"))
    await _db().contacts.update_one({"id": contact["id"]}, {"$set": {"email_status": v["status"], "verify_detail": v["detail"], "verified_at": now_iso()}})
    if v["status"] in ("valid", "invalid"):
        await score_email_source(contact, v["status"] == "valid", "verification")
    return {**contact, "email_status": v["status"], "verify_detail": v["detail"]}


_PATTERNS = ["{first}.{last}", "{f}{last}", "{first}{last}", "{first}", "{first}_{last}", "{f}.{last}", "{last}.{first}", "{first}{l}", "{last}{f}", "{last}"]


def _norm_name(x: str) -> str:
    return re.sub(r"[^a-z]", "", (x or "").lower())


def pattern_of(email: str, first: str, last: str) -> Optional[str]:
    """Which address pattern an address follows for this person ("{first}.{last}"), if any."""
    local = (email or "").split("@")[0].lower()
    f, l = _norm_name(first), _norm_name(last)
    if not (f and l and local):
        return None
    for p in _PATTERNS:
        if p.format(first=f, last=l, f=f[0], l=l[0]) == local:
            return p
    return None


async def learned_pattern(domain: str, company: Optional[dict] = None) -> Optional[str]:
    """The pattern the company's verified addresses follow: from contacts we've verified, else the one Hunter reported."""
    from collections import Counter
    seen = Counter()
    async for c in _db().contacts.find({"email": {"$regex": f"@{re.escape(domain)}$"}, "email_status": "valid"}, {"_id": 0, "email": 1, "first_name": 1, "last_name": 1}).limit(50):
        p = pattern_of(c["email"], c.get("first_name"), c.get("last_name"))
        if p:
            seen[p] += 1
    if seen:
        return seen.most_common(1)[0][0]
    hp = (company or {}).get("email_pattern")
    return hp if hp in _PATTERNS else None


def guess_emails(first: str, last: str, domain: str, pattern: Optional[str] = None) -> List[str]:
    """Candidate addresses, the company's known pattern first."""
    f, l = _norm_name(first), _norm_name(last)
    if f and l and pattern in _PATTERNS:
        lead = [pattern.format(first=f, last=l, f=f[0], l=l[0]) + "@" + domain]
        return list(dict.fromkeys(lead + _guess_emails(first, last, domain)))
    return _guess_emails(first, last, domain)


def _guess_emails(first: str, last: str, domain: str) -> List[str]:
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


PERSONA_SEQUENCE = {"name": "Persona-led: explore, then buy", "region": "global", "steps": [
    {"day": 0, "subject_a": "{company} and {product}", "subject_b": "A thought for {company}'s {team}",
     "body_a": "Hi {first_name},\n\n{opener}\n\n{explore_cta}\n\nBest,\n{rep_name}",
     "body_b": "Hi {first_name},\n\n{opener}\n\nIf it helps, here's how it works: {resource_link}\n\nBest,\n{rep_name}"},
    {"day": 4, "subject_a": "Re: {company} and {product}", "subject_b": "Re: A thought for {company}'s {team}",
     "body_a": "Hi {first_name},\n\nA different angle: in {industry}, {team} usually feel it as {pain}. The teams that fix it {outcome} first, then expand from there.\n\nThe short version is here if useful: {resource_link}\n\nBest,\n{rep_name}",
     "body_b": "Hi {first_name},\n\nOne question rather than a pitch: how much of {company}'s estate is kept running only for its records?\n\nIf the answer is \"more than we'd like\", {resource_link} shows how others approached it.\n\nBest,\n{rep_name}"},
    {"day": 9, "subject_a": "A working session for {company}", "subject_b": "A working session for {company}",
     "body_a": "Hi {first_name},\n\n{buy_cta}\n\nYou'd leave with a first-wave shortlist and a rough saving, whether or not we work together.\n\nBest,\n{rep_name}",
     "body_b": "Hi {first_name},\n\n{buy_cta} I'll bring a draft plan based on what we can see from outside; you correct it.\n\nBest,\n{rep_name}"},
    {"day": 14, "subject_a": "Closing the loop", "subject_b": "Closing the loop",
     "body_a": "Hi {first_name},\n\nI'll leave it here. If someone else at {company} owns this, a pointer would be appreciated.\n\nBest,\n{rep_name}",
     "body_b": "Hi {first_name},\n\nLast note from me. If {product} becomes a priority, just reply and I'll pick it up.\n\nBest,\n{rep_name}"},
]}


def _step_objects():
    import messaging
    return [{"intent": o["intent"], "objective": o["ask"]} for o in messaging.STEP_OBJECTIVES]


STEP_OBJECTS = _step_objects()


async def ensure_default_sequences() -> None:
    if not await _db().sequences.find_one({"name": PERSONA_SEQUENCE["name"]}, {"_id": 1}) and await _db().sequences.count_documents({}) > 0:
        await _db().sequences.insert_one({"id": str(uuid.uuid4()), **PERSONA_SEQUENCE, "active": True, "provider": None, "instantly_campaign_id": None, "ab_winner": None,
                                          "created_by": "system", "created_at": now_iso(), "updated_at": now_iso()})
    if await _db().sequences.count_documents({}) == 0:
        for s in DEFAULT_SEQUENCES + [PERSONA_SEQUENCE]:
            await _db().sequences.insert_one({"id": str(uuid.uuid4()), **s, "active": True, "provider": None, "instantly_campaign_id": None, "ab_winner": None,
                                              "created_by": "system", "created_at": now_iso(), "updated_at": now_iso()})


def _fill(template: str, ctx: dict) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: str(ctx.get(m.group(1), m.group(0))), template or "")


# --- Personalisation ----------------------------------------------------------------------------

OPENER_SYSTEM = ("You write the first two sentences of a B2B cold email for Solix Technologies (archiving, application retirement, data governance). "
                 "Ground them ONLY in the evidence given about the prospect's company. Never name any software vendor or competitor product. Never mention pricing. "
                 "No greeting, no sign-off, no flattery, no questions. Maximum 2 sentences, under 45 words. Reply with the two sentences only.")


async def write_opener(contact: dict, company: Optional[dict]) -> dict:
    """A first-touch opener for this person (messaging.py): the account's trigger, said in their role's terms, for their
    industry and the account's lead product. A model writes it when one is set up; every draft is checked and a generic,
    off-brief or risky one falls back to the approved deep dive, then to the rule-based persona opener."""
    import messaging
    company = company or {}
    research = await _db().radar_research.find_one({"company_id": company.get("id"), "status": "approved"}, {"_id": 0}, sort=[("created_at", -1)]) if company.get("id") else None
    sig, sigs = None, []
    if company.get("id"):
        sigs = await _db().company_signals.find({"company_id": company["id"]}, {"_id": 0, "raw": 0}).to_list(200)
        sigs.sort(key=lambda s: (rm.STRENGTH_RANK.get(s.get("role_strength") or "Unrated", 3), str(s.get("posted_at") or "")), reverse=False)
        sig = sigs[0] if sigs else None
    settings = await compliance.outbound_settings()
    b = messaging.brief(contact, company, signal=sig, research=research, site_url=settings.get("site_url") or "https://www.solix.com", signals=sigs)
    evidence = []
    if research:
        evidence.append(f"Research headline: {research.get('headline')}\nTrigger: {research.get('trigger')}")
    if sig:
        evidence.append(f"Signal ({sig['type'].replace('_', ' ')}): {sig.get('title') or ''} - {sig.get('evidence_text') or ''}")
    opener, provider, quality, model = None, "rules", None, None
    import grounding
    import llm_log
    import messaging as _m
    started = time.monotonic()
    prompt_version = llm_log.version("opener", _m.OPENER_SYSTEM)
    prospect_ev = evidence + [f"{x.get('title') or ''} {x.get('evidence_text') or ''}" for x in sigs[:6]] + ([b.get("hook") or ""] if b.get("hook") else [])
    solix_ev = [b.get(k) or "" for k in ("outcome", "pain", "product_label", "industry_reason", "explore_offer", "team")] + _solix_facts(b)

    def grounded(text: str) -> dict:
        return grounding.check(text, prospect_evidence=prospect_ev, solix_evidence=solix_ev, company=company.get("name"), person=contact.get("name"))

    if evidence or b.get("industry_reason"):
        import llm
        import privacy
        providers, mode = privacy.providers_for("opener")
        if mode == "redacted":  # the brief carries no names; strip any from the signal and research text too
            red = privacy.Redactor([contact.get("name"), (sig or {}).get("person")])
            evidence = [red.redact(e) for e in evidence]
        if providers:
            text = []
            try:
                async for ev in llm.stream_completion([{"role": "system", "content": messaging.OPENER_SYSTEM},
                                                       {"role": "user", "content": messaging.prompt(b) + "\n\nEvidence:\n" + ("\n\n".join(evidence) or "none")}],
                                                      max_tokens=160, temperature=0.4, providers=providers):
                    if ev["type"] == "text":
                        text.append(ev["text"])
                    elif ev["type"] == "meta":
                        provider, model = ev.get("provider"), ev.get("model")
                opener = re.sub(r"\s+", " ", "".join(text)).strip().strip('"')
            except llm.ProviderError:
                opener = None
    g = None
    if opener:
        g = await grounding.judge(grounded(opener), prospect_ev, solix_ev)
        await llm_log.record("opener", prompt_version=prompt_version, provider=provider, model=model, ms=round((time.monotonic() - started) * 1000),
                             ok=g["ok"], outcome="; ".join(g["problems"])[:200] or "grounded", ref={"contact_id": contact.get("id"), "company_id": company.get("id")})
    if opener and (rm.find_competitors(opener) or len(opener.split()) > 70 or re.search(r"\[\d+\]", opener) or not messaging.check(opener, b)["ok"] or not g["ok"]):
        opener, provider, model = None, "rules", None
    if not opener and research and (research.get("first_touch") or {}).get("body"):
        body = research["first_touch"]["body"]
        paras = [p.strip() for p in body.split("\n\n") if p.strip() and not p.strip().lower().startswith(("hi ", "best", "regards"))]
        if paras and not rm.find_competitors(paras[0]) and messaging.check(paras[0], b)["ok"] and grounded(paras[0])["ok"]:
            opener, provider = paras[0], "deep_dive"
    if not opener:
        opener, provider = messaging.rule_opener(b), "rules"
    quality = messaging.check(opener, b)
    g = grounded(opener)
    return {"opener": opener, "provider": provider, "model": model if provider not in ("rules", "deep_dive") else None,
            "prompt_version": prompt_version if provider not in ("rules", "deep_dive") else f"{provider}@{_m.RULES_VERSION if hasattr(_m, 'RULES_VERSION') else '1'}",
            "signal_id": (sig or {}).get("id"), "research_id": (research or {}).get("id"), "brief": b, "quality": quality, "grounding": g,
            "evidence": {"prospect": prospect_ev[:8], "solix": [e for e in solix_ev if e][:8]}}


def _solix_facts(b: dict) -> list:
    """What Solix's own published content says about the product being pitched: the only source for Solix claims."""
    import sol_search
    q = " ".join(x for x in (b.get("product_label"), b.get("outcome")) if x)
    return [f"{h['title']}: {h['text'][:700]}" for h in sol_search.hybrid_search(q, 3)] if q else []


def render_steps(sequence: dict, variant: str, ctx: dict) -> List[dict]:
    v = variant.lower()
    return [{"day": int(s.get("day") or 0), "subject": _fill(s.get(f"subject_{v}") or s.get("subject_a") or "", ctx), "body": _fill(s.get(f"body_{v}") or s.get("body_a") or "", ctx)}
            for s in sequence.get("steps") or []]


def footer_html(email: str, settings: dict, *, contact: Optional[dict] = None, first: bool = False) -> str:
    """Sender identity, postal address and a one-step opt-out on every email (PECR reg. 23, ePrivacy 13(4)); on the
    first touch also the GDPR Art. 14 notice: who we are, where we got their details, the lawful basis and their rights."""
    unsub = compliance.unsubscribe_url(email)
    parts = []
    if settings.get("ai_disclosure"):
        parts.append(escape(settings["ai_disclosure"]))
    parts.append(f"{escape(settings.get('sender_name') or 'Solix Technologies')}, {escape(settings.get('postal_address') or '')}")
    if first and settings.get("art14_notice", True):
        source = compliance.source_label(contact or {})
        privacy = settings.get("privacy_notice_url") or ((settings.get("site_url") or "https://www.solix.com").rstrip("/") + "/privacy")
        parts.append(f"We contacted you because your role relates to what we do. We found your work details via {escape(source)} and use them on the basis of "
                     f"legitimate interest. You can object or ask what we hold at any time: <a href=\"{escape(privacy)}\">privacy notice</a>.")
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
    if settings.get("enroll_hold"):
        raise HTTPException(status_code=422, detail={"message": "New enrollments are on hold for deliverability.", "reasons": [settings.get("enroll_hold_reason") or "Deliverability warning"],
                                                     "warnings": []})
    channel = sequence.get("channel") or "email"
    g = await compliance.gate(contact, company, settings, channel=channel)
    if channel == "linkedin":
        if not contact.get("linkedin_url"):
            g["ok"], g["reasons"] = False, g["reasons"] + ["No LinkedIn profile URL for this person"]
        if not sequence.get("dripify_campaign_id"):
            g["ok"], g["reasons"] = False, g["reasons"] + ["This LinkedIn sequence has no Dripify campaign id"]
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
    b = op["brief"]
    ctx = {"first_name": contact.get("first_name") or "there", "last_name": contact.get("last_name") or "", "company": (company or {}).get("name") or contact.get("company") or "your team",
           "title": contact.get("title") or "", "opener": op["opener"], "rep_name": rep_name,
           # Persona placeholders (messaging.py) for sequences written to them.
           "pain": b["pain"], "outcome": b["outcome"], "product": b.get("product_label") or "Solix", "resource_link": b["resource_link"], "explore_offer": b["explore_offer"],
           "explore_cta": b["explore_cta"], "buy_cta": b["buy_cta"], "industry": (b.get("industry_label") or "your industry").lower(), "team": b["team"]}
    steps = render_steps(sequence, variant, ctx)
    now = now_iso()
    doc = {
        "id": str(uuid.uuid4()), "contact_id": contact["id"], "company_id": contact.get("company_id"), "sequence_id": sequence["id"], "sequence_name": sequence["name"],
        "email": contact["email"], "contact_name": contact.get("name"), "company_name": ctx["company"], "variant": variant, "opener": op["opener"], "opener_provider": op["provider"],
        "signal_id": op["signal_id"], "research_id": op["research_id"], "steps": steps,
        "persona": b["persona"], "seniority_band": b["seniority"], "industry_slug": b.get("industry"), "pitch_product": b.get("product"), "opener_quality": op["quality"]["score"],
        "opener_model": op.get("model"), "prompt_version": op.get("prompt_version"), "grounding": op["grounding"], "evidence": op.get("evidence"),
        "step_index": 0, "channel": channel, "campaign": sequence.get("dripify_campaign_id") if channel == "linkedin" else None,
        "provider": "dripify" if channel == "linkedin" else (sequence.get("provider") or settings.get("provider")), "status": "pending_approval" if settings.get("approval_required") else "scheduled",
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
    """Bulk-add endpoint with a deterministic idempotency key: a retry after a timeout can't create the lead twice."""
    import deliverability as dl
    campaign = sequence.get("instantly_campaign_id") or settings.get("instantly_campaign_id")
    if not campaign:
        raise RuntimeError("No Instantly campaign is set for this sequence")
    custom = {"opener": enr["opener"], "rep_name": (enr["steps"][0]["body"].rsplit("\n", 1)[-1] if enr["steps"] else ""), "solix_enrollment": enr["id"],
              "solix_contact": contact["id"], "job_title": contact.get("title") or "", "unsubscribe_url": compliance.unsubscribe_url(contact["email"])}
    for i, s in enumerate(enr["steps"], 1):
        custom[f"subject_{i}"], custom[f"body_{i}"] = s["subject"], s["body"]
    lead = {"email": contact["email"], "first_name": contact.get("first_name"), "last_name": contact.get("last_name"), "company_name": enr.get("company_name"),
            "personalization": enr["opener"], "custom_variables": custom}
    res = await instantly_request("POST", "/leads/add", json={"campaign_id": campaign, "leads": [{k: v for k, v in lead.items() if v is not None}],
                                                              "skip_if_in_workspace": True, "idempotency_key": dl.instantly_key(campaign, contact["id"], enr)})
    created = res.get("created_leads") or []
    if not created and (res.get("blocklist_count") or res.get("in_blocklist")):
        raise RuntimeError("Instantly refused the lead: the address is on the workspace blocklist")
    return (created[0].get("id") if created else None) or res.get("id") or "instantly"


async def _claim(enr: dict) -> bool:
    """Only one run sends a given step: the in-API loop, "Run now" and an external tick can overlap."""
    now = _now()
    got = await _db().enrollments.find_one_and_update(
        {"id": enr["id"], "step_index": enr["step_index"], "$or": [{"claim_until": None}, {"claim_until": {"$lt": now.isoformat()}}]},
        {"$set": {"claim_until": (now + timedelta(minutes=10)).isoformat()}})
    return got is not None


async def send_step(enr: dict, settings: dict) -> str:
    """Send one step, exactly once.

    The enrollment is claimed for 10 minutes (two runs can't take the same step), then the send row is written
    *before* the provider is called under a unique (enrollment, step) key (deliverability.claim_send). If that row
    already exists and isn't a clean failure, this step went out or may have: nothing is sent again."""
    import deliverability as dl
    if not await _claim(enr):
        return "skipped"
    contact = await _db().contacts.find_one({"id": enr["contact_id"]}, {"_id": 0})
    company = await _db().companies.find_one({"id": enr.get("company_id")}, {"_id": 0}) if enr.get("company_id") else None
    if not contact:
        await _set_status(enr, "stopped", "system", "Contact deleted")
        return "stopped"
    # Re-check suppression, exclusions and the overlap ledger at send time: they can change after approval.
    import ledger
    sup = await compliance.is_suppressed(contact.get("email"))
    status_block = (company or {}).get("status") if (company or {}).get("status") in ("not_a_fit", "meeting_booked", "opportunity", "won", "lost") else None
    reason = (f"suppressed ({sup})" if sup else None) or (company or {}).get("excluded_reason") or (f"account is {status_block}" if status_block else None) \
        or await ledger.conflict(contact, enr.get("channel") or "email", enr["id"])
    if reason:
        await _set_status(enr, "stopped", "system", f"Blocked at send time: {reason}")
        return "stopped"
    if (enr.get("channel") or "email") == "linkedin":
        return await _send_linkedin(enr, contact, company, settings)
    step = enr["steps"][enr["step_index"]]
    provider = enr.get("provider") or settings.get("provider") or "dry_run"
    if provider != "dry_run" and not compliance.live_sending_allowed():
        # Held, not simulated: a simulated send would mark the account contacted and count as delivered.
        await _db().enrollments.update_one({"id": enr["id"]}, {"$set": {"claim_until": None}})
        return "held"
    if provider != "dry_run" and enr["step_index"] == 0 and _stale(contact.get("verified_at"), int(settings.get("reverify_days") or 30)):
        # Addresses decay 2-3% a month: re-check a mailbox verified long ago before the first email goes.
        contact = await set_verification(contact)
        if contact.get("email_status") == "invalid":
            await _set_status(enr, "stopped", "system", "Address failed re-verification at send time")
            return "stopped"
        if contact.get("email_status") != "valid" and settings.get("require_verified_email"):
            await _set_status(enr, "paused", "system", f"Re-verification came back {contact.get('email_status')}: held for a person to check", {"claim_until": None, "resume_at": None})
            return "held"
    sequence = await _db().sequences.find_one({"id": enr["sequence_id"]}, {"_id": 0}) or {}
    from_addr = os.environ.get("EMAIL_FROM") or os.environ.get("SMTP_USER") or ""
    send = {"id": str(uuid.uuid4()), "enrollment_id": enr["id"], "contact_id": contact["id"], "company_id": enr.get("company_id"), "sequence_id": enr["sequence_id"],
            "variant": enr["variant"], "step": enr["step_index"] + 1, "email": contact["email"], "subject": step["subject"], "provider": provider, "sent_at": now_iso(),
            "channel": "email", "sending_domain": dl.domain_of(from_addr) if provider == "smtp" else (settings.get("instantly_sending_domain") if provider == "instantly" else "dry-run"),
            "model": enr.get("opener_model"), "prompt_version": enr.get("prompt_version"), "approved_by": enr.get("approved_by")}
    claimed = await dl.claim_send(send)
    if not claimed:
        # This step already has a send row: it went out (or may have). Move on rather than send it twice.
        await _db().enrollments.update_one({"id": enr["id"]}, {"$set": {"claim_until": None}})
        await _set_status(enr, enr["status"], "system", "Step already sent or in flight: not sent again", {"claim_until": None})
        return "skipped"
    key = claimed["idem_key"]
    try:
        if provider == "instantly":
            ref = await _send_instantly(enr, contact, sequence, settings)
        elif provider == "smtp":
            from emailer import send_email
            unsub = compliance.unsubscribe_url(contact["email"])
            html = body_html(step["body"]) + footer_html(contact["email"], settings, contact=contact, first=enr["step_index"] == 0)
            ref = await send_email(to=contact["email"], subject=step["subject"], html=html,
                                   headers={"List-Unsubscribe": f"<{unsub}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"})
        else:
            ref = None
    except Exception:
        await dl.finish_send(key, "failed")  # the provider refused: nothing left, a later attempt may retry this row
        raise
    status = "handed_off" if provider == "instantly" else "sent" if provider == "smtp" else "simulated"
    await dl.finish_send(key, status, provider_ref=ref)
    send["status"] = status
    await ledger.record(contact, "email", enr["id"], by="echolocate")
    if enr["step_index"] == 0 and not contact.get("notice_sent_at") and provider != "dry_run":
        await _db().contacts.update_one({"id": contact["id"]}, {"$set": {"notice_sent_at": now_iso()}})  # Art. 14(3) clock stops
    if provider == "instantly":
        await _set_status(enr, "active", "system", "Handed to Instantly (it sends every step)", {"step_index": len(enr["steps"]), "handed_off_at": now_iso(), "claim_until": None})
    else:
        nxt = enr["step_index"] + 1
        if nxt >= len(enr["steps"]):
            await _set_status(enr, "completed", "system", f"Step {nxt} sent; sequence finished", {"step_index": nxt, "claim_until": None})
        else:
            started = datetime.fromisoformat(enr.get("first_sent_at") or now_iso())
            due = (started + timedelta(days=enr["steps"][nxt]["day"])).isoformat()
            await _set_status(enr, "active", "system", f"Step {nxt} sent", {"step_index": nxt, "next_send_at": due, "first_sent_at": enr.get("first_sent_at") or send["sent_at"], "claim_until": None})
    if company and company.get("status") in rm.SYSTEM_STATUSES:
        await _db().companies.update_one({"id": company["id"]}, {"$set": {"status": "contacted"}})
        rs.companies_changed()
        await rs.add_history(company["id"], "system", "outbound", f"First email {'handed to Instantly' if provider == 'instantly' else provider} for {contact.get('name')}")
    return send["status"]


async def _send_linkedin(enr: dict, contact: dict, company: Optional[dict], settings: dict) -> str:
    """Hand an approved person to a running Dripify campaign (it runs the LinkedIn steps), paced per seat."""
    import deliverability as dl
    import ledger
    import linkedin
    release = {"claim_until": None}
    if settings.get("linkedin_paused"):
        await _db().enrollments.update_one({"id": enr["id"]}, {"$set": release})
        return "held"
    if not compliance.live_sending_allowed() or not linkedin.ready():
        await _db().enrollments.update_one({"id": enr["id"]}, {"$set": release})
        return "held"
    campaign = enr.get("campaign")
    full = await linkedin.seat_room(campaign, settings)
    if full:
        tomorrow = (_now() + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0).isoformat()
        await _db().enrollments.update_one({"id": enr["id"]}, {"$set": {**release, "next_send_at": tomorrow}})
        return "held"
    send = {"id": str(uuid.uuid4()), "enrollment_id": enr["id"], "contact_id": contact["id"], "company_id": enr.get("company_id"), "sequence_id": enr["sequence_id"],
            "variant": enr["variant"], "step": 1, "email": contact.get("email"), "subject": "LinkedIn sequence", "provider": "dripify", "channel": "linkedin",
            "campaign": campaign, "sent_at": now_iso(), "sending_domain": "linkedin", "model": enr.get("opener_model"), "prompt_version": enr.get("prompt_version"),
            "approved_by": enr.get("approved_by")}
    claimed = await dl.claim_send(send)
    if not claimed:
        await _set_status(enr, enr["status"], "system", "Already handed to Dripify: not sent again", release)
        return "skipped"
    try:
        ref = await linkedin.push(contact, enr, campaign)
    except linkedin.SeatProblem as e:
        await dl.finish_send(claimed["idem_key"], "failed")
        await linkedin.pause_channel(str(e))
        await _db().enrollments.update_one({"id": enr["id"]}, {"$set": release})
        return "held"
    except Exception:
        await dl.finish_send(claimed["idem_key"], "failed")
        raise
    await dl.finish_send(claimed["idem_key"], "handed_off", provider_ref=ref)
    await ledger.record(contact, "linkedin", enr["id"], by="echolocate")
    await _set_status(enr, "active", "system", "Handed to Dripify (it runs the LinkedIn steps)", {"step_index": len(enr["steps"]), "handed_off_at": now_iso(), **release})
    if company and company.get("status") in rm.SYSTEM_STATUSES:
        await _db().companies.update_one({"id": company["id"]}, {"$set": {"status": "contacted"}})
        rs.companies_changed()
    return "handed_off"


def _stale(iso: Optional[str], days: int) -> bool:
    if not iso:
        return True
    try:
        return datetime.fromisoformat(iso) < _now() - timedelta(days=days)
    except ValueError:
        return True


async def bounce_check(settings: dict) -> Optional[float]:
    """Staged deliverability response per sending domain (deliverability.py). Returns the overall 7-day bounce %
    once there are enough sends to mean anything, else None."""
    import deliverability as dl
    table = await dl.enforce(settings)
    row = table.get("all") or {}
    return row.get("bounce_pct") if row.get("enough_data") else None


def _in_window(settings: dict, now: datetime) -> bool:
    days = settings.get("send_days") or [0, 1, 2, 3, 4]
    start, end = (settings.get("send_hours_utc") or [8, 17])[:2]
    return now.weekday() in days and start <= now.hour < end


async def tick() -> dict:
    """The sender: resumes paused enrollments that are due, then sends what's due within the cap."""
    settings = await compliance.outbound_settings()
    counts = {"sent": 0, "stopped": 0, "errors": 0, "resumed": 0, "held": 0}
    now = _now()
    async for e in _db().enrollments.find({"status": "paused", "resume_at": {"$lte": now.isoformat()}}, {"_id": 0, "id": 1, "paused_from": 1, "first_sent_at": 1}):
        if e.get("paused_from") == "pending_approval":
            await _set_status(e, "pending_approval", "system", "Pause ended: back in the approval queue", {"resume_at": None, "paused_from": None})
        else:
            await _set_status(e, "active" if e.get("first_sent_at") else "scheduled", "system", "Resumed after pause",
                              {"next_send_at": now.isoformat(), "resume_at": None, "paused_from": None})
        counts["resumed"] += 1
    if now.weekday() == 0 and now.hour >= 6:
        state = await _db().settings.find_one({"key": "learning"}, {"_id": 0}) or {}
        if (state.get("last_run") or "")[:10] != now.date().isoformat():
            try:
                await learn()
            except Exception:  # noqa: BLE001
                logger.exception("weekly learning failed")
    try:
        import retention
        await retention.run_if_due(settings)
    except Exception:  # noqa: BLE001
        logger.exception("retention run failed")
    import learning as intel
    await intel.weekly_if_due()  # the self-review runs after signal weights are re-learned
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
                if result == "skipped":  # another run has it
                    continue
                counts["stopped" if result == "stopped" else "held" if result == "held" else "sent"] += 1
            except Exception as exc:  # noqa: BLE001 - one bad address must not stop the batch
                counts["errors"] += 1
                await _set_status(e, e["status"], "system", f"Send failed: {str(exc)[:200]}", {"next_send_at": (now + timedelta(hours=2)).isoformat(), "last_error": str(exc)[:300], "claim_until": None})
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
                   "A reply can carry more than one intent (an out-of-office that names a colleague; interest plus 'take me off this list'): list them all. "
                   'Reply with JSON only: {"label": "primary label", "intents": ["every label that applies"], "confidence": 0.0-1.0, '
                   '"reason": "one short sentence", "resume_on": "YYYY-MM-DD or null"}')
REVIEW_CONFIDENCE = 0.6
POSITIVE_SET, NEGATIVE_SET = {"interested", "meeting"}, {"not_interested", "unsubscribe", "objection"}


def _rule_intents(t: str) -> list:
    found = []
    for label, rx in _KW:
        if rx.search(t) and label not in found:
            found.append(label)
    return found


async def classify(text: str) -> dict:
    """Primary intent, every intent present, and whether a person should look.

    Rules first: an opt-out anywhere in the reply wins over everything and needs no model (it must reach the
    suppression list even if the model is down or wrong). A reply whose intents pull in opposite directions
    ("interested, but not this year"), or that a model labels with low confidence or against the rules, goes
    to the review lane instead of being acted on: about one reply in ten is misread by any classifier."""
    import llm_log
    t = text or ""
    rules = _rule_intents(t)
    if "unsubscribe" in rules:
        return {"label": "unsubscribe", "intents": rules, "by": "rules", "reason": "Asked to stop", "needs_review": False}
    if rules[:1] == ["ooo"] and len(rules) == 1:
        return {"label": "ooo", "intents": rules, "by": "rules", "reason": "Auto-reply", "needs_review": False}
    import llm
    import privacy
    providers, mode = privacy.providers_for("classify")
    tm = privacy.Redactor().redact(t, strip_signature=True) if mode == "redacted" else t
    model_out = None
    if providers:
        out, meta, started = [], {}, time.monotonic()
        try:
            async for ev in llm.stream_completion([{"role": "system", "content": CLASSIFY_SYSTEM}, {"role": "user", "content": tm[:4000]}], max_tokens=180, temperature=0,
                                                  providers=providers):
                if ev["type"] == "text":
                    out.append(ev["text"])
                elif ev["type"] == "meta":
                    meta = ev
            raw = "".join(out)
            data = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
            if data.get("label") in REPLY_LABELS:
                model_out = data
            await llm_log.record("reply_classifier", prompt_version=llm_log.version("classify", CLASSIFY_SYSTEM), provider=meta.get("provider"), model=meta.get("model"),
                                 ms=round((time.monotonic() - started) * 1000), ok=model_out is not None, outcome=(model_out or {}).get("label") or "unparsed")
        except Exception:  # noqa: BLE001
            model_out = None
    if model_out:
        intents = [i for i in dict.fromkeys([model_out["label"], *[x for x in model_out.get("intents") or [] if x in REPLY_LABELS], *rules]) if i != "neutral"] or ["neutral"]
        if "unsubscribe" in intents:
            return {"label": "unsubscribe", "intents": intents, "by": "model", "reason": model_out.get("reason") or "", "needs_review": False}
        conf = float(model_out.get("confidence") or 0.5)
        mixed = bool(set(intents) & POSITIVE_SET and set(intents) & NEGATIVE_SET)
        disagrees = bool(rules) and model_out["label"] not in rules and not (model_out["label"] in POSITIVE_SET and set(rules) & POSITIVE_SET)
        hidden_referral = "ooo" in intents and bool({"referral", "forward"} & set(intents))  # a colleague named inside an auto-reply
        return {"label": model_out["label"], "intents": intents, "reason": model_out.get("reason") or "", "resume_on": model_out.get("resume_on"), "by": "model",
                "confidence": conf, "needs_review": mixed or conf < REVIEW_CONFIDENCE or disagrees or hidden_referral}
    if rules:
        mixed = bool(set(rules) & POSITIVE_SET and set(rules) & NEGATIVE_SET) or ("ooo" in rules and bool({"referral", "forward"} & set(rules)))
        return {"label": rules[0], "intents": rules, "reason": "Matched a phrase", "by": "rules", "needs_review": mixed}
    return {"label": "neutral", "intents": ["neutral"], "reason": "No clear intent", "by": "rules", "needs_review": False}


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
    cache.bump("reports")
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


async def _credit_outbound(lead: dict) -> None:
    """Our email reached them before they became a lead with no better-known source: outbound is the first touch."""
    first = await _db().sends.find_one({"email": lead["email"], "bounced": {"$ne": True}, "status": {"$in": ["sent", "simulated"]}},
                                       {"_id": 0, "sent_at": 1}, sort=[("sent_at", 1)])
    if not first:
        return
    import intent
    ft = lead.get("first_touch") or {}
    sets = {"tags": sorted(set(lead.get("tags") or []) | {"outbound"})}
    known = any(ft.get(k) for k in ("referrer", "utm_source", "utm_medium", "chat", "outbound"))
    if not known and first["sent_at"] < (lead.get("created_at") or ""):
        sets["first_touch"] = {"outbound": True, "source": "outbound", "at": first["sent_at"], "landing": ft.get("landing")}
    await _db().leads.update_one({"id": lead["id"]}, {"$set": sets})
    if "first_touch" in sets:
        await intent.rollup_lead(lead["id"])
    cache.bump("reports")


async def on_inbound(lead: dict, stype: str) -> int:
    """Someone in outbound engaged on the website: stop emailing them and hand them to the owner."""
    email = (lead.get("email") or "").lower()
    if not email:
        return 0
    await _db().contacts.update_many({"email": email}, {"$set": {"lead_id": lead["id"]}})
    await _credit_outbound(lead)
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
    cls = {"label": label, "reason": "Set by a person", "by": by, "intents": [label], "needs_review": False} if label else await classify(text)
    label = cls["label"]
    if cls.get("needs_review"):
        return await _hold_for_review(contact, text, cls, source=source, meta=meta)
    company = await _db().companies.find_one({"id": contact.get("company_id")}, {"_id": 0}) if contact.get("company_id") else None
    enr = await _db().enrollments.find_one({"contact_id": contact["id"]}, {"_id": 0, "steps": 0}, sort=[("created_at", -1)])
    rec = {"id": str(uuid.uuid4()), "contact_id": contact["id"], "company_id": contact.get("company_id"), "email": contact.get("email"), "enrollment_id": (enr or {}).get("id"),
           "sequence_id": (enr or {}).get("sequence_id"), "variant": (enr or {}).get("variant"), "step": (enr or {}).get("step_index"), "label": label, "reason": cls.get("reason"),
           "classified_by": cls.get("by"), "intents": cls.get("intents"), "confidence": cls.get("confidence"), "text": (text or "")[:5000], "source": source,
           "meta": meta or {}, "received_at": now_iso()}
    first = not await _db().reply_log.count_documents({"contact_id": contact["id"]})
    await _db().reply_log.insert_one(dict(rec))
    if first:  # the first reply proves the address reaches a person, and says whether it's the right one
        import data_quality as dq
        await score_email_source(contact, True, "reply")
        if label in ("referral", "interested", "meeting", "objection", "not_interested"):
            await dq.log("contact", "current_role", contact.get("source") or "unknown", label != "referral", how="reply", contact_id=contact["id"],
                         company_id=contact.get("company_id"))
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
                rs.companies_changed()
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
        async for e in _db().enrollments.find({"contact_id": contact["id"], "status": {"$in": ACTIVE}}, {"_id": 0, "id": 1, "status": 1, "paused_from": 1}):
            # Keep where it was paused from: an unapproved draft must come back to the approval queue, not send.
            src = e.get("paused_from") if e["status"] == "paused" else e["status"]
            await _set_status(e, "paused", "echolocate", "Objection: re-engage in 90 days", {"resume_at": resume, "paused_from": src})
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


async def _hold_for_review(contact: dict, text: str, cls: dict, *, source: str, meta: Optional[dict]) -> dict:
    """Mixed or uncertain reply: pause every sequence for this person and ask a human the same day (C-07)."""
    enr = await _db().enrollments.find_one({"contact_id": contact["id"]}, {"_id": 0, "steps": 0}, sort=[("created_at", -1)])
    rec = {"id": str(uuid.uuid4()), "contact_id": contact["id"], "company_id": contact.get("company_id"), "email": contact.get("email"), "enrollment_id": (enr or {}).get("id"),
           "sequence_id": (enr or {}).get("sequence_id"), "variant": (enr or {}).get("variant"), "step": (enr or {}).get("step_index"), "label": "needs_review",
           "suggested": cls["label"], "intents": cls.get("intents"), "confidence": cls.get("confidence"), "reason": cls.get("reason"), "classified_by": cls.get("by"),
           "text": (text or "")[:5000], "source": source, "meta": meta or {}, "received_at": now_iso(), "review": {"status": "open"}}
    await _db().reply_log.insert_one(dict(rec))
    async for e in _db().enrollments.find({"contact_id": contact["id"], "status": {"$in": ["scheduled", "active"]}}, {"_id": 0, "id": 1}):
        await _set_status(e, "paused", "echolocate", "Reply needs a person to read it", {"resume_at": None})
    who = f"{contact.get('name') or contact.get('email')} ({contact.get('title') or 'unknown title'})"
    await ops.notify("replies", f":eyes: Reply from {who} needs a person: it reads as {', '.join(cls.get('intents') or [cls['label']])}. Outbound > Review.", event="reply_review")
    rec.pop("_id", None)
    return rec


async def resolve_review(reply_id: str, label: str, by: str) -> dict:
    rec = await _db().reply_log.find_one({"id": reply_id, "label": "needs_review"}, {"_id": 0})
    if not rec:
        raise HTTPException(status_code=404, detail="Nothing waiting for review with that id")
    if label not in REPLY_LABELS:
        raise HTTPException(status_code=422, detail=f"label must be one of {', '.join(REPLY_LABELS)}")
    contact = await _db().contacts.find_one({"id": rec["contact_id"]}, {"_id": 0})
    if not contact:
        raise HTTPException(status_code=404, detail="Contact no longer exists")
    done = await handle_reply(contact, rec.get("text") or "", source="review", label=label, by=by, meta={"review_of": reply_id})
    await _db().reply_log.update_one({"id": reply_id}, {"$set": {"review": {"status": "resolved", "label": label, "by": by, "at": now_iso()}, "superseded_by": done["id"]}})
    return done


async def on_account_status(company: dict, user: dict) -> None:
    if company.get("status") in ("not_a_fit", "meeting_booked", "won"):
        async for c in _db().contacts.find({"company_id": company["id"]}, {"_id": 0, "id": 1}):
            await stop_contact(c["id"], "stopped", f"Account marked {rm.STATUS_LABELS.get(company['status'])} by {user.get('email')}")


# --- Learning --------------------------------------------------------------------------------------

async def learn() -> dict:
    """Weekly re-weight of signal types from real outcomes. Needs 20+ closed outcomes, else keeps the defaults."""
    async with ops.run("learn") as run:
        positive = {"replied", "meeting_booked", "opportunity", "won"}
        closed = await _db().companies.find({"status": {"$in": list(positive | {"contacted", "not_a_fit", "lost"})}}, {"_id": 0, "id": 1, "status": 1, "signal_types": 1}).to_list(20000)
        # "Contacted, no reply" only counts as a no once the sequence has had time to work: an account emailed
        # yesterday isn't a negative outcome yet, and counting it would teach the scoring that fresh signals fail.
        mature = (_now() - timedelta(days=int(os.environ.get("LEARN_MATURE_DAYS", "21")))).isoformat()
        first_sends = {r["_id"]: r["first"] async for r in _db().sends.aggregate([{"$match": {"company_id": {"$ne": None}}},
                                                                                   {"$group": {"_id": "$company_id", "first": {"$min": "$sent_at"}}}])}
        outcomes = [c for c in closed if c["status"] != "contacted" or (first_sends.get(c.get("id")) or "9999") <= mature]
        run["counts"]["immature_skipped"] = len(closed) - len(outcomes)
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


def ab_sample_size(baseline_pct: float, lift: float, alpha: float = 0.05, power: float = 0.8) -> int:
    """Contacts per variant needed to detect `lift` x the baseline positive-reply rate (two-proportion test).
    At a 2% baseline, detecting a doubling needs about 1,140 per variant: 50 sends can't decide anything."""
    from statistics import NormalDist
    p1 = max(1e-4, baseline_pct / 100)
    p2 = min(0.99, p1 * lift)
    za, zb = NormalDist().inv_cdf(1 - alpha / 2), NormalDist().inv_cdf(power)
    pbar = (p1 + p2) / 2
    n = (za * (2 * pbar * (1 - pbar)) ** 0.5 + zb * (p1 * (1 - p1) + p2 * (1 - p2)) ** 0.5) ** 2 / (p2 - p1) ** 2
    return int(n) + 1


def ab_significant(n1: int, x1: int, n2: int, x2: int, alpha: float = 0.05) -> bool:
    from statistics import NormalDist
    if not n1 or not n2:
        return False
    p = (x1 + x2) / (n1 + n2)
    se = (p * (1 - p) * (1 / n1 + 1 / n2)) ** 0.5
    if se == 0:
        return False
    z = abs(x1 / n1 - x2 / n2) / se
    return 2 * (1 - NormalDist().cdf(z)) < alpha


async def ab_update() -> None:
    """Pre-declared test: variants alternate until each has the sample size declared up front, then a winner is
    named only if the difference is significant; otherwise the control (A) carries on. No peeking: nothing is
    decided early on a lucky streak."""
    settings = await compliance.outbound_settings()
    need = max(int(settings.get("ab_min_sends") or 50), ab_sample_size(float(settings.get("ab_baseline_pct") or 2.0), float(settings.get("ab_lift") or 2.0)))
    async for seq in _db().sequences.find({"ab_winner": None}, {"_id": 0, "id": 1, "name": 1}):
        stats = {}
        for v in ("A", "B"):
            sends = await _db().sends.count_documents({"sequence_id": seq["id"], "variant": v, "step": 1, "status": {"$in": ["sent", "delivered", "handed_off"]}})
            pos = await _db().reply_log.count_documents({"sequence_id": seq["id"], "variant": v, "label": {"$in": list(POSITIVE)}, "superseded_by": {"$exists": False}})
            stats[v] = (sends, pos)
        await _db().sequences.update_one({"id": seq["id"]}, {"$set": {"ab_needed": need, "ab_progress": {k: {"sends": n, "positive": x} for k, (n, x) in stats.items()}}})
        (na, xa), (nb, xb) = stats["A"], stats["B"]
        if na < need or nb < need:
            continue
        rates = {k: (x / n if n else 0) for k, (n, x) in stats.items()}
        if ab_significant(na, xa, nb, xb):
            winner, verdict = ("A" if rates["A"] > rates["B"] else "B"), "significant"
        else:
            winner, verdict = "A", "no_difference"
        await _db().sequences.update_one({"id": seq["id"]}, {"$set": {"ab_winner": winner, "ab_verdict": verdict, "ab_decided_at": now_iso(),
                                                                      "ab_stats": {k: {"sends": n, "positive_rate": round(rates[k] * 100, 2)} for k, (n, _) in stats.items()}}})
        await ops.notify("pipeline", f"A/B test on {seq.get('name')}: " + (f"variant {winner} wins (significant)." if verdict == "significant"
                                                                           else "no significant difference; staying with A."), event="ab_winner")


# --- KPIs ---------------------------------------------------------------------------------------------

async def kpis(days: int = 30) -> dict:
    db = _db()
    since = (_now() - timedelta(days=days)).isoformat()
    week = (_now() - timedelta(days=7)).isoformat()
    sends, replies, seq_rows, runs, status = await asyncio.gather(
        db.sends.find({"sent_at": {"$gte": since}}, {"_id": 0, "sequence_id": 1, "variant": 1, "bounced": 1, "status": 1, "step": 1}).to_list(100000),
        db.reply_log.find({"received_at": {"$gte": since}, "superseded_by": {"$exists": False}}, {"_id": 0, "label": 1, "sequence_id": 1, "variant": 1, "received_at": 1}).to_list(100000),
        db.sequences.find({}, {"_id": 0, "id": 1, "name": 1}).to_list(None),
        db.ops_runs.find({"job": "radar_daily", "status": "ok"}, {"_id": 0, "started_at": 1}).sort("started_at", -1).to_list(60),
        db.enrollments.aggregate([{"$group": {"_id": "$status", "n": {"$sum": 1}}}]).to_list(None))
    delivered = [s for s in sends if s.get("status") in ("sent", "handed_off", "delivered", "simulated")]
    bounced = sum(1 for s in sends if s.get("bounced"))
    positive = [r for r in replies if r["label"] in POSITIVE]
    by_seq: Dict[str, dict] = {}
    names = {s["id"]: s["name"] for s in seq_rows}
    for s in delivered:
        row = by_seq.setdefault(f"{s['sequence_id']}|{s.get('variant')}", {"sequence": names.get(s["sequence_id"], "?"), "variant": s.get("variant"), "sends": 0, "replies": 0, "positive": 0})
        row["sends"] += 1
    for r in replies:
        row = by_seq.setdefault(f"{r.get('sequence_id')}|{r.get('variant')}", {"sequence": names.get(r.get("sequence_id"), "Logged by hand"), "variant": r.get("variant"), "sends": 0, "replies": 0, "positive": 0})
        row["replies"] += 1
        row["positive"] += r["label"] in POSITIVE
    days_ok = sorted({r["started_at"][:10] for r in runs}, reverse=True)
    streak, d = 0, _now().date()
    for i in range(0, 60):
        day = (d - timedelta(days=i)).isoformat()
        if day in days_ok:
            streak += 1
        elif i > 0:
            break
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
    total, items = await asyncio.gather(_db().contacts.count_documents(query),
                                        _db().contacts.find(query, {"_id": 0}).sort("created_at", -1).skip((page - 1) * page_size).limit(page_size).to_list(page_size))
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
    # Country policy inputs (country_policy.py): a person's role-relevance decision and a consent record.
    role_relevant: Optional[bool] = None
    role_relevance_basis: Optional[str] = Field(default=None, max_length=300)
    consent_wording: Optional[str] = Field(default=None, max_length=1000)
    consent_channel: Optional[str] = Field(default=None, max_length=40)
    consent_proof: Optional[str] = Field(default=None, max_length=500)


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
    rr, basis = sets.pop("role_relevant", None), sets.pop("role_relevance_basis", None)
    if rr is not None:
        if rr and not basis:
            raise HTTPException(status_code=422, detail="Say why the pitch relates to this person's job (role_relevance_basis): it's the logged basis France, Ireland, Sweden and Finland require")
        sets["role_relevance"] = {"relevant": rr, "basis": basis or "Marked not relevant", "set_by": user["email"], "at": now_iso()}
    wording, channel, proof = sets.pop("consent_wording", None), sets.pop("consent_channel", None), sets.pop("consent_proof", None)
    if wording or proof:
        if not (wording and proof):
            raise HTTPException(status_code=422, detail="A consent record needs the wording they agreed to and the proof (a link or reference)")
        sets["consent"] = {"at": now_iso(), "wording": wording, "channel": channel or "email", "proof": proof, "recorded_by": user["email"]}
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


@router.get("/contacts/{contact_id}/brief")
async def contact_brief(contact_id: str):
    """The first-touch plan for this person (messaging.py) and the email it would produce, before anyone enrols them."""
    c = await _db().contacts.find_one({"id": contact_id}, {"_id": 0})
    if not c:
        raise HTTPException(status_code=404, detail="Contact not found")
    company = await _db().companies.find_one({"id": c.get("company_id")}, {"_id": 0}) if c.get("company_id") else None
    op = await write_opener(c, company)
    b = op["brief"]
    settings = await compliance.outbound_settings()
    ctx = {"first_name": c.get("first_name") or "there", "company": (company or {}).get("name") or c.get("company") or "your team", "opener": op["opener"],
           "rep_name": (company or {}).get("owner_name") or settings.get("sender_name") or "Solix", "pain": b["pain"], "outcome": b["outcome"],
           "product": b.get("product_label") or "Solix", "resource_link": b["resource_link"], "explore_offer": b["explore_offer"], "explore_cta": b["explore_cta"],
           "buy_cta": b["buy_cta"], "industry": (b.get("industry_label") or "your industry").lower(), "team": b["team"]}
    steps = render_steps(PERSONA_SEQUENCE, "A", ctx)
    return {"brief": b, "opener": op["opener"], "provider": op["provider"], "quality": op["quality"],
            "steps": [{**st, **STEP_OBJECTS[i]} for i, st in enumerate(steps) if i < len(STEP_OBJECTS)]}


@router.post("/contacts/{contact_id}/verify", dependencies=[can_work])
async def verify_contact(contact_id: str):
    c = await _db().contacts.find_one({"id": contact_id}, {"_id": 0})
    if not c or not c.get("email"):
        raise HTTPException(status_code=422, detail="This contact has no email address to verify")
    return await set_verification(c)


@router.post("/contacts/{contact_id}/find-phone", dependencies=[can_work])
async def find_phone(contact_id: str, user: dict = Depends(get_current_admin)):
    """A direct line or mobile for this person: numbers the company publishes next to their name first (free), then a
    verified mobile from Prospeo (10 of its 75 monthly credits, charged only when one is found). Also refreshes their
    title, and flags a job change, from the same lookup."""
    import radar_sources
    c = await _db().contacts.find_one({"id": contact_id}, {"_id": 0})
    if not c:
        raise HTTPException(status_code=404, detail="Contact not found")
    company = await _db().companies.find_one({"id": c.get("company_id")}, {"_id": 0}) if c.get("company_id") else None
    if not (company and company.get("domains")) and not c.get("linkedin_url"):
        raise HTTPException(status_code=422, detail="Add the account's web domain (Account → Details) or the person's LinkedIn URL first")
    problems: List[str] = []
    async with httpx.AsyncClient(follow_redirects=True) as client:
        hit, via = await radar_sources.find_phone(client, c, company or {}, problems=problems)
        board = None
        if not hit and company:  # no personal number: offer the company's main line, clearly labelled, never as theirs
            board, _ = await radar_sources.switchboard(client, company, problems=problems)
            if board and not company.get("phone_hq"):
                await _db().companies.update_one({"id": company["id"]}, {"$set": {"phone_hq": board["phone"]}})
    if not hit:
        ready = await _phone_ready()
        missing = [n for n, k in (("prospeo_mobile", "PROSPEO_API_KEY"), ("tomba_phone", "TOMBA_KEY"), ("apollo_mobile", "APOLLO_API_KEY + PUBLIC_API_URL")) if n not in ready]
        return {"found": None, "switchboard": (board or {}).get("phone"), "problems": problems,
                "detail": "No personal mobile or direct line found for this person." + (f" The company's switchboard is {board['phone']}." if board else "") +
                          (f" More sources would help: {', '.join(missing)}." if missing else " The free allowances may be used for this month.")}
    if hit.get("type") == "pending":  # Apollo answers by webhook
        upd = {"phone_pending": via, "phone_requested_at": now_iso()}
        if hit.get("title") and hit["title"] != c.get("title"):
            upd.update({"title": hit["title"], "persona": persona_of(hit["title"]), "seniority": seniority_of(hit["title"]), "title_source": via})
        await _db().contacts.update_one({"id": contact_id}, {"$set": upd})
        return {"found": None, "pending": True, "via": via, "detail": "Apollo is looking up their mobile; it appears on the contact within a few minutes."}
    sets = {"phone": hit["phone"], "phone_type": hit.get("type"), "phone_status": hit.get("status"), "phone_source": via, "phone_source_url": hit.get("source_url"),
            "phone_found_at": now_iso(), "updated_at": now_iso()}
    notes = []
    if hit.get("title") and hit["title"] != c.get("title"):
        sets.update({"title": hit["title"], "persona": persona_of(hit["title"]), "seniority": seniority_of(hit["title"]), "title_source": via})
        notes.append(f"title updated to {hit['title']}")
    if hit.get("email") and not c.get("email"):
        sets.update({"email": hit["email"], "email_status": "valid", "source_email": via, "verified_at": now_iso(), "verify_detail": f"{via} (verified)"})
        notes.append("verified email added")
    if hit.get("job_change_at"):
        sets["job_change_at"] = hit["job_change_at"]
    await _db().contacts.update_one({"id": contact_id}, {"$set": sets})
    if company and hit.get("phone_hq") and not company.get("phone_hq"):
        await _db().companies.update_one({"id": company["id"]}, {"$set": {"phone_hq": hit["phone_hq"]}})
    if company:
        await rs.add_history(company["id"], user.get("email", "system"), "contact", f"{c.get('name')}: {hit.get('type') or 'phone'} found via {via}" +
                             (f"; {', '.join(notes)}" if notes else ""))
    return {"found": hit["phone"], "type": hit.get("type"), "via": via, "notes": notes}


async def _phone_ready() -> List[str]:
    import providers as pv
    return await pv.available("phone_find")


@webhooks.post("/apollo-phones")
async def apollo_phones_webhook(request: Request, c: str = Query(...), t: str = Query(...)):
    """Apollo's asynchronous phone reveal (radar_sources._phone_apollo). Signed per contact, so only our requests land."""
    import hmac as _hmac
    import radar_sources
    if not _hmac.compare_digest(radar_sources.apollo_phone_token(c), t):
        raise HTTPException(status_code=401, detail="Invalid token")
    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    contact = await _db().contacts.find_one({"id": c}, {"_id": 0})
    if not contact:
        return {"ok": True, "matched": False}
    best = radar_sources.apollo_best_number(payload)
    if not best:
        await _db().contacts.update_one({"id": c}, {"$set": {"phone_pending": None, "phone_lookup_note": "Apollo found no mobile or direct dial"}})
        return {"ok": True, "found": False}
    await _db().contacts.update_one({"id": c}, {"$set": {"phone": best["phone"], "phone_type": best["type"], "phone_status": "verified", "phone_source": "apollo_mobile",
                                                     "phone_found_at": now_iso(), "phone_pending": None, "phone_dnc": best["dnc"], "phone_dnc_status": best["dnc_status"]}})
    if contact.get("company_id"):
        await rs.add_history(contact["company_id"], "apollo", "contact", f"{contact.get('name')}: {best['type']} found via Apollo" + (" (on a Do Not Call list)" if best["dnc"] else ""))
    return {"ok": True, "found": True}


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
    import radar_intel
    import radar_sources
    via = "hunter"
    pattern = await learned_pattern(domain, company)
    if pattern and c.get("first_name") and c.get("last_name") and radar_sources.mailbox_verifier_ready():
        cand = guess_emails(c["first_name"], c["last_name"], domain, pattern)[0]
        v = await verify_email(cand)
        tried.append({"email": cand, **v, "detail": f"Company pattern {pattern}: {v['detail']}"})
        if v["status"] == "valid":
            await _db().contacts.update_one({"id": contact_id}, {"$set": {"email": cand, "email_status": "valid", "verify_detail": f"Company pattern {pattern}, verified",
                                                                        "verified_at": now_iso(), "source_email": "pattern+verify"}})
            return {"found": cand, "tried": tried}
    async with httpx.AsyncClient() as client:
        try:
            hit = await radar_intel.hunter_email(client, domain, c.get("first_name"), c.get("last_name"))
        except (httpx.HTTPError, ValueError):
            hit = None
        if not hit and c.get("first_name") and c.get("last_name"):  # Hunter had nothing or is out of searches: the other finders
            hit, via = await radar_sources.find_email(client, domain, c["first_name"], c["last_name"], skip={"hunter_find"})
    if hit:
        label = {"hunter": "Hunter", "tomba_find": "Tomba", "snov_find": "Snov.io", "prospeo": "Prospeo", "skrapp": "Skrapp"}.get(via, via)
        v = await verify_email(hit["email"])
        status = "valid" if hit.get("status") == "valid" or v["status"] == "valid" else v["status"]
        detail = f"{label} (confidence {hit.get('score')})" if hit.get("score") else label
        await _db().contacts.update_one({"id": contact_id}, {"$set": {"email": hit["email"], "email_status": status, "verified_at": now_iso() if status == "valid" else None,
                                                                    "verify_detail": detail, "source_email": "hunter" if via == "hunter" else via,
                                                                    "email_source_url": hit.get("source_url")}})
        if v["status"] in ("valid", "invalid"):  # the finder's answer checked by an independent verifier
            await score_email_source({**c, "source_email": "hunter" if via == "hunter" else via}, v["status"] == "valid", "verification")
        return {"found": hit["email"], "tried": [{"email": hit["email"], "status": status, "detail": f"{label} Email Finder"}]}
    for cand in [x for x in guess_emails(c.get("first_name"), c.get("last_name"), domain, pattern) if x not in {t["email"] for t in tried}][:6]:
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


@router.post("/contacts/{contact_id}/enrich-seamless", dependencies=[can_work])
async def enrich_seamless(contact_id: str, user: dict = Depends(get_current_admin)):
    """Research one contact on Seamless.AI and keep only the fields the gates need, tagged by vendor."""
    import seamless
    if not seamless.ready():
        raise HTTPException(status_code=422, detail="Set SEAMLESS_API_KEY on the server (needs Seamless's Public API licence).")
    c = await _db().contacts.find_one({"id": contact_id}, {"_id": 0})
    if not c:
        raise HTTPException(status_code=404, detail="Contact not found")
    company = await _db().companies.find_one({"id": c.get("company_id")}, {"_id": 0}) if c.get("company_id") else None
    try:
        found = await seamless.research(c, company)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    if found.get("pending"):
        return {"pending": True, "request_id": found["pending"], "contact": c}
    return {"pending": False, "contact": await seamless.apply(c, found, user["email"]), "found": {k: v for k, v in found.items() if k != "seamless_id"}}


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
    channel: str = Field(default="email", pattern="^(email|linkedin)$")
    dripify_campaign_id: Optional[str] = Field(default=None, max_length=80)  # a campaign built and switched on in Dripify's UI


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
    total, items = await asyncio.gather(_db().enrollments.count_documents(q),
                                        _db().enrollments.find(q, {"_id": 0}).sort("created_at", -1).skip((page - 1) * page_size).limit(page_size).to_list(page_size))
    return {"items": items, "total": total}


class EnrollmentPatch(BaseModel):
    action: Literal["approve", "reject", "pause", "resume", "stop", "edit"]
    steps: Optional[List[Dict[str, object]]] = None
    opener: Optional[str] = Field(default=None, max_length=600)
    reason: Optional[str] = Field(default=None, max_length=300)


@router.get("/enrollments/{enr_id}")
async def get_enrollment(enr_id: str):
    e = await _db().enrollments.find_one({"id": enr_id}, {"_id": 0})
    if not e:
        raise HTTPException(status_code=404, detail="Enrollment not found")
    return e


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
        if sets:
            # A person's edit is grounded like a model's draft: a wrong figure typed by hand is still wrong.
            import grounding
            ev = e.get("evidence") or {}
            text = " ".join([sets.get("opener", e.get("opener") or "")] + [st["body"] for st in sets.get("steps", [])[:1]])
            approved_templates = [st.get("subject", "") + " " + st.get("body", "") for st in e.get("steps") or []] + [e.get("opener") or ""]
            sets["grounding"] = grounding.check(text, prospect_evidence=ev.get("prospect") or [], solix_evidence=(ev.get("solix") or []) + approved_templates,
                                                company=e.get("company_name"), person=e.get("contact_name")) if ev else e.get("grounding")
            sets["edited_by"] = user["email"]
        await _set_status(e, e["status"], user["email"], "Edited", sets)
    elif body.action == "approve":
        if e["status"] != "pending_approval":
            raise HTTPException(status_code=409, detail="Only enrollments waiting for approval can be approved")
        c = await _db().contacts.find_one({"id": e["contact_id"]}, {"_id": 0}) or {}
        company = await _db().companies.find_one({"id": e.get("company_id")}, {"_id": 0}) if e.get("company_id") else None
        g = await compliance.gate(c, company, enrollment_id=e["id"])
        if not g["ok"]:
            raise HTTPException(status_code=422, detail={"message": "No longer passes the gate", "reasons": g["reasons"]})
        if (e.get("grounding") or {}).get("ok") is False:
            raise HTTPException(status_code=422, detail={"message": "A claim in this draft isn't supported by its sources. Edit it, then approve.",
                                                         "reasons": e["grounding"].get("problems") or []})
        # C-17: who approved which model's draft under which prompt, kept with the enrollment and every send.
        await _set_status(e, "scheduled", user["email"], "Approved", {"approved_by": user["email"], "approved_at": now_iso(), "next_send_at": now_iso(),
                                                                       "approval_record": {"by": user["email"], "at": now_iso(), "model": e.get("opener_model"),
                                                                                           "prompt_version": e.get("prompt_version"), "opener_provider": e.get("opener_provider")}})
    elif body.action == "reject":
        await _set_status(e, "stopped", user["email"], f"Rejected: {body.reason or 'no reason given'}")
    elif body.action == "pause":
        if e["status"] not in ("pending_approval", "scheduled", "active"):
            raise HTTPException(status_code=409, detail=f"A {e['status'].replace('_', ' ')} enrollment can't be paused")
        # Remember a pause taken before approval, so resuming returns it to the approval queue instead of sending.
        await _set_status(e, "paused", user["email"], body.reason or "Paused by hand", {"resume_at": None, "paused_from": e["status"]})
    elif body.action == "resume":
        # Only a paused enrollment resumes, and never past approval: stopped, rejected or waiting drafts go through
        # approve (which re-runs the compliance gate), not here.
        if e["status"] != "paused":
            raise HTTPException(status_code=409, detail="Only paused enrollments can be resumed")
        if e.get("paused_from") == "pending_approval":
            await _set_status(e, "pending_approval", user["email"], "Resumed: back in the approval queue", {"resume_at": None, "paused_from": None})
        else:
            await _set_status(e, "active" if e.get("first_sent_at") else "scheduled", user["email"], "Resumed",
                              {"next_send_at": now_iso(), "resume_at": None, "paused_from": None})
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


class ResolveIn(BaseModel):
    label: str = Field(max_length=20)


@router.post("/replies/{reply_id}/resolve", dependencies=[can_work])
async def resolve_reply(reply_id: str, body: ResolveIn, user: dict = Depends(get_current_admin)):
    return await resolve_review(reply_id, body.label, user["email"])


@router.get("/replies")
async def list_replies(label: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(50, le=200)):
    q = {"label": label} if label else {}
    total, items = await asyncio.gather(_db().reply_log.count_documents(q),
                                        _db().reply_log.find(q, {"_id": 0}).sort("received_at", -1).skip((page - 1) * page_size).limit(page_size).to_list(page_size))
    names = {c["id"]: c async for c in _db().contacts.find({"id": {"$in": [i["contact_id"] for i in items]}}, {"_id": 0, "id": 1, "name": 1, "company": 1, "title": 1, "lead_id": 1})}
    for i in items:
        i["contact"] = names.get(i["contact_id"])
    return {"items": items, "total": total}


@router.get("/sends")
async def list_sends(page: int = Query(1, ge=1), page_size: int = Query(50, le=200)):
    total, items = await asyncio.gather(_db().sends.count_documents({}), _db().sends.find({}, {"_id": 0}).sort("sent_at", -1).skip((page - 1) * page_size).limit(page_size).to_list(page_size))
    return {"items": items, "total": total}


@router.get("/review")
async def review_queue():
    """Replies a person must read (mixed or uncertain intent), oldest first."""
    items = await _db().reply_log.find({"label": "needs_review", "review.status": "open"}, {"_id": 0}).sort("received_at", 1).to_list(200)
    return {"items": items, "total": len(items)}


@router.get("/deliverability")
async def deliverability_health():
    import deliverability as dl
    settings = await compliance.outbound_settings()
    return {"domains": await dl.health(settings), "uncertain_sends": await dl.uncertain(),
            "thresholds": {k: settings.get(k) for k in ("bounce_warn_pct", "bounce_pause_pct", "complaint_warn_pct", "complaint_pause_pct", "health_min_sends")},
            "enroll_hold": settings.get("enroll_hold"), "enroll_hold_reason": settings.get("enroll_hold_reason"), "paused": settings.get("paused"),
            "linkedin_paused": settings.get("linkedin_paused"), "linkedin_pause_reason": settings.get("linkedin_pause_reason")}


class LedgerIn(BaseModel):
    email: Optional[str] = Field(default=None, max_length=200)
    linkedin_url: Optional[str] = Field(default=None, max_length=300)
    channel: str = Field(pattern="^(email|linkedin|phone|event)$")
    note: Optional[str] = Field(default=None, max_length=300)


@router.post("/ledger", dependencies=[can_work])
async def ledger_record(body: LedgerIn, user: dict = Depends(get_current_admin)):
    """Record a touch made outside MarkHub (a rep's own campaign), so automated outreach sees it (spec G9)."""
    import ledger
    if not (body.email or body.linkedin_url):
        raise HTTPException(status_code=422, detail="Give an email or a LinkedIn URL")
    person = {"email": body.email, "linkedin_url": body.linkedin_url}
    clash = await ledger.conflict(person, body.channel)
    return {"recorded": await ledger.record(person, body.channel, None, by=user["email"], note=body.note), "warning": clash}


@router.get("/ledger")
async def ledger_list(page: int = Query(1, ge=1), page_size: int = Query(50, le=200)):
    total = await _db().outreach_ledger.count_documents({})
    items = await _db().outreach_ledger.find({}, {"_id": 0, "touches": {"$slice": -5}}).sort("last_at", -1).skip((page - 1) * page_size).to_list(page_size)
    return {"items": items, "total": total}


@router.post("/ledger/{person_key}/release", dependencies=[can_work])
async def ledger_release(person_key: str, user: dict = Depends(get_current_admin)):
    import ledger
    if not await ledger.release(person_key, user["email"]):
        raise HTTPException(status_code=404, detail="Not in the ledger")
    return {"released": True}


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



@webhooks.post("/dripify")
async def dripify_webhook(request: Request, x_solix_webhook_secret: Optional[str] = Header(default=None)):
    """Dripify's per-campaign webhook (set in its UI, one trigger each): replies and connection events."""
    import hmac
    import linkedin
    secret = os.environ.get("DRIPIFY_WEBHOOK_SECRET")
    if not secret or not x_solix_webhook_secret or not hmac.compare_digest(secret, x_solix_webhook_secret):
        raise HTTPException(status_code=401, detail="Invalid webhook secret")
    try:
        p = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    lead = p.get("lead") or p
    url = lead.get("linkedinProfileUrl") or lead.get("profileUrl") or lead.get("linkedin_url") or ""
    event = (p.get("event") or p.get("trigger") or p.get("type") or "").lower()
    dedupe = hashlib.sha256("|".join(str(x or "") for x in (event, p.get("eventId") or p.get("timestamp"), lead.get("id"), url)).encode()).hexdigest()[:40]
    try:
        await _db().webhook_events.insert_one({"id": str(uuid.uuid4()), "dedupe_key": dedupe, "source": "dripify", "event": event, "received_at": now_iso()})
    except Exception as e:  # noqa: BLE001
        if "duplicate" in str(e).lower() or "E11000" in str(e):
            return {"ok": True, "duplicate": True}
        raise
    import ledger
    li = ledger.linkedin_id(url)
    contact = None
    if li:
        async for c in _db().contacts.find({"linkedin_url": {"$regex": re.escape(li), "$options": "i"}}, {"_id": 0}):
            contact = c
            break
    if not contact:
        return {"ok": True, "matched": False}
    if "repl" in event or "message" in event:
        text = p.get("message") or p.get("text") or ""
        if not text and lead.get("id") and linkedin.ready():
            try:
                text = await linkedin.conversation(lead["id"])  # one read, only for a lead whose webhook fired
            except Exception:  # noqa: BLE001
                text = ""
        await handle_reply(contact, text, source="dripify", meta={"event": event})
    return {"ok": True, "matched": True}


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
    step = p.get("step") or p.get("email_sequence_step")
    # Instantly's webhooks carry no event id and may be retried: derive one, and let a unique index drop repeats.
    dedupe = hashlib.sha256("|".join(str(x or "") for x in (event, p.get("timestamp"), p.get("campaign_id") or p.get("campaign"), email, step)).encode()).hexdigest()[:40]
    try:
        await _db().webhook_events.insert_one({"id": str(uuid.uuid4()), "dedupe_key": dedupe, "source": "instantly", "event": event,
                                               "email_hash": compliance.email_hash(email) if email else None, "received_at": now_iso(),
                                               "campaign": p.get("campaign_id") or p.get("campaign")})
    except Exception as e:  # noqa: BLE001 - duplicate key: already processed
        if "duplicate" in str(e).lower() or "E11000" in str(e):
            return {"ok": True, "duplicate": True}
        raise
    contact = await _db().contacts.find_one({"email": email}, {"_id": 0}) if email else None
    if not contact:
        if event in ("lead_unsubscribed",) and email:
            await compliance.suppress(email, "unsubscribed", source="instantly", by="instantly")
        return {"ok": True, "matched": False}
    enr = await _db().enrollments.find_one({"contact_id": contact["id"]}, {"_id": 0}, sort=[("created_at", -1)])
    account = p.get("email_account") or p.get("from_email") or ""
    sending_domain = account.rsplit("@", 1)[-1].lower() if "@" in account else None
    if event == "email_sent" and enr:
        # The hand-off row stands for step 1; later steps get their own row. One email, one row: rates stay honest.
        step_n = int(step) if str(step or "").isdigit() else None
        handoff = await _db().sends.find_one_and_update({"enrollment_id": enr["id"], "status": "handed_off"},
                                                       {"$set": {"status": "delivered", "step": step_n or 1, "sent_at": now_iso(), "sending_domain": sending_domain, "subject": p.get("email_subject")}})
        if not handoff:
            key = f"{enr['id']}:{step_n or 'x'}:instantly"
            await _db().sends.update_one({"idem_key": key}, {"$setOnInsert": {
                "id": str(uuid.uuid4()), "idem_key": key, "enrollment_id": enr["id"], "contact_id": contact["id"], "company_id": enr.get("company_id"),
                "sequence_id": enr["sequence_id"], "variant": enr.get("variant"), "step": step_n, "email": email, "subject": p.get("email_subject"),
                "provider": "instantly", "status": "delivered", "sent_at": now_iso(), "channel": "email", "sending_domain": sending_domain}}, upsert=True)
    elif event == "email_bounced":
        # Only the email that bounced counts as bounced: the latest one to this address, not every send ever made to them.
        latest = await _db().sends.find_one({"contact_id": contact["id"], "status": {"$in": ["sent", "delivered", "handed_off"]}}, {"_id": 0, "id": 1}, sort=[("sent_at", -1)])
        if latest:
            await _db().sends.update_one({"id": latest["id"]}, {"$set": {"bounced": True, "bounced_at": now_iso(), **({"sending_domain": sending_domain} if sending_domain else {})}})
        await _db().contacts.update_one({"id": contact["id"]}, {"$set": {"email_status": "invalid", "verify_detail": "Hard bounce"}})
        await score_email_source(contact, False, "bounce")
        await compliance.suppress(email, "bounced", source="instantly", by="instantly")
    elif event in ("email_complaint", "lead_marked_spam", "spam_complaint"):
        await compliance.suppress(email, "complaint", source="instantly", by="instantly")
        await _db().suppression.update_one({"email_hash": compliance.email_hash(email)}, {"$set": {"sending_domain": sending_domain}})
    elif event in ("reply_received", "lead_interested", "lead_meeting_booked", "lead_not_interested", "lead_out_of_office", "lead_wrong_person", "lead_unsubscribed"):
        text = p.get("reply_text") or p.get("reply_text_snippet") or ""
        label = {"lead_interested": "interested", "lead_meeting_booked": "meeting", "lead_not_interested": "not_interested", "lead_out_of_office": "ooo",
                 "lead_wrong_person": "referral", "lead_unsubscribed": "unsubscribe"}.get(event)
        recent = await _db().reply_log.find_one({"contact_id": contact["id"], "source": "instantly", "received_at": {"$gte": (_now() - timedelta(minutes=30)).isoformat()}},
                                                {"_id": 0}, sort=[("received_at", -1)])
        if label and recent:
            # Instantly labels the reply we already classified: note its view, and act again only to stop contact.
            await _db().reply_log.update_one({"id": recent["id"]}, {"$set": {"instantly_label": label}})
            if label in ("unsubscribe", "not_interested") and recent.get("label") not in ("unsubscribe", "not_interested"):
                await compliance.suppress(email, "unsubscribed" if label == "unsubscribe" else "not_interested", source="instantly-label", by="instantly")
            return {"ok": True, "duplicate": True}
        if event == "reply_received" or label:
            await handle_reply(contact, text, source="instantly", label=label, meta={"event": event, "unibox_url": p.get("unibox_url")})
    elif event in ("email_opened", "email_link_clicked") and enr:
        await _db().enrollments.update_one({"id": enr["id"]}, {"$inc": {"opens" if event == "email_opened" else "clicks": 1}})
    return {"ok": True, "matched": True}
