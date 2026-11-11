"""Website visitors -> organisations, for the Radar's "visit" signal.

When a consenting visitor's browser sends its tracking batch (POST /api/track only fires after the
analytics cookie is accepted), the network address it came from is looked up once along the
ip_company chain (ipapi.is, IPinfo Lite, Ipregistry, RDAP, reverse DNS). Only business, government
and education networks count; home broadband, mobile, VPN and cloud addresses are dropped. The
address itself is never stored: the lookup cache keys on a salted hash, and the visitor keeps only
the organisation's name, domain and network type.

Each daily run turns the last `visitor_days` of identified visits into one "visit" signal per
organisation and ISO week: how many people from it browsed, how many pages, and which product
lines they read about. It lands on the matching Radar account (by domain, else name), so an account
whose people are reading about application retirement shows up before anyone fills in a form.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger("solix.radar.visitors")
RECHECK_DAYS = 7
_tasks: set = set()
# Vendors and the site's own company: their staff browsing isn't a buying signal.
NOT_PROSPECTS = {"solix.com", "solixtechnologies.com", "opentext.com", "informatica.com", "smarsh.com", "globalrelay.com", "globalrelay.net", "mimecast.com",
                 "proofpoint.com", "veritas.com", "archive360.com", "delphix.com", "perforce.com", "platform3solutions.com", "jivs.com"}


def _db():
    import database
    return database.db


async def _enabled() -> bool:
    import cache
    import radar_service as rs

    async def load():
        return bool((await rs.radar_settings())["collectors"].get("visitors", True))
    try:
        return await cache.memo("radar_ctx", "visitors_on", 60, load)
    except Exception:  # noqa: BLE001
        return False


def schedule(visitor_id: str, ip: str, checked_at: Optional[str] = None) -> None:
    """Look the visitor's organisation up in the background (at most once a week per visitor)."""
    import radar_sources
    if not ip or not radar_sources._public_ip(ip):
        return
    if checked_at and checked_at > (datetime.now(timezone.utc) - timedelta(days=RECHECK_DAYS)).isoformat():
        return
    t = asyncio.create_task(identify(visitor_id, ip))
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


async def identify(visitor_id: str, ip: str) -> Optional[dict]:
    import radar_sources
    if not await _enabled():
        return None
    try:
        async with httpx.AsyncClient(headers=radar_sources.UA, follow_redirects=True) as client:
            org = await radar_sources.ip_company(client, ip)
    except Exception:  # noqa: BLE001 - identification must never affect tracking
        logger.info("visitor identification failed", exc_info=True)
        return None
    keep = {k: org.get(k) for k in ("org", "domain", "type", "country", "via")} if org else None
    if keep and keep.get("domain") in NOT_PROSPECTS:
        keep = None
    await _db().visitors.update_one({"id": visitor_id}, {"$set": {"org": keep, "org_checked_at": datetime.now(timezone.utc).isoformat()}})
    return keep


def _label(slug: str) -> str:
    return slug.replace("-", " ").title()


async def signals(days: int = 30) -> List[dict]:
    """One "visit" signal per organisation and ISO week, from identified visitors seen in the last `days`."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    groups: Dict[tuple, dict] = {}
    async for v in _db().visitors.find({"org.domain": {"$ne": None}, "org.type": {"$in": ["business", "government", "education"]}, "last_seen": {"$gte": since}},
                                       {"_id": 0, "id": 1, "org": 1, "last_seen": 1, "pages_count": 1, "sessions": 1, "product_scores": 1, "lead_id": 1}):
        org = v.get("org") or {}
        dom = org.get("domain")
        if not dom or dom in NOT_PROSPECTS:
            continue
        try:
            seen = datetime.fromisoformat(v["last_seen"][:32].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        year, week, _ = seen.isocalendar()
        g = groups.setdefault((dom, f"{year}-W{week:02d}"), {"org": org, "visitors": 0, "pages": 0, "sessions": 0, "leads": 0, "scores": {}, "last": ""})
        g["visitors"] += 1
        g["pages"] += v.get("pages_count") or 0
        g["sessions"] += v.get("sessions") or 0
        g["leads"] += 1 if v.get("lead_id") else 0
        g["last"] = max(g["last"], v["last_seen"][:10])
        for k, s in (v.get("product_scores") or {}).items():
            g["scores"][k] = g["scores"].get(k, 0) + (s or 0)
    site = os.environ.get("SITE_URL", "https://www.solix.com").rstrip("/")
    out = []
    for (dom, week), g in groups.items():
        org = g["org"]
        top = [_label(k) for k, _ in sorted(g["scores"].items(), key=lambda kv: -kv[1])[:3] if g["scores"][k] > 0]
        name = org.get("org") or dom.split(".")[0].title()
        people = f"{g['visitors']} visitor{'s' if g['visitors'] != 1 else ''}"
        out.append({"company": name, "domain": dom, "type": "visit", "source": "website", "source_ref": f"visit|{dom}|{week}", "evidence_url": site,
                    "title": f"{people} from {name} browsed the site ({week})", "posted_at": g["last"], "country": org.get("country"),
                    "evidence_text": f"{people}, {g['sessions']} session(s), {g['pages']} page(s)" + (f"; reading about {', '.join(top)}" if top else "")
                                     + (f"; {g['leads']} already a lead" if g["leads"] else "") + f". Network identified by {org.get('via') or 'IP lookup'}.",
                    "meta": {"visitors": g["visitors"], "pages": g["pages"], "interest": top, "via": org.get("via")}})
    return out
