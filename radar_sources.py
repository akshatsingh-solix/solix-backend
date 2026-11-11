"""Provider adapters for the Radar's free data chains (see providers.py for failover and quotas).

Each public function here is one Radar job, answered by the first provider in its chain that
can: `search`, `read_page`, `news`, `job_postings`, `firmographics`, `people`, `find_email`,
`verify_email`, `ip_company`, `subdomains`, `public_sector`. Adapters normalise every
provider's answer into the same small dicts, so callers never see which provider answered
(except in `via`, kept for the evidence trail).

Rules every adapter keeps:
* data, not instructions: page and API text is only ever quoted as evidence;
* only business facts and business contact details (name, title, work email, public profile);
* a person is never invented: names come from a source that is kept with them;
* nothing a provider returns is fetched unless it is a public http(s) address.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, urlparse

import httpx

import providers as pv
from providers import Billed, OutOfCredits, Skip

logger = logging.getLogger("solix.radar.sources")
UA = {"User-Agent": "SolixRadar/2.0 (+https://www.solix.com; sales-ops@solix.com)"}


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _strip(text: Optional[str]) -> str:
    text = re.sub(r"(?s)<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", text.replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'")).strip()


def _hit(url: str, title: str = "", text: str = "", date: str = "") -> dict:
    return {"url": url, "title": _strip(title) or url, "date": str(date or "")[:10], "text": _strip(text)}


def _iso(value) -> str:
    if not value:
        return ""
    s = str(value)
    m = re.match(r"(\d{4})-?(\d{2})-?(\d{2})", s)
    if m:
        return "-".join(m.groups())
    try:
        return parsedate_to_datetime(s).astimezone(timezone.utc).date().isoformat()
    except (TypeError, ValueError, IndexError):
        pass
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    m = re.match(r"(\d+)\s+(minute|hour|day|week|month)s?\s+ago", s.strip(), re.I)
    if m:
        days = int(m.group(1)) * {"minute": 0, "hour": 0, "day": 1, "week": 7, "month": 30}[m.group(2).lower()]
        return (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    return ""


def registrable(host: str) -> str:
    parts = (host or "").lower().strip(".").split(".")
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in ("co", "com", "org", "net", "ac", "gov", "edu"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _public_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


async def _public_url(url: str) -> bool:
    try:
        import migrate
        await migrate.check_url(url)
        return True
    except Exception:  # noqa: BLE001 - not public, or not resolvable
        return False


def _raise_for(r: httpx.Response) -> httpx.Response:
    r.raise_for_status()
    return r


# =====================================================================================
# Web search
# =====================================================================================

TBS_DAYS = {"qdr:d": 1, "qdr:w": 7, "qdr:m": 31, "qdr:y": 365}


async def _firecrawl(client, q, limit, tbs):
    import firecrawl_api
    return await firecrawl_api.search(client, q, limit, tbs)


async def _tavily(client, q, limit, tbs):
    body = {"query": q[:400], "max_results": min(limit, 20), "search_depth": "basic"}
    if tbs in ("qdr:y", "qdr:m", "qdr:w", "qdr:d"):
        body["time_range"] = {"qdr:y": "year", "qdr:m": "month", "qdr:w": "week", "qdr:d": "day"}[tbs]
    r = _raise_for(await client.post("https://api.tavily.com/search", json=body, headers={"Authorization": f"Bearer {_env('TAVILY_API_KEY')}"}, timeout=40))
    return [_hit(x["url"], x.get("title"), x.get("content"), x.get("published_date")) for x in r.json().get("results") or [] if x.get("url")]


async def _serper(client, q, limit, tbs):
    body = {"q": q[:400], "num": min(limit, 20), **({"tbs": tbs} if tbs else {})}
    r = _raise_for(await client.post("https://google.serper.dev/search", json=body, headers={"X-API-KEY": _env("SERPER_API_KEY")}, timeout=30))
    return [_hit(x["link"], x.get("title"), x.get("snippet"), _iso(x.get("date"))) for x in r.json().get("organic") or [] if x.get("link")]


async def _brave(client, q, limit, tbs):
    params = {"q": q[:400], "count": min(limit, 20), **({"freshness": {"qdr:y": "py", "qdr:m": "pm", "qdr:w": "pw", "qdr:d": "pd"}[tbs]} if tbs in TBS_DAYS else {})}
    r = _raise_for(await client.get("https://api.search.brave.com/res/v1/web/search", params=params,
                                    headers={"X-Subscription-Token": _env("BRAVE_API_KEY"), "Accept": "application/json"}, timeout=30))
    return [_hit(x["url"], x.get("title"), x.get("description"), _iso(x.get("page_age"))) for x in (r.json().get("web") or {}).get("results") or [] if x.get("url")]


async def _google_cse(client, q, limit, tbs):
    params = {"key": _env("GOOGLE_CSE_KEY"), "cx": _env("GOOGLE_CSE_CX"), "q": q[:400], "num": min(limit, 10)}
    if tbs in TBS_DAYS:
        params["dateRestrict"] = f"d{TBS_DAYS[tbs]}"
    r = _raise_for(await client.get("https://www.googleapis.com/customsearch/v1", params=params, timeout=30))
    return [_hit(x["link"], x.get("title"), x.get("snippet")) for x in r.json().get("items") or [] if x.get("link")]


async def _serpapi(client, q, limit, tbs):
    params = {"engine": "google", "q": q[:400], "num": min(limit, 20), "api_key": _env("SERPAPI_API_KEY"), **({"tbs": tbs} if tbs else {})}
    r = await client.get("https://serpapi.com/search.json", params=params, timeout=40)
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if data.get("error") and re.search(r"run out|limit", str(data["error"]), re.I):
        raise OutOfCredits(data["error"])
    r.raise_for_status()
    return [_hit(x["link"], x.get("title"), x.get("snippet"), _iso(x.get("date"))) for x in data.get("organic_results") or [] if x.get("link")]


async def _searxng(client, q, limit, tbs):
    params = {"q": q[:400], "format": "json", **({"time_range": {"qdr:y": "year", "qdr:m": "month", "qdr:w": "week", "qdr:d": "day"}[tbs]} if tbs in TBS_DAYS else {})}
    r = _raise_for(await client.get(_env("SEARXNG_URL").rstrip("/") + "/search", params=params, timeout=30))
    return [_hit(x["url"], x.get("title"), x.get("content"), _iso(x.get("publishedDate"))) for x in (r.json().get("results") or [])[:limit] if x.get("url")]


async def _jina(client, q, limit, tbs):
    r = _raise_for(await client.get("https://s.jina.ai/", params={"q": q[:400]}, timeout=40,
                                    headers={"Authorization": f"Bearer {_env('JINA_API_KEY')}", "Accept": "application/json", "X-Respond-With": "no-content"}))
    return [_hit(x["url"], x.get("title"), x.get("description") or (x.get("content") or "")[:600], _iso(x.get("date"))) for x in (r.json().get("data") or [])[:limit] if x.get("url")]


async def _parallel(client, q, limit, tbs):
    import radar_intel
    plain = re.sub(r"\b(site|inurl|intitle):\S+", "", q).replace('"', "").strip()
    return (await radar_intel.parallel_search(client, f"Find pages about: {plain}", [plain[:200]]))[:limit]


SEARCH_ADAPTERS = {"firecrawl": _firecrawl, "tavily": _tavily, "serper": _serper, "brave": _brave, "google_cse": _google_cse,
                   "serpapi": _serpapi, "searxng": _searxng, "jina": _jina, "parallel": _parallel}
# Engines that honour site:, quotes and OR (LinkedIn X-ray and ATS discovery need them).
KEYWORD_ENGINES = ("firecrawl", "tavily", "serper", "brave", "google_cse", "serpapi", "searxng", "jina")


async def search(client: httpx.AsyncClient, query: str, *, limit: int = 8, tbs: Optional[str] = None, problems: Optional[List[str]] = None,
                 bulk: bool = False, keyword_only: bool = False, cache_hours: float = 24, only: Optional[List[str]] = None) -> List[dict]:
    """Ranked pages for a query from the first web-search provider that finds something; cached for a day."""
    key = "q:" + hashlib.sha1(json.dumps([query, limit, tbs, keyword_only, only]).encode()).hexdigest()
    cached = await _cache_get(key, cache_hours)
    if cached is not None:
        return cached
    names = [n for n in (only or SEARCH_ADAPTERS) if not keyword_only or n in KEYWORD_ENGINES]
    calls = {n: (lambda n=n: SEARCH_ADAPTERS[n](client, query, limit, tbs)) for n in names}
    found, _ = await pv.first("web_search", calls, on_error=_noting(problems, "Web search failed"), bulk=bulk)
    found = found or []
    if found:
        await _cache_put(key, found)
    return found


def _noting(problems: Optional[List[str]], prefix: str):
    def on_error(spec, exc):
        if problems is None:
            return
        import radar_intel
        text = f"{prefix}: {radar_intel.why(exc)}"
        if text not in problems:
            problems.append(text)
    return on_error


async def _cache_get(key: str, hours: float):
    if hours <= 0:
        return None
    try:
        import database
        hit = await database.db.web_cache.find_one({"key": key}, {"_id": 0})
    except Exception:  # noqa: BLE001
        return None
    if hit and hit.get("at", "") > (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat():
        return hit.get("results")
    return None


async def _cache_put(key: str, results) -> None:
    try:
        import database
        await database.db.web_cache.update_one({"key": key}, {"$set": {"key": key, "at": datetime.now(timezone.utc).isoformat(), "results": results}}, upsert=True)
    except Exception:  # noqa: BLE001
        pass


# =====================================================================================
# Page reading
# =====================================================================================

async def _read_firecrawl(client, url):
    import firecrawl_api
    return await firecrawl_api.read(client, url)


async def _read_jina(client, url):
    headers = {"Accept": "application/json", **({"Authorization": f"Bearer {_env('JINA_API_KEY')}"} if _env("JINA_API_KEY") else {})}
    r = _raise_for(await client.get("https://r.jina.ai/" + url, headers=headers, timeout=45))
    d = r.json().get("data") or {}
    import firecrawl_api
    return {"title": d.get("title") or url, "text": firecrawl_api.plain(d.get("content") or ""), "date": _iso(d.get("publishedTime"))}


async def _read_tavily(client, url):
    r = _raise_for(await client.post("https://api.tavily.com/extract", json={"urls": [url]}, headers={"Authorization": f"Bearer {_env('TAVILY_API_KEY')}"}, timeout=45))
    res = (r.json().get("results") or [{}])[0]
    import firecrawl_api
    return {"title": url, "text": firecrawl_api.plain(res.get("raw_content") or ""), "date": ""}


async def _read_parallel(client, url):
    r = _raise_for(await client.post("https://api.parallel.ai/v1/extract", headers={"x-api-key": _env("PARALLEL_API_KEY")}, timeout=90,
                                     json={"urls": [url], "objective": "The page's main content, including people's names and job titles", "max_chars_total": 12000}))
    res = (r.json().get("results") or [{}])[0]
    return {"title": res.get("title") or url, "text": res.get("full_content") or "\n".join(res.get("excerpts") or []), "date": _iso(res.get("publish_date"))}


async def _read_wayback(client, url):
    """The Internet Archive's latest copy of a page: keyless, and it reads pages whose own site blocks automated requests."""
    r = _raise_for(await client.get("https://archive.org/wayback/available", params={"url": url}, headers=UA, timeout=20))
    snap = ((r.json().get("archived_snapshots") or {}).get("closest") or {})
    if not snap.get("available") or not snap.get("timestamp"):
        return {"title": url, "text": "", "date": ""}
    page = _raise_for(await client.get(f"https://web.archive.org/web/{snap['timestamp']}id_/{url}", headers=UA, timeout=40, follow_redirects=True))
    html = page.text[:2_000_000]
    try:
        import trafilatura
        text = await asyncio.to_thread(trafilatura.extract, html, include_links=False, include_tables=True) or ""
    except Exception:  # noqa: BLE001
        text = _strip(re.sub(r"(?is)<(script|style).*?</\1>", " ", html))
    title = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    return {"title": _strip(title.group(1)) if title else url, "text": text, "date": _iso(snap["timestamp"][:8])}


async def _read_direct(client, url):
    if not await _public_url(url):
        raise Skip()
    r = _raise_for(await client.get(url, headers={**UA, "Accept": "text/html"}, timeout=30, follow_redirects=True))
    html = r.text[:2_000_000]
    try:
        import trafilatura
        text = await asyncio.to_thread(trafilatura.extract, html, include_links=False, include_tables=True) or ""
    except Exception:  # noqa: BLE001
        text = _strip(re.sub(r"(?is)<(script|style).*?</\1>", " ", html))
    title = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    return {"title": _strip(title.group(1)) if title else url, "text": text, "date": ""}


READ_ADAPTERS = {"firecrawl_read": _read_firecrawl, "jina_reader": _read_jina, "tavily_extract": _read_tavily, "parallel_extract": _read_parallel, "direct_read": _read_direct,
                 "wayback": _read_wayback}


async def read_page(client: httpx.AsyncClient, url: str, *, problems: Optional[List[str]] = None, bulk: bool = False, prefer: Optional[str] = None) -> Optional[dict]:
    """A page's main text from the first reader that gets it; None when none could."""
    names = ([prefer] if prefer in READ_ADAPTERS else []) + [n for n in READ_ADAPTERS if n != prefer]
    calls = {n: (lambda n=n: READ_ADAPTERS[n](client, url)) for n in names}
    page, _ = await pv.first("page_read", calls, accept=lambda p: bool(p and (p.get("text") or "").strip()), on_error=_noting(problems, "Page reading failed"), bulk=bulk, max_empty=3)
    return page if page and (page.get("text") or "").strip() else None


# =====================================================================================
# News
# =====================================================================================

def _first_phrase(q: str) -> str:
    m = re.search(r'"([^"]+)"', q)
    return m.group(1) if m else q.split(" (")[0]


async def _news_google(client, q, days):
    import seo
    return [{"title": n.get("title") or "", "url": n.get("url"), "source": n.get("source") or "News", "at": n.get("at") or ""} for n in await seo._news(client, q)]


def _rss_items(content: bytes) -> List[dict]:
    from defusedxml import ElementTree as ET
    out = []
    for it in ET.fromstring(content).iter("item"):
        link = it.findtext("link") or ""
        if "bing.com" in link and "url=" in link:
            link = (parse_qs(urlparse(link).query).get("url") or [link])[0]
        try:
            at = parsedate_to_datetime(it.findtext("pubDate")).astimezone(timezone.utc).isoformat()
        except (TypeError, ValueError):
            at = ""
        src = it.findtext("{https://www.bing.com/news/search?q=rss}Source") or it.findtext("source") or urlparse(link).hostname or "News"
        out.append({"title": it.findtext("title") or "", "url": link, "source": src, "at": at})
    return out


async def _news_bing(client, q, days):
    r = _raise_for(await client.get("https://www.bing.com/news/search", params={"q": q[:300], "format": "rss"}, headers=UA, timeout=20))
    return _rss_items(r.content)


async def _news_gdelt(client, q, days):
    phrase = _first_phrase(q)
    if len(phrase) < 4:
        raise Skip()
    params = {"query": f'"{phrase}"', "mode": "artlist", "format": "json", "maxrecords": 30, "timespan": f"{min(days, 90)}d", "sort": "datedesc"}
    r = _raise_for(await client.get("https://api.gdeltproject.org/api/v2/doc/doc", params=params, headers=UA, timeout=30))
    try:
        arts = r.json().get("articles") or []
    except ValueError:
        return []  # GDELT answers a query it can't parse with a line of text
    return [{"title": a.get("title") or "", "url": a.get("url"), "source": a.get("domain") or "News", "at": _iso(a.get("seendate"))} for a in arts if a.get("url")]


async def _news_newsdata(client, q, days):
    r = await client.get("https://newsdata.io/api/1/latest", params={"apikey": _env("NEWSDATA_API_KEY"), "q": _first_phrase(q)[:100], "language": "en"}, timeout=30)
    data = r.json() if r.content else {}
    if r.status_code == 429 or "limit" in str((data.get("results") or {}) if isinstance(data.get("results"), dict) else "").lower():
        raise OutOfCredits("NewsData daily limit")
    r.raise_for_status()
    return [{"title": a.get("title") or "", "url": a.get("link"), "source": a.get("source_name") or a.get("source_id") or "News", "at": _iso(a.get("pubDate"))}
            for a in data.get("results") or [] if isinstance(a, dict) and a.get("link")]


async def _news_web(client, q, days):
    hits = await search(client, q, limit=8, tbs="qdr:y" if days > 31 else "qdr:m", keyword_only=True)
    return [{"title": h["title"], "url": h["url"], "source": urlparse(h["url"]).hostname or "Web", "at": h.get("date") or ""} for h in hits]


NEWS_ADAPTERS = {"google_news": _news_google, "bing_news": _news_bing, "gdelt": _news_gdelt, "newsdata": _news_newsdata, "web_news": _news_web}


async def news(client: httpx.AsyncClient, query: str, *, days: int = 90, problems: Optional[List[str]] = None, bulk: bool = False,
               only: Optional[List[str]] = None) -> List[dict]:
    """Recent headlines {title, url, source, at} from the first news source that has any."""
    names = only or list(NEWS_ADAPTERS)
    calls = {n: (lambda n=n: NEWS_ADAPTERS[n](client, query, days)) for n in names if n in NEWS_ADAPTERS}
    found, _ = await pv.first("news", calls, on_error=_noting(problems, "News search failed"), bulk=bulk, max_empty=3)
    return [n for n in found or [] if n.get("url")]


# =====================================================================================
# Job postings across the market (normalised postings; radar_collectors classifies them)
# =====================================================================================

ISO2_COUNTRY = {"US": "United States", "GB": "United Kingdom", "UK": "United Kingdom", "CA": "Canada", "AU": "Australia", "DE": "Germany", "FR": "France", "NL": "Netherlands",
                "IN": "India", "SG": "Singapore", "IE": "Ireland", "CH": "Switzerland", "ES": "Spain", "IT": "Italy", "BE": "Belgium", "SE": "Sweden", "AE": "United Arab Emirates"}


def _posting(company, title, text, url, *, source, ref, location="", country="", state="", posted="", domain=None, employees=None) -> dict:
    return {"company": (company or "").strip(), "title": title or "", "text": text or "", "url": url, "location": location or "", "country": country or "", "state": state or "",
            "posted": _iso(posted) or None, "source": source, "source_ref": ref, "domain": domain, "employees": employees}


async def _jobs_theirstack(client, term, settings):
    countries = [k for k, v in ISO2_COUNTRY.items() if v in (settings.get("countries") or ["United States"]) and k != "UK"]
    body = {"page": 0, "limit": 10, "posted_at_max_age_days": 60, "job_description_contains_or": [term], "order_by": [{"desc": True, "field": "date_posted"}],
            "include_total_results": False, **({"job_country_code_or": countries} if countries else {}),
            **({"min_employee_count": int(settings["min_employees"])} if settings.get("min_employees") else {})}
    r = _raise_for(await client.post("https://api.theirstack.com/v1/jobs/search", json=body, headers={"Authorization": f"Bearer {_env('THEIRSTACK_API_KEY')}"}, timeout=60))
    jobs = r.json().get("data") or []
    out = []
    for j in jobs:
        co = j.get("company_object") or {}
        out.append(_posting(j.get("company") or co.get("name"), j.get("job_title"), j.get("description"), j.get("url") or j.get("final_url"), source="theirstack",
                            ref=f"ts:{j.get('id')}", location=j.get("location") or j.get("short_location") or "", country=ISO2_COUNTRY.get(j.get("country_code") or "", j.get("country") or ""),
                            posted=j.get("date_posted"), domain=j.get("company_domain") or co.get("domain"), employees=co.get("employee_count")))
    return Billed(out, max(1, len(jobs)))


async def _jobs_jsearch(client, term, settings):
    where = "in USA" if "United States" in (settings.get("countries") or ["United States"]) else ""
    r = _raise_for(await client.get("https://jsearch.p.rapidapi.com/search", timeout=40, params={"query": f'"{term}" jobs {where}'.strip(), "page": 1, "num_pages": 1, "date_posted": "month"},
                                    headers={"X-RapidAPI-Key": _env("RAPIDAPI_KEY"), "X-RapidAPI-Host": "jsearch.p.rapidapi.com"}))
    out = []
    for j in r.json().get("data") or []:
        dom = urlparse(j.get("employer_website") or "").hostname
        out.append(_posting(j.get("employer_name"), j.get("job_title"), j.get("job_description"), j.get("job_apply_link") or j.get("job_google_link"), source="jsearch",
                            ref=f"js:{j.get('job_id')}", location=", ".join(x for x in (j.get("job_city"), j.get("job_state")) if x), country=ISO2_COUNTRY.get(j.get("job_country") or "", j.get("job_country") or ""),
                            state=j.get("job_state") or "", posted=j.get("job_posted_at_datetime_utc"), domain=registrable(dom) if dom else None))
    return out


async def _jobs_jooble(client, term, settings):
    r = _raise_for(await client.post(f"https://jooble.org/api/{_env('JOOBLE_API_KEY')}", json={"keywords": f'"{term}"', "page": 1}, timeout=40))
    return [_posting(j.get("company"), j.get("title"), j.get("snippet"), j.get("link"), source="jooble", ref=f"jb:{j.get('id')}", location=j.get("location") or "", posted=j.get("updated"))
            for j in r.json().get("jobs") or []]


async def _jobs_reed(client, term, settings):
    if "United Kingdom" not in (settings.get("countries") or []):
        raise Skip()
    auth = base64.b64encode(f"{_env('REED_API_KEY')}:".encode()).decode()
    r = _raise_for(await client.get("https://www.reed.co.uk/api/1.0/search", params={"keywords": term, "resultsToTake": 100}, headers={"Authorization": f"Basic {auth}"}, timeout=40))
    out = []
    for j in r.json().get("results") or []:
        d = j.get("date") or ""
        m = re.match(r"(\d{2})/(\d{2})/(\d{4})", d)
        out.append(_posting(j.get("employerName"), j.get("jobTitle"), j.get("jobDescription"), j.get("jobUrl"), source="reed", ref=f"reed:{j.get('jobId')}",
                            location=j.get("locationName") or "", country="United Kingdom", posted=f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else ""))
    return out


async def _jobs_usajobs(client, term, settings):
    if "United States" not in (settings.get("countries") or ["United States"]):
        raise Skip()
    r = _raise_for(await client.get("https://data.usajobs.gov/api/search", params={"Keyword": term, "DatePosted": 60, "ResultsPerPage": 100}, timeout=40,
                                    headers={"Host": "data.usajobs.gov", "User-Agent": _env("USAJOBS_EMAIL"), "Authorization-Key": _env("USAJOBS_API_KEY")}))
    out = []
    for item in ((r.json().get("SearchResult") or {}).get("SearchResultItems") or []):
        d = item.get("MatchedObjectDescriptor") or {}
        details = ((d.get("UserArea") or {}).get("Details") or {})
        out.append(_posting(d.get("DepartmentName") or d.get("OrganizationName"), d.get("PositionTitle"), "\n".join(filter(None, [details.get("JobSummary"), d.get("QualificationSummary")])),
                            d.get("PositionURI"), source="usajobs", ref=f"usaj:{item.get('MatchedObjectId')}", location=d.get("PositionLocationDisplay") or "", country="United States",
                            posted=d.get("PublicationStartDate")))
    return out


JOB_ADAPTERS = {"theirstack": _jobs_theirstack, "jsearch": _jobs_jsearch, "jooble": _jobs_jooble, "reed": _jobs_reed, "usajobs": _jobs_usajobs}


async def job_postings(client: httpx.AsyncClient, term: str, settings: dict, *, per_term: int = 2, problems: Optional[List[str]] = None) -> List[dict]:
    """Postings naming `term`, from up to `per_term` job sources (paced so the month's free allowance lasts)."""
    calls = {n: (lambda n=n: JOB_ADAPTERS[n](client, term, settings)) for n in JOB_ADAPTERS}
    out = []
    for _, got in await pv.gather("jobs", calls, on_error=_noting(problems, "Job search failed"), bulk=True, limit=per_term):
        out += got or []
    return out


# --- ATS X-ray: search engines find competitor-named postings on public ATS hosts --------------------

ATS_HOSTS = {"greenhouse": ("boards.greenhouse.io", "job-boards.greenhouse.io"), "lever": ("jobs.lever.co", "jobs.eu.lever.co"), "ashby": ("jobs.ashbyhq.com",),
             "workday": ("myworkdayjobs.com",), "smartrecruiters": ("jobs.smartrecruiters.com",), "recruitee": ("recruitee.com",), "workable": ("apply.workable.com",)}


def ats_from_url(url: str) -> Optional[dict]:
    """(ats, board) from a public posting URL: boards.greenhouse.io/acme/jobs/123 -> greenhouse 'acme'."""
    p = urlparse(url or "")
    host, segs = (p.hostname or "").lower(), [s for s in p.path.split("/") if s]
    if host.endswith("greenhouse.io") and segs and segs[0] not in ("embed", "v1"):
        return {"ats": "greenhouse", "board": segs[0]}
    if host in ("jobs.lever.co", "jobs.eu.lever.co") and segs:
        return {"ats": "lever", "board": segs[0], **({"region": "eu"} if ".eu." in host else {})}
    if host == "jobs.ashbyhq.com" and segs:
        return {"ats": "ashby", "board": segs[0]}
    if host.endswith("myworkdayjobs.com"):
        return {"ats": "workday", "board": host, "workday_url": f"https://{host}/{'/'.join(s for s in segs[:2] if not re.fullmatch(r'[a-z]{2}-[A-Z]{2}', s))}"}
    if host == "jobs.smartrecruiters.com" and segs:
        return {"ats": "smartrecruiters", "board": segs[0]}
    if host.endswith(".recruitee.com"):
        return {"ats": "recruitee", "board": host.split(".")[0]}
    if host == "apply.workable.com" and segs:
        return {"ats": "workable", "board": segs[0]}
    return None


async def ats_xray(client: httpx.AsyncClient, term: str, *, problems: Optional[List[str]] = None) -> List[dict]:
    """Public ATS boards with a posting that names `term`, found through the keyword search engines."""
    sites = " OR ".join(f"site:{h}" for hosts in ATS_HOSTS.values() for h in hosts[:1])
    hits = await search(client, f'"{term}" ({sites})', limit=10, tbs="qdr:m", keyword_only=True, bulk=True, problems=problems, cache_hours=72)
    boards, seen = [], set()
    for h in hits:
        b = ats_from_url(h["url"])
        if b and (b["ats"], b["board"]) not in seen:
            seen.add((b["ats"], b["board"]))
            boards.append({**b, "found_url": h["url"], "found_title": h["title"]})
    return boards


# =====================================================================================
# Company size, HQ and domain (mid-market vs enterprise)
# =====================================================================================

def employees_of(value) -> Optional[int]:
    """1200, "1,200", "1001-5000", "1K-5K", "10,001+" -> the lower bound as an int."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).replace(",", "").strip().lower()
    m = re.match(r"(\d+(?:\.\d+)?)\s*([km]?)", s)
    if not m:
        return None
    return int(float(m.group(1)) * {"": 1, "k": 1000, "m": 1_000_000}[m.group(2)])


_WD_BUSINESS = {"Q4830453", "Q783794", "Q891723", "Q6881511", "Q22687", "Q1616075", "Q161726", "Q2085381", "Q1058914", "Q43229", "Q210167", "Q167037", "Q658255"}
_WD_USD = {"Q4917": 1.0, "Q4916": 1.08, "Q25224": 1.27, "Q1104069": 0.73, "Q259502": 0.66, "Q8146": 0.0067, "Q80524": 0.012}


def _wd_claim_amount(claims: dict, prop: str) -> Tuple[Optional[float], Optional[str]]:
    best, best_date = None, ""
    for c in claims.get(prop) or []:
        v = ((c.get("mainsnak") or {}).get("datavalue") or {}).get("value") or {}
        if not isinstance(v, dict) or "amount" not in v:
            continue
        date = (((c.get("qualifiers") or {}).get("P585") or [{}])[0].get("datavalue") or {}).get("value", {}).get("time", "") if c.get("qualifiers") else ""
        if best is None or date > best_date or c.get("rank") == "preferred":
            best, best_date = (float(v["amount"]), (v.get("unit") or "").rsplit("/", 1)[-1]), date
    return best if best else (None, None)


def _wd_ids(claims: dict, prop: str) -> List[str]:
    return [((c.get("mainsnak") or {}).get("datavalue") or {}).get("value", {}).get("id") for c in claims.get(prop) or []
            if isinstance(((c.get("mainsnak") or {}).get("datavalue") or {}).get("value"), dict)]


async def _firmo_wikidata(client, name, domain, country):
    api = "https://www.wikidata.org/w/api.php"
    r = _raise_for(await client.get(api, params={"action": "wbsearchentities", "search": name[:120], "language": "en", "type": "item", "limit": 6, "format": "json"}, headers=UA, timeout=20))
    ids = [x["id"] for x in r.json().get("search") or []]
    if not ids:
        return {}
    r = _raise_for(await client.get(api, params={"action": "wbgetentities", "ids": "|".join(ids), "props": "claims|labels|descriptions", "languages": "en", "format": "json"}, headers=UA, timeout=25))
    ents = r.json().get("entities") or {}
    best, best_score = None, 0
    import radar_model as rm
    for qid in ids:
        e = ents.get(qid) or {}
        claims = e.get("claims") or {}
        site = ((((claims.get("P856") or [{}])[0].get("mainsnak") or {}).get("datavalue") or {}).get("value") or "") if claims.get("P856") else ""
        label = ((e.get("labels") or {}).get("en") or {}).get("value", "")
        desc = ((e.get("descriptions") or {}).get("en") or {}).get("value", "")
        score = 0
        if domain and site and registrable(urlparse(site).hostname or "") == domain:
            score += 10
        if rm.company_key(label) == rm.company_key(name):
            score += 4
        if set(_wd_ids(claims, "P31")) & _WD_BUSINESS or re.search(r"\b(company|corporation|bank|insurer|insurance|manufacturer|retailer|conglomerate|business|firm|group|provider)\b", desc, re.I):
            score += 3
        if claims.get("P1128"):
            score += 1
        if score > best_score:
            best, best_score = (qid, e, site), score
    if not best or best_score < 5:
        return {}
    qid, e, site = best
    claims = e.get("claims") or {}
    employees, _ = _wd_claim_amount(claims, "P1128")
    revenue, unit = _wd_claim_amount(claims, "P2139")
    want = [i for i in _wd_ids(claims, "P452")[:2] + _wd_ids(claims, "P17")[:1] + _wd_ids(claims, "P159")[:1] if i]
    labels = {}
    if want:
        lr = await client.get(api, params={"action": "wbgetentities", "ids": "|".join(want), "props": "labels", "languages": "en", "format": "json"}, headers=UA, timeout=20)
        labels = {k: ((v.get("labels") or {}).get("en") or {}).get("value") for k, v in (lr.json().get("entities") or {}).items()} if lr.status_code == 200 else {}
    host = urlparse(site).hostname if site else None
    return {"employees": int(employees) if employees else None, "revenue_usd": round(revenue * _WD_USD[unit]) if revenue and unit in _WD_USD else None,
            "industry": next((labels.get(i) for i in _wd_ids(claims, "P452") if labels.get(i)), None), "domain": registrable(host) if host else None,
            "hq": {"country": next((labels.get(i) for i in _wd_ids(claims, "P17") if labels.get(i)), None), "city": next((labels.get(i) for i in _wd_ids(claims, "P159") if labels.get(i)), None)},
            "ids": {"wikidata": qid}, "source_url": f"https://www.wikidata.org/wiki/{qid}"}


async def _firmo_gleif(client, name, domain, country):
    import radar_model as rm
    r = _raise_for(await client.get("https://api.gleif.org/api/v1/lei-records", params={"filter[fulltext]": name[:120], "page[size]": 8}, headers=UA, timeout=25))
    key = rm.company_key(name)
    best = None
    for rec in r.json().get("data") or []:
        a = rec.get("attributes") or {}
        ent = a.get("entity") or {}
        legal = (ent.get("legalName") or {}).get("name") or ""
        if ent.get("status") != "ACTIVE" or rm.company_key(legal) != key:
            continue
        best = (rec.get("id"), ent)
        break
    if not best:
        return {}
    lei, ent = best
    hq = ent.get("headquartersAddress") or ent.get("legalAddress") or {}
    cc = (hq.get("country") or "").upper()
    region = hq.get("region") or ""
    return {"hq": {"country": ISO2_COUNTRY.get(cc, cc), "state": region.split("-")[-1] if region.startswith("US-") else None, "city": hq.get("city"), "zip": hq.get("postalCode")},
            "legal_name": (ent.get("legalName") or {}).get("name"), "ids": {"lei": lei}, "source_url": f"https://search.gleif.org/#/record/{lei}"}


_SEC_TICKERS: Dict[str, Any] = {"at": 0, "by_key": {}}


async def _firmo_sec(client, name, domain, country):
    import radar_model as rm
    if country and country not in ("United States", "US"):
        raise Skip()
    if time.monotonic() - _SEC_TICKERS["at"] > 86400 or not _SEC_TICKERS["by_key"]:
        r = _raise_for(await client.get("https://www.sec.gov/files/company_tickers.json", headers=UA, timeout=40))
        _SEC_TICKERS["by_key"] = {rm.company_key(re.sub(r"/[A-Z]{2,3}/?$", "", v["title"]).strip()): v["cik_str"] for v in r.json().values()}
        _SEC_TICKERS["at"] = time.monotonic()
    cik = _SEC_TICKERS["by_key"].get(rm.company_key(name))
    if not cik:
        return {}
    r = _raise_for(await client.get(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json", headers=UA, timeout=30))
    d = r.json()
    addr = ((d.get("addresses") or {}).get("business") or {})
    site = d.get("website") or ""
    host = urlparse(site if "://" in site else "https://" + site).hostname if site else None
    state = addr.get("stateOrCountry") or ""
    return {"industry": (d.get("sicDescription") or "").title() or None, "domain": registrable(host) if host else None,
            "hq": {"country": "United States" if len(state) == 2 and state.isalpha() else None, "state": state if len(state) == 2 else None, "city": (addr.get("city") or "").title() or None,
                   "zip": (addr.get("zipCode") or "")[:5] or None},
            "public": bool(d.get("tickers")), "ids": {"cik": str(cik)}, "source_url": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}"}


def _ch_auth() -> dict:
    return {"Authorization": "Basic " + base64.b64encode(f"{_env('COMPANIES_HOUSE_API_KEY')}:".encode()).decode()}


async def _ch_company(client, name) -> Optional[dict]:
    import radar_model as rm
    r = _raise_for(await client.get("https://api.company-information.service.gov.uk/search/companies", params={"q": name[:120], "items_per_page": 8}, headers=_ch_auth(), timeout=20))
    key = rm.company_key(name)
    for it in r.json().get("items") or []:
        if it.get("company_status") == "active" and rm.company_key(it.get("title") or "") == key:
            return it
    return None


async def _firmo_companies_house(client, name, domain, country):
    if country and country not in ("United Kingdom", "UK", "GB"):
        raise Skip()
    it = await _ch_company(client, name)
    if not it:
        return {}
    addr = it.get("address") or {}
    return {"hq": {"country": "United Kingdom", "city": addr.get("locality"), "zip": addr.get("postal_code")}, "ids": {"companies_house": it.get("company_number")},
            "source_url": f"https://find-and-update.company-information.service.gov.uk/company/{it.get('company_number')}"}


async def _firmo_pdl(client, name, domain, country):
    params = {"website": domain} if domain else {"name": name[:120]}
    r = await client.get("https://api.peopledatalabs.com/v5/company/enrich", params=params, headers={"X-Api-Key": _env("PDL_API_KEY")}, timeout=30)
    if r.status_code == 404:
        return Billed({}, 0)
    r.raise_for_status()
    d = r.json()
    loc = d.get("location") or {}
    host = urlparse("https://" + (d.get("website") or "")).hostname if d.get("website") else None
    return {"employees": employees_of(d.get("employee_count") or d.get("size")), "industry": (d.get("industry") or "").title() or None, "domain": registrable(host) if host else None,
            "hq": {"country": (loc.get("country") or "").title() or None, "state": (loc.get("region") or "").title() or None, "city": (loc.get("locality") or "").title() or None,
                   "zip": loc.get("postal_code")}, "revenue_band": d.get("inferred_revenue"), "linkedin_url": ("https://" + d["linkedin_url"]) if d.get("linkedin_url") else None,
            "source_url": "https://www.peopledatalabs.com"}


async def _firmo_hunter(client, name, domain, country):
    if not domain:
        raise Skip()
    r = await client.get("https://api.hunter.io/v2/companies/find", params={"domain": domain, "api_key": _env("HUNTER_API_KEY")}, timeout=30)
    if r.status_code == 404:
        return {}
    r.raise_for_status()
    d = r.json().get("data") or {}
    geo = d.get("geo") or {}
    return {"employees": employees_of((d.get("metrics") or {}).get("employees")), "industry": (d.get("category") or {}).get("industry"),
            "hq": {"country": geo.get("country"), "state": geo.get("stateCode") or geo.get("state"), "city": geo.get("city"), "zip": geo.get("postalCode")},
            "source_url": f"https://hunter.io/companies/{domain}"}


FIRMO_ADAPTERS = {"wikidata": _firmo_wikidata, "gleif": _firmo_gleif, "sec": _firmo_sec, "companies_house": _firmo_companies_house, "pdl_company": _firmo_pdl, "hunter_company": _firmo_hunter}


def _merge_firmo(into: dict, got: dict, via: str) -> None:
    for k in ("employees", "revenue_usd", "industry", "domain", "revenue_band", "linkedin_url", "legal_name", "public"):
        if got.get(k) not in (None, "") and into.get(k) in (None, ""):
            into[k] = got[k]
            into.setdefault("fields_from", {})[k] = via
    hq = into.setdefault("hq", {})
    for k, v in (got.get("hq") or {}).items():
        if v and not hq.get(k):
            hq[k] = v
            into.setdefault("fields_from", {})[f"hq.{k}"] = via
    into.setdefault("ids", {}).update({k: v for k, v in (got.get("ids") or {}).items() if v})
    if got.get("source_url"):
        into.setdefault("sources", []).append({"via": via, "url": got["source_url"]})


async def firmographics(client: httpx.AsyncClient, name: str, domain: Optional[str] = None, country: Optional[str] = None, *, bulk: bool = False,
                        problems: Optional[List[str]] = None) -> dict:
    """Employees, revenue, industry, HQ and domain, merged along the chain until the essentials are known."""
    import radar_model as rm
    out: dict = {"hq": {}}

    def complete() -> bool:
        hq = out["hq"]
        located = bool(hq.get("country")) and (rm.country_norm(hq["country"]) not in ("United States", "Canada") or bool(hq.get("state") or hq.get("zip")))
        return bool(out.get("employees") and located and (out.get("domain") or domain) and out.get("industry"))
    for spec in await pv.plan("firmographics"):
        if complete():
            break
        if not (await pv.ready(spec, bulk))[0]:
            continue
        country_now = out["hq"].get("country") or country
        call = FIRMO_ADAPTERS[spec.name]
        ok, got = await pv._attempt(spec, lambda: call(client, name, out.get("domain") or domain, country_now), _noting(problems, "Company lookup failed"))
        if ok and got:
            _merge_firmo(out, got, spec.name)
    return out if any(v for k, v in out.items() if k != "hq") or any(out["hq"].values()) else {}


# =====================================================================================
# People who work at the account now
# =====================================================================================

TARGET_ROLES = '("CIO" OR "Chief Information" OR "Chief Data" OR "IT Director" OR "Head of IT" OR "Enterprise Architect" OR "Records Manager" OR "Information Governance" OR "VP Infrastructure")'


def _person(name, title, *, source_url, via, email=None, linkedin_url=None, current=None, source_title=None, **extra) -> dict:
    return {"name": re.sub(r"\s+", " ", name or "").strip(), "title": (title or "").strip(), "email": email, "linkedin_url": linkedin_url, "source_url": source_url,
            "source_title": source_title or via, "via": via, "current": current, **{k: v for k, v in extra.items() if v is not None}}


def _title_case(name: str) -> str:
    return " ".join(w.capitalize() if w.islower() or w.isupper() else w for w in (name or "").split())


_XRAY_TITLE = re.compile(r"^(?P<name>[^|\-–—]{3,60}?)\s+[-–—]\s+(?P<rest>.+?)(?:\s*\|\s*LinkedIn.*)?$")


def xray_people(hits: List[dict], company: str) -> List[dict]:
    """Search results for public LinkedIn profiles whose headline names the company: "Jane Doe - CIO - Acme | LinkedIn".
    The headline shows a person's current employer, so these are people who work there now."""
    import radar_intel
    tokens = radar_intel._name_tokens(company) or [re.sub(r"[^a-z0-9]", "", company.lower())]
    out = []
    for h in hits:
        url = h.get("url") or ""
        if not re.match(r"https?://([a-z]{2,3}\.)?linkedin\.com/in/", url):
            continue
        m = _XRAY_TITLE.match((h.get("title") or "").strip())
        if not m:
            continue
        name = m.group("name").strip()
        parts = [p.strip() for p in re.split(r"\s+[-–—]\s+", m.group("rest")) if p.strip()]
        employer = parts[-1] if parts else ""
        title = parts[0] if len(parts) >= 2 else ""
        if not any(t in employer.lower() for t in tokens) or len(name.split()) < 2 or not re.match(r"^[A-Z][\w'.\-]+(?: [A-Z][\w'.\-]+){1,3}$", name):
            continue
        out.append(_person(name, title, source_url=url, via="linkedin_xray", linkedin_url=url.split("?")[0], current=True, source_title=h.get("title")))
    return out


async def linkedin_xray(client: httpx.AsyncClient, company: str, *, problems: Optional[List[str]] = None) -> Tuple[List[dict], List[dict]]:
    """(people, the search hits that name them) for public profiles that list the company as the current employer."""
    import radar_intel
    short = radar_intel.search_name(company)
    hits = await search(client, f'site:linkedin.com/in "{short}" {TARGET_ROLES}', limit=10, keyword_only=True, problems=problems, cache_hours=24 * 7)
    people = xray_people(hits, short)
    names = {p["source_url"] for p in people}
    return people, [{**h, "kind": "web:xray"} for h in hits if h["url"] in names]


async def _people_tomba(client, domain, company):
    if not domain:
        raise Skip()
    r = _raise_for(await client.get("https://api.tomba.io/v1/domain-search", params={"domain": domain, "limit": 10}, timeout=30,
                                    headers={"X-Tomba-Key": _env("TOMBA_KEY"), "X-Tomba-Secret": _env("TOMBA_SECRET")}))
    out = []
    for e in ((r.json().get("data") or {}).get("emails") or []):
        name = e.get("full_name") or " ".join(x for x in (e.get("first_name"), e.get("last_name")) if x)
        if not name or len(name.split()) < 2:
            continue
        srcs = e.get("sources") or []
        current = True if any(s.get("still_on_page") for s in srcs) else None
        out.append(_person(name, e.get("position"), email=e.get("email"), linkedin_url=e.get("linkedin"), via="tomba", current=current,
                           source_url=(srcs[0].get("uri") if srcs else None) or f"https://tomba.io/domain-search/{domain}", email_confidence=e.get("score"),
                           email_status=((e.get("verification") or {}).get("status"))))
    return out


async def _snov_token(client) -> str:
    form = {"grant_type": "client_credentials", "client_id": _env("SNOV_CLIENT_ID"), "client_secret": _env("SNOV_CLIENT_SECRET")}
    r = _raise_for(await client.post("https://api.snov.io/v1/oauth/access_token", data=form, timeout=20))
    return r.json()["access_token"]


async def _snov_task(client, start_url: str, payload: dict, result_base: str) -> Any:
    token = await _snov_token(client)
    auth = {"Authorization": f"Bearer {token}"}
    r = _raise_for(await client.post(start_url, json=payload, headers=auth, timeout=30))
    d = r.json()
    link = ((d.get("links") or {}).get("result")) or (f"{result_base}{(d.get('meta') or {}).get('task_hash') or d.get('task_hash')}" if ((d.get("meta") or {}).get("task_hash") or d.get("task_hash")) else None)
    if not link:
        return d.get("data")
    for _ in range(6):
        await asyncio.sleep(2)
        res = _raise_for(await client.get(link, headers=auth, timeout=30)).json()
        if str(res.get("status", "completed")).lower() == "completed":
            return res.get("data")
    return None


async def _people_snov(client, domain, company):
    if not domain:
        raise Skip()
    data = await _snov_task(client, "https://api.snov.io/v2/domain-search/prospects/start", {"domain": domain}, "https://api.snov.io/v2/domain-search/prospects/result/")
    out = []
    for p in data or []:
        name = " ".join(x for x in (p.get("first_name"), p.get("last_name")) if x)
        if len(name.split()) >= 2:
            out.append(_person(name, p.get("position"), via="snov", source_url=p.get("source_page") or f"https://snov.io/domain-search?domain={domain}", linkedin_url=p.get("linkedin_url")))
    return out


async def _people_pdl(client, domain, company):
    if not domain:
        raise Skip()
    body = {"size": 10, "query": {"bool": {"must": [{"term": {"job_company_website": domain}},
                                                    {"terms": {"job_title_levels": ["cxo", "vp", "director", "owner", "partner"]}}]}}}
    r = await client.post("https://api.peopledatalabs.com/v5/person/search", json=body, headers={"X-Api-Key": _env("PDL_API_KEY")}, timeout=40)
    if r.status_code == 404:
        return Billed([], 0)
    r.raise_for_status()
    rows = r.json().get("data") or []
    return Billed([_person(_title_case(p.get("full_name") or ""), _title_case(p.get("job_title") or ""), via="pdl", current=True,
                           linkedin_url=("https://" + p["linkedin_url"]) if p.get("linkedin_url") and not str(p["linkedin_url"]).startswith("http") else p.get("linkedin_url"),
                           source_url=("https://" + p["linkedin_url"]) if p.get("linkedin_url") and not str(p["linkedin_url"]).startswith("http") else (p.get("linkedin_url") or "https://www.peopledatalabs.com"))
                   for p in rows if p.get("full_name")], max(1, len(rows)))


async def _people_apollo(client, domain, company):
    if not domain:
        raise Skip()
    body = {"q_organization_domains_list": [domain], "person_seniorities": ["c_suite", "vp", "director", "head"], "page": 1, "per_page": 10}
    r = _raise_for(await client.post("https://api.apollo.io/api/v1/mixed_people/search", json=body, headers={"X-Api-Key": _env("APOLLO_API_KEY"), "Cache-Control": "no-cache"}, timeout=40))
    out = []
    for p in r.json().get("people") or []:
        name = p.get("name") or " ".join(x for x in (p.get("first_name"), p.get("last_name")) if x)
        if name and len(name.split()) >= 2 and "*" not in name:
            out.append(_person(name, p.get("title"), via="apollo", current=True, linkedin_url=p.get("linkedin_url"), source_url=p.get("linkedin_url") or f"https://app.apollo.io/#/people?q={quote(name)}"))
    return out


async def _people_companies_house(client, domain, company, country=None):
    if country and country not in ("United Kingdom", "UK", "GB"):
        raise Skip()
    it = await _ch_company(client, company)
    if not it:
        return []
    num = it["company_number"]
    r = _raise_for(await client.get(f"https://api.company-information.service.gov.uk/company/{num}/officers", params={"items_per_page": 50}, headers=_ch_auth(), timeout=20))
    out = []
    for o in r.json().get("items") or []:
        if o.get("resigned_on") or "," not in (o.get("name") or ""):
            continue
        last, first = [x.strip() for x in o["name"].split(",", 1)]
        name = _title_case(f"{first.split()[0] if first else ''} {last}")
        out.append(_person(name, (o.get("occupation") or o.get("officer_role") or "").replace("-", " ").title(), via="companies_house", current=True,
                           source_url=f"https://find-and-update.company-information.service.gov.uk/company/{num}/officers"))
    return out


async def people(client: httpx.AsyncClient, company: dict, domain: Optional[str], *, problems: Optional[List[str]] = None) -> List[dict]:
    """People from every contact-data source that's set up, asked together (Hunter is called by radar_intel)."""
    country = (company.get("hq") or {}).get("country")
    calls = {"tomba": lambda: _people_tomba(client, domain, company["name"]), "snov": lambda: _people_snov(client, domain, company["name"]),
             "pdl_person": lambda: _people_pdl(client, domain, company["name"]), "apollo": lambda: _people_apollo(client, domain, company["name"]),
             "companies_house_officers": lambda: _people_companies_house(client, domain, company["name"], country)}
    out = []
    for _, got in await pv.gather("people", calls, on_error=_noting(problems, "People search failed")):
        out += got or []
    return out


# =====================================================================================
# A named person's work email, and whether a mailbox exists
# =====================================================================================

def _tomba_headers() -> dict:
    return {"X-Tomba-Key": _env("TOMBA_KEY"), "X-Tomba-Secret": _env("TOMBA_SECRET")}


async def _find_hunter(client, domain, first, last):
    import radar_intel
    return await radar_intel.hunter_email(client, domain, first, last)


async def _find_tomba(client, domain, first, last):
    r = await client.get("https://api.tomba.io/v1/email-finder", params={"domain": domain, "first_name": first, "last_name": last}, headers=_tomba_headers(), timeout=30)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    d = r.json().get("data") or {}
    if not d.get("email") or (d.get("score") or 0) < 50:
        return None
    return {"email": d["email"].lower(), "score": d.get("score"), "status": ((d.get("verification") or {}).get("status")),
            "source_url": ((d.get("sources") or [{}])[0]).get("uri")}


async def _find_snov(client, domain, first, last):
    data = await _snov_task(client, "https://api.snov.io/v2/emails-by-domain-by-name/start", {"rows": [{"first_name": first, "last_name": last, "domain": domain}]},
                            "https://api.snov.io/v2/emails-by-domain-by-name/result?task_hash=")
    for row in data or []:
        for res in row.get("result") or []:
            if res.get("email") and str(res.get("smtp_status", "valid")).lower() in ("valid", "unknown"):
                return {"email": res["email"].lower(), "score": None, "status": res.get("smtp_status"), "source_url": None}
    return None


async def _find_prospeo(client, domain, first, last):
    r = _raise_for(await client.post("https://api.prospeo.io/email-finder", json={"first_name": first, "last_name": last, "company": domain},
                                     headers={"X-KEY": _env("PROSPEO_API_KEY"), "Content-Type": "application/json"}, timeout=30))
    d = r.json()
    if d.get("error"):
        if re.search(r"credit|limit", str(d.get("message") or d.get("error_code") or ""), re.I):
            raise OutOfCredits(str(d.get("message") or "Prospeo credits used"))
        return None
    resp = d.get("response") or {}
    em = resp.get("email") if isinstance(resp.get("email"), str) else (resp.get("email") or {}).get("email")
    return {"email": em.lower(), "score": None, "status": (resp.get("email_status") or "").lower() or None, "source_url": None} if em else None


async def _find_skrapp(client, domain, first, last):
    r = _raise_for(await client.get("https://api.skrapp.io/api/v2/find", params={"firstName": first, "lastName": last, "domain": domain},
                                    headers={"X-Access-Key": _env("SKRAPP_API_KEY")}, timeout=30))
    d = r.json()
    em = d.get("email")
    return {"email": em.lower(), "score": None, "status": ((d.get("quality") or {}).get("status")), "source_url": None} if em else None


FIND_ADAPTERS = {"hunter_find": _find_hunter, "tomba_find": _find_tomba, "snov_find": _find_snov, "prospeo": _find_prospeo, "skrapp": _find_skrapp}


async def find_email(client: httpx.AsyncClient, domain: str, first: str, last: str, *, problems: Optional[List[str]] = None,
                     skip: Optional[set] = None) -> Tuple[Optional[dict], Optional[str]]:
    """(hit, provider) from the first finder with a confident address."""
    calls = {n: (lambda n=n: FIND_ADAPTERS[n](client, domain, first, last)) for n in FIND_ADAPTERS if n not in (skip or set())}
    return await pv.first("email_find", calls, accept=lambda h: bool(h and h.get("email")), on_error=_noting(problems, "Email finder failed"), max_empty=3)


async def _verify_reacher(client, email):
    headers = {"Authorization": _env("REACHER_TOKEN")} if _env("REACHER_TOKEN") else {}
    res = _raise_for(await client.post(f"{_env('REACHER_URL').rstrip('/')}/v0/check_email", json={"to_email": email}, headers=headers, timeout=60)).json()
    reach = res.get("is_reachable")
    smtp = res.get("smtp") or {}
    return {"status": {"safe": "valid", "invalid": "invalid", "risky": "catch_all" if smtp.get("is_catch_all") else "risky"}.get(reach, "unknown"), "detail": f"Reacher: {reach}"}


async def _verify_mbv(client, email):
    d = _raise_for(await client.get("https://api.mailboxvalidator.com/v2/validation/single", params={"key": _env("MAILBOXVALIDATOR_API_KEY"), "email": email, "format": "json"}, timeout=40)).json()
    if d.get("error"):
        msg = str((d.get("error") or {}).get("error_message") if isinstance(d.get("error"), dict) else d.get("error"))
        if re.search(r"credit|limit|quota", msg, re.I):
            raise OutOfCredits(msg)
        raise ValueError(msg)
    if d.get("is_server_down") or d.get("is_greylisted"):
        status = "unknown"
    elif d.get("is_catchall"):
        status = "catch_all"
    elif d.get("status") is True or str(d.get("status")).lower() == "true":
        status = "valid"
    else:
        status = "invalid"
    return {"status": status, "detail": f"MailboxValidator score {d.get('mailboxvalidator_score')}"}


async def _verify_zerobounce(client, email):
    d = _raise_for(await client.get("https://api.zerobounce.net/v2/validate", params={"api_key": _env("ZEROBOUNCE_API_KEY"), "email": email, "ip_address": ""}, timeout=40)).json()
    if d.get("error"):
        if re.search(r"credit", str(d["error"]), re.I):
            raise OutOfCredits(d["error"])
        raise ValueError(d["error"])
    st = (d.get("status") or "").lower()
    return {"status": {"valid": "valid", "invalid": "invalid", "catch-all": "catch_all", "spamtrap": "invalid", "abuse": "risky", "do_not_mail": "risky"}.get(st, "unknown"),
            "detail": f"ZeroBounce: {st}{' / ' + d['sub_status'] if d.get('sub_status') else ''}"}


async def _verify_abstract(client, email):
    d = _raise_for(await client.get("https://emailvalidation.abstractapi.com/v1/", params={"api_key": _env("ABSTRACT_EMAIL_API_KEY"), "email": email}, timeout=40)).json()
    deliv = (d.get("deliverability") or "").upper()
    catch = (d.get("is_catchall_email") or {}).get("value")
    status = "catch_all" if catch and deliv != "UNDELIVERABLE" else {"DELIVERABLE": "valid", "UNDELIVERABLE": "invalid", "RISKY": "risky"}.get(deliv, "unknown")
    return {"status": status, "detail": f"Abstract: {deliv.lower() or 'unknown'}"}


async def _verify_hunter(client, email):
    r = await client.get("https://api.hunter.io/v2/email-verifier", params={"email": email, "api_key": _env("HUNTER_API_KEY")}, timeout=40)
    if r.status_code == 202:
        return {"status": "unknown", "detail": "Hunter: still checking"}
    r.raise_for_status()
    d = r.json().get("data") or {}
    st = d.get("status")
    return {"status": {"valid": "valid", "invalid": "invalid", "accept_all": "catch_all", "disposable": "invalid", "webmail": "risky"}.get(st, "unknown"), "detail": f"Hunter: {st}"}


async def _verify_tomba(client, email):
    d = _raise_for(await client.get(f"https://api.tomba.io/v1/email-verifier/{quote(email)}", headers=_tomba_headers(), timeout=40)).json().get("data") or {}
    e = d.get("email") or {}
    st = (e.get("status") or e.get("result") or "").lower()
    return {"status": {"valid": "valid", "deliverable": "valid", "invalid": "invalid", "undeliverable": "invalid", "accept_all": "catch_all", "risky": "risky"}.get(st, "unknown"),
            "detail": f"Tomba: {st or 'unknown'}"}


VERIFY_ADAPTERS = {"reacher": _verify_reacher, "mailboxvalidator": _verify_mbv, "zerobounce": _verify_zerobounce, "abstract_email": _verify_abstract,
                   "hunter_verify": _verify_hunter, "tomba_verify": _verify_tomba}


async def verify_email(client: httpx.AsyncClient, email: str) -> Optional[dict]:
    """A definite answer (valid, invalid, catch-all) from the first verifier that gives one; None when none is set up."""
    calls = {n: (lambda n=n: VERIFY_ADAPTERS[n](client, email)) for n in VERIFY_ADAPTERS}
    res, via = await pv.first("email_verify", calls, accept=lambda v: bool(v and v.get("status") in ("valid", "invalid", "catch_all")), max_empty=2)
    return res


def mailbox_verifier_ready() -> bool:
    return pv.any_configured("email_verify")


# =====================================================================================
# Website visitors: which organisation does an address belong to
# =====================================================================================

_ISP = re.compile(r"\b(telecom\w*|communications?|cable|broadband|mobile|wireless|cellular|internet|isp|dsl|fiber|fibre|net(?:work)?s? ltd|verizon|comcast|at&t|att|charter|spectrum|"
                  r"cox|centurylink|lumen|frontier|vodafone|t-mobile|sprint|bt|sky|virgin|orange|telefonica|telstra|optus|rogers|bell canada|shaw|jio|airtel|deutsche telekom|"
                  r"starlink|hughes|viasat|residential|dynamic|pool|dialup|static-ip|cust)\b", re.I)
_HOSTING = re.compile(r"\b(amazon|aws|google|microsoft|azure|akamai|cloudflare|fastly|digitalocean|ovh|hetzner|linode|vultr|leaseweb|oracle cloud|alibaba|tencent|"
                      r"hosting|datacenter|data center|colo\w*|vpn|proxy|tor exit|choopa|m247|zscaler|netskope|iboss|forcepoint)\b", re.I)


def network_type(*labels: str) -> str:
    text = " ".join(x for x in labels if x)
    if _HOSTING.search(text):
        return "hosting"
    if _ISP.search(text):
        return "isp"
    if re.search(r"\b(university|college|school|\.edu\b|academ)", text, re.I):
        return "education"
    if re.search(r"\b(government|ministry|department of|\.gov\b|county|city of|state of)\b", text, re.I):
        return "government"
    return "business" if text.strip() else "unknown"


async def _ip_ipapi_is(client, ip):
    d = _raise_for(await client.get("https://api.ipapi.is", params={"q": ip, "key": _env("IPAPI_IS_KEY")}, timeout=20)).json()
    co, asn = d.get("company") or {}, d.get("asn") or {}
    kind = (co.get("type") or asn.get("type") or "").lower()
    if any(d.get(k) for k in ("is_datacenter", "is_vpn", "is_proxy", "is_tor")):
        kind = "hosting"
    return {"org": co.get("name") or asn.get("org"), "domain": co.get("domain") or asn.get("domain"), "type": {"business": "business", "isp": "isp", "hosting": "hosting",
            "education": "education", "government": "government", "banking": "business"}.get(kind, network_type(co.get("name"), asn.get("org"))), "asn": asn.get("asn"),
            "country": (d.get("location") or {}).get("country")}


async def _ip_ipinfo(client, ip):
    d = _raise_for(await client.get(f"https://api.ipinfo.io/lite/{ip}", params={"token": _env("IPINFO_TOKEN")}, timeout=20)).json()
    return {"org": d.get("as_name"), "domain": d.get("as_domain"), "type": network_type(d.get("as_name"), d.get("as_domain")), "asn": d.get("asn"), "country": d.get("country")}


async def _ip_ipregistry(client, ip):
    d = _raise_for(await client.get(f"https://api.ipregistry.co/{ip}", params={"key": _env("IPREGISTRY_KEY"), "fields": "company,connection,location.country.name,security"}, timeout=20)).json()
    co, conn, sec = d.get("company") or {}, d.get("connection") or {}, d.get("security") or {}
    kind = (co.get("type") or conn.get("type") or "").lower()
    if sec.get("is_vpn") or sec.get("is_proxy") or sec.get("is_cloud_provider") or sec.get("is_tor"):
        kind = "hosting"
    return {"org": co.get("name") or conn.get("organization"), "domain": co.get("domain") or conn.get("domain"), "type": kind if kind in ("business", "isp", "hosting", "education", "government")
            else network_type(co.get("name"), conn.get("organization")), "asn": conn.get("asn"), "country": ((d.get("location") or {}).get("country") or {}).get("name")}


def _vcard(entity: dict, field: str) -> Optional[str]:
    for row in ((entity.get("vcardArray") or [None, []])[1] or []):
        if row and row[0] == field:
            return row[3] if isinstance(row[3], str) else None
    return None


async def _ip_rdap(client, ip):
    d = _raise_for(await client.get(f"https://rdap.org/ip/{ip}", headers={"Accept": "application/rdap+json", **UA}, timeout=25, follow_redirects=True)).json()
    org, email = None, None
    for e in d.get("entities") or []:
        if "registrant" in (e.get("roles") or []) or "customer" in (e.get("roles") or []):
            org = _vcard(e, "fn") or org
            email = _vcard(e, "email") or email
    org = org or d.get("name")
    dom = registrable(email.split("@")[1]) if email and "@" in email else None
    return {"org": org, "domain": dom, "type": network_type(org, d.get("name"), dom), "asn": None, "country": d.get("country")}


async def _ip_ptr(client, ip):
    import dns.asyncresolver
    import dns.exception
    import dns.reversename
    try:
        ans = await dns.asyncresolver.resolve(dns.reversename.from_address(ip), "PTR", lifetime=6)
    except (dns.exception.DNSException, OSError):
        return {}
    host = str(ans[0]).rstrip(".").lower() if len(ans) else ""
    if not host:
        return {}
    dom = registrable(host)
    return {"org": None, "domain": dom, "type": network_type(host), "asn": None, "country": None, "host": host}


IP_ADAPTERS = {"ipapi_is": _ip_ipapi_is, "ipinfo_lite": _ip_ipinfo, "ipregistry": _ip_ipregistry, "rdap": _ip_rdap, "ptr": _ip_ptr}
ORG_TYPES = ("business", "government", "education")


def ip_hash(ip: str) -> str:
    salt = os.environ.get("UNSUBSCRIBE_SECRET") or os.environ.get("JWT_SECRET") or "solix"
    return hashlib.sha256(f"{salt}|{ip}".encode()).hexdigest()


async def ip_company(client: httpx.AsyncClient, ip: str) -> Optional[dict]:
    """The organisation behind a public address (business, government or education networks only), cached 30 days by
    a salted hash of the address: the address itself is never stored."""
    if not _public_ip(ip):
        return None
    key = ip_hash(ip)
    try:
        import database
        hit = await database.db.ip_cache.find_one({"key": key}, {"_id": 0})
        if hit and hit.get("at", "") > (datetime.now(timezone.utc) - timedelta(days=30)).isoformat():
            return hit.get("org")
    except Exception:  # noqa: BLE001
        hit = None
    calls = {n: (lambda n=n: IP_ADAPTERS[n](client, ip)) for n in IP_ADAPTERS}
    found, via = await pv.first("ip_company", calls, accept=lambda o: bool(o and o.get("type") in ORG_TYPES and (o.get("domain") or o.get("org"))), max_empty=3)
    org = {**found, "via": via} if via else None
    try:
        import database
        await database.db.ip_cache.update_one({"key": key}, {"$set": {"key": key, "org": org, "at": datetime.now(timezone.utc).isoformat()}}, upsert=True)
    except Exception:  # noqa: BLE001
        pass
    return org


# =====================================================================================
# Subdomains and DNS history (competitor-named hosts, CNAMEs into a competitor's service)
# =====================================================================================

async def _sub_crtsh(client, domain):
    r = _raise_for(await client.get("https://crt.sh/", params={"q": f"%.{domain}", "output": "json", "exclude": "expired"}, headers=UA, timeout=30))
    if not r.text.startswith("["):
        raise ValueError("crt.sh returned no JSON")
    return {"hosts": sorted({n.strip().lower().lstrip("*.") for row in r.json()[:3000] for n in (row.get("name_value") or "").split("\n") if n.strip().lower().endswith(domain)}), "targets": []}


async def _sub_certspotter(client, domain):
    headers = {**UA, **({"Authorization": f"Bearer {_env('CERTSPOTTER_API_KEY')}"} if _env("CERTSPOTTER_API_KEY") else {})}
    r = _raise_for(await client.get("https://api.certspotter.com/v1/issuances", params={"domain": domain, "include_subdomains": "true", "expand": "dns_names"}, headers=headers, timeout=30))
    return {"hosts": sorted({n.lower().lstrip("*.") for row in r.json() for n in row.get("dns_names") or [] if n.lower().endswith(domain)}), "targets": []}


async def _sub_hackertarget(client, domain):
    r = _raise_for(await client.get("https://api.hackertarget.com/hostsearch/", params={"q": domain}, headers=UA, timeout=30))
    if "API count exceeded" in r.text:
        raise OutOfCredits("HackerTarget daily limit")
    if r.text.startswith("error"):
        return {"hosts": [], "targets": []}
    return {"hosts": sorted({ln.split(",")[0].strip().lower() for ln in r.text.splitlines() if ln.strip().lower().split(",")[0].endswith(domain)}), "targets": []}


async def _sub_otx(client, domain):
    headers = {**UA, **({"X-OTX-API-KEY": _env("OTX_API_KEY")} if _env("OTX_API_KEY") else {})}
    r = _raise_for(await client.get(f"https://otx.alienvault.com/api/v1/indicators/domain/{domain}/passive_dns", headers=headers, timeout=40))
    rows = r.json().get("passive_dns") or []
    hosts = sorted({(x.get("hostname") or "").lower() for x in rows if (x.get("hostname") or "").lower().endswith(domain)})
    targets = sorted({(x.get("address") or "").lower().rstrip(".") for x in rows if x.get("record_type") == "CNAME" and x.get("address")})
    return {"hosts": hosts, "targets": targets}


async def _sub_urlscan(client, domain):
    r = _raise_for(await client.get("https://urlscan.io/api/v1/search/", params={"q": f"domain:{domain}", "size": 100}, headers={"API-Key": _env("URLSCAN_API_KEY")}, timeout=30))
    return {"hosts": sorted({((x.get("page") or {}).get("domain") or "").lower() for x in r.json().get("results") or [] if ((x.get("page") or {}).get("domain") or "").lower().endswith(domain)}),
            "targets": []}


SUB_ADAPTERS = {"crtsh": _sub_crtsh, "certspotter": _sub_certspotter, "hackertarget": _sub_hackertarget, "otx": _sub_otx, "urlscan": _sub_urlscan}


async def subdomains(client: httpx.AsyncClient, domain: str, *, sources: int = 2) -> dict:
    """{"hosts": names under the domain, "targets": CNAME targets} merged from up to `sources` providers."""
    calls = {n: (lambda n=n: SUB_ADAPTERS[n](client, domain)) for n in SUB_ADAPTERS}
    hosts, targets = set(), set()
    for _, got in await pv.gather("dns_history", calls, bulk=True, limit=sources):
        hosts |= set((got or {}).get("hosts") or [])
        targets |= set((got or {}).get("targets") or [])
    return {"hosts": sorted(hosts), "targets": sorted(targets)}


# =====================================================================================
# Public sector: tenders, and agencies already paying a competitor
# =====================================================================================

TENDER_TERMS = ["records management", "electronic archive", "data archiving", "application retirement", "legacy decommissioning", "email archiving"]
SPEND_VENDORS = {"opentext-documentum": "Open Text", "informatica-idmc": "Informatica", "smarsh": "Smarsh", "global-relay": "Global Relay", "mimecast": "Mimecast",
                 "proofpoint": "Proofpoint", "veritas-ev": "Veritas Technologies", "archive360": "Archive360", "delphix": "Delphix"}


async def _sam(client, days):
    end = datetime.now(timezone.utc)
    out = []
    for term in TENDER_TERMS:
        r = _raise_for(await client.get("https://api.sam.gov/opportunities/v2/search", timeout=40, params={
            "api_key": _env("SAM_GOV_API_KEY"), "postedFrom": (end - timedelta(days=days)).strftime("%m/%d/%Y"), "postedTo": end.strftime("%m/%d/%Y"), "title": term, "limit": 100}))
        for o in r.json().get("opportunitiesData") or []:
            agency = (o.get("fullParentPathName") or o.get("department") or "").split(".")[0].strip().title()
            if not agency:
                continue
            out.append({"company": agency, "type": "tender", "source": "sam_gov", "source_ref": o.get("noticeId"), "evidence_url": o.get("uiLink") or f"https://sam.gov/opp/{o.get('noticeId')}/view",
                        "title": (o.get("title") or "")[:300], "evidence_text": f"{o.get('type') or 'Notice'}: {o.get('title')}"[:400], "posted_at": _iso(o.get("postedDate")),
                        "country": "United States", "state": (((o.get("placeOfPerformance") or {}).get("state") or {}).get("code")), "buyer": agency, "public_sector": True})
    return Billed(out, len(TENDER_TERMS))


async def _contracts_finder(client, days):
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00")
    out = []
    for term in TENDER_TERMS[:4]:
        r = _raise_for(await client.post("https://www.contractsfinder.service.gov.uk/api/rest/2/search_notices/json", timeout=40, headers=UA,
                                         json={"searchCriteria": {"keyword": term, "publishedFrom": since, "statuses": ["Open"]}, "size": 100}))
        for n in r.json().get("noticeList") or []:
            it = n.get("item") or {}
            org = (it.get("organisationName") or "").strip()
            if not org:
                continue
            out.append({"company": org, "type": "tender", "source": "contracts_finder", "source_ref": it.get("id"), "title": (it.get("title") or "")[:300],
                        "evidence_url": f"https://www.contractsfinder.service.gov.uk/Notice/{it.get('id')}", "evidence_text": (it.get("description") or it.get("title") or "")[:400],
                        "posted_at": _iso(it.get("publishedDate")), "country": "United Kingdom", "buyer": org, "public_sector": True})
    return out


async def _usaspending(client, days):
    import radar_model as rm
    start = (datetime.now(timezone.utc) - timedelta(days=max(days, 540))).date().isoformat()
    end = datetime.now(timezone.utc).date().isoformat()
    out = []
    for key, vendor in SPEND_VENDORS.items():
        body = {"filters": {"recipient_search_text": [vendor], "time_period": [{"start_date": start, "end_date": end}], "award_type_codes": ["A", "B", "C", "D"]},
                "fields": ["Award ID", "Recipient Name", "Awarding Agency", "Awarding Sub Agency", "Award Amount", "Start Date", "generated_internal_id"],
                "limit": 50, "page": 1, "sort": "Award Amount", "order": "desc"}
        r = _raise_for(await client.post("https://api.usaspending.gov/api/v2/search/spending_by_award/", json=body, headers=UA, timeout=60))
        comp = rm.COMPETITOR_BY_KEY[key]
        for a in r.json().get("results") or []:
            if vendor.lower().split()[0] not in (a.get("Recipient Name") or "").lower():
                continue  # a reseller or a near-miss name
            agency = (a.get("Awarding Sub Agency") or a.get("Awarding Agency") or "").strip()
            if not agency:
                continue
            amount = a.get("Award Amount") or 0
            out.append({"company": agency.title(), "type": "spend", "source": "usaspending", "source_ref": f"{a.get('generated_internal_id')}|{key}",
                        "evidence_url": f"https://www.usaspending.gov/award/{a.get('generated_internal_id')}", "posted_at": _iso(a.get("Start Date")),
                        "title": f"Federal award to {a.get('Recipient Name')} (${amount:,.0f})"[:300], "evidence_text": f"{agency} awarded {a.get('Recipient Name')} ${amount:,.0f}, award {a.get('Award ID')}"[:400],
                        "competitor_key": key, "competitor_product": comp["product"], "country": "United States", "buyer": agency, "public_sector": True})
        await asyncio.sleep(0.3)
    return Billed(out, len(SPEND_VENDORS))


PUBLIC_ADAPTERS = {"sam_gov": _sam, "contracts_finder": _contracts_finder, "usaspending": _usaspending}


async def public_sector(client: httpx.AsyncClient, *, days: int = 14) -> List[dict]:
    """Signals from every public-sector source that's set up (TED stays in radar_collectors)."""
    calls = {n: (lambda n=n: PUBLIC_ADAPTERS[n](client, days)) for n in PUBLIC_ADAPTERS}
    out = []
    for _, got in await pv.gather("public_sector", calls, bulk=True):
        out += got or []
    return out


# =====================================================================================
# Admin "Test" button: one light call per provider
# =====================================================================================

async def probe(name: str) -> dict:
    spec = pv.SPEC_BY_NAME.get(name)
    if not spec:
        return {"ok": False, "detail": "Unknown provider"}
    if not pv.configured(spec):
        return {"ok": False, "detail": "Not set up: " + (", ".join(spec.env) or "keyless sources are off")}
    started = time.monotonic()
    async with httpx.AsyncClient(follow_redirects=True, headers=UA) as client:
        try:
            cap = spec.capability
            if cap == "web_search" and name in SEARCH_ADAPTERS:
                out = await SEARCH_ADAPTERS[name](client, "Solix Technologies data archiving", 3, None)
            elif cap == "page_read" and name in READ_ADAPTERS:
                out = await READ_ADAPTERS[name](client, "https://www.solix.com/")
            elif cap == "news" and name in NEWS_ADAPTERS:
                out = await NEWS_ADAPTERS[name](client, '"Microsoft" acquisition', 30)
            elif cap == "jobs" and name in JOB_ADAPTERS:
                out = await JOB_ADAPTERS[name](client, "Documentum", {"countries": ["United States"], "min_employees": 0})
            elif cap == "firmographics" and name in FIRMO_ADAPTERS:
                out = await FIRMO_ADAPTERS[name](client, "Microsoft", "microsoft.com", None)
            elif cap == "ip_company" and name in IP_ADAPTERS:
                out = await IP_ADAPTERS[name](client, "8.8.8.8")
            elif cap == "dns_history" and name in SUB_ADAPTERS:
                out = await SUB_ADAPTERS[name](client, "solix.com")
            elif cap == "email_verify" and name in VERIFY_ADAPTERS:
                out = await VERIFY_ADAPTERS[name](client, "info@solix.com")
            else:
                return {"ok": True, "detail": "Set up; this source is exercised through the Radar itself (no separate test)."}
        except Skip:
            return {"ok": True, "detail": "Set up (not applicable to the test request)."}
        except Exception as exc:  # noqa: BLE001
            import radar_intel
            return {"ok": False, "detail": radar_intel.why(exc) if isinstance(exc, httpx.HTTPStatusError) else f"{type(exc).__name__}: {str(exc)[:200]}"}
    if isinstance(out, Billed):
        out = out.value
    n = len(out) if isinstance(out, (list, dict)) else (1 if out else 0)
    return {"ok": True, "detail": f"Answered in {time.monotonic() - started:.1f}s ({n} item{'s' if n != 1 else ''}).", "sample": (out[:2] if isinstance(out, list) else out)}
