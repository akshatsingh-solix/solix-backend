"""Internal traffic: Solix's own people, identified everywhere and kept out of MarkHub's numbers.

Staff browse the site, test the forms, chat with Sol, register for events and sign up for trials. Left in, that
activity inflates visitors, leads, MQLs, chat counts, content views and Radar "visit" signals, and fires sales
alerts at the team about itself. This module recognises it, flags each record, and gives every metric one switch
to hide it (the default), show it, or show only it.

How a record is recognised (the first rule that matches is kept as the reason):

    email_domain  an address at an internal domain (solix.com, solixtechnologies.com, any subdomain)
    email         an address on the internal list (a contractor's or agency's own address)
    staff         a MarkHub user or a Settings > Team member
    ip            the request came from an internal network range (office or VPN egress, CIDR)
    network       the visitor's network lookup (radar_visitors.py) names an internal domain
    device        a browser marked internal: it signed in to MarkHub, or opened the site with ?solix_internal=1
    linked        the same person as an internal record (a lead's browsing, a visitor's chat, a lead's forms)
    manual        marked by hand in MarkHub; a manual "not internal" also wins over every rule

Each flagged record carries `internal: true` and `internal_reason: {kind, detail, at}`. The flag spreads across
everything that is the same person: visitor <-> events <-> lead <-> form submissions <-> event registrations <->
Sol conversations. IP addresses are matched in memory and never stored, the same as radar_visitors.py.

Every admin metric reads the `internal` query parameter (`exclude` | `include` | `only`, default from the
settings) through `view_mode`, and adds `scope(mode)` to its query.
"""
from __future__ import annotations

import ipaddress
import logging
import re
from collections import OrderedDict
from typing import Iterable, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

import cache
from auth import get_current_admin, require_roles
from database import now_iso

logger = logging.getLogger("solix.internal")

MODES = ("exclude", "include", "only")
Mode = Literal["exclude", "include", "only"]
KINDS = {
    "email_domain": "Internal email domain",
    "email": "Internal email address",
    "staff": "MarkHub user or team member",
    "ip": "Internal network (IP range)",
    "network": "Internal organisation network",
    "device": "Browser marked internal",
    "linked": "Same person as an internal record",
    "manual": "Marked by hand",
}
RULE_KINDS = {"email_domain", "email", "staff"}  # recomputed from the rules on every re-scan
DEFAULT_DOMAINS = ["solix.com", "solixtechnologies.com"]
_DOMAIN_RX = re.compile(r"^(?=.{3,253}$)([a-z0-9-]{1,63}\.)+[a-z]{2,63}$")


def _db():
    import database
    return database.db


# --- Settings -----------------------------------------------------------------------------------------------

class IpRange(BaseModel):
    cidr: str = Field(min_length=3, max_length=64)
    label: str = Field(default="", max_length=80)

    @field_validator("cidr")
    @classmethod
    def _cidr(cls, v: str) -> str:
        try:
            return str(ipaddress.ip_network(v.strip(), strict=False))
        except ValueError:
            raise ValueError(f"{v!r} is not an IP address or range (use 203.0.113.7 or 203.0.113.0/24)")


class InternalSettings(BaseModel):
    enabled: bool = True
    domains: List[str] = Field(default_factory=lambda: list(DEFAULT_DOMAINS), max_length=50)
    emails: List[str] = Field(default_factory=list, max_length=500)
    ip_ranges: List[IpRange] = Field(default_factory=list, max_length=200)
    include_staff: bool = True  # MarkHub users and Settings > Team members are internal wherever they appear
    flag_markhub_browsers: bool = True  # a browser that signs in to MarkHub is marked internal
    suppress_alerts: bool = True  # no sales alert email for internal form fills and chats
    keep_out_of_pipeline: bool = True  # internal leads are not linked to Radar accounts or handed to outbound
    default_view: Mode = "exclude"

    @field_validator("domains")
    @classmethod
    def _domains(cls, v: List[str]) -> List[str]:
        out: List[str] = []
        for d in v:
            d = re.sub(r"^(https?://)?(www\.)?|@", "", (d or "").strip().lower()).split("/")[0]
            if not d:
                continue
            if not _DOMAIN_RX.match(d):
                raise ValueError(f"{d!r} is not a domain (use solix.com)")
            if d not in out:
                out.append(d)
        return out

    @field_validator("emails")
    @classmethod
    def _emails(cls, v: List[str]) -> List[str]:
        out: List[str] = []
        for e in v:
            e = (e or "").strip().lower()
            if not e:
                continue
            if not re.match(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", e):
                raise ValueError(f"{e!r} is not an email address")
            if e not in out:
                out.append(e)
        return out


def _env_defaults() -> dict:
    import os
    base = InternalSettings().model_dump()
    doms = [d for d in (os.environ.get("INTERNAL_DOMAINS") or "").split(",") if d.strip()]
    nets = [n for n in (os.environ.get("INTERNAL_IP_RANGES") or "").split(",") if n.strip()]
    try:
        if doms:
            base["domains"] = InternalSettings(domains=doms).domains
        if nets:
            base["ip_ranges"] = [r.model_dump() for r in InternalSettings(ip_ranges=[{"cidr": n, "label": "INTERNAL_IP_RANGES"} for n in nets]).ip_ranges]
    except ValueError:
        logger.warning("INTERNAL_DOMAINS / INTERNAL_IP_RANGES has an invalid entry; using the built-in defaults")
    return base


async def load_settings() -> dict:
    async def load():
        doc = await _db().settings.find_one({"key": "internal_traffic"}, {"_id": 0}) or {}
        base = _env_defaults()
        merged = {**base, **{k: doc[k] for k in base if k in doc}}
        s = InternalSettings(**merged).model_dump()
        s["_nets"] = [ipaddress.ip_network(r["cidr"], strict=False) for r in s["ip_ranges"]]
        return s
    return await cache.memo("internal", "settings", 30, load)


async def staff_emails() -> frozenset:
    async def load():
        users = await _db().users.distinct("email")
        team = (await _db().settings.find_one({"key": "team"}, {"_id": 0, "members": 1}) or {}).get("members") or []
        return frozenset({(u or "").lower() for u in users} | {(m.get("email") or "").lower() for m in team}) - {""}
    return await cache.memo("internal", "staff", 60, load)


def _reason(kind: str, detail: str = "", by: Optional[str] = None) -> dict:
    r = {"kind": kind, "label": KINDS.get(kind, kind), "detail": (detail or "")[:200], "at": now_iso()}
    if by:
        r["by"] = by
    return r


def domain_matches(domain: str, domains: Iterable[str]) -> Optional[str]:
    domain = (domain or "").lower().strip(".")
    for d in domains:
        if domain == d or domain.endswith("." + d):
            return d
    return None


def match_email(email: Optional[str], s: dict, staff: frozenset = frozenset()) -> Optional[dict]:
    email = (email or "").strip().lower()
    if "@" not in email:
        return None
    if email in s["emails"]:
        return _reason("email", email)
    d = domain_matches(email.rsplit("@", 1)[1], s["domains"])
    if d:
        return _reason("email_domain", f"@{d}")
    if s["include_staff"] and email in staff:
        return _reason("staff", email)
    return None


def match_ip(ip: Optional[str], s: dict) -> Optional[dict]:
    if not ip or not s["_nets"]:
        return None
    try:
        addr = ipaddress.ip_address(ip.strip())
    except ValueError:
        return None
    for net, r in zip(s["_nets"], s["ip_ranges"]):
        if addr.version == net.version and addr in net:
            return _reason("ip", r.get("label") or r["cidr"])  # the rule, never the address
    return None


def client_ip(request: Request) -> str:
    """The client address, resolved by client_ip.py (configurable, not the spoofable first entry)."""
    import client_ip as _cip
    ip = _cip.client_ip(request)
    return "" if ip == "unknown" else ip


def locked_out(doc: Optional[dict]) -> bool:
    """Marked "not internal" by hand: no rule may flag it again."""
    return bool(doc) and doc.get("internal") is False and (doc.get("internal_reason") or {}).get("kind") == "manual"


# Sol conversations known to be internal, so each chat message is stamped without a database read.
_SESSIONS: "OrderedDict[str, dict]" = OrderedDict()


def _remember_session(sid: str, reason: dict) -> None:
    _SESSIONS[sid] = reason
    _SESSIONS.move_to_end(sid)
    while len(_SESSIONS) > 5000:
        _SESSIONS.popitem(last=False)


def session_internal(session_id: Optional[str]) -> bool:
    return bool(session_id) and session_id in _SESSIONS


async def check_session(session_id: str, *, ip: Optional[str] = None, visitor_id: Optional[str] = None) -> bool:
    """Called on every Sol message: is this conversation internal? Flags it (and its lead) the first time."""
    try:
        if session_id in _SESSIONS:
            return True
        reason = await classify(ip=ip, visitor_id=visitor_id, session_id=session_id)
        if reason:
            await mark_chat_session(session_id, reason)
            return True
    except Exception:  # noqa: BLE001 - never blocks a visitor's answer
        logger.exception("internal traffic check failed for chat %s", session_id)
    return False


async def classify(*, email: Optional[str] = None, ip: Optional[str] = None, visitor_id: Optional[str] = None,
                   session_id: Optional[str] = None, device: bool = False) -> Optional[dict]:
    """Why this activity is internal, or None. Cheap checks first; at most two indexed reads."""
    s = await load_settings()
    if not s["enabled"]:
        return None
    reason = match_email(email, s, await staff_emails() if s["include_staff"] and email else frozenset()) if email else None
    reason = reason or match_ip(ip, s)
    if not reason and device:
        reason = _reason("device", "Browser marked internal")
    if not reason and visitor_id:
        v = await _db().visitors.find_one({"id": visitor_id}, {"_id": 0, "internal": 1, "internal_reason": 1})
        if v and v.get("internal"):
            reason = _reason("linked", f"Visitor flagged: {(v.get('internal_reason') or {}).get('label') or 'internal'}")
    if not reason and session_id:
        reason = _SESSIONS.get(session_id)
        if reason is None:
            sub = await _db().submissions.find_one({"source_page": f"chat:{session_id}", "internal": True}, {"_id": 0, "internal_reason": 1})
            if sub:
                reason = sub.get("internal_reason") or _reason("linked")
                _remember_session(session_id, reason)
    return reason


async def stamp(doc: dict, *, ip: Optional[str] = None, visitor_id: Optional[str] = None, session_id: Optional[str] = None,
                device: bool = False) -> dict:
    """Flag a new submission or registration (before it is saved), unless its person was cleared by hand."""
    try:
        email = (doc.get("email") or "").lower()
        if email:
            lead = await _db().leads.find_one({"email": email}, {"_id": 0, "internal": 1, "internal_reason": 1})
            if locked_out(lead):
                return doc
            if lead and lead.get("internal"):
                doc["internal"], doc["internal_reason"] = True, _reason("linked", "Lead is internal")
                return doc
        reason = await classify(email=email, ip=ip, visitor_id=visitor_id, session_id=session_id, device=device)
        if reason:
            doc["internal"], doc["internal_reason"] = True, reason
    except Exception:  # noqa: BLE001 - flagging must never lose a form
        logger.exception("internal traffic check failed")
    return doc


# --- Propagation: one person, every record ------------------------------------------------------------------

_ACTIVE = {"$ne": True}


async def mark_visitor(visitor_id: str, reason: dict, *, _depth: int = 0) -> bool:
    db = _db()
    v = await db.visitors.find_one({"id": visitor_id}, {"_id": 0, "internal": 1, "internal_reason": 1, "lead_id": 1})
    if locked_out(v) or (v and v.get("internal")):
        return False
    await db.visitors.update_one({"id": visitor_id}, {"$set": {"internal": True, "internal_reason": reason}, "$setOnInsert": {"created_at": now_iso()}}, upsert=True)
    await db.events.update_many({"visitor_id": visitor_id, "internal": _ACTIVE}, {"$set": {"internal": True}})
    await db.sol_profiles.update_many({"visitor_id": visitor_id}, {"$set": {"internal": True}})
    linked = _reason("linked", f"Visitor flagged: {reason.get('label')}")
    async for sub in db.submissions.find({"visitor_id": visitor_id, "source": "chat"}, {"_id": 0, "source_page": 1}):
        await mark_chat_session((sub.get("source_page") or "")[5:], linked, _depth=_depth + 1)
    if v and v.get("lead_id") and _depth < 3:
        await mark_lead(v["lead_id"], linked, _depth=_depth + 1)
    cache.bump("reports")
    return True


async def mark_lead(lead_id: str, reason: dict, *, _depth: int = 0) -> bool:
    db = _db()
    lead = await db.leads.find_one({"id": lead_id}, {"_id": 0, "email": 1, "visitor_ids": 1, "internal": 1, "internal_reason": 1})
    if not lead or locked_out(lead) or lead.get("internal"):
        return False
    await db.leads.update_one({"id": lead_id}, {"$set": {"internal": True, "internal_reason": reason}})
    await _mark_person(lead.get("email"), _reason("linked", "Lead is internal"), _depth=_depth)
    if _depth < 3:
        for vid in lead.get("visitor_ids") or []:
            await mark_visitor(vid, _reason("linked", "Lead is internal"), _depth=_depth + 1)
    cache.bump("reports")
    return True


async def _mark_person(email: Optional[str], reason: dict, *, _depth: int = 0) -> None:
    """Every form, registration and conversation under this email address."""
    email = (email or "").lower()
    if not email:
        return
    db = _db()
    unlocked = {"$or": [{"internal_reason.kind": {"$ne": "manual"}}, {"internal": True}]}
    await db.submissions.update_many({"email": email, "internal": _ACTIVE, **unlocked}, {"$set": {"internal": True, "internal_reason": reason}})
    await db.event_registrations.update_many({"email": email, "internal": _ACTIVE, **unlocked}, {"$set": {"internal": True, "internal_reason": reason}})
    async for sub in db.submissions.find({"email": email, "source": "chat", "source_page": {"$regex": "^chat:"}}, {"_id": 0, "source_page": 1}):
        await mark_chat_session(sub["source_page"][5:], reason, _depth=_depth + 1)


async def mark_chat_session(session_id: str, reason: dict, *, _depth: int = 0) -> None:
    if not session_id:
        return
    db = _db()
    _remember_session(session_id, reason)
    await db.chat_messages.update_many({"session_id": session_id, "internal": _ACTIVE}, {"$set": {"internal": True}})
    await db.sol_profiles.update_many({"session_id": session_id}, {"$set": {"internal": True}})
    sub = await db.submissions.find_one({"source_page": f"chat:{session_id}"}, {"_id": 0, "id": 1, "internal": 1, "internal_reason": 1, "email": 1, "visitor_id": 1})
    if sub and not sub.get("internal") and not locked_out(sub):
        await db.submissions.update_one({"id": sub["id"]}, {"$set": {"internal": True, "internal_reason": reason}})
        if sub.get("email") and _depth < 3:
            lead = await db.leads.find_one({"email": sub["email"].lower()}, {"_id": 0, "id": 1})
            if lead:
                await mark_lead(lead["id"], _reason("linked", "Sol conversation is internal"), _depth=_depth + 1)


async def on_lead(lead: dict, sub: Optional[dict] = None) -> Optional[dict]:
    """After a lead is created or updated (intent.record_submission): flag it if anything says it is internal."""
    if not lead or locked_out(lead):
        return None
    if lead.get("internal"):
        if sub and sub.get("id") and not sub.get("internal"):  # a new form from a known internal person
            await _mark_person(lead.get("email"), _reason("linked", "Lead is internal"))
        return lead.get("internal_reason")
    reason = (sub or {}).get("internal_reason") if (sub or {}).get("internal") else None
    reason = reason or await classify(email=lead.get("email"))
    if not reason:
        for vid in lead.get("visitor_ids") or []:
            v = await _db().visitors.find_one({"id": vid}, {"_id": 0, "internal": 1, "internal_reason": 1})
            if v and v.get("internal"):
                reason = _reason("linked", f"Browsing flagged: {(v.get('internal_reason') or {}).get('label') or 'internal'}")
                break
    if reason:
        await mark_lead(lead["id"], reason)
        lead["internal"], lead["internal_reason"] = True, reason
    return reason


async def clear(kind: str, ident: str, *, internal: bool, by: str) -> dict:
    """Mark a lead, visitor or Sol conversation internal, or "not internal", by hand. Both are sticky."""
    db = _db()
    reason = _reason("manual", "Marked internal" if internal else "Marked not internal", by=by)
    if kind == "lead":
        lead = await db.leads.find_one({"id": ident}, {"_id": 0, "id": 1, "email": 1, "visitor_ids": 1})
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")
        if internal:
            await db.leads.update_one({"id": ident}, {"$set": {"internal": False, "internal_reason": None}})  # let mark_lead run
            await mark_lead(ident, reason)
        else:
            await db.leads.update_one({"id": ident}, {"$set": {"internal": False, "internal_reason": reason}})
            await _unmark_person(lead.get("email"), lead.get("visitor_ids") or [], reason)
    elif kind == "visitor":
        if internal:
            await db.visitors.update_one({"id": ident}, {"$set": {"internal": False, "internal_reason": None}})
            await mark_visitor(ident, reason)
        else:
            await db.visitors.update_one({"id": ident}, {"$set": {"internal": False, "internal_reason": reason}})
            await db.events.update_many({"visitor_id": ident}, {"$set": {"internal": False}})
    elif kind == "chat":
        sub = await db.submissions.find_one({"source_page": f"chat:{ident}"}, {"_id": 0, "id": 1})
        if internal:
            if sub:
                await db.submissions.update_one({"id": sub["id"]}, {"$set": {"internal": False, "internal_reason": None}})
            await mark_chat_session(ident, reason)
        else:
            _SESSIONS.pop(ident, None)
            await db.chat_messages.update_many({"session_id": ident}, {"$set": {"internal": False}})
            await db.sol_profiles.update_many({"session_id": ident}, {"$set": {"internal": False}})
            if sub:
                await db.submissions.update_one({"id": sub["id"]}, {"$set": {"internal": False, "internal_reason": reason}})
    elif kind == "submission":
        sub = await db.submissions.find_one({"id": ident}, {"_id": 0, "id": 1, "email": 1})
        if not sub:
            raise HTTPException(status_code=404, detail="Submission not found")
        lead = await db.leads.find_one({"email": (sub.get("email") or "").lower()}, {"_id": 0, "id": 1})
        if lead:
            return await clear("lead", lead["id"], internal=internal, by=by)
        await db.submissions.update_one({"id": ident}, {"$set": {"internal": internal, "internal_reason": reason}})
    else:
        raise HTTPException(status_code=400, detail="Unknown record type")
    cache.bump("reports")
    cache.bump("engagement")
    return {"kind": kind, "id": ident, "internal": internal, "internal_reason": reason}


async def _unmark_person(email: Optional[str], visitor_ids: List[str], reason: dict) -> None:
    db = _db()
    email = (email or "").lower()
    if email:
        await db.submissions.update_many({"email": email}, {"$set": {"internal": False, "internal_reason": reason}})
        await db.event_registrations.update_many({"email": email}, {"$set": {"internal": False, "internal_reason": reason}})
        async for sub in db.submissions.find({"email": email, "source": "chat", "source_page": {"$regex": "^chat:"}}, {"_id": 0, "source_page": 1}):
            sid = sub["source_page"][5:]
            _SESSIONS.pop(sid, None)
            await db.chat_messages.update_many({"session_id": sid}, {"$set": {"internal": False}})
    # Browsing that was only internal because of this lead; a browser flagged in its own right stays flagged.
    for vid in visitor_ids:
        res = await db.visitors.update_one({"id": vid, "internal_reason.kind": "linked"}, {"$set": {"internal": False, "internal_reason": None}})
        if res.modified_count:
            await db.events.update_many({"visitor_id": vid}, {"$set": {"internal": False}})


# --- Re-scan: apply today's rules to everything already recorded --------------------------------------------

async def rescan(limit: int = 50000) -> dict:
    """Flag history that matches the current rules, and clear rule-based flags that no longer match.

    IP, network and browser flags are kept: the address was never stored, so they can't be re-derived."""
    s = await load_settings()
    staff = await staff_emails() if s["include_staff"] else frozenset()
    db = _db()
    counts = {"leads_flagged": 0, "leads_cleared": 0, "submissions_flagged": 0, "registrations_flagged": 0, "visitors_flagged": 0, "chats_flagged": 0}

    def rule(email: Optional[str]) -> Optional[dict]:
        return match_email(email, s, staff) if s["enabled"] else None

    async for lead in db.leads.find({}, {"_id": 0, "id": 1, "email": 1, "visitor_ids": 1, "internal": 1, "internal_reason": 1}).limit(limit):
        if locked_out(lead):
            continue
        r = rule(lead.get("email"))
        kind = (lead.get("internal_reason") or {}).get("kind")
        if r and not lead.get("internal"):
            if await mark_lead(lead["id"], r):
                counts["leads_flagged"] += 1
        elif not r and lead.get("internal") and kind in RULE_KINDS:
            await db.leads.update_one({"id": lead["id"]}, {"$set": {"internal": False, "internal_reason": None}})
            await db.submissions.update_many({"email": lead["email"], "internal_reason.kind": {"$in": list(RULE_KINDS | {"linked"})}}, {"$set": {"internal": False, "internal_reason": None}})
            await db.event_registrations.update_many({"email": lead["email"], "internal_reason.kind": {"$in": list(RULE_KINDS | {"linked"})}}, {"$set": {"internal": False, "internal_reason": None}})
            counts["leads_cleared"] += 1
        elif not lead.get("internal") and s["enabled"]:
            for vid in lead.get("visitor_ids") or []:
                v = await db.visitors.find_one({"id": vid, "internal": True}, {"_id": 0, "internal_reason": 1})
                if v and await mark_lead(lead["id"], _reason("linked", f"Browsing flagged: {(v.get('internal_reason') or {}).get('label')}")):
                    counts["leads_flagged"] += 1
                    break

    if s["enabled"]:
        async for sub in db.submissions.find({"internal": _ACTIVE, "email": {"$nin": [None, ""]}}, {"_id": 0, "id": 1, "email": 1, "source_page": 1, "internal": 1, "internal_reason": 1}).limit(limit):
            r = rule(sub["email"])
            if r and not locked_out(sub):
                await db.submissions.update_one({"id": sub["id"]}, {"$set": {"internal": True, "internal_reason": r}})
                counts["submissions_flagged"] += 1
                if (sub.get("source_page") or "").startswith("chat:"):
                    await mark_chat_session(sub["source_page"][5:], r)
        async for reg in db.event_registrations.find({"internal": _ACTIVE}, {"_id": 0, "id": 1, "email": 1, "internal": 1, "internal_reason": 1}).limit(limit):
            r = rule(reg.get("email"))
            if r and not locked_out(reg):
                await db.event_registrations.update_one({"id": reg["id"]}, {"$set": {"internal": True, "internal_reason": r}})
                counts["registrations_flagged"] += 1
        async for v in db.visitors.find({"internal": _ACTIVE, "$or": [{"email": {"$nin": [None, ""]}}, {"org.domain": {"$nin": [None, ""]}}]},
                                        {"_id": 0, "id": 1, "email": 1, "org": 1, "internal": 1, "internal_reason": 1}).limit(limit):
            if locked_out(v):
                continue
            dom = domain_matches((v.get("org") or {}).get("domain") or "", s["domains"])
            r = rule(v.get("email")) or (_reason("network", (v.get("org") or {}).get("org") or dom) if dom else None)
            if r and await mark_visitor(v["id"], r):
                counts["visitors_flagged"] += 1
        # Events and chats of everything flagged, including flags set before this re-scan.
        internal_vids = [v["id"] async for v in db.visitors.find({"internal": True}, {"_id": 0, "id": 1})]
        for i in range(0, len(internal_vids), 500):
            await db.events.update_many({"visitor_id": {"$in": internal_vids[i:i + 500]}, "internal": _ACTIVE}, {"$set": {"internal": True}})
        async for sub in db.submissions.find({"internal": True, "source_page": {"$regex": "^chat:"}}, {"_id": 0, "source_page": 1, "internal_reason": 1}):
            sid = sub["source_page"][5:]
            res = await db.chat_messages.update_many({"session_id": sid, "internal": _ACTIVE}, {"$set": {"internal": True}})
            if res.modified_count:
                counts["chats_flagged"] += 1
            _remember_session(sid, sub.get("internal_reason") or _reason("linked"))
    for ns in ("reports", "engagement"):
        cache.bump(ns)
    await db.settings.update_one({"key": "internal_traffic"}, {"$set": {"last_rescan": {"at": now_iso(), **counts}}}, upsert=True)
    return counts


async def summary() -> dict:
    """How much internal traffic is on record, per kind of record and per reason."""
    db = _db()

    async def by_reason(coll) -> dict:
        rows = await coll.aggregate([{"$match": {"internal": True}}, {"$group": {"_id": "$internal_reason.kind", "n": {"$sum": 1}}}]).to_list(None)
        return {(r["_id"] or "linked"): r["n"] for r in rows}

    leads, visitors, subs, regs = (await by_reason(db.leads), await by_reason(db.visitors), await by_reason(db.submissions), await by_reason(db.event_registrations))
    chats = await db.chat_messages.aggregate([{"$match": {"internal": True}}, {"$group": {"_id": "$session_id"}}, {"$count": "n"}]).to_list(1)
    return {
        "leads": {"total": sum(leads.values()), "by_reason": leads},
        "visitors": {"total": sum(visitors.values()), "by_reason": visitors},
        "submissions": {"total": sum(subs.values()), "by_reason": subs},
        "registrations": {"total": sum(regs.values()), "by_reason": regs},
        "chats": {"total": chats[0]["n"] if chats else 0},
        "events": await db.events.count_documents({"internal": True}),
        "reasons": KINDS,
    }


# --- Filtering every metric -----------------------------------------------------------------------------------

def scope(mode: str) -> dict:
    """The query clause for a view mode: exclude (default) hides internal records, only shows nothing else."""
    if mode == "include":
        return {}
    if mode == "only":
        return {"internal": True}
    return {"internal": {"$ne": True}}


def scoped(query: Optional[dict], mode: str) -> dict:
    extra = scope(mode)
    if not extra:
        return dict(query or {})
    if not query:
        return extra
    return {"$and": [query, extra]}


async def view_mode(internal: Optional[str] = Query(None, description="Internal traffic: exclude, include or only")) -> str:
    """FastAPI dependency: the view mode a request asked for, else the admin's default."""
    if internal in MODES:
        return internal
    try:
        return (await load_settings())["default_view"]
    except Exception:  # noqa: BLE001 - a metric never fails over its filter
        return "exclude"


def is_internal(doc: Optional[dict]) -> bool:
    return bool(doc) and doc.get("internal") is True


async def should_alert(sub: dict) -> bool:
    if not is_internal(sub):
        return True
    return not (await load_settings())["suppress_alerts"]


async def keep_out_of_pipeline(lead: dict) -> bool:
    return is_internal(lead) and (await load_settings())["keep_out_of_pipeline"]


# --- Admin API ------------------------------------------------------------------------------------------------

router = APIRouter(prefix="/api/admin/internal", tags=["internal-traffic"], dependencies=[Depends(get_current_admin)])
admin_only = Depends(require_roles("admin"))


def _public(s: dict) -> dict:
    return {k: v for k, v in s.items() if not k.startswith("_")}


@router.get("")
async def get_internal(request: Request):
    s = await load_settings()
    doc = await _db().settings.find_one({"key": "internal_traffic"}, {"_id": 0, "last_rescan": 1}) or {}
    ip = client_ip(request)
    return {"settings": _public(s), "summary": await summary(), "last_rescan": doc.get("last_rescan"),
            "you": {"ip": ip, "match": match_ip(ip, s)}, "modes": list(MODES), "reasons": KINDS}


@router.put("", dependencies=[admin_only])
async def put_internal(body: InternalSettings, user: dict = Depends(get_current_admin)):
    await _db().settings.update_one({"key": "internal_traffic"}, {"$set": {**body.model_dump(), "updated_at": now_iso(), "updated_by": user["email"]}}, upsert=True)
    cache.bump("internal")
    cache.bump("reports")
    return _public(await load_settings())


@router.post("/rescan", dependencies=[admin_only])
async def post_rescan():
    cache.bump("internal")
    return await rescan()


class BrowserIn(BaseModel):
    visitor_id: Optional[str] = Field(default=None, min_length=8, max_length=64)
    internal: bool = True
    explicit: bool = False  # the person pressed the button (Settings), rather than the automatic sign-in mark


@router.post("/this-browser")
async def this_browser(body: BrowserIn, user: dict = Depends(get_current_admin)):
    """Called by MarkHub after sign-in (and from Settings): the browser a staff member uses is internal.

    Returns whether the browser should carry the internal mark, so the site's tracker tags its own beacons even
    before this browser has a visitor id (analytics consent comes later)."""
    s = await load_settings()
    mark_it = body.internal and s["enabled"] and (body.explicit or s["flag_markhub_browsers"])
    if body.visitor_id:
        if mark_it:
            await mark_visitor(body.visitor_id, _reason("device", f"Signed in to MarkHub as {user['email']}"))
        elif not body.internal and body.explicit:
            await clear("visitor", body.visitor_id, internal=False, by=user["email"])
    return {"internal": mark_it, "flag_markhub_browsers": s["flag_markhub_browsers"], "enabled": s["enabled"]}


class FlagIn(BaseModel):
    """Body of the per-record "internal / not internal" switches (leads, form inbox, Sol chats)."""
    internal: bool


async def ensure_indexes() -> None:
    db = _db()
    for coll in (db.leads, db.visitors, db.submissions, db.chat_messages, db.events, db.event_registrations):
        await coll.create_index("internal", sparse=True)
    await db.submissions.create_index("source_page")
    await db.submissions.create_index("visitor_id", sparse=True)
