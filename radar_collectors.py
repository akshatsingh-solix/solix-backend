"""Radar collectors: each one turns a public source into normalised signals.

A signal dict carries `company` (name), optionally `domain` and `hq`, plus the
fields in radar_service.SIGNAL_FIELDS. Collectors never touch the database;
radar_run.py stores what they return. Every collector is optional and fails
soft: a source that is down costs that source's signals for the day, nothing
else.

Sources (all free unless noted):
  ATS boards   Greenhouse, Lever, Ashby public job-board APIs; Workday career
               sites' JSON search. Configured per target in Admin > Radar.
  Crustdata    Indexed job search (paid credits, capped per run). Optional.
  DNS / certs  MX and SPF records plus certificate-transparency names (crt.sh)
               that fingerprint a competitor's service.
  News         Google News RSS for strong accounts (via seo._news).
  Filings      SEC EDGAR full-text search for competitor products in filings.
  Tenders      EU TED notices for archiving / retirement / records work.
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from urllib.parse import urlparse

import httpx

import radar_model as rm

logger = logging.getLogger("solix.radar.collect")
UA = {"User-Agent": "SolixRadar/1.0 (+https://www.solix.com; sales-ops@solix.com)"}
WINDOW_DAYS = 60


def _since(days: int = WINDOW_DAYS) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def _plain(text: Optional[str]) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</div>|</h\d>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def _date(value) -> Optional[str]:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, tz=timezone.utc).date().isoformat()
    s = str(value)
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    if m:
        return m.group(1)
    m = re.match(r"Posted (\d+)\+? Days? Ago", s, re.I)
    if m:
        return (datetime.now(timezone.utc) - timedelta(days=int(m.group(1)))).date().isoformat()
    if re.match(r"Posted (Today|Yesterday)", s, re.I):
        return (datetime.now(timezone.utc) - timedelta(days=0 if "today" in s.lower() else 1)).date().isoformat()
    return None


def classify_posting(company: str, title: str, text: str, *, url: str, location: str = "", country: str = "", state: str = "", posted: Optional[str] = None,
                     source: str, source_ref: Optional[str] = None, domain: Optional[str] = None) -> List[dict]:
    """A posting becomes one competitor_job signal per competitor it names, else at most one problem_job."""
    if rm.is_staffing(company):
        return []
    body = f"{title}\n{text}"
    out = []
    base = {"company": company, "domain": domain, "title": title, "role_title": title, "role_strength": rm.role_strength(title), "evidence_url": url,
            "location": location, "country": country, "state": state, "posted_at": posted, "source": source}
    for hit in rm.find_competitors(body):
        out.append({**base, "type": "competitor_job", "competitor_key": hit["key"], "competitor_product": hit["product"], "evidence_text": hit["sentence"],
                    "source_ref": f"{source_ref or url}|{hit['key']}"})
    if not out:
        prob = rm.find_problem(body)
        if prob:
            out.append({**base, "type": "problem_job", "problem_term": prob["term"], "evidence_text": prob["sentence"], "source_ref": source_ref or url})
    return out


# --- ATS job boards ---------------------------------------------------------------------

async def greenhouse(client: httpx.AsyncClient, t: dict) -> List[dict]:
    r = await client.get(f"https://boards-api.greenhouse.io/v1/boards/{t['board']}/jobs", params={"content": "true"}, timeout=40)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        posted = _date(j.get("first_published") or j.get("updated_at"))
        if posted and posted < _since().date().isoformat():
            continue
        loc = (j.get("location") or {}).get("name") or ""
        out += classify_posting(t["company"], j.get("title") or "", _plain(j.get("content")), url=j.get("absolute_url"), location=loc, posted=posted,
                                source="greenhouse", source_ref=f"gh:{j.get('id')}", domain=t.get("domain"))
    return out


async def lever(client: httpx.AsyncClient, t: dict) -> List[dict]:
    host = "api.eu.lever.co" if t.get("region") == "eu" else "api.lever.co"
    r = await client.get(f"https://{host}/v0/postings/{t['board']}", params={"mode": "json"}, timeout=40)
    r.raise_for_status()
    out = []
    for j in r.json():
        posted = _date(j.get("createdAt"))
        if posted and posted < _since().date().isoformat():
            continue
        lists = "\n".join(f"{l.get('text', '')}\n{_plain(l.get('content'))}" for l in j.get("lists") or [])
        text = "\n".join([j.get("descriptionPlain") or "", lists, j.get("additionalPlain") or ""])
        cats = j.get("categories") or {}
        out += classify_posting(t["company"], j.get("text") or "", text, url=j.get("hostedUrl"), location=cats.get("location") or "", country=j.get("country") or "",
                                posted=posted, source="lever", source_ref=f"lever:{j.get('id')}", domain=t.get("domain"))
    return out


async def ashby(client: httpx.AsyncClient, t: dict) -> List[dict]:
    r = await client.get(f"https://api.ashbyhq.com/posting-api/job-board/{t['board']}", timeout=40)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        posted = _date(j.get("publishedAt"))
        if posted and posted < _since().date().isoformat():
            continue
        addr = ((j.get("address") or {}).get("postalAddress") or {})
        out += classify_posting(t["company"], j.get("title") or "", j.get("descriptionPlain") or _plain(j.get("descriptionHtml")), url=j.get("jobUrl"),
                                location=j.get("location") or "", country=addr.get("addressCountry") or "", state=addr.get("addressRegion") or "", posted=posted,
                                source="ashby", source_ref=f"ashby:{j.get('id')}", domain=t.get("domain"))
    return out


def _workday_parts(url: str) -> Optional[tuple]:
    p = urlparse(url if "://" in url else "https://" + url)
    if "myworkdayjobs.com" not in p.netloc:
        return None
    tenant = p.netloc.split(".")[0]
    segs = [s for s in p.path.split("/") if s and not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", s)]
    if not segs:
        return None
    return p.netloc, tenant, segs[-1] if segs[0] in ("wday", "cxs") else segs[0]


async def workday(client: httpx.AsyncClient, t: dict) -> List[dict]:
    """Workday's career-site JSON: search each competitor term, then read matching postings."""
    parts = _workday_parts(t.get("workday_url") or t.get("board") or "")
    if not parts:
        return []
    host, tenant, site = parts
    api = f"https://{host}/wday/cxs/{tenant}/{site}"
    terms = sorted({c["terms"][0] for c in rm.COMPETITORS} | {"application retirement", "data archiving", "decommission"})
    paths: dict = {}
    for term in terms:
        try:
            r = await client.post(f"{api}/jobs", json={"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": term}, timeout=30)
            if r.status_code != 200:
                continue
            for p in r.json().get("jobPostings") or []:
                if p.get("externalPath"):
                    paths[p["externalPath"]] = p
        except httpx.HTTPError:
            continue
        await asyncio.sleep(0.3)
    out = []
    for path, p in list(paths.items())[:60]:
        try:
            d = (await client.get(f"{api}{path}", timeout=30)).json().get("jobPostingInfo") or {}
        except (httpx.HTTPError, ValueError):
            continue
        posted = _date(d.get("startDate") or p.get("postedOn"))
        if posted and posted < _since().date().isoformat():
            continue
        out += classify_posting(t["company"], d.get("title") or p.get("title") or "", _plain(d.get("jobDescription")), url=d.get("externalUrl") or f"https://{host}/{site}{path}",
                                location=d.get("location") or p.get("locationsText") or "", country=(d.get("country") or {}).get("descriptor") or "", posted=posted,
                                source="workday", source_ref=f"wd:{tenant}:{d.get('jobReqId') or path}", domain=t.get("domain"))
    return out


ATS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby, "workday": workday}


async def detect_board(client: httpx.AsyncClient, company: str, domain: Optional[str]) -> Optional[dict]:
    """Guess a public ATS board from the company's domain stem (e.g. acme.com -> greenhouse 'acme')."""
    stem = (rm.domain_of(domain) or "").split(".")[0] if domain else re.sub(r"[^a-z0-9]", "", rm.company_key(company))
    if not stem:
        return None
    probes = [("greenhouse", f"https://boards-api.greenhouse.io/v1/boards/{stem}/jobs"), ("lever", f"https://api.lever.co/v0/postings/{stem}?mode=json&limit=1"),
              ("ashby", f"https://api.ashbyhq.com/posting-api/job-board/{stem}")]
    for ats, url in probes:
        try:
            r = await client.get(url, timeout=15)
            if r.status_code == 200 and r.text.strip() not in ("[]", ""):
                return {"company": company, "domain": domain, "ats": ats, "board": stem}
        except httpx.HTTPError:
            continue
    return None


# --- Crustdata (paid, capped) --------------------------------------------------------------

async def crustdata(client: httpx.AsyncClient, settings: dict, budget: dict) -> List[dict]:
    """Competitor-named postings across the indexed job market. Each result costs 0.03 credits."""
    key = os.environ.get("CRUSTDATA_API_KEY")
    if not key:
        return []
    headers = {"Authorization": f"Bearer {key}", "x-api-version": "2025-11-01", "Content-Type": "application/json"}
    since = _since().date().isoformat()
    countries = settings.get("countries") or ["United States", "Canada", "United Kingdom"]
    fields = ["crustdata_job_id", "job_details.title", "job_details.url", "company.basic_info.name", "company.basic_info.primary_domain", "company.headcount.total",
              "location.raw", "location.country", "location.state", "content.description", "metadata.date_added"]
    out = []
    for comp in rm.COMPETITORS:
        for term in comp["terms"][:2]:
            if budget["credits"] >= budget["cap"]:
                return out
            body = {"filters": {"op": "and", "conditions": [
                {"field": "content.description", "type": "[.]", "value": term},
                {"field": "metadata.date_added", "type": "=>", "value": since},
                {"field": "location.country", "type": "in", "value": countries},
                {"field": "company.headcount.total", "type": "=>", "value": int(settings.get("min_employees") or 0)},
            ]}, "fields": fields, "sorts": [{"field": "metadata.date_added", "order": "desc"}], "limit": 50}
            try:
                r = await client.post("https://api.crustdata.com/job/search", json=body, headers=headers, timeout=60)
            except httpx.HTTPError as e:
                logger.warning("crustdata request failed: %s", e)
                continue
            if r.status_code == 429:
                await asyncio.sleep(3)
                continue
            if r.status_code >= 400:
                logger.warning("crustdata %s for %s", r.status_code, term)
                continue
            jobs = r.json().get("job_listings") or []
            budget["credits"] = round(budget["credits"] + 0.03 * len(jobs), 2)
            for j in jobs:
                c = j.get("company") or {}
                bi = c.get("basic_info") or {}
                loc = j.get("location") or {}
                jd = j.get("job_details") or {}
                out += classify_posting(bi.get("name") or "", jd.get("title") or "", (j.get("content") or {}).get("description") or "", url=jd.get("url"),
                                        location=loc.get("raw") or "", country=loc.get("country") or "", state=loc.get("state") or "",
                                        posted=_date((j.get("metadata") or {}).get("date_added")), source="crustdata", source_ref=f"cd:{j.get('crustdata_job_id')}",
                                        domain=bi.get("primary_domain"))
            await asyncio.sleep(2.1)  # 30 requests per minute
    return out


# --- DNS and certificate fingerprints -----------------------------------------------------------

def _fingerprint(value: str) -> Optional[str]:
    v = (value or "").lower()
    for fp in rm.DNS_FINGERPRINTS:
        if any(p in v for p in fp["patterns"]):
            return fp["key"]
    return None


async def dns_signals(client: httpx.AsyncClient, company: str, domain: str, use_crtsh: bool = True) -> List[dict]:
    import dns.asyncresolver
    import dns.exception
    resolver = dns.asyncresolver.Resolver()
    resolver.lifetime = 8
    out = []
    records = []
    for rtype in ("MX", "TXT"):
        try:
            for ans in await resolver.resolve(domain, rtype):
                records.append((rtype, ans.to_text().strip('"')))
        except (dns.exception.DNSException, OSError):
            continue
    if use_crtsh:
        try:
            r = await client.get("https://crt.sh/", params={"q": f"%.{domain}", "output": "json", "exclude": "expired"}, timeout=25)
            if r.status_code == 200 and r.text.startswith("["):
                names = {n.strip().lower() for row in r.json()[:2000] for n in (row.get("name_value") or "").split("\n")}
                records += [("CERT", n) for n in sorted(names) if n.endswith(domain)]
        except (httpx.HTTPError, ValueError):
            pass
    for rtype, value in records:
        key = _fingerprint(value if rtype != "CERT" else value.split("." + domain)[0] + ".")
        if not key:
            continue
        comp = rm.COMPETITOR_BY_KEY[key]
        out.append({"company": company, "domain": domain, "type": "cert" if rtype == "CERT" else "dns", "source": "crtsh" if rtype == "CERT" else "dns",
                    "source_ref": f"{domain}|{rtype}|{value}", "record_type": rtype, "record_value": value[:300], "competitor_key": key, "competitor_product": comp["product"],
                    "title": f"{rtype} record points to {comp['vendor']}", "evidence_text": f"{rtype} {value[:200]}",
                    "evidence_url": f"https://crt.sh/?q={value}" if rtype == "CERT" else f"https://dns.google/query?name={domain}&type={rtype}"})
    return out


# --- News, filings, tenders ---------------------------------------------------------------------

_EVENT_WORDS = re.compile(r"\b(acqui\w+|merger|divest\w*|spin-?off|moderni[sz]\w+|migrat\w+|decommission\w*|retir\w+|archiv\w+|cloud|consolidat\w+|transformation|cio|cto|fine[ds]?|recordkeeping)\b", re.I)


async def news_signals(client: httpx.AsyncClient, company: str, domain: Optional[str] = None) -> List[dict]:
    import seo
    q = f'"{company}" (archiving OR decommission OR "legacy systems" OR migration OR acquisition OR modernization OR recordkeeping)'
    try:
        items = await seo._news(client, q)
    except Exception as e:  # noqa: BLE001 - RSS feeds fail in many small ways
        logger.warning("news failed for %s: %s", company, e)
        return []
    out = []
    for it in items[:15]:
        title = it.get("title") or ""
        if not (_EVENT_WORDS.search(title) or rm.find_competitors(title)):
            continue
        comps = rm.find_competitors(title)
        out.append({"company": company, "domain": domain, "type": "news", "source": "google_news", "source_ref": it.get("url"), "evidence_url": it.get("url"),
                    "title": title[:300], "evidence_text": f"{it.get('source') or 'News'}: {title}"[:400], "posted_at": (it.get("at") or "")[:10],
                    "competitor_key": comps[0]["key"] if comps else None, "competitor_product": comps[0]["product"] if comps else None})
    return out


async def edgar_signals(client: httpx.AsyncClient, days: int = WINDOW_DAYS) -> List[dict]:
    """10-K/10-Q/8-K filings that name a competitor product (SEC full-text search, free)."""
    start = _since(days).date().isoformat()
    end = datetime.now(timezone.utc).date().isoformat()
    out = []
    for comp in rm.COMPETITORS:
        term = comp["terms"][0]
        try:
            r = await client.get("https://efts.sec.gov/LATEST/search-index", params={"q": f'"{term}"', "dateRange": "custom", "startdt": start, "enddt": end,
                                                                                  "forms": "10-K,10-Q,8-K,20-F,S-1"}, headers=UA, timeout=30)
            if r.status_code != 200:
                continue
            hits = (r.json().get("hits") or {}).get("hits") or []
        except (httpx.HTTPError, ValueError):
            continue
        for h in hits[:25]:
            src = h.get("_source") or {}
            names = src.get("display_names") or []
            if not names:
                continue
            name = re.sub(r"\s*\(.*$", "", names[0]).strip().title()
            if comp["vendor"].lower() in name.lower():
                continue  # the competitor's own filings
            adsh, _, filename = (h.get("_id") or "").partition(":")
            cik = (src.get("ciks") or [""])[0].lstrip("0")
            url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{adsh.replace('-', '')}/{filename}" if cik and adsh else "https://efts.sec.gov/LATEST/search-index"
            out.append({"company": name, "type": "filing", "source": "sec_edgar", "source_ref": f"{h.get('_id')}|{comp['key']}", "evidence_url": url,
                        "title": f"{src.get('form')} filed {src.get('file_date')} mentions {term}", "evidence_text": f"{src.get('form')} mentions {term}",
                        "posted_at": src.get("file_date"), "competitor_key": comp["key"], "competitor_product": comp["product"], "country": "United States"})
        await asyncio.sleep(0.2)  # SEC asks for at most 10 requests a second
    return out


TED_QUERY = '(FT~"application retirement" OR FT~"data archiving" OR FT~"archiving solution" OR FT~"legacy decommissioning" OR FT~"records management system" OR FT~"electronic archive")'
ISO3 = {"GBR": "United Kingdom", "IRL": "Ireland", "FRA": "France", "DEU": "Germany", "NLD": "Netherlands", "BEL": "Belgium", "LUX": "Luxembourg", "ESP": "Spain", "PRT": "Portugal",
        "ITA": "Italy", "AUT": "Austria", "CHE": "Switzerland", "SWE": "Sweden", "DNK": "Denmark", "FIN": "Finland", "NOR": "Norway", "POL": "Poland", "CZE": "Czechia",
        "HUN": "Hungary", "ROU": "Romania", "BGR": "Bulgaria", "GRC": "Greece", "HRV": "Croatia", "SVN": "Slovenia", "SVK": "Slovakia", "LTU": "Lithuania", "LVA": "Latvia", "EST": "Estonia"}


def _lang(v) -> str:
    if isinstance(v, dict):
        for k in ("eng", "ENG", "en"):
            if v.get(k):
                x = v[k]
                return x[0] if isinstance(x, list) else x
        x = next(iter(v.values()), "")
        return x[0] if isinstance(x, list) and x else (x if isinstance(x, str) else "")
    if isinstance(v, list):
        return v[0] if v else ""
    return v or ""


async def ted_tenders(client: httpx.AsyncClient, settings: dict, days: int = 14) -> List[dict]:
    since = _since(days).strftime("%Y%m%d")
    q = f"{TED_QUERY} AND PD>={since}"
    if settings.get("tender_countries"):
        q += " AND buyer-country IN (" + " ".join(settings["tender_countries"]) + ")"
    try:
        r = await client.post("https://api.ted.europa.eu/v3/notices/search", json={"query": q, "fields": ["publication-number", "notice-title", "buyer-name", "buyer-country",
                                                                                                            "publication-date", "links"], "limit": 100, "page": 1}, timeout=40)
        r.raise_for_status()
        notices = r.json().get("notices") or []
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("TED search failed: %s", e)
        return []
    out = []
    for n in notices:
        buyer = _lang(n.get("buyer-name"))
        if not buyer:
            continue
        num = n.get("publication-number")
        country = _lang(n.get("buyer-country"))
        title = _lang(n.get("notice-title")) or f"Tender {num}"
        out.append({"company": buyer, "type": "tender", "source": "ted", "source_ref": num, "evidence_url": f"https://ted.europa.eu/en/notice/-/detail/{num}",
                    "title": title[:300], "evidence_text": title[:400], "posted_at": str(n.get("publication-date") or "")[:10], "country": ISO3.get(country, country),
                    "buyer": buyer, "public_sector": True})
    return out
