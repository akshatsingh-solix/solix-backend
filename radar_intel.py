"""Live account intelligence for the Radar: web research and the people to reach.

The original Leads Radar's deep dives came from open web research (press releases, the company's own site,
filings, investor material, conference talks, job postings). This does the same from live sources:

* Web research, three angles per account searched together: the account's programs and migrations, the named
  leaders who own IT, data, architecture and records, and recent material news. The web-search chain in
  providers.py answers: Firecrawl first (ranked pages, the best leadership pages read in full, because names
  and titles rarely fit in a snippet), then Tavily, Serper, Brave, Exa, Google Programmable Search, SerpApi,
  a self-hosted SearXNG, Jina and Parallel - whichever are set up and have free allowance left.
  An identical search within a day comes from the cache, so repeat clicks and a deep dive after Find people
  spend no credits.
* People who work there now, merged from every source that's set up: leadership pages, public LinkedIn
  profiles found through search engines (the headline shows the current employer), Hunter, Tomba, Snov.io,
  People Data Labs, Apollo and UK Companies House officers - each with the page or record it came from.

People are never invented: a name is kept only if it appears in a fetched source (or comes from Hunter with its
sources), and every person carries the link it came from, which is recorded on the contact (GDPR Art. 14).
Only business contact details are collected: name, title, work email, public profile link.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
from urllib.parse import urlparse

import httpx

import firecrawl_api

logger = logging.getLogger("solix.radar.intel")

PARALLEL_URL = "https://api.parallel.ai/v1"
HUNTER_URL = "https://api.hunter.io/v2"
TARGET_TITLES = re.compile(r"\b(chief|cio|cto|cdo|cdao|ciso|vice president|vp|svp|evp|director|head of|manager|architect|officer|lead|counsel|president)\b", re.I)


def sources_available() -> Dict[str, bool]:
    import providers as pv
    return {"web_search": pv.any_configured("web_search"), "hunter": bool(os.environ.get("HUNTER_API_KEY")),
            "contacts": pv.any_configured("people", keyed_only=True)}


def web_provider() -> Optional[str]:
    """The first web-search provider that's set up (the chain decides who actually answers)."""
    import providers as pv
    return next((s.name for s in pv.SPECS if s.capability == "web_search" and pv.configured(s)), None)


HOSTS = {"api.parallel.ai": "Parallel", "api.hunter.io": "Hunter", "api.firecrawl.dev": "Firecrawl", "api.tavily.com": "Tavily", "google.serper.dev": "Serper",
         "api.search.brave.com": "Brave", "api.exa.ai": "Exa", "www.googleapis.com": "Google", "serpapi.com": "SerpApi", "s.jina.ai": "Jina", "r.jina.ai": "Jina",
         "api.theirstack.com": "TheirStack", "api.adzuna.com": "Adzuna", "jsearch.p.rapidapi.com": "JSearch", "jooble.org": "Jooble", "www.reed.co.uk": "Reed",
         "data.usajobs.gov": "USAJOBS", "api.tomba.io": "Tomba", "api.snov.io": "Snov.io", "api.peopledatalabs.com": "People Data Labs", "api.apollo.io": "Apollo",
         "api.prospeo.io": "Prospeo", "api.skrapp.io": "Skrapp", "api.mailboxvalidator.com": "MailboxValidator", "api.zerobounce.net": "ZeroBounce",
         "emailvalidation.abstractapi.com": "Abstract", "api.ipapi.is": "ipapi.is", "api.ipinfo.io": "IPinfo", "api.ipregistry.co": "Ipregistry", "newsdata.io": "NewsData",
         "api.company-information.service.gov.uk": "Companies House", "api.sam.gov": "SAM.gov", "api.usaspending.gov": "USAspending"}


def why(exc: Exception) -> str:
    """The provider's own reason for a failed call, for the screen ("Parallel returned 401: Invalid API key"). Never includes the key."""
    if not isinstance(exc, httpx.HTTPStatusError):
        return f"{exc.__class__.__name__} (no response)"
    r = exc.response
    msg = ""
    try:
        body = r.json()
        err = body.get("error")
        msg = body.get("message") or (err.get("message") if isinstance(err, dict) else err) or ((body.get("errors") or [{}])[0].get("details")) or ""
    except (ValueError, AttributeError, IndexError):
        pass
    host = HOSTS.get(r.request.url.host, r.request.url.host)
    return f"{host} returned {r.status_code}" + (f": {str(msg)[:160]}" if msg else "")


def _note(problems: Optional[List[str]], text: str) -> None:
    if problems is not None and text not in problems:
        problems.append(text)


_LEGAL = re.compile(r"(?:\s*/[A-Za-z]{2,3}/?)$|,?\s+(?:inc|incorporated|corp|corporation|co|ltd|limited|llc|plc|lp|sa|ag|nv|se|gmbh)\.?$", re.I)


def search_name(name: str) -> str:
    """"8X8 Inc /De/" -> "8X8", "Varonis Systems Inc" -> "Varonis Systems": filing-style suffixes only add noise to a search."""
    prev, out = None, name.strip()
    while prev != out:
        prev, out = out, _LEGAL.sub("", out).strip(" ,")
    return out or name


# --- Web search --------------------------------------------------------------------------------

FC_RESULTS = 5          # per Firecrawl query: one 2-credit block of results
WEB_CACHE_TTL = timedelta(hours=24)
_PEOPLE_PAGE = re.compile(r"leader|team|management|executive|about|board|officer|people|governance", re.I)


async def parallel_search(client: httpx.AsyncClient, objective: str, queries: List[str], max_chars: int = 9000) -> List[dict]:
    """One Parallel Search call: ranked pages with the excerpts relevant to the objective."""
    r = await client.post(f"{PARALLEL_URL}/search", headers={"x-api-key": os.environ["PARALLEL_API_KEY"]}, timeout=60,
                          json={"objective": objective, "search_queries": [q[:200] for q in queries[:5]], "mode": "fast", "max_chars_total": max_chars})
    r.raise_for_status()
    return [{"url": x["url"], "title": x.get("title") or x["url"], "date": (x.get("publish_date") or "")[:10], "text": "\n".join(x.get("excerpts") or [])}
            for x in r.json().get("results", []) if x.get("url")]


async def firecrawl_angle(client: httpx.AsyncClient, angle: dict):
    """Firecrawl searches for one angle, then the most promising pages read in full (`read` of them, 1 credit each)."""
    import providers as pv
    batches = await asyncio.gather(*(firecrawl_api.search(client, q, FC_RESULTS, angle.get("tbs")) for q in angle["fc_queries"]))
    found, seen = [], set()
    for batch in batches:
        for x in batch:
            if x["url"] not in seen:
                seen.add(x["url"])
                found.append(x)
    # Read leadership-looking pages (or else the top result): a credit spent on a news article rarely finds a title.
    likely = [x for x in found if _PEOPLE_PAGE.search(x["url"] + " " + x["title"])]
    picks = (likely or found[:1])[:angle.get("read", 0)]

    async def read(x: dict) -> None:
        try:
            page = await firecrawl_api.read(client, x["url"])
        except (httpx.HTTPError, ValueError) as exc:
            logger.info("could not read %s: %s", x["url"], why(exc))  # the snippet still stands
            return
        if page["text"]:
            x["text"] = focus(page["text"])
            x["date"] = x["date"] or page["date"]
    await asyncio.gather(*(read(x) for x in picks))
    return pv.Billed(found, 2 * len(angle["fc_queries"]) + len(picks))


async def keyword_angle(client: httpx.AsyncClient, name: str, angle: dict):
    """Any other keyword engine: the angle's short queries, then the leadership pages read by the page-reading chain."""
    import providers as pv
    import radar_sources
    adapter = radar_sources.SEARCH_ADAPTERS[name]
    batches = await asyncio.gather(*(adapter(client, q, FC_RESULTS, angle.get("tbs")) for q in angle["fc_queries"]))
    found, seen = [], set()
    for batch in batches:
        for x in batch:
            if x["url"] not in seen:
                seen.add(x["url"])
                found.append(x)
    likely = [x for x in found if _PEOPLE_PAGE.search(x["url"] + " " + x["title"])]

    async def read(x: dict) -> None:
        page = await radar_sources.read_page(client, x["url"])
        if page and page.get("text"):
            x["text"] = focus(page["text"])
            x["date"] = x["date"] or page.get("date") or ""
    await asyncio.gather(*(read(x) for x in (likely or found[:1])[:angle.get("read", 0)]))
    return pv.Billed(found, len(angle["fc_queries"]))


def focus(text: str, limit: int = 3500) -> str:
    """The lines of a full page that name roles, with the line either side (where the name usually is)."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    keep = sorted({j for i, ln in enumerate(lines) if len(ln) < 300 and TARGET_TITLES.search(ln) for j in (i - 1, i, i + 1) if 0 <= j < len(lines)})
    return "\n".join(lines[i] for i in keep)[:limit] if keep else "\n".join(lines)[:limit]


async def web_search(client: httpx.AsyncClient, angle: dict, problems: Optional[List[str]] = None) -> List[dict]:
    """One angle's pages from the first web provider in the chain that answers (see providers.py). Failures go to `problems`."""
    import providers as pv
    import radar_sources
    calls = {}
    for name in radar_sources.SEARCH_ADAPTERS:
        if name == "firecrawl":
            calls[name] = lambda: firecrawl_angle(client, angle)
        elif name == "parallel":
            calls[name] = lambda: parallel_search(client, angle["objective"], angle["queries"])
        else:
            calls[name] = lambda n=name: keyword_angle(client, n, angle)

    def failed(spec, exc):
        logger.warning("web search via %s failed: %s", spec.name, why(exc))
        _note(problems, f"Web search failed: {why(exc)}")
    found, _ = await pv.first("web_search", calls, on_error=failed)
    return found or []


async def cached_search(client: httpx.AsyncClient, angle: dict, problems: Optional[List[str]] = None) -> List[dict]:
    """web_search, reusing an identical search from the last day; only searches that found something are kept."""
    import database
    key = hashlib.sha1(json.dumps([angle["objective"], angle["queries"], angle["fc_queries"]]).encode()).hexdigest()
    hit = await database.db.web_cache.find_one({"key": key}, {"_id": 0})
    if hit and hit["at"] > (datetime.now(timezone.utc) - WEB_CACHE_TTL).isoformat():
        return hit["results"]
    results = await web_search(client, angle, problems)
    if results:
        await database.db.web_cache.update_one({"key": key}, {"$set": {"key": key, "at": datetime.now(timezone.utc).isoformat(), "results": results}}, upsert=True)
    return results


def _angles(company: dict) -> Dict[str, dict]:
    """Per angle: Parallel's objective and keyword queries, and Firecrawl's queries, time filter and pages to read in full."""
    name, short = company["name"], search_name(company["name"])
    comps = company.get("competitors") or []
    site = f" Prefer {company['domains'][0]}, press releases, filings and conference talks." if company.get("domains") else ""
    year = datetime.now(timezone.utc).year
    # Objectives carry the full name; the keyword queries stay short (Parallel asks for 3 to 6 words each).
    return {
        "programs": {"objective": f"Find recent, specific evidence that {name} is modernising, migrating, consolidating or retiring legacy applications, content "
                                  f"management, records or data archive platforms{', including ' + ', '.join(comps[:3]) if comps else ''}, and why.{site}",
                     "queries": [f"{short} legacy system modernization", f"{short} {comps[0]} migration" if comps else f"{short} application retirement",
                                 f"{short} records retention archive"],
                     "fc_queries": [f"{short} {comps[0]} migration" if comps else f"{short} legacy system modernization"], "read": 0},
        "people": {"objective": f"Identify the named people at {name} who lead IT, data, enterprise architecture, records management, information governance or "
                                f"content platforms, with their exact current job titles.{site}",
                   "queries": [f"{short} chief information officer", f"{short} VP enterprise architecture", f"{short} records management director"],
                   "fc_queries": [f"{short} leadership team", f"{short} chief information officer"], "read": 2},
        "news": {"objective": f"Material news about {name} in the last 12 months: acquisitions, divestitures, restructuring, regulatory actions, technology "
                              f"partnerships and IT investments.",
                 "queries": [f"{short} acquisition", f"{short} technology partnership", f"{short} press release {year}"],
                 "fc_queries": [f"{short} news"], "tbs": "qdr:y", "read": 0},
    }


async def research_sources(client: httpx.AsyncClient, company: dict, angles: Optional[List[str]] = None, problems: Optional[List[str]] = None) -> List[dict]:
    """The account's live web sources, every angle searched at once. Empty without a web provider key.
    Why a search failed goes into `problems`, so the screen can say so instead of showing an empty list."""
    if not web_provider():
        return []
    plan = {k: v for k, v in _angles(company).items() if not angles or k in angles}

    async def one(kind: str, angle: dict) -> List[dict]:
        return [{**x, "kind": f"web:{kind}"} for x in await cached_search(client, angle, problems)]
    found = await asyncio.gather(*(one(k, a) for k, a in plan.items()))
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


_NAME_LINE = re.compile(r"^([A-Z][a-z]+(?:[ -][A-Z]\.)?(?: [A-Z][a-zA-Z'\-]+){1,2})$")
_TITLE_LINE = re.compile(r"^(?:(?:Executive|Senior|Group|Global|Deputy|Associate) )?(?:Vice President|VP|SVP|EVP|Chief [A-Za-z ]{2,40}?Officer|Director|Head of|"
                         r"Manager|President|CIO|CTO|CDO|CISO)\b")
# Headings that look like two capitalised words but aren't people ("Our Team", "Executive Leadership").
_NOT_NAMES = {"Our", "The", "Team", "Leadership", "Executive", "Executives", "Board", "Management", "Officers", "Directors", "Company", "About", "Meet",
              "Chief", "Senior", "Global", "Read", "More", "View", "Bio", "Contact", "News", "Committee", "Corporate", "Governance", "Investor", "Relations"}


def people_from_text(sources: List[dict]) -> List[dict]:
    """Without a model: "Name, Title" in a sentence, or a name line followed by a title line (how leadership pages
    lay it out). Narrow on purpose; the model does better."""
    out = []
    for s in sources:
        text = s.get("text") or ""
        for name, title in _PERSON.findall(text):
            title = re.split(r",\s+(?=[a-z])", title)[0]  # "CIO, leads technology" -> "CIO"
            out.append({"name": name.strip(), "title": title.strip(" ,"), "source_url": s.get("url"), "source_title": s.get("title")})
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        for a, b in zip(lines, lines[1:]):
            m = _NAME_LINE.match(a)
            if m and len(b) < 160 and _TITLE_LINE.match(b) and not set(m.group(1).split()) & _NOT_NAMES:
                out.append({"name": m.group(1), "title": b.strip(" ,"), "source_url": s.get("url"), "source_title": s.get("title")})
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
    block = "\n\n".join(f"[{i + 1}] {s['title']} ({s.get('url')})\n{(s.get('text') or '')[:3500]}" for i, s in enumerate(sources[:12]))
    text, _, _ = await radar_research._complete(PEOPLE_SYSTEM, f"Company: {company['name']}\n\nSOURCES\n{block}")
    data = radar_research._parse_json(text or "") if text else None
    people = []
    for p in (data or {}).get("people") or []:
        if isinstance(p, dict) and p.get("name"):
            n = p.get("source")
            src = sources[n - 1] if isinstance(n, int) and 0 < n <= len(sources) else {}
            people.append({"name": str(p["name"])[:120], "title": str(p.get("title") or "")[:200], "source_url": src.get("url"), "source_title": src.get("title")})
    return people + people_from_text(sources)  # the rules catch "Name / Title" lines the model skipped; duplicates are dropped later


_GENERIC_WORDS = {"systems", "system", "group", "holdings", "holding", "technologies", "technology", "tech", "solutions", "data", "software", "international",
                  "global", "services", "the", "and", "company", "networks", "labs", "digital", "health", "financial", "bank", "energy", "industries",
                  "partners", "capital", "resources", "mutual", "insurance", "communications", "enterprises", "therapeutics", "pharmaceuticals"}


def _name_tokens(name: str) -> List[str]:
    """The distinctive words of a company name: "Varonis Systems Inc" -> ["varonis"]."""
    return [t for t in re.findall(r"[a-z0-9]+", search_name(name).lower()) if len(t) >= 3 and t not in _GENERIC_WORDS]


def registrable(host: str) -> str:
    """"blog.varonis.com" -> "varonis.com", "www.bbc.co.uk" -> "bbc.co.uk"."""
    parts = host.lower().strip(".").split(".")
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in ("co", "com", "org", "net", "ac", "gov", "edu"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def domain_matches(domain: str, name: str) -> bool:
    """Whether a domain plausibly belongs to the company: a distinctive word of its name is in the domain's own label
    (varonis.com for Varonis Systems; not marini.systems, not varonis.myworkdayjobs.com)."""
    label = registrable(domain).split(".")[0]
    compact = re.sub(r"[^a-z0-9]", "", search_name(name).lower())
    return any(t in label for t in _name_tokens(name)) or (len(compact) >= 3 and compact in label)


def company_domain(company: dict, sources: List[dict]) -> Optional[str]:
    """The account's domain, or else the company's own site among the fetched pages (its About page, its blog)."""
    if company.get("domains"):
        return company["domains"][0]
    for s in sources:
        host = urlparse(s.get("url") or "").hostname or ""
        if host and domain_matches(host, company["name"]):
            return registrable(host)
    return None


async def hunter_people(client: httpx.AsyncClient, domain: Optional[str], limit: int = 10, company: Optional[str] = None) -> dict:
    """Hunter Domain Search: decision makers in IT, executive and management, with work emails and where they were seen.
    Without a domain on the account, Hunter is asked by company name and says which domain it matched.
    10 results is the most the free plan returns (more is a 400), and enough once filtered to senior IT and executives."""
    import providers as pv
    if not os.environ.get("HUNTER_API_KEY") or not (domain or company):
        return {"people": [], "pattern": None, "domain": None}
    if not await pv.usable("hunter"):
        return {"people": [], "pattern": None, "domain": None, "resting": True}
    try:
        r = await client.get(f"{HUNTER_URL}/domain-search", timeout=30, params={
            **({"domain": domain} if domain else {"company": search_name(company)}), "api_key": os.environ["HUNTER_API_KEY"], "type": "personal", "limit": limit,
            "department": "it,executive,management,legal,operations", "seniority": "senior,executive"})
        r.raise_for_status()
    except httpx.HTTPError as exc:
        pv.failed("hunter", exc)
        raise
    await pv.succeeded("hunter")
    data = r.json().get("data") or {}
    if not domain and data.get("domain") and not domain_matches(data["domain"], company):
        # Asked by name, Hunter picked a different company ("Varonis Systems" -> marini.systems): its people aren't the account's.
        return {"people": [], "pattern": None, "domain": None, "mismatch": data["domain"]}
    domain = domain or data.get("domain")
    people = []
    for e in data.get("emails") or []:
        if not (e.get("first_name") and e.get("last_name")):
            continue
        srcs = e.get("sources") or [{}]
        src = srcs[0]
        people.append({"name": f"{e['first_name']} {e['last_name']}", "title": e.get("position") or "", "email": e.get("value"),
                       "email_confidence": e.get("confidence"), "email_status": ((e.get("verification") or {}).get("status")),
                       "linkedin_url": e.get("linkedin"), "source_url": src.get("uri") or f"https://hunter.io/search/{domain}", "source_title": src.get("domain") or "Hunter",
                       "via": "hunter", "current": True if any(s.get("still_on_page") for s in srcs) else None})
    return {"people": people, "pattern": data.get("pattern"), "domain": domain}


async def hunter_email(client: httpx.AsyncClient, domain: str, first: str, last: str) -> Optional[dict]:
    """Hunter Email Finder for one person; None when Hunter isn't set up or has no confident answer."""
    import providers as pv
    if not os.environ.get("HUNTER_API_KEY") or not (domain and first and last) or not await pv.usable("hunter_find"):
        return None
    r = await client.get(f"{HUNTER_URL}/email-finder", timeout=30, params={"domain": domain, "first_name": first, "last_name": last,
                                                                          "api_key": os.environ["HUNTER_API_KEY"]})
    if r.status_code == 404:
        return None
    try:
        r.raise_for_status()
    except httpx.HTTPError as exc:
        pv.failed("hunter_find", exc)
        raise
    await pv.succeeded("hunter_find")
    d = r.json().get("data") or {}
    if not d.get("email") or (d.get("score") or 0) < 50:
        return None
    return {"email": d["email"].lower(), "score": d.get("score"), "status": ((d.get("verification") or {}).get("status")),
            "source_url": ((d.get("sources") or [{}])[0]).get("uri")}


async def find_people(client: httpx.AsyncClient, company: dict, known_names: Optional[set] = None) -> dict:
    """Live candidates for an account, never invented: named in fetched web sources, on public LinkedIn profiles that list
    the company as the current employer, or found by a contact-data provider (Hunter, Tomba, Snov.io, PDL, Apollo,
    Companies House). The same person from several sources becomes one candidate with every detail found."""
    import radar_sources
    known = {n.lower() for n in known_names or set()}
    problems: List[str] = []
    xray = radar_sources.linkedin_xray(client, company["name"], problems=problems)
    if company.get("domains"):
        domain = company["domains"][0]
        web, hunter, (linked, _) = await asyncio.gather(research_sources(client, company, ["people"], problems),
                                                        _safe_hunter(client, domain, None, problems), xray)
    else:
        # No domain on the account: find the company's own site in the web results first, so Hunter searches the right company.
        web, (linked, _) = await asyncio.gather(research_sources(client, company, ["people"], problems), xray)
        domain = company_domain(company, web)
        hunter = await _safe_hunter(client, domain, None if domain else company["name"], problems)
    if hunter.get("mismatch"):
        _note(problems, f"Hunter matched {hunter['mismatch']}, which doesn't look like {company['name']}, so its people were left out. "
                        "Add the account's website domain to use Hunter.")
    others = await radar_sources.people(client, company, domain or hunter.get("domain"), problems=problems)
    people = named_in_sources(await extract_people(company, web), web) + hunter["people"] + linked + others
    out, by_name = [], {}
    for p in people:
        key = p["name"].lower()
        if key in known:
            continue
        if key in by_name:  # the same person from another source: keep the first record, add what this one knows
            first = by_name[key]
            for f in ("email", "email_confidence", "email_status", "linkedin_url"):
                if p.get(f) and not first.get(f):
                    first[f] = p[f]
            if not first.get("title") and p.get("title"):
                first["title"] = p["title"]
            if p.get("current"):
                first["current"] = True
            first["also_via"] = sorted(set(first.get("also_via") or []) | {p.get("via") or "web"})
            continue
        rec = {**p, "current": p.get("current")}
        by_name[key] = rec
        out.append(rec)
    for p in out:  # after merging: a title can arrive with a later source
        title = p.get("title") or ""
        p["persona"] = "target" if TARGET_TITLES.search(title) else "other"
        p["relevant"] = bool(re.search(r"information|data|architect|record|governance|content|technology|digital|it\b|cio|cto|cdo|compliance|legal|security", title, re.I))
    out.sort(key=lambda p: (not p["relevant"], p["persona"] != "target", p.get("current") is not True, p["name"]))
    return {"people": out[:25], "sources_used": {"web_pages": len(web), "hunter": len(hunter["people"]), "linkedin_xray": len(linked), "other": len(others)},
            "email_pattern": hunter.get("pattern"), "hunter_domain": hunter.get("domain"), "problems": problems, "available": sources_available()}


async def _safe_hunter(client: httpx.AsyncClient, domain: Optional[str], company: Optional[str] = None, problems: Optional[List[str]] = None) -> dict:
    try:
        return await hunter_people(client, domain, company=company)
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("hunter lookup failed for %s: %s", domain or company, why(exc))
        _note(problems, f"Hunter failed: {why(exc)}")
        return {"people": [], "pattern": None, "domain": None}


def evidence_excerpt(text: str, limit: int = 400) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:limit]

