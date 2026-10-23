"""Live account intelligence for the Radar: web research and the people to reach.

The original Leads Radar's deep dives came from open web research (press releases, the company's own site,
filings, investor material, conference talks, job postings). This does the same from live sources:

* Parallel Search (PARALLEL_API_KEY): three searches per account, run together, each returning ranked pages
  with readable excerpts: the account's programs and migrations, the named leaders who own IT, data,
  architecture and records, and recent material news.
* Hunter (HUNTER_API_KEY, optional): business email addresses, titles and the company's email pattern, each
  with the public pages it was seen on.

People are never invented: a name is kept only if it appears in a fetched source (or comes from Hunter with its
sources), and every person carries the link it came from, which is recorded on the contact (GDPR Art. 14).
Only business contact details are collected: name, title, work email, public profile link.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger("solix.radar.intel")

PARALLEL_URL = "https://api.parallel.ai/v1"
HUNTER_URL = "https://api.hunter.io/v2"
TARGET_TITLES = re.compile(r"\b(chief|cio|cto|cdo|cdao|ciso|vice president|vp|svp|evp|director|head of|manager|architect|officer|lead|counsel|president)\b", re.I)


def sources_available() -> Dict[str, bool]:
    return {"web_search": bool(os.environ.get("PARALLEL_API_KEY")), "hunter": bool(os.environ.get("HUNTER_API_KEY"))}


# --- Web search --------------------------------------------------------------------------------

async def web_search(client: httpx.AsyncClient, objective: str, queries: List[str], max_chars: int = 9000) -> List[dict]:
    """One Parallel Search call: ranked pages with the excerpts relevant to the objective."""
    r = await client.post(f"{PARALLEL_URL}/search", headers={"x-api-key": os.environ["PARALLEL_API_KEY"]}, timeout=60,
                          json={"objective": objective, "search_queries": [q[:200] for q in queries[:5]], "mode": "fast", "max_chars_total": max_chars})
    r.raise_for_status()
    return [{"url": x["url"], "title": x.get("title") or x["url"], "date": (x.get("publish_date") or "")[:10], "text": "\n".join(x.get("excerpts") or [])}
            for x in r.json().get("results", []) if x.get("url")]


def _angles(company: dict) -> Dict[str, tuple]:
    name = company["name"]
    comps = company.get("competitors") or []
    site = f" Prefer {company['domains'][0]}, press releases, filings and conference talks." if company.get("domains") else ""
    return {
        "programs": (f"Find recent, specific evidence that {name} is modernising, migrating, consolidating or retiring legacy applications, content management, "
                     f"records or data archive platforms{', including ' + ', '.join(comps[:3]) if comps else ''}, and why.{site}",
                     [f"{name} legacy system modernization", f"{name} {comps[0]} migration" if comps else f"{name} application retirement",
                      f"{name} records retention archive"]),
        "people": (f"Identify the named people at {name} who lead IT, data, enterprise architecture, records management, information governance or content "
                   f"platforms, with their exact current job titles.{site}",
                   [f"{name} chief information officer", f"{name} vice president enterprise architecture", f"{name} director records management"]),
        "news": (f"Material news about {name} in the last 12 months: acquisitions, divestitures, restructuring, regulatory actions, technology partnerships "
                 f"and IT investments.", [f"{name} acquisition", f"{name} technology partnership", f"{name} press release 2026"]),
    }


async def research_sources(client: httpx.AsyncClient, company: dict, angles: Optional[List[str]] = None) -> List[dict]:
    """The account's live web sources, every angle searched at once. Empty without PARALLEL_API_KEY."""
    if not os.environ.get("PARALLEL_API_KEY"):
        return []
    plan = {k: v for k, v in _angles(company).items() if not angles or k in angles}

    async def one(kind: str, objective: str, queries: List[str]) -> List[dict]:
        try:
            return [{**x, "kind": f"web:{kind}"} for x in await web_search(client, objective, queries)]
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            logger.warning("web search (%s) failed for %s: %s", kind, company["name"], exc)
            return []
    found = await asyncio.gather(*(one(k, o, q) for k, (o, q) in plan.items()))
    out, seen = [], set()
    for batch in found:
        for x in batch:
            if x["url"] not in seen:
                seen.add(x["url"])
                out.append(x)
    return out


# --- People -----------------------------------------------------------------------------------

_PERSON = re.compile(r"\b([A-Z][a-z]+(?:[ -][A-Z]\.)?(?: [A-Z][a-zA-Z'\-]+){1,2}),\s+((?:(?:Executive|Senior|Group|Global|Deputy|Associate) )?"
                     r"(?:Vice President|VP|SVP|EVP|Chief [A-Za-z ]{2,40}?Officer|Director|Head of [A-Za-z ,&]{2,40}|Manager|CIO|CTO|CDO|CISO)[^.;\n]{0,80})")


def people_from_text(sources: List[dict]) -> List[dict]:
    """Without a model: "Name, Title" patterns in the excerpts. Narrow on purpose; the model does better."""
    out = []
    for s in sources:
        for name, title in _PERSON.findall(s.get("text") or ""):
            title = re.split(r",\s+(?=[a-z])", title)[0]  # "CIO, leads technology" -> "CIO"
            out.append({"name": name.strip(), "title": title.strip(" ,"), "source_url": s.get("url"), "source_title": s.get("title")})
    return out


def named_in_sources(people: List[dict], sources: List[dict]) -> List[dict]:
    """Keep only people whose full name appears in a fetched source, and attach the first source that names them."""
    kept, seen = [], set()
    for p in people:
        name = re.sub(r"\s+", " ", str(p.get("name") or "")).strip()
        if len(name.split()) < 2 or name.lower() in seen:
            continue
        hit = next((s for s in sources if name.lower() in (s.get("text") or "").lower() or name.lower() in (s.get("title") or "").lower()), None)
        if not hit:
            continue
        seen.add(name.lower())
        kept.append({**p, "name": name, "source_url": p.get("source_url") or hit.get("url"), "source_title": p.get("source_title") or hit.get("title")})
    return kept


PEOPLE_SYSTEM = ("You extract people from web excerpts for a B2B sales researcher. From ONLY the numbered sources, list people who currently work at the "
                 "named company in IT, data, enterprise architecture, records management, information governance, content management, security, compliance or "
                 "executive leadership. Use each person's name exactly as written and the title the source gives. Never guess or complete a name. "
                 'Reply with JSON only: {"people": [{"name": "...", "title": "...", "source": 1}]}')


async def extract_people(company: dict, sources: List[dict]) -> List[dict]:
    if not sources:
        return []
    import radar_research
    block = "\n\n".join(f"[{i + 1}] {s['title']} ({s.get('url')})\n{(s.get('text') or '')[:1500]}" for i, s in enumerate(sources[:12]))
    text, _, _ = await radar_research._complete(PEOPLE_SYSTEM, f"Company: {company['name']}\n\nSOURCES\n{block}")
    data = radar_research._parse_json(text or "") if text else None
    people = []
    for p in (data or {}).get("people") or []:
        if isinstance(p, dict) and p.get("name"):
            n = p.get("source")
            src = sources[n - 1] if isinstance(n, int) and 0 < n <= len(sources) else {}
            people.append({"name": str(p["name"])[:120], "title": str(p.get("title") or "")[:200], "source_url": src.get("url"), "source_title": src.get("title")})
    return people if data else people_from_text(sources)


async def hunter_people(client: httpx.AsyncClient, domain: str, limit: int = 15) -> dict:
    """Hunter Domain Search: decision makers in IT, executive and management, with work emails and where they were seen."""
    if not os.environ.get("HUNTER_API_KEY") or not domain:
        return {"people": [], "pattern": None}
    r = await client.get(f"{HUNTER_URL}/domain-search", timeout=30, params={
        "domain": domain, "api_key": os.environ["HUNTER_API_KEY"], "type": "personal", "limit": limit,
        "department": "it,executive,management,legal,operations", "seniority": "senior,executive"})
    r.raise_for_status()
    data = r.json().get("data") or {}
    people = []
    for e in data.get("emails") or []:
        if not (e.get("first_name") and e.get("last_name")):
            continue
        src = (e.get("sources") or [{}])[0]
        people.append({"name": f"{e['first_name']} {e['last_name']}", "title": e.get("position") or "", "email": e.get("value"),
                       "email_confidence": e.get("confidence"), "email_status": ((e.get("verification") or {}).get("status")),
                       "linkedin_url": e.get("linkedin"), "source_url": src.get("uri") or f"https://hunter.io/search/{domain}", "source_title": src.get("domain") or "Hunter",
                       "via": "hunter"})
    return {"people": people, "pattern": data.get("pattern")}


async def hunter_email(client: httpx.AsyncClient, domain: str, first: str, last: str) -> Optional[dict]:
    """Hunter Email Finder for one person; None when Hunter isn't set up or has no confident answer."""
    if not os.environ.get("HUNTER_API_KEY") or not (domain and first and last):
        return None
    r = await client.get(f"{HUNTER_URL}/email-finder", timeout=30, params={"domain": domain, "first_name": first, "last_name": last,
                                                                          "api_key": os.environ["HUNTER_API_KEY"]})
    if r.status_code == 404:
        return None
    r.raise_for_status()
    d = r.json().get("data") or {}
    if not d.get("email") or (d.get("score") or 0) < 50:
        return None
    return {"email": d["email"].lower(), "score": d.get("score"), "status": ((d.get("verification") or {}).get("status")),
            "source_url": ((d.get("sources") or [{}])[0]).get("uri")}


async def find_people(client: httpx.AsyncClient, company: dict, known_names: Optional[set] = None) -> dict:
    """Live candidates for an account: named in fetched web sources and/or found by Hunter, never invented."""
    known = {n.lower() for n in known_names or set()}
    web, hunter = await asyncio.gather(research_sources(client, company, ["people"]),
                                       _safe_hunter(client, (company.get("domains") or [None])[0]))
    people = named_in_sources(await extract_people(company, web), web) + hunter["people"]
    out, seen = [], set()
    for p in people:
        key = p["name"].lower()
        if key in seen or key in known:
            continue
        seen.add(key)
        title = p.get("title") or ""
        out.append({**p, "persona": "target" if TARGET_TITLES.search(title) else "other",
                    "relevant": bool(re.search(r"information|data|architect|record|governance|content|technology|digital|it\b|cio|cto|cdo|compliance|legal|security",
                                               title, re.I))})
    out.sort(key=lambda p: (not p["relevant"], p["persona"] != "target", p["name"]))
    return {"people": out[:25], "sources_used": {"web_pages": len(web), "hunter": len(hunter["people"])}, "email_pattern": hunter.get("pattern"),
            "available": sources_available()}


async def _safe_hunter(client: httpx.AsyncClient, domain: Optional[str]) -> dict:
    try:
        return await hunter_people(client, domain)
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("hunter lookup failed for %s: %s", domain, exc)
        return {"people": [], "pattern": None}


def evidence_excerpt(text: str, limit: int = 400) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:limit]

