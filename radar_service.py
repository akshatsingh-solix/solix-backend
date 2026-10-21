"""Radar data layer: companies, signals, research and per-viewer 'seen' state.

Rules that keep the data honest (carried over from the original Leads Radar):
* signals are upserted on a stable `dedupe_key`; a signal is never duplicated
  and its `first_seen_at` never moves;
* collectors never write the fields people own on a company (owner, status,
  notes) - only `patch_company` does, and every change goes into `history`;
* every number on a company is recomputed from its signals by `recompute`.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Iterable, List, Optional

import database
import ops
import radar_model as rm
import scoring
from database import now_iso

LEAD_STAGE_TO_STATUS = {"sql": "meeting_booked", "opportunity": "opportunity", "won": "won", "lost": "lost"}
STATUS_ORDER = ["new", "researching", "ready", "contacted", "replied", "meeting_booked", "opportunity", "won"]


def _db():
    return database.db


# --- Settings -----------------------------------------------------------------------

DEFAULT_RADAR = {
    "targets": [],            # [{company, domain, ats: greenhouse|lever|ashby|workday, board, workday_url}]
    "dns_domains": [],        # domains to fingerprint (the original Radar scanned 543)
    "collectors": {"ats": True, "crustdata": False, "dns": True, "news": True, "filings": True, "tenders": True},
    "credit_cap": 200,        # Crustdata credits per run
    "research_per_run": 5,
    "refresh_days": 30,
    "min_employees": 500,
    "countries": ["United States", "Canada", "United Kingdom"],
    "tender_countries": [],   # ISO3 codes for TED, empty = all EU
    "weights": {},            # learned multipliers per signal type (outbound.learn)
}


async def radar_settings() -> dict:
    doc = await _db().settings.find_one({"key": "radar"}, {"_id": 0}) or {}
    out = {**DEFAULT_RADAR, **{k: v for k, v in doc.items() if k in DEFAULT_RADAR}}
    out["collectors"] = {**DEFAULT_RADAR["collectors"], **(doc.get("collectors") or {})}
    return out


async def save_radar_settings(values: dict) -> dict:
    clean = {k: v for k, v in values.items() if k in DEFAULT_RADAR}
    await _db().settings.update_one({"key": "radar"}, {"$set": {**clean, "updated_at": now_iso()}}, upsert=True)
    return await radar_settings()


async def territories() -> dict:
    doc = await _db().settings.find_one({"key": "territories"}, {"_id": 0}) or {}
    out = {k: doc.get(k, v) for k, v in rm.DEFAULT_TERRITORIES.items()}
    return out


async def save_territories(values: dict) -> dict:
    clean = {k: values[k] for k in rm.DEFAULT_TERRITORIES if k in values}
    await _db().settings.update_one({"key": "territories"}, {"$set": {**clean, "updated_at": now_iso()}}, upsert=True)
    return await territories()


async def team_members() -> list:
    doc = await _db().settings.find_one({"key": "team"}, {"_id": 0}) or {}
    members = list(doc.get("members") or [])
    known = {m["email"] for m in members}
    async for u in _db().users.find({"role": {"$in": ["admin", "sales"]}, "disabled": {"$ne": True}}, {"_id": 0, "email": 1, "name": 1}):
        if u["email"] not in known:
            members.append({"name": u.get("name") or u["email"], "email": u["email"]})
    return members


def rep_email(rep: Optional[str], terr: dict, members: list) -> Optional[str]:
    if not rep:
        return None
    if (terr.get("rep_emails") or {}).get(rep):
        return terr["rep_emails"][rep].lower()
    for m in members:
        if (m.get("name") or "").strip().lower() == rep.strip().lower():
            return m["email"].lower()
    return None


# --- Companies ------------------------------------------------------------------------

async def get_or_create_company(name: str, *, domain: Optional[str] = None, hq: Optional[dict] = None, source: str = "radar") -> dict:
    name = (name or "").strip()
    key = rm.company_key(name)
    dom = rm.domain_of(domain) if domain else None
    company = None
    if dom:
        company = await _db().companies.find_one({"domains": dom}, {"_id": 0})
    if not company and key:
        company = await _db().companies.find_one({"$or": [{"key": key}, {"alias_keys": key}]}, {"_id": 0})
    if company:
        changes = {}
        if dom and dom not in (company.get("domains") or []):
            changes.setdefault("$addToSet", {})["domains"] = dom
        if name and name != company["name"] and key != company["key"]:
            changes.setdefault("$addToSet", {}).update({"aliases": name, "alias_keys": key})
        if hq and not (company.get("hq") or {}).get("country"):
            changes["$set"] = {"hq": {**(company.get("hq") or {}), **{k: v for k, v in hq.items() if v}}}
        if changes:
            await _db().companies.update_one({"id": company["id"]}, changes)
            company = await _db().companies.find_one({"id": company["id"]}, {"_id": 0})
            if "domains" in changes.get("$addToSet", {}):
                await link_by_domain(company, [dom])
        return company
    now = now_iso()
    company = {
        "id": str(uuid.uuid4()), "name": name or dom or "Unknown", "key": key or (dom or str(uuid.uuid4())), "domains": [dom] if dom else [], "aliases": [], "alias_keys": [],
        "hq": {k: v for k, v in (hq or {}).items() if v}, "revenue_band": None, "employees": None, "industry": None, "is_customer": False, "excluded_reason": None,
        "owner": None, "owner_name": None, "owner_basis": None, "territory": None, "route_basis": None, "status": "new", "notes": "", "history": [],
        "strength": "Unrated", "score": 0, "surge": False, "signal_types": [], "signal_count": 0, "signals_30d": 0, "competitors": [], "competitor_keys": [],
        "countries": [], "states": [], "lead_ids": [], "research_id": None, "research_score": None, "research_verdict": None, "research_status": None,
        "source": source, "created_at": now, "updated_at": now,
    }
    await _db().companies.insert_one(dict(company))
    if dom:
        await link_by_domain(company)
    return company


async def add_history(company_id: str, by: str, action: str, detail: str = "") -> None:
    await _db().companies.update_one({"id": company_id}, {"$push": {"history": {"$each": [{"at": now_iso(), "by": by, "action": action, "detail": detail[:500]}], "$slice": -100}}})


# --- Signals --------------------------------------------------------------------------

SIGNAL_FIELDS = ("type", "source", "source_ref", "evidence_url", "evidence_text", "title", "competitor_key", "competitor_product", "problem_term", "role_title",
                 "role_strength", "location", "state", "country", "zip", "posted_at", "person", "person_title", "action", "domain", "record_type", "record_value",
                 "buyer", "value", "public_sector", "meta")


async def upsert_signal(company: dict, sig: dict, run_id: Optional[str] = None) -> bool:
    """Insert a signal once; later sightings only refresh `last_seen_at`. Returns True when new."""
    key = sig.get("dedupe_key") or rm.dedupe_key(sig.get("type"), sig.get("source"), sig.get("source_ref") or sig.get("evidence_url") or f"{company['key']}|{sig.get('title')}|{sig.get('posted_at')}")
    now = now_iso()
    fields = {k: sig.get(k) for k in SIGNAL_FIELDS if sig.get(k) is not None}
    if fields.get("country"):
        fields["country"] = rm.country_norm(fields["country"])
    res = await _db().company_signals.update_one(
        {"dedupe_key": key},
        {"$set": {**fields, "company_id": company["id"], "last_seen_at": now},
         "$setOnInsert": {"id": str(uuid.uuid4()), "dedupe_key": key, "first_seen_at": sig.get("first_seen_at") or now, "created_at": now, "run_id": run_id}},
        upsert=True,
    )
    return res.upserted_id is not None


# --- Recompute --------------------------------------------------------------------------

async def _website_intent(company: dict) -> tuple[Optional[dict], list]:
    if not company.get("lead_ids") and not company.get("domains"):
        return None, []
    q = {"$or": [{"company_id": company["id"]}] + ([{"id": {"$in": company["lead_ids"]}}] if company.get("lead_ids") else [])}
    leads = await _db().leads.find(q, {"_id": 0, "id": 1, "score": 1, "product_scores": 1, "stage": 1, "email": 1, "owner": 1}).to_list(200)
    if not leads:
        return None, []
    scores: dict = {}
    for l in leads:
        for k, v in (l.get("product_scores") or {}).items():
            scores[k] = scores.get(k, 0) + v
    return {"score": max((l.get("score") or 0) for l in leads), "product_scores": scores}, leads


async def recompute(company_id: str, *, terr: Optional[dict] = None, members: Optional[list] = None, settings: Optional[dict] = None, half_life: Optional[float] = None) -> Optional[dict]:
    db = _db()
    company = await db.companies.find_one({"id": company_id}, {"_id": 0})
    if not company:
        return None
    terr = terr or await territories()
    members = members if members is not None else await team_members()
    settings = settings or await radar_settings()
    if half_life is None:
        half_life = (await db.settings.find_one({"key": "scoring"}, {"_id": 0}) or {}).get("half_life_days", scoring.DEFAULT_SETTINGS["half_life_days"])
    signals = await db.company_signals.find({"company_id": company_id}, {"_id": 0, "raw": 0}).to_list(1000)
    research = await db.radar_research.find_one({"company_id": company_id, "status": {"$in": ["approved", "draft"]}}, {"_id": 0}, sort=[("created_at", -1)])
    website, leads = await _website_intent(company)
    roll = rm.rollup(signals, half_life_days=half_life, weights=settings.get("weights") or None, website=website, research=research)
    update = {**roll, "countries": sorted({s["country"] for s in signals if s.get("country")}), "states": sorted({s["state"] for s in signals if s.get("state")}),
              "lead_ids": sorted({l["id"] for l in leads} | set(company.get("lead_ids") or [])), "recomputed_at": now_iso(), "updated_at": now_iso()}
    if research:
        update.update({"research_id": research["id"], "research_score": research.get("score"), "research_verdict": research.get("verdict"), "research_status": research.get("status"), "research_at": research.get("created_at")})
    # Routing: HQ when known, else the majority of the signals' locations. A person's choice wins.
    r = rm.route_company(company["name"], signals, company.get("hq"), terr)
    update.update({"territory": r["territory"], "route_basis": r["basis"], "route_rep": r["rep"]})
    if company.get("owner_basis") != "manual":
        email = rep_email(r["rep"], terr, members)
        update.update({"owner": email, "owner_name": r["rep"], "owner_basis": r["basis"]})
    # Exclusions: existing customers and accounts already in a live deal never get automated outreach.
    active = [l for l in leads if l.get("stage") in ("sal", "sql", "opportunity", "won")]
    reason = None
    if company.get("is_customer"):
        reason = "Existing customer"
    elif active:
        reason = f"Active deal in the lead pipeline ({active[0].get('stage', '').upper()})"
    if company.get("manual_exclusion"):
        reason = company["manual_exclusion"]
    update["excluded_reason"] = reason
    status = company.get("status") or "new"
    if status in rm.SYSTEM_STATUSES:
        if reason:
            status = "excluded"
        elif research and research.get("status") == "approved" and rm.verdict_class(research) == "ready":
            status = "ready"
        elif await db.radar_research.find_one({"company_id": company_id, "status": {"$in": ["queued", "running", "draft"]}}, {"_id": 1}):
            status = "researching"
        else:
            status = "new"
    rank = {s: i for i, s in enumerate(STATUS_ORDER)}
    for l in leads:  # downstream outcomes from the lead funnel move the account forward, never back
        target = LEAD_STAGE_TO_STATUS.get(l.get("stage"))
        if target in rank and rank[target] > rank.get(status, -1):
            status = target
    update["status"] = status
    update["stamp"] = rm.freshness_stamp({**company, **update})
    await db.companies.update_one({"id": company_id}, {"$set": update})
    await _sync_lead_owners({**company, **update}, leads)
    return {**company, **update}


async def _sync_lead_owners(company: dict, leads: list) -> None:
    """The account owner works its unowned website leads; a lead someone else owns is flagged, never moved."""
    owner = company.get("owner")
    if not owner or not leads:
        return
    unowned = [l["id"] for l in leads if not l.get("owner")]
    if unowned:
        entry = {"at": now_iso(), "by": "radar", "action": "owner", "detail": f"Owner → {owner} (owner of {company['name']} in the Radar)"}
        await _db().leads.update_many({"id": {"$in": unowned}, "owner": {"$in": [None, ""]}}, {"$set": {"owner": owner}, "$push": {"history": {"$each": [entry], "$slice": -50}}})
    for l in leads:
        if l.get("owner") and l["owner"] != owner and l.get("stage") not in ("lost", "disqualified"):
            await ops.notify("conflicts", f"{l.get('email')} is owned by {l['owner']}, but its account {company['name']} belongs to {owner}. Agree who works it.",
                             event="owner_conflict", key=f"conflict:{l['id']}:{l['owner']}:{owner}")


async def recompute_many(ids: Iterable[str]) -> int:
    terr, members, settings = await territories(), await team_members(), await radar_settings()
    half = (await _db().settings.find_one({"key": "scoring"}, {"_id": 0}) or {}).get("half_life_days", scoring.DEFAULT_SETTINGS["half_life_days"])
    n = 0
    for cid in set(ids):
        if await recompute(cid, terr=terr, members=members, settings=settings, half_life=half):
            n += 1
    return n


async def recompute_all() -> int:
    ids = [c["id"] async for c in _db().companies.find({}, {"_id": 0, "id": 1})]
    return await recompute_many(ids)


# --- People edits --------------------------------------------------------------------------

async def patch_company(company: dict, body: dict, user: dict) -> dict:
    sets, notes = {}, []
    if "owner" in body and body["owner"] is not None:
        owner = (body["owner"] or "").strip().lower() or None
        if owner != company.get("owner"):
            members = await team_members()
            sets.update({"owner": owner, "owner_name": next((m["name"] for m in members if m["email"] == owner), owner), "owner_basis": "manual" if owner else None})
            notes.append(("owner", f"Owner → {owner or 'unassigned'}"))
    if body.get("status") and body["status"] != company.get("status"):
        if body["status"] not in rm.STATUSES:
            raise ValueError("Unknown status")
        sets["status"] = body["status"]
        notes.append(("status", f"Status → {rm.STATUS_LABELS[body['status']]}" + (f" ({body['reason']})" if body.get("reason") else "")))
    if body.get("notes") is not None and body["notes"] != company.get("notes"):
        sets["notes"] = body["notes"][:5000]
        notes.append(("notes", "Notes updated"))
    for k in ("is_customer", "revenue_band", "employees", "industry"):
        if k in body and body[k] is not None and body[k] != company.get(k):
            sets[k] = body[k]
            notes.append((k, f"{k.replace('_', ' ').capitalize()} → {body[k]}"))
    if body.get("hq") is not None:
        sets["hq"] = {k: v for k, v in body["hq"].items() if k in ("country", "state", "zip", "location") and v}
        notes.append(("hq", "Headquarters updated"))
    if body.get("domains") is not None:
        sets["domains"] = sorted({d for d in (rm.domain_of(x) for x in body["domains"]) if d})
        notes.append(("domains", "Domains → " + ", ".join(sets["domains"])))
    if "manual_exclusion" in body:
        sets["manual_exclusion"] = (body["manual_exclusion"] or "").strip() or None
        notes.append(("exclusion", f"Excluded: {sets['manual_exclusion']}" if sets["manual_exclusion"] else "Exclusion removed"))
    if not sets:
        return company
    sets["updated_at"] = now_iso()
    await _db().companies.update_one({"id": company["id"]}, {"$set": sets})
    for action, detail in notes:
        await add_history(company["id"], user.get("email", "system"), action, detail)
    if sets.get("domains"):
        await link_by_domain({**company, **sets})
    return await recompute(company["id"])


# --- Leads <-> companies ---------------------------------------------------------------------

async def link_lead(lead: dict) -> Optional[str]:
    """Attach an inbound lead to its company by business email domain (or exact company name)."""
    if not lead or not lead.get("email"):
        return None
    dom = rm.domain_of(lead["email"])
    company = None
    if dom and dom not in scoring.FREE_EMAIL_DOMAINS:
        company = await _db().companies.find_one({"domains": dom}, {"_id": 0, "id": 1})
    if not company and lead.get("company"):
        key = rm.company_key(lead["company"])
        company = await _db().companies.find_one({"$or": [{"key": key}, {"alias_keys": key}]}, {"_id": 0, "id": 1}) if key else None
    if not company:  # the same person may already be an outbound contact on an account
        contact = await _db().contacts.find_one({"email": lead["email"].lower(), "company_id": {"$ne": None}}, {"_id": 0, "company_id": 1})
        company = {"id": contact["company_id"]} if contact else None
    if not company:
        return None
    if lead.get("company_id") != company["id"]:
        await _db().leads.update_one({"id": lead["id"]}, {"$set": {"company_id": company["id"]}})
        await _db().companies.update_one({"id": company["id"]}, {"$addToSet": {"lead_ids": lead["id"]}})
        await add_history(company["id"], "system", "lead", f"Website lead linked: {lead['email']}")
    await recompute(company["id"])
    return company["id"]


async def link_by_domain(company: dict, domains: Optional[List[str]] = None) -> int:
    """Attach website leads and outbound contacts that arrived before this account (or this domain) was known."""
    n = 0
    for dom in domains or company.get("domains") or []:
        if not dom or dom in scoring.FREE_EMAIL_DOMAINS:
            continue
        rx = {"$regex": f"@{re.escape(dom)}$", "$options": "i"}
        ids = [l["id"] async for l in _db().leads.find({"email": rx, "company_id": None}, {"_id": 0, "id": 1})]
        if ids:
            await _db().leads.update_many({"id": {"$in": ids}}, {"$set": {"company_id": company["id"]}})
            await _db().companies.update_one({"id": company["id"]}, {"$addToSet": {"lead_ids": {"$each": ids}}})
            n += len(ids)
        await _db().contacts.update_many({"email": rx, "company_id": None}, {"$set": {"company_id": company["id"], "company": company.get("name")}})
    if n:
        await add_history(company["id"], "system", "lead", f"{n} earlier website lead(s) linked by email domain")
    return n


async def on_lead_changed(lead: dict) -> None:
    if lead.get("company_id"):
        await recompute(lead["company_id"])


# --- Seen state -------------------------------------------------------------------------------

async def seen_map(user_id: str) -> dict:
    return {d["company_id"]: d.get("stamp") async for d in _db().radar_seen.find({"user_id": user_id}, {"_id": 0, "company_id": 1, "stamp": 1})}


def freshness(company: dict, seen: dict) -> str:
    if company["id"] not in seen:
        return "new"
    return "seen" if seen[company["id"]] == company.get("stamp") else "updated"


async def mark_seen(user_id: str, company_ids: Optional[List[str]] = None) -> int:
    q = {"id": {"$in": company_ids}} if company_ids else {}
    n = 0
    async for c in _db().companies.find(q, {"_id": 0, "id": 1, "stamp": 1}):
        await _db().radar_seen.update_one({"user_id": user_id, "company_id": c["id"]}, {"$set": {"stamp": c.get("stamp"), "seen_at": now_iso()}}, upsert=True)
        n += 1
    return n


# --- Import from the original Leads Radar ------------------------------------------------------

def parse_radar_payload(raw: bytes) -> dict:
    """Accepts radar.html (the page with its embedded JSON) or the bare JSON contract."""
    text = raw.decode("utf-8", "replace")
    m = re.search(r'<script[^>]*id="radar-data"[^>]*>(.*?)</script>', text, re.S)
    data = json.loads(m.group(1) if m else text)
    if not isinstance(data, dict) or not any(k in data for k in ("jobs", "research", "accounts")):
        raise ValueError("This doesn't look like Leads Radar data (expected jobs, research or accounts).")
    return data


def _competitor_key_for(product: str) -> Optional[str]:
    hits = rm.find_competitors(product or "")
    if hits:
        return hits[0]["key"]
    p = (product or "").lower()
    for c in rm.COMPETITORS:
        if c["product"].lower() in p or p in c["product"].lower():
            return c["key"]
    return None


async def import_radar(data: dict, by: str) -> dict:
    """Load a Leads Radar export: postings, problems, LinkedIn, DNS, deep dives and the human columns."""
    terr, members = await territories(), await team_members()
    counts = {"companies": 0, "signals_new": 0, "signals_seen": 0, "research": 0, "contacts": 0, "owners": 0, "statuses": 0}
    touched: set = set()
    before = await _db().companies.count_documents({})

    async def human(company: dict, row: dict):
        owner = (row.get("Owner") or "").strip()
        status = rm.legacy_status(row.get("Status"))
        sets = {}
        if owner and not company.get("owner_basis") == "manual":
            email = owner.lower() if "@" in owner else rep_email(owner, terr, members)
            sets.update({"owner": email or None, "owner_name": owner, "owner_basis": "manual"})
            counts["owners"] += 1
        if status != "new" and company.get("status") in rm.SYSTEM_STATUSES:
            sets["status"] = status
            counts["statuses"] += 1
        if row.get("Notes"):
            sets["notes"] = str(row["Notes"])[:5000]
        if sets:
            await _db().companies.update_one({"id": company["id"]}, {"$set": sets})

    for kind in ("jobs", "problems"):
        for row in data.get(kind) or []:
            if not row.get("Company"):
                continue
            company = await get_or_create_company(row["Company"], source="leads_radar")
            product = str(row.get("Competitor Product") or "")
            is_problem = kind == "problems" or product.startswith("Problem:")
            sig = {
                "type": "problem_job" if is_problem else "competitor_job", "source": "leads_radar", "source_ref": row.get("Posting URL") or None,
                "evidence_url": row.get("Posting URL"), "evidence_text": row.get("Matching Sentence"), "title": row.get("Job Title"), "role_title": row.get("Job Title"),
                "role_strength": row.get("Lead Strength") if row.get("Lead Strength") in rm.STRENGTHS else rm.role_strength(row.get("Job Title") or ""),
                "competitor_key": None if is_problem else _competitor_key_for(product), "competitor_product": None if is_problem else product,
                "problem_term": product.replace("Problem: ", "") if is_problem else None, "location": row.get("Location"), "state": row.get("State"),
                "country": row.get("Country"), "posted_at": row.get("Date"), "first_seen_at": row.get("First Seen") or row.get("Date Found") or row.get("Date"),
            }
            new = await upsert_signal(company, sig)
            counts["signals_new" if new else "signals_seen"] += 1
            await human(company, row)
            touched.add(company["id"])
    for row in data.get("linkedin") or []:
        if not row.get("Company"):
            continue
        company = await get_or_create_company(row["Company"], source="leads_radar")
        new = await upsert_signal(company, {"type": "linkedin", "source": "leads_radar", "source_ref": row.get("Evidence URL") or row.get("Post URL") or f"{row.get('Person')}|{row.get('Date')}",
                                            "evidence_url": row.get("Evidence URL") or row.get("Post URL"), "person": row.get("Person"), "person_title": row.get("Title"), "role_title": row.get("Title"),
                                            "role_strength": rm.role_strength(row.get("Title") or ""), "action": row.get("Action"), "competitor_key": _competitor_key_for(row.get("Competitor") or ""),
                                            "competitor_product": row.get("Competitor"), "country": row.get("Country"), "posted_at": row.get("Date")})
        counts["signals_new" if new else "signals_seen"] += 1
        await human(company, row)
        touched.add(company["id"])
    for row in data.get("dns") or []:
        if not row.get("Company"):
            continue
        company = await get_or_create_company(row["Company"], domain=row.get("Domain"), source="leads_radar")
        new = await upsert_signal(company, {"type": "dns", "source": "leads_radar", "source_ref": f"{row.get('Domain')}|{row.get('Record Type')}|{row.get('Record Value')}",
                                            "evidence_url": row.get("Evidence URL"), "domain": row.get("Domain"), "record_type": row.get("Record Type"), "record_value": row.get("Record Value"),
                                            "competitor_key": _competitor_key_for(row.get("Competitor Fingerprint") or ""), "competitor_product": row.get("Competitor Fingerprint"), "posted_at": row.get("Date")})
        counts["signals_new" if new else "signals_seen"] += 1
        await human(company, row)
        touched.add(company["id"])
    for row in data.get("accounts") or []:
        if row.get("Company"):
            touched.add((await get_or_create_company(row["Company"], source="leads_radar"))["id"])
    for r in data.get("research") or []:
        if not r.get("Company"):
            continue
        company = await get_or_create_company(r["Company"], source="leads_radar")
        exists = await _db().radar_research.find_one({"company_id": company["id"], "source": "leads_radar", "created_at": r.get("Date")}, {"_id": 1})
        if not exists:
            ft = r.get("First Touch") or {}
            doc = {
                "id": str(uuid.uuid4()), "company_id": company["id"], "company": company["name"], "version": 1, "source": "leads_radar", "status": "approved",
                "score": r.get("Score"), "verdict": r.get("Verdict"), "headline": r.get("Headline"), "summary": r.get("Summary"), "trigger": r.get("Trigger"),
                "evidence": [{"n": i + 1, "title": e.get("Title"), "url": e.get("URL"), "date": e.get("Date")} for i, e in enumerate(r.get("Evidence") or [])],
                "why_solix_wins": r.get("Why Solix Wins"), "contacts": [{"name": c.get("Name"), "title": c.get("Title"), "linkedin_url": c.get("LinkedIn URL") or None} for c in r.get("Contacts") or []],
                "first_touch": {"subject": ft.get("Subject"), "body": ft.get("Body")}, "gaps": r.get("Gaps"), "provider": "leads_radar", "model": "claude-code",
                "reviewed_by": f"imported by {by}", "reviewed_at": now_iso(), "created_at": r.get("Date") or now_iso(), "checks": [],
            }
            await _db().radar_research.insert_one(dict(doc))
            counts["research"] += 1
            for c in doc["contacts"]:
                if c.get("name") and not await _db().contacts.find_one({"company_id": company["id"], "name": c["name"]}, {"_id": 1}):
                    from outbound import new_contact_doc
                    await _db().contacts.insert_one(new_contact_doc({"company_id": company["id"], "name": c["name"], "title": c.get("title"), "linkedin_url": c.get("linkedin_url"),
                                                                       "source": "research", "source_url": (doc["evidence"][0]["url"] if doc["evidence"] else None)}, company))
                    counts["contacts"] += 1
        touched.add(company["id"])
    await recompute_many(touched)
    for cid in touched:
        await add_history(cid, by, "import", "Imported from Leads Radar")
    counts["companies"] = await _db().companies.count_documents({}) - before
    counts["touched"] = len(touched)
    return counts


# --- Legacy export (the radar.html JSON contract) -------------------------------------------

def _legacy_strength(s: Optional[str]) -> str:
    return s if s in rm.STRENGTHS else "Unrated"


async def legacy_export(owner: Optional[str] = None) -> dict:
    q = {"owner": owner} if owner else {}
    companies = {c["id"]: c async for c in _db().companies.find(q, {"_id": 0, "history": 0})}
    out = {"generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"), "accounts": [], "jobs": [], "problems": [], "linkedin": [], "dns": [], "research": []}
    for c in companies.values():
        out["accounts"].append({"Company": c["name"], "Lead Strength": _legacy_strength(c.get("strength"))})
    status_label = lambda c: rm.STATUS_LABELS.get(c.get("status"), "New")  # noqa: E731
    async for s in _db().company_signals.find({"company_id": {"$in": list(companies)}}, {"_id": 0}):
        c = companies[s["company_id"]]
        base = {"Company": c["name"], "Owner": c.get("owner_name") or "", "Status": status_label(c), "Date": str(s.get("posted_at") or s.get("first_seen_at") or "")[:10],
                "First Seen": str(s.get("first_seen_at") or "")[:10]}
        if s["type"] in ("competitor_job", "problem_job"):
            row = {**base, "Account Strength": _legacy_strength(c.get("strength")), "Lead Strength": _legacy_strength(s.get("role_strength")),
                   "Competitor Product": s.get("competitor_product") or (f"Problem: {s.get('problem_term')}" if s["type"] == "problem_job" else ""), "Job Title": s.get("role_title") or s.get("title") or "",
                   "Location": s.get("location") or "", "State": s.get("state") or "", "Country": s.get("country") or "", "Posting URL": s.get("evidence_url") or "", "Matching Sentence": s.get("evidence_text") or ""}
            out["jobs" if s["type"] == "competitor_job" else "problems"].append(row)
        elif s["type"] == "linkedin":
            out["linkedin"].append({**base, "Person": s.get("person") or "", "Title": s.get("person_title") or "", "Action": s.get("action") or "", "Competitor": s.get("competitor_product") or "",
                                    "Country": s.get("country") or "", "Evidence URL": s.get("evidence_url") or ""})
        elif s["type"] in ("dns", "cert"):
            out["dns"].append({**base, "Domain": s.get("domain") or "", "Record Type": s.get("record_type") or "", "Record Value": s.get("record_value") or "",
                               "Competitor Fingerprint": s.get("competitor_product") or "", "Evidence URL": s.get("evidence_url") or ""})
    async for r in _db().radar_research.find({"company_id": {"$in": list(companies)}, "status": "approved"}, {"_id": 0}).sort("created_at", -1):
        if any(x["Company"] == companies[r["company_id"]]["name"] for x in out["research"]):
            continue
        out["research"].append({
            "Company": companies[r["company_id"]]["name"], "Score": r.get("score"), "Verdict": r.get("verdict"), "Date": str(r.get("created_at") or "")[:10], "Headline": r.get("headline"),
            "Summary": r.get("summary"), "Trigger": r.get("trigger"), "Evidence": [{"Title": e.get("title"), "URL": e.get("url"), "Date": e.get("date")} for e in r.get("evidence") or []],
            "Why Solix Wins": r.get("why_solix_wins"), "Contacts": [{"Name": c.get("name"), "Title": c.get("title"), "LinkedIn URL": c.get("linkedin_url") or ""} for c in r.get("contacts") or []],
            "First Touch": {"Subject": (r.get("first_touch") or {}).get("subject"), "Body": (r.get("first_touch") or {}).get("body")}, "Gaps": r.get("gaps"), "Brief Path": "",
        })
    return out
