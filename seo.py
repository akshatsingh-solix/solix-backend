"""SEO / AEO / GEO analytics for the admin dashboard.

Three live data sources, each optional and cached in MongoDB so the dashboard
stays fast and API spend stays predictable:

- Semrush Analytics API (SEMRUSH_API_KEY): organic traffic, keywords, pages,
  competitors, keyword demand and authority, per country database.
- Public conversation feeds (no key): Google News, Hacker News and Reddit, for
  the industry "hot topics" radar.
- An AI model (OPENROUTER_API_KEY, free models available, or ANTHROPIC_API_KEY
  for Claude with web search): asks the buyer questions in the config and
  records which brands the answer mentions and cites (GEO).

Without a key the matching endpoint reports what is missing and the admin UI
falls back to clearly labelled sample data.
"""
import asyncio
import csv
import importlib.util
import io
import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote_plus, urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from auth import get_current_admin
from database import db, now_iso

logger = logging.getLogger("solix.seo")
router = APIRouter(prefix="/api/admin/seo", tags=["seo"], dependencies=[Depends(get_current_admin)])

def _roles(*roles: str):
    """Only these staff roles may change settings or spend API quota (viewers can still read)."""
    async def check(user: dict = Depends(get_current_admin)) -> dict:
        if user.get("role", "admin") not in roles:
            raise HTTPException(status_code=403, detail="You don't have permission to do this")
        return user
    return check


require_manage = _roles("admin")
require_editor = _roles("admin", "editor")

SEMRUSH_URL = "https://api.semrush.com/"
SEMRUSH_BACKLINKS_URL = "https://api.semrush.com/analytics/v1/"
AI_MODEL = "claude-opus-5"
UA = {"User-Agent": "SolixAdminSEO/1.0 (+https://www.solix.com)"}

# --- Defaults ---------------------------------------------------------------
# Every list here is editable from the admin (SEO > Settings). Competitors are
# a starting point; "Discover from Semrush" replaces them with the domains that
# actually share the most keywords with you in each country.

GEOS = [
    {"code": "us", "label": "United States"},
    {"code": "uk", "label": "United Kingdom"},
    {"code": "ca", "label": "Canada"},
    {"code": "de", "label": "Germany"},
    {"code": "fr", "label": "France"},
    {"code": "in", "label": "India"},
    {"code": "au", "label": "Australia"},
    {"code": "sg", "label": "Singapore"},
    {"code": "ae", "label": "UAE"},
]

_GLOBAL = ["informatica.com", "commvault.com", "opentext.com", "veritas.com", "archive360.com", "delphix.com", "bigid.com", "smarsh.com", "globalrelay.com", "platform3solutions.com", "cohesity.com", "rubrik.com"]
DEFAULT_COMPETITORS = {
    "us": _GLOBAL + ["infobelt.com", "avepoint.com"],
    "uk": ["informatica.com", "commvault.com", "opentext.com", "veritas.com", "archive360.com", "mimecast.com", "smarsh.com", "globalrelay.com", "bigid.com", "onetrust.com", "ironmountain.com", "avepoint.com"],
    "ca": _GLOBAL,
    "de": ["informatica.com", "commvault.com", "opentext.com", "veritas.com", "ser.de", "d-velop.de", "easy-software.com", "dataglobal.com", "fabasoft.com", "kgs-software.com", "bigid.com", "cohesity.com"],
    "fr": ["informatica.com", "commvault.com", "opentext.com", "veritas.com", "archive360.com", "cohesity.com", "rubrik.com", "bigid.com", "onetrust.com", "ironmountain.com", "avepoint.com", "mimecast.com"],
    "in": ["informatica.com", "commvault.com", "opentext.com", "veritas.com", "newgensoft.com", "bigid.com", "cohesity.com", "rubrik.com", "delphix.com", "securiti.ai", "archive360.com", "ironmountain.com"],
    "au": ["informatica.com", "commvault.com", "opentext.com", "veritas.com", "archive360.com", "mimecast.com", "smarsh.com", "globalrelay.com", "bigid.com", "avepoint.com", "cohesity.com", "rubrik.com"],
    "sg": ["informatica.com", "commvault.com", "opentext.com", "veritas.com", "archive360.com", "bigid.com", "securiti.ai", "onetrust.com", "cohesity.com", "rubrik.com", "smarsh.com", "delphix.com"],
    "ae": ["informatica.com", "commvault.com", "opentext.com", "veritas.com", "archive360.com", "bigid.com", "securiti.ai", "onetrust.com", "cohesity.com", "rubrik.com", "newgensoft.com", "mimecast.com"],
}

DEFAULT_INDUSTRY_KEYWORDS = [
    "data archiving", "application retirement", "legacy application decommissioning", "sap data archiving", "database archiving",
    "email archiving", "data governance", "ai governance", "data privacy compliance", "records retention",
    "enterprise ai", "data lakehouse", "unstructured data management", "ediscovery", "data masking",
]

DEFAULT_TOPICS = [
    "data archiving", "application retirement", "AI governance", "EU AI Act", "data governance",
    "enterprise AI agents", "SAP S/4HANA migration", "data privacy regulation", "records retention",
    "unstructured data", "data sovereignty", "RAG enterprise data",
]

DEFAULT_AI_PROMPTS = [
    "What are the best enterprise data archiving platforms?",
    "How do I retire legacy applications like Oracle E-Business Suite while staying compliant?",
    "Which vendors offer SAP data archiving for an S/4HANA migration?",
    "What are the leading AI governance and data governance platforms for enterprises?",
    "What are good alternatives to Informatica for application retirement and test data management?",
    "How can an enterprise make archived data usable for generative AI securely?",
]

BRAND_NAMES = {
    "solix.com": "Solix", "informatica.com": "Informatica", "commvault.com": "Commvault", "opentext.com": "OpenText",
    "veritas.com": "Veritas", "archive360.com": "Archive360", "delphix.com": "Delphix", "bigid.com": "BigID",
    "smarsh.com": "Smarsh", "globalrelay.com": "Global Relay", "platform3solutions.com": "Archon", "cohesity.com": "Cohesity",
    "rubrik.com": "Rubrik", "infobelt.com": "Infobelt", "avepoint.com": "AvePoint", "mimecast.com": "Mimecast",
    "onetrust.com": "OneTrust", "ironmountain.com": "Iron Mountain", "securiti.ai": "Securiti", "newgensoft.com": "Newgen",
    "ser.de": "SER", "d-velop.de": "d.velop", "easy-software.com": "EASY Software", "dataglobal.com": "dataglobal",
    "fabasoft.com": "Fabasoft", "kgs-software.com": "KGS",
}


def default_config() -> dict:
    return {
        "domain": "solix.com",
        "live_domain": "",
        "brand": "Solix",
        "geos": GEOS,
        "competitors": DEFAULT_COMPETITORS,
        "brand_names": BRAND_NAMES,
        "industry_keywords": DEFAULT_INDUSTRY_KEYWORDS,
        "topics": DEFAULT_TOPICS,
        "ai_prompts": DEFAULT_AI_PROMPTS,
        "limits": {"keywords": 300, "pages": 100, "competitor_keywords": 60, "gap_competitors": 3},
    }


async def load_config() -> dict:
    cfg = default_config()
    doc = await db.settings.find_one({"key": "seo"}, {"_id": 0, "key": 0, "updated_at": 0})
    if doc:
        for k, v in doc.items():
            if v not in (None, "", [], {}):
                cfg[k] = v
    return cfg


def tracked_brands(cfg: dict) -> Dict[str, str]:
    """You plus every competitor in any country, as {domain: brand name}."""
    comps = {d for ds in (cfg.get("competitors") or {}).values() for d in ds}
    return {cfg["domain"]: cfg.get("brand") or brand_of(cfg["domain"], cfg), **{d: brand_of(d, cfg) for d in sorted(comps)}}


def brand_of(domain: str, cfg: dict) -> str:
    names = cfg.get("brand_names") or {}
    if domain in names:
        return names[domain]
    base = domain.split(".")[0]
    return base[:1].upper() + base[1:]


# --- Semrush ----------------------------------------------------------------


class SemrushError(Exception):
    pass


def _num(v):
    if v is None or v == "":
        return None
    try:
        f = float(v)
        return int(f) if f.is_integer() else f
    except ValueError:
        return v


def _codes(v):
    return [int(x) for x in re.findall(r"\d+", v or "")]


def parse_semrush_csv(text: str, columns: List[str]) -> List[dict]:
    """Semrush returns ';'-separated CSV with human headers; map by position to the codes we asked for."""
    text = (text or "").strip()
    if not text:
        return []
    if text.startswith("ERROR"):
        if "NOTHING FOUND" in text:
            return []
        raise SemrushError(text)
    rows = list(csv.reader(io.StringIO(text), delimiter=";"))
    out = []
    for r in rows[1:]:
        if not r:
            continue
        out.append({c: (r[i] if i < len(r) else "") for i, c in enumerate(columns)})
    return out


class Semrush:
    def __init__(self, key: str, client: httpx.AsyncClient):
        self.key, self.client = key, client
        self.sem = asyncio.Semaphore(4)
        self.calls = 0

    async def report(self, type_: str, columns: List[str], url: str = SEMRUSH_URL, **params) -> List[dict]:
        q = {"type": type_, "key": self.key, "export_columns": ",".join(columns), **{k: v for k, v in params.items() if v is not None}}
        async with self.sem:
            self.calls += 1
            r = await self.client.get(url, params=q, timeout=60)
        if r.status_code >= 400 and not r.text.startswith("ERROR"):
            raise SemrushError(f"HTTP {r.status_code}")
        return parse_semrush_csv(r.text, columns)


KW_COLS = ["Ph", "Po", "Pp", "Nq", "Cp", "Ur", "Tr", "Co", "Kd", "Td", "Fk", "Fp", "In"]
KW_COLS_CORE = ["Ph", "Po", "Pp", "Nq", "Cp", "Ur", "Tr"]


def _keyword(r: dict, total_traffic) -> dict:
    tr = _num(r.get("Tr")) or 0
    return {
        "keyword": r.get("Ph"), "position": _num(r.get("Po")), "prev_position": _num(r.get("Pp")) or None,
        "volume": _num(r.get("Nq")) or 0, "cpc": _num(r.get("Cp")) or 0, "url": r.get("Ur"),
        "traffic_pct": tr, "traffic": round((total_traffic or 0) * tr / 100),
        "competition": _num(r.get("Co")), "kd": _num(r.get("Kd")),
        "trend": [float(x) for x in (r.get("Td") or "").split(",") if x.strip()],
        "serp": _codes(r.get("Fk")), "owned": _codes(r.get("Fp")), "intent": _codes(r.get("In")),
    }


async def organic_keywords(sr: Semrush, domain: str, geo: str, limit: int, total_traffic) -> List[dict]:
    try:
        rows = await sr.report("domain_organic", KW_COLS, domain=domain, database=geo, display_limit=limit, display_sort="tr_desc")
    except SemrushError as e:
        if "UNITS" in str(e).upper() or "KEY" in str(e).upper():
            raise
        rows = await sr.report("domain_organic", KW_COLS_CORE, domain=domain, database=geo, display_limit=limit, display_sort="tr_desc")
    return [_keyword(r, total_traffic) for r in rows]


def _history(rows: List[dict]) -> List[dict]:
    out = []
    for r in rows:
        d = r.get("Dt") or ""
        date = f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else d
        out.append({"date": date, "rank": _num(r.get("Rk")), "keywords": _num(r.get("Or")) or 0, "traffic": _num(r.get("Ot")) or 0, "traffic_cost": _num(r.get("Oc")) or 0})
    return sorted(out, key=lambda x: x["date"])


async def domain_block(sr: Semrush, domain: str, geo: str) -> dict:
    """Overview counters + 12-month history for one domain in one database."""
    rank, hist = await asyncio.gather(
        sr.report("domain_rank", ["Dn", "Rk", "Or", "Ot", "Oc", "Ad", "At", "Ac"], domain=domain, database=geo),
        sr.report("domain_rank_history", ["Rk", "Or", "Ot", "Oc", "Dt"], domain=domain, database=geo, display_limit=12, display_sort="dt_desc"),
        return_exceptions=True,
    )
    r = rank[0] if isinstance(rank, list) and rank else {}
    return {
        "domain": domain,
        "rank": _num(r.get("Rk")), "keywords": _num(r.get("Or")) or 0, "traffic": _num(r.get("Ot")) or 0,
        "traffic_cost": _num(r.get("Oc")) or 0, "paid_keywords": _num(r.get("Ad")) or 0, "paid_traffic": _num(r.get("At")) or 0,
        "history": _history(hist) if isinstance(hist, list) else [],
        "errors": [str(x) for x in (rank, hist) if isinstance(x, Exception)],
    }


async def authority(sr: Semrush, domains: List[str]) -> Dict[str, dict]:
    out = {}

    async def one(d):
        try:
            rows = await sr.report("backlinks_overview", ["ascore", "total", "domains_num"], url=SEMRUSH_BACKLINKS_URL, target=d, target_type="root_domain")
            if rows:
                out[d] = {"authority": _num(rows[0]["ascore"]), "backlinks": _num(rows[0]["total"]), "ref_domains": _num(rows[0]["domains_num"])}
        except (SemrushError, httpx.HTTPError) as e:
            logger.info("backlinks %s: %s", d, e)

    await asyncio.gather(*(one(d) for d in domains))
    return out


async def build_snapshot(sr: Semrush, cfg: dict, geo: str) -> dict:
    domain = cfg["domain"]
    limits = {**default_config()["limits"], **(cfg.get("limits") or {})}
    comps = [c for c in (cfg.get("competitors") or {}).get(geo, []) if c and c != domain][:20]
    warnings: List[str] = []

    me = await domain_block(sr, domain, geo)
    if me["errors"]:
        joined = "; ".join(me["errors"])
        if "UNITS" in joined.upper() or "WRONG KEY" in joined.upper() or "KEY" in joined.upper():
            raise SemrushError(joined)
        warnings.append(f"Overview: {joined}")

    kw_task = organic_keywords(sr, domain, geo, limits["keywords"], me["traffic"])
    pages_task = sr.report("domain_organic_unique", ["Ur", "Pc", "Tg", "Tr"], domain=domain, database=geo, display_limit=limits["pages"], display_sort="tg_desc")
    rel_task = sr.report("domain_organic_organic", ["Dn", "Cr", "Np", "Or", "Ot"], domain=domain, database=geo, display_limit=40)
    ind_task = sr.report("phrase_these", ["Ph", "Nq", "Cp", "Co", "Td", "Kd", "In"], phrase=";".join(cfg.get("industry_keywords") or [])[:3000], database=geo)
    comp_tasks = [domain_block(sr, c, geo) for c in comps]
    results = await asyncio.gather(kw_task, pages_task, rel_task, ind_task, authority(sr, [domain] + comps), *comp_tasks, return_exceptions=True)
    kws, pages, rel, ind, auth, *comp_blocks = results

    def ok(x, label):
        if isinstance(x, Exception):
            warnings.append(f"{label}: {x}")
            return []
        return x

    kws, pages, rel, ind = ok(kws, "Keywords"), ok(pages, "Pages"), ok(rel, "Competitor discovery"), ok(ind, "Industry keywords")
    auth = auth if isinstance(auth, dict) else {}
    rel_by = {r["Dn"]: r for r in rel}

    competitors = []
    for c, block in zip(comps, comp_blocks):
        if isinstance(block, Exception):
            warnings.append(f"{c}: {block}")
            continue
        info = rel_by.get(c, {})
        competitors.append({**{k: v for k, v in block.items() if k != "errors"}, "name": brand_of(c, cfg), "relevance": _num(info.get("Cr")), "common_keywords": _num(info.get("Np")), **auth.get(c, {})})

    # Keyword gap: what the closest competitors rank for that you don't (within your pulled keywords).
    gap_comps = sorted(competitors, key=lambda x: -(x.get("common_keywords") or 0))[: limits["gap_competitors"]]
    comp_kw_lists = await asyncio.gather(*(organic_keywords(sr, c["domain"], geo, limits["competitor_keywords"], c["traffic"]) for c in gap_comps), return_exceptions=True)
    mine = {k["keyword"]: k for k in kws}
    gap: Dict[str, dict] = {}
    for c, lst in zip(gap_comps, comp_kw_lists):
        if isinstance(lst, Exception):
            warnings.append(f"Keyword gap {c['domain']}: {lst}")
            continue
        c["top_keywords"] = [{k: kw[k] for k in ("keyword", "position", "volume", "url", "traffic")} for kw in lst[:25]]
        for kw in lst:
            g = gap.setdefault(kw["keyword"], {"keyword": kw["keyword"], "volume": kw["volume"], "kd": kw["kd"], "cpc": kw["cpc"], "intent": kw["intent"], "positions": {}})
            g["positions"][c["domain"]] = kw["position"]
            if kw["keyword"] in mine:
                g["positions"][domain] = mine[kw["keyword"]]["position"]

    industry = [{"keyword": r["Ph"], "volume": _num(r["Nq"]) or 0, "cpc": _num(r["Cp"]) or 0, "competition": _num(r["Co"]), "kd": _num(r["Kd"]), "intent": _codes(r["In"]), "trend": [float(x) for x in (r["Td"] or "").split(",") if x.strip()]} for r in ind]
    discovered = [{"domain": r["Dn"], "relevance": _num(r["Cr"]), "common_keywords": _num(r["Np"]), "keywords": _num(r["Or"]), "traffic": _num(r["Ot"])} for r in rel]

    return {
        "geo": geo, "domain": domain, "source": "semrush", "fetched_at": now_iso(), "api_calls": sr.calls,
        "overview": {k: me[k] for k in ("rank", "keywords", "traffic", "traffic_cost", "paid_keywords", "paid_traffic")} | auth.get(domain, {}),
        "history": me["history"],
        "keywords": kws,
        "pages": [{"url": p["Ur"], "keywords": _num(p["Pc"]) or 0, "traffic": _num(p["Tg"]) or 0, "traffic_pct": _num(p["Tr"]) or 0} for p in pages],
        "competitors": competitors,
        "discovered_competitors": discovered,
        "gap": sorted(gap.values(), key=lambda g: -(g["volume"] or 0))[:300],
        "industry_keywords": industry,
        "warnings": warnings,
    }


def semrush_client(client: httpx.AsyncClient) -> Semrush:
    key = os.environ.get("SEMRUSH_API_KEY")
    if not key:
        raise HTTPException(status_code=409, detail="Semrush is not connected. Add SEMRUSH_API_KEY to the backend environment to pull live data.")
    return Semrush(key, client)


def semrush_http_error(e: Exception) -> HTTPException:
    msg = str(e)
    if "UNITS" in msg.upper():
        return HTTPException(status_code=402, detail="Semrush has no API units left on this account. Add units in Semrush, then sync again.")
    if "KEY" in msg.upper():
        return HTTPException(status_code=401, detail="Semrush rejected the API key. Check SEMRUSH_API_KEY.")
    return HTTPException(status_code=502, detail=f"Semrush error: {msg}")


# --- Routes: config, status, snapshots ---------------------------------------


class SeoConfig(BaseModel):
    domain: Optional[str] = Field(default=None, max_length=200)
    live_domain: Optional[str] = Field(default=None, max_length=200)
    brand: Optional[str] = Field(default=None, max_length=80)
    competitors: Optional[Dict[str, List[str]]] = None
    brand_names: Optional[Dict[str, str]] = None
    industry_keywords: Optional[List[str]] = Field(default=None, max_length=100)
    topics: Optional[List[str]] = Field(default=None, max_length=40)
    ai_prompts: Optional[List[str]] = Field(default=None, max_length=30)


def _clean_domain(d: str) -> str:
    d = (d or "").strip().lower()
    d = re.sub(r"^https?://", "", d).split("/")[0]
    return d[4:] if d.startswith("www.") else d


@router.get("/config")
async def get_config():
    return await load_config()


@router.put("/config", dependencies=[Depends(require_manage)])
async def put_config(body: SeoConfig):
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    for k in ("domain", "live_domain"):
        if k in changes:
            changes[k] = _clean_domain(changes[k])
    if "competitors" in changes:
        changes["competitors"] = {g: list(dict.fromkeys(filter(None, (_clean_domain(d) for d in ds))))[:25] for g, ds in changes["competitors"].items()}
    for k in ("industry_keywords", "topics", "ai_prompts"):
        if k in changes:
            changes[k] = [s.strip()[:300] for s in changes[k] if s and s.strip()]
    changes["updated_at"] = now_iso()
    await db.settings.update_one({"key": "seo"}, {"$set": changes}, upsert=True)
    return await load_config()


@router.get("/status")
async def status():
    synced = {}
    async for d in db.seo_snapshots.find({}, {"_id": 0, "geo": 1, "fetched_at": 1}):
        synced[d["geo"]] = d["fetched_at"]
    ai = await db.seo_ai_runs.find_one({}, {"_id": 0, "ran_at": 1}, sort=[("ran_at", -1)])
    return {
        "semrush": bool(os.environ.get("SEMRUSH_API_KEY")),
        "ai": bool(ai_provider()),
        "ai_provider": (ai_provider() or {}).get("name"),
        "ai_models": (ai_provider() or {}).get("models", []),
        "ai_web": (ai_provider() or {}).get("web", False),
        "newsmcp": bool(os.environ.get("NEWSMCP_API_KEY")),
        "parallel": bool(os.environ.get("PARALLEL_API_KEY")),
        "synced": synced,
        "ai_last_run": (ai or {}).get("ran_at"),
    }


class SyncBody(BaseModel):
    geo: str = Field(default="us", pattern=r"^[a-z]{2}$")


@router.post("/sync", dependencies=[Depends(require_manage)])
async def sync(body: SyncBody):
    cfg = await load_config()
    async with httpx.AsyncClient(headers=UA) as client:
        sr = semrush_client(client)
        try:
            snap = await build_snapshot(sr, cfg, body.geo)
        except (SemrushError, httpx.HTTPError) as e:
            raise semrush_http_error(e)
    await db.seo_snapshots.replace_one({"geo": body.geo}, dict(snap), upsert=True)
    return snap


@router.get("/snapshot")
async def snapshot(geo: str = Query("us", pattern=r"^[a-z]{2}$")):
    doc = await db.seo_snapshots.find_one({"geo": geo}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="No synced data for this country yet.")
    return doc


@router.post("/competitors/discover", dependencies=[Depends(require_manage)])
async def discover(body: SyncBody):
    """The domains that share the most organic keywords with you in this country."""
    cfg = await load_config()
    async with httpx.AsyncClient(headers=UA) as client:
        sr = semrush_client(client)
        try:
            rows = await sr.report("domain_organic_organic", ["Dn", "Cr", "Np", "Or", "Ot"], domain=cfg["domain"], database=body.geo, display_limit=25)
        except (SemrushError, httpx.HTTPError) as e:
            raise semrush_http_error(e)
    return [{"domain": r["Dn"], "relevance": _num(r["Cr"]), "common_keywords": _num(r["Np"]), "keywords": _num(r["Or"]), "traffic": _num(r["Ot"])} for r in rows]


CACHE_TTL = timedelta(days=7)


async def _cached(kind: str, key: str, fetch):
    doc = await db.seo_cache.find_one({"kind": kind, "key": key}, {"_id": 0})
    if doc and doc.get("at", "") > (datetime.now(timezone.utc) - CACHE_TTL).isoformat():
        return doc["data"]
    data = await fetch()
    await db.seo_cache.replace_one({"kind": kind, "key": key}, {"kind": kind, "key": key, "at": now_iso(), "data": data}, upsert=True)
    return data


@router.get("/url")
async def url_performance(url: str = Query(..., max_length=1000), geo: str = Query("us", pattern=r"^[a-z]{2}$")):
    """Keywords one page ranks for (CMS drill-down, cached for a week)."""
    async def fetch():
        async with httpx.AsyncClient(headers=UA) as client:
            sr = semrush_client(client)
            try:
                rows = await sr.report("url_organic", ["Ph", "Po", "Nq", "Cp", "Co", "Tr", "Kd", "Td"], url=url, database=geo, display_limit=50, display_sort="tr_desc")
            except (SemrushError, httpx.HTTPError) as e:
                raise semrush_http_error(e)
        return [{"keyword": r["Ph"], "position": _num(r["Po"]), "volume": _num(r["Nq"]) or 0, "cpc": _num(r["Cp"]) or 0, "traffic_pct": _num(r["Tr"]) or 0, "kd": _num(r["Kd"]), "trend": [float(x) for x in (r["Td"] or "").split(",") if x.strip()]} for r in rows]

    return {"url": url, "geo": geo, "keywords": await _cached("url", f"{geo}|{url}", fetch)}


@router.get("/keyword")
async def keyword_detail(phrase: str = Query(..., max_length=200), geo: str = Query("us", pattern=r"^[a-z]{2}$")):
    """Who ranks on page one for a keyword, plus related questions to answer."""
    async def fetch():
        async with httpx.AsyncClient(headers=UA) as client:
            sr = semrush_client(client)
            try:
                serp, questions = await asyncio.gather(
                    sr.report("phrase_organic", ["Dn", "Ur", "Fk", "Fp"], phrase=phrase, database=geo, display_limit=10),
                    sr.report("phrase_questions", ["Ph", "Nq", "Kd"], phrase=phrase, database=geo, display_limit=15, display_sort="nq_desc"),
                )
            except (SemrushError, httpx.HTTPError) as e:
                raise semrush_http_error(e)
        return {
            "serp": [{"position": i + 1, "domain": r["Dn"], "url": r["Ur"], "owned": _codes(r["Fp"])} for i, r in enumerate(serp)],
            "questions": [{"question": r["Ph"], "volume": _num(r["Nq"]) or 0, "kd": _num(r["Kd"])} for r in questions],
        }

    return {"phrase": phrase, "geo": geo, **await _cached("keyword", f"{geo}|{phrase.lower()}", fetch)}


# --- Hot topics ---------------------------------------------------------------

STOP = set("""a an the and or of to in for on with by at from as is are was be been it its this that these those how why what
when who which your you our we they their not no vs via into over new more most can will just about after before than
up out all any per use using used get 2024 2025 2026 says said report reports year years day week data""".split())


async def _news(client, q):
    r = await client.get(f"https://news.google.com/rss/search?q={quote_plus(q + ' when:30d')}&hl=en-US&gl=US&ceid=US:en", timeout=20)
    items = []
    for it in ET.fromstring(r.content).iter("item"):
        try:
            at = parsedate_to_datetime(it.findtext("pubDate")).astimezone(timezone.utc)
        except (TypeError, ValueError):
            continue
        src = it.find("source")
        items.append({"title": it.findtext("title") or "", "url": it.findtext("link"), "at": at.isoformat(), "source": src.text if src is not None else "News", "kind": "news", "engagement": 1})
    return items


async def _hn(client, q):
    since = int(time.time()) - 30 * 86400
    r = await client.get("https://hn.algolia.com/api/v1/search", params={"query": q, "tags": "story", "numericFilters": f"created_at_i>{since}", "hitsPerPage": 30}, timeout=20)
    return [{"title": h.get("title") or "", "url": h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}", "discussion": f"https://news.ycombinator.com/item?id={h['objectID']}",
             "at": datetime.fromtimestamp(h["created_at_i"], timezone.utc).isoformat(), "source": "Hacker News", "kind": "practitioners",
             "engagement": (h.get("points") or 0) + 2 * (h.get("num_comments") or 0)} for h in r.json().get("hits", [])]


async def _reddit(client, q):
    r = await client.get("https://www.reddit.com/search.json", params={"q": q, "sort": "top", "t": "month", "limit": 30}, timeout=20)
    if r.status_code != 200:
        return []
    out = []
    for c in r.json().get("data", {}).get("children", []):
        d = c.get("data", {})
        out.append({"title": d.get("title") or "", "url": f"https://www.reddit.com{d.get('permalink', '')}", "at": datetime.fromtimestamp(d.get("created_utc", 0), timezone.utc).isoformat(),
                    "source": f"r/{d.get('subreddit')}", "kind": "community", "engagement": (d.get("score") or 0) + 2 * (d.get("num_comments") or 0)})
    return out


NEWSMCP_URL = "https://api.newsmcp.com/v0/news"
# Analysis, opinion and interviews are where analysts and SMEs speak.
NEWSMCP_KIND = {"analysis": "analysts", "opinion": "analysts", "interview": "analysts", "explainer": "analysts", "press_release": "vendor"}


async def _newsmcp(client, q, lock: Optional[asyncio.Lock] = None):
    """Story events (articles clustered, with independent-newsroom counts) from NewsMCP.
    Needs NEWSMCP_API_KEY on a server: keyless calls are blocked from cloud IPs."""
    key = os.environ.get("NEWSMCP_API_KEY")
    if not key:
        return []
    phrase = f'"{q}"' if " " in q.strip() else q
    async with lock or asyncio.Lock():  # the free plan allows one request in flight
        r = await client.get(NEWSMCP_URL, params={"q": phrase, "from": "14 days ago", "limit": 20, "fields": "one_liner,entities,sources,content_type"}, headers={"x-api-key": key}, timeout=30)
    r.raise_for_status()
    out = []
    for e in r.json().get("events", []):
        out.append({
            "title": e.get("headline") or "", "url": (e.get("sources") or [None])[0], "at": e.get("first_seen", "")[:19] + "+00:00" if e.get("first_seen") else now_iso(),
            "source": f"{e.get('newsrooms') or 1} newsrooms", "kind": NEWSMCP_KIND.get(e.get("content_type"), "news"),
            "engagement": 5 * (e.get("newsrooms") or 1), "summary": e.get("one_liner") or "",
            "entities": [x.get("name") for x in (e.get("entities") or []) if x.get("name")][:8], "sources": (e.get("sources") or [])[:5],
        })
    return [i for i in out if i["url"]]


def _terms(titles: List[str]) -> Counter:
    c = Counter()
    for t in titles:
        words = [w for w in re.findall(r"[a-z][a-z0-9\-\.]+", t.lower()) if w not in STOP]
        c.update(set(words))
        c.update({f"{a} {b}" for a, b in zip(words, words[1:])})
    return c


async def gather_topic(client, q: str, brands: Optional[Dict[str, str]] = None, news_lock: Optional[asyncio.Lock] = None) -> dict:
    results = await asyncio.gather(_news(client, q), _hn(client, q), _reddit(client, q), _newsmcp(client, q, news_lock), return_exceptions=True)
    items, failed = [], []
    for name, res in zip(("news", "hn", "reddit", "newsmcp"), results):
        if isinstance(res, Exception):
            failed.append(name)
        else:
            items.extend(res)
    now = datetime.now(timezone.utc)
    week = (now - timedelta(days=7)).isoformat()
    recent = [i for i in items if i["at"] >= week]
    older = [i for i in items if i["at"] < week]
    base = max(len(older) / 3, 1)  # prior three weeks, per week
    momentum = round(len(recent) / base, 2)
    engagement = sum(i["engagement"] for i in items)
    by_kind = Counter(i["kind"] for i in items)
    rising = _terms([i["title"] for i in recent]) - _terms([i["title"] for i in older])
    qwords = set(q.lower().split())
    rising_terms = [t for t, n in rising.most_common(20) if n >= 2 and not set(t.split()) <= qwords][:8]
    top = sorted(items, key=lambda i: (i["engagement"], i["at"]), reverse=True)[:12]
    # Which tracked brands (you and competitors) appear in the conversation.
    in_news: Counter = Counter()
    for domain, name in (brands or {}).items():
        rx = re.compile(r"\b" + re.escape(name.lower()) + r"\b")
        in_news[domain] = sum(1 for i in items if rx.search(" ".join([i["title"], i.get("summary", ""), *i.get("entities", [])]).lower()))
    entities = Counter(e for i in items for e in i.get("entities", []))
    return {
        "topic": q, "mentions_7d": len(recent), "mentions_30d": len(items), "momentum": momentum, "engagement": engagement,
        "voices": dict(by_kind), "rising_terms": rising_terms, "top": top, "failed_sources": failed,
        "brands_in_news": [{"domain": d, "brand": (brands or {})[d], "mentions": n} for d, n in in_news.most_common() if n],
        "entities": [{"name": n, "count": c} for n, c in entities.most_common(10)],
        "score": round(len(items) + 4 * len(recent) * min(momentum, 5) + engagement ** 0.5, 1),
    }


@router.get("/topics")
async def topics(refresh: bool = False):
    if not refresh:
        doc = await db.seo_cache.find_one({"kind": "topics", "key": "all"}, {"_id": 0})
        if doc and doc["at"] > (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat():
            return doc["data"]
    cfg = await load_config()
    brands = tracked_brands(cfg)
    async with httpx.AsyncClient(headers=UA, follow_redirects=True) as client:
        sem, news_lock = asyncio.Semaphore(3), asyncio.Lock()

        async def one(q):
            async with sem:
                return await gather_topic(client, q, brands, news_lock)

        rows = await asyncio.gather(*(one(q) for q in cfg["topics"]))
    data = {"fetched_at": now_iso(), "topics": sorted(rows, key=lambda r: -r["score"])}
    await db.seo_cache.replace_one({"kind": "topics", "key": "all"}, {"kind": "topics", "key": "all", "at": now_iso(), "data": data}, upsert=True)
    return data


# --- AI answer visibility (GEO) ------------------------------------------------
# Two providers, picked by which key is set (OpenRouter first):
#   OPENROUTER_API_KEY  - any OpenRouter model; free ":free" models answer from
#                         their own knowledge. OPENROUTER_WEB=1 adds OpenRouter's
#                         web search (billed per search, even on free models).
#   ANTHROPIC_API_KEY   - Claude with live web search and citations.

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_OPENROUTER_MODELS = "nvidia/nemotron-3.5-lightning:free"

AI_SYSTEM_WEB = (
    "You are answering a question from an enterprise IT or data leader who is researching vendors. "
    "Search the web, then answer the way a helpful AI assistant would: name the specific vendors and products you would "
    "recommend, most relevant first, with one line on why each fits. Keep it under 300 words."
)
AI_SYSTEM_MEMORY = (
    "You are answering a question from an enterprise IT or data leader who is researching vendors. "
    "Answer from your own knowledge the way a helpful AI assistant would: name the specific vendors and products you would "
    "recommend, most relevant first, with one line on why each fits. Keep it under 300 words."
)


def ai_provider() -> Optional[dict]:
    if os.environ.get("OPENROUTER_API_KEY"):
        models = [m.strip() for m in os.environ.get("OPENROUTER_MODELS", DEFAULT_OPENROUTER_MODELS).split(",") if m.strip()]
        return {"name": "openrouter", "models": models, "web": os.environ.get("OPENROUTER_WEB") == "1"}
    if os.environ.get("ANTHROPIC_API_KEY") and importlib.util.find_spec("anthropic"):
        return {"name": "anthropic", "models": [AI_MODEL], "web": True}
    return None


def _mentions(text: str, brands: Dict[str, str]) -> List[dict]:
    low = text.lower()
    found = []
    for domain, name in brands.items():
        idx = [m.start() for m in re.finditer(r"\b" + re.escape(name.lower()) + r"\b", low)]
        if idx:
            found.append({"domain": domain, "brand": name, "count": len(idx), "first": idx[0]})
    found.sort(key=lambda f: f["first"])
    for i, f in enumerate(found):
        f["rank"] = i + 1
    return found


def _root(url: str) -> str:
    host = urlparse(url or "").netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    parts = host.split(".")
    return ".".join(parts[-3:]) if len(parts) > 2 and parts[-2] in ("co", "com") else ".".join(parts[-2:])


async def ask_openrouter(client: httpx.AsyncClient, model: str, prompt: str, web: bool) -> dict:
    body = {
        "model": model,
        "messages": [{"role": "system", "content": AI_SYSTEM_WEB if web else AI_SYSTEM_MEMORY}, {"role": "user", "content": prompt}],
        "max_tokens": 2000,
    }
    if web:
        body["plugins"] = [{"id": "web", "max_results": 5}]
    headers = {"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}", "HTTP-Referer": os.environ.get("SITE_URL", "https://www.solix.com"), "X-Title": "Solix SEO dashboard"}
    try:
        r = await client.post(OPENROUTER_URL, json=body, headers=headers, timeout=120)
    except httpx.HTTPError:
        return {"error": "Could not reach OpenRouter."}
    if r.status_code == 429:
        return {"error": "OpenRouter rate limit reached (free models allow 50 requests a day). Try again tomorrow."}
    if r.status_code == 402:
        return {"error": "OpenRouter needs credits for this request (web search is billed even on free models)."}
    if r.status_code >= 400:
        detail = (r.json().get("error") or {}).get("message", "") if r.headers.get("content-type", "").startswith("application/json") else ""
        return {"error": f"OpenRouter error {r.status_code}. {detail}".strip()}
    data = r.json()
    if data.get("error"):
        return {"error": f"OpenRouter: {data['error'].get('message', 'request failed')}"}
    msg = ((data.get("choices") or [{}])[0].get("message") or {})
    text = msg.get("content") or ""
    cited = [a.get("url_citation", {}).get("url") for a in msg.get("annotations") or [] if a.get("type") == "url_citation"]
    if not text.strip():
        return {"error": "The model returned an empty answer."}
    return {"answer": text.strip(), "sources": [], "cited": list(dict.fromkeys(u for u in cited if u))}


async def ask_claude(client, prompt: str) -> dict:
    import anthropic

    messages = [{"role": "user", "content": prompt}]
    text, sources, cited = "", [], []
    try:
        for _ in range(3):  # continue a paused server-tool turn at most twice
            resp = await client.beta.messages.create(
                model=AI_MODEL,
                max_tokens=4000,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                system=AI_SYSTEM_WEB,
                tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 4}],
                messages=messages,
            )
            for block in resp.content:
                if block.type == "text":
                    text += block.text
                    for c in getattr(block, "citations", None) or []:
                        if getattr(c, "url", None):
                            cited.append(c.url)
                elif block.type == "web_search_tool_result" and isinstance(block.content, list):
                    sources.extend(r.url for r in block.content if getattr(r, "url", None))
            if resp.stop_reason != "pause_turn":
                break
            messages = [*messages, {"role": "assistant", "content": resp.content}]
    except anthropic.RateLimitError:
        return {"error": "Rate limited; try again in a minute."}
    except anthropic.APIStatusError as e:
        return {"error": f"AI request failed ({e.status_code})."}
    except anthropic.APIConnectionError:
        return {"error": "Could not reach the AI service."}
    if resp.stop_reason == "refusal":
        return {"error": "The model declined to answer this prompt."}
    return {"answer": text.strip(), "sources": list(dict.fromkeys(sources)), "cited": list(dict.fromkeys(cited))}


def share_of_voice(results: List[dict], brands: Dict[str, str], own: str) -> List[dict]:
    ok = [r for r in results if "error" not in r]
    sov = []
    for domain, name in brands.items():
        hits = [m for r in ok for m in r["mentions"] if m["domain"] == domain]
        cites = sum(r["cited_domains"].get(domain, 0) for r in ok)
        if hits or cites or domain == own:
            sov.append({"domain": domain, "brand": name, "prompts_mentioned": len(hits), "share": round(100 * len(hits) / max(len(ok), 1)),
                        "avg_rank": round(sum(h["rank"] for h in hits) / len(hits), 1) if hits else None, "citations": cites})
    sov.sort(key=lambda s: (-s["prompts_mentioned"], s["avg_rank"] or 99))
    return sov


@router.post("/ai-visibility/run", dependencies=[Depends(require_editor)])
async def run_ai_visibility():
    provider = ai_provider()
    if not provider:
        raise HTTPException(status_code=409, detail="AI answer tracking is not connected. Add OPENROUTER_API_KEY (free models available) or ANTHROPIC_API_KEY to the backend environment.")
    cfg = await load_config()
    brands = tracked_brands(cfg)
    jobs = [(p, m) for m in provider["models"] for p in cfg["ai_prompts"]]
    # Free OpenRouter models also cap requests per minute, so go gently.
    sem = asyncio.Semaphore(2 if provider["name"] == "openrouter" else 3)

    async with httpx.AsyncClient(headers=UA) as http:
        claude = None
        if provider["name"] == "anthropic":
            import anthropic
            claude = anthropic.AsyncAnthropic()

        async def one(prompt, model):
            async with sem:
                res = await (ask_openrouter(http, model, prompt, provider["web"]) if claude is None else ask_claude(claude, prompt))
            if "error" in res:
                return {"prompt": prompt, "model": model, **res}
            cited_roots = [_root(u) for u in res["cited"] or res["sources"]]
            return {"prompt": prompt, "model": model, **res, "mentions": _mentions(res["answer"], brands), "cited_domains": dict(Counter(cited_roots))}

        results = await asyncio.gather(*(one(p, m) for p, m in jobs))
    if all("error" in r for r in results):
        raise HTTPException(status_code=502, detail=results[0]["error"])
    run = {"ran_at": now_iso(), "model": ", ".join(provider["models"]), "provider": provider["name"], "web": provider["web"], "brand_domain": cfg["domain"],
           "results": results, "share_of_voice": share_of_voice(results, brands, cfg["domain"])}
    await db.seo_ai_runs.insert_one(dict(run))
    return run


@router.get("/ai-visibility")
async def ai_visibility():
    runs = await db.seo_ai_runs.find({}, {"_id": 0}).sort("ran_at", -1).to_list(8)
    if not runs:
        raise HTTPException(status_code=404, detail="No AI answer checks have run yet.")
    latest = runs[0]
    latest["history"] = [{"ran_at": r["ran_at"], "share": next((s["share"] for s in r["share_of_voice"] if s["domain"] == r["brand_domain"]), 0)} for r in reversed(runs)]
    return latest


# --- Topic deep dive -------------------------------------------------------------
# For one hot topic: find expert and analyst coverage (Parallel Search), read
# the full articles (Parallel Extract handles JavaScript-heavy publisher pages;
# without a key, a plain fetch), then have the AI model distil what experts are
# saying, the numbers being quoted, open debates, buyer questions, competitor
# moves and content angles, each tied to a numbered source. Cached for a day.

PARALLEL_URL = "https://api.parallel.ai/v1"
DEEP_TTL = timedelta(hours=24)
DEEP_SYSTEM = (
    "You are a B2B content strategist and industry analyst for {brand}, an enterprise data management company "
    "(archiving, application retirement, data governance and privacy, enterprise AI). You read recent coverage of one "
    "topic and report what the market is saying. Use ONLY the numbered sources you are given and cite them by number. "
    "Never invent people, statistics or quotes. Reply with one JSON object and nothing else."
)
DEEP_SCHEMA = """{
  "summary": "2-3 sentences: what the conversation is about right now and why it matters to enterprise data leaders",
  "sentiment": "positive | mixed | negative",
  "expert_views": [{"who": "person, role or outlet as named in the source", "view": "their point in one sentence", "source": 1}],
  "key_stats": [{"stat": "a specific number or finding quoted in the source", "source": 2}],
  "debates": ["a point experts disagree on or an open question"],
  "buyer_questions": ["a question an enterprise buyer is asking about this topic"],
  "competitor_moves": [{"brand": "one of the tracked competitors", "move": "what they announced or argued", "source": 3}],
  "content_angles": [{"title": "working title", "angle": "why {brand} can say something distinctive", "format": "blog | guide | checklist | webinar | report | comparison"}]
}"""


async def parallel_search(client: httpx.AsyncClient, topic: str, objective: str) -> List[dict]:
    r = await client.post(f"{PARALLEL_URL}/search", headers={"x-api-key": os.environ["PARALLEL_API_KEY"]}, timeout=60, json={
        "objective": objective,
        "search_queries": [topic, f"{topic} analysis", f"{topic} expert opinion enterprise"],
        "mode": "fast",
        "max_chars_total": 12000,
    })
    r.raise_for_status()
    return [{"url": x.get("url"), "title": x.get("title") or "", "date": x.get("publish_date"), "text": "\n".join(x.get("excerpts") or []), "via": "parallel"}
            for x in r.json().get("results", []) if x.get("url")]


async def parallel_extract(client: httpx.AsyncClient, urls: List[str], objective: str) -> Dict[str, dict]:
    r = await client.post(f"{PARALLEL_URL}/extract", headers={"x-api-key": os.environ["PARALLEL_API_KEY"]}, timeout=120, json={
        "urls": urls[:20], "objective": objective, "max_chars_total": 40000,
    })
    r.raise_for_status()
    return {x["url"]: {"title": x.get("title") or "", "text": x.get("full_content") or "\n".join(x.get("excerpts") or [])} for x in r.json().get("results", []) if x.get("url")}


def _html_text(html: str) -> str:
    try:
        import trafilatura  # optional: cleaner article extraction when installed
        text = trafilatura.extract(html) or ""
        if text:
            return text
    except ImportError:
        pass
    html = re.sub(r"(?is)<(script|style|nav|header|footer|aside|form)[^>]*>.*?</\1>", " ", html)
    return re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", html)).strip()


async def basic_extract(client: httpx.AsyncClient, url: str) -> Optional[dict]:
    try:
        r = await client.get(url, timeout=20, follow_redirects=True)
        if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
            return None
        m = re.search(r"(?is)<title[^>]*>(.*?)</title>", r.text)
        return {"title": (m.group(1).strip() if m else ""), "text": _html_text(r.text)[:8000]}
    except httpx.HTTPError:
        return None


def parse_json_object(text: str) -> Optional[dict]:
    """Free models sometimes wrap JSON in prose or code fences; take the outermost object."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


async def complete(client: httpx.AsyncClient, system: str, user: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """One completion from the configured AI provider: (text, model, error). Falls through OpenRouter models."""
    provider = ai_provider()
    if not provider:
        return None, None, "No AI key set: showing sources without a summary."
    if provider["name"] == "anthropic":
        import anthropic
        try:
            resp = await anthropic.AsyncAnthropic().messages.create(model=AI_MODEL, max_tokens=4000, system=system, messages=[{"role": "user", "content": user}])
        except anthropic.APIError as e:
            return None, AI_MODEL, f"AI request failed: {getattr(e, 'message', e)}"
        return "".join(b.text for b in resp.content if b.type == "text"), AI_MODEL, None
    headers = {"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}", "HTTP-Referer": os.environ.get("SITE_URL", "https://www.solix.com"), "X-Title": "Solix SEO dashboard"}
    error = None
    for model in provider["models"]:
        try:
            r = await client.post(OPENROUTER_URL, headers=headers, timeout=180, json={"model": model, "max_tokens": 3000, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
        except httpx.HTTPError:
            error = "Could not reach OpenRouter."
            continue
        if r.status_code == 429:
            error = "OpenRouter's free daily limit is used up; try again tomorrow."
            continue
        if r.status_code >= 400:
            error = f"OpenRouter error {r.status_code}."
            continue
        text = (((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
        if text:
            return text, model, None
        error = "The model returned an empty answer."
    return None, None, error


class DeepDiveBody(BaseModel):
    topic: str = Field(min_length=2, max_length=200)
    refresh: bool = False


@router.post("/topics/deep-dive", dependencies=[Depends(require_editor)])
async def deep_dive(body: DeepDiveBody):
    topic = body.topic.strip()
    key = topic.lower()
    if not body.refresh:
        doc = await db.seo_cache.find_one({"kind": "deep", "key": key}, {"_id": 0})
        if doc and doc["at"] > (datetime.now(timezone.utc) - DEEP_TTL).isoformat():
            return doc["data"]
    cfg = await load_config()
    brands = tracked_brands(cfg)
    brand = cfg.get("brand") or "Solix"
    objective = f"What are industry analysts, experts and practitioners saying about {topic} for enterprise data and IT leaders in the last 30 days? Prefer analysis, research and expert commentary over press releases."
    notes: List[str] = []

    async with httpx.AsyncClient(headers=UA, follow_redirects=True) as client:
        radar = await gather_topic(client, topic, brands)
        found: List[dict] = []
        if os.environ.get("PARALLEL_API_KEY"):
            try:
                found = await parallel_search(client, topic, objective)
            except (httpx.HTTPError, ValueError) as e:
                notes.append(f"Parallel search failed ({e.__class__.__name__}); using the news radar only.")
        else:
            notes.append("Add PARALLEL_API_KEY for expert and analyst sources and full-text extraction of JavaScript-heavy pages.")
        # Expert-leaning sources first, then the rest of the radar (Reddit threads are opinions, not articles).
        radar_items = sorted([i for i in radar["top"] if "reddit.com" not in (i.get("url") or "")], key=lambda i: i["kind"] != "analysts")
        candidates, seen = [], set()
        for it in found + [{"url": i["url"], "title": i["title"], "date": i["at"][:10], "text": i.get("summary", ""), "via": i["source"]} for i in radar_items]:
            u = it["url"]
            if u and u not in seen and "news.google.com" not in u:
                seen.add(u)
                candidates.append(it)
        candidates = candidates[:8]

        full: Dict[str, dict] = {}
        if candidates and os.environ.get("PARALLEL_API_KEY"):
            try:
                full = await parallel_extract(client, [c["url"] for c in candidates], objective)
            except (httpx.HTTPError, ValueError):
                notes.append("Parallel extract failed; read pages directly instead.")
        missing = [c for c in candidates if c["url"] not in full]
        if missing:
            sem = asyncio.Semaphore(4)

            async def fetch(c):
                async with sem:
                    return c["url"], await basic_extract(client, c["url"])

            for url, got in await asyncio.gather(*(fetch(c) for c in missing)):
                if got and len(got["text"]) > 400:
                    full[url] = got

        sources = []
        for c in candidates:
            got = full.get(c["url"], {})
            text = (got.get("text") or c.get("text") or "").strip()
            if len(text) < 200:
                continue
            sources.append({"n": len(sources) + 1, "url": c["url"], "title": got.get("title") or c["title"], "date": c.get("date"), "via": c.get("via"), "text": text[:6000], "chars": len(text)})

        insight, model, err = None, None, None
        if sources:
            comp_names = ", ".join(n for d, n in brands.items() if d != cfg["domain"])
            user = (f"Topic: {topic}\nTracked competitors: {comp_names}\n\nSources:\n\n"
                    + "\n\n".join(f"[{s['n']}] {s['title']} ({s['url']}, {s.get('date') or 'undated'})\n{s['text']}" for s in sources)
                    + "\n\nReturn JSON in exactly this shape (arrays may be empty; 3-6 items where the sources support it):\n" + DEEP_SCHEMA.replace("{brand}", brand))
            text, model, err = await complete(client, DEEP_SYSTEM.replace("{brand}", brand), user)
            insight = parse_json_object(text) if text else None
            if text and insight is None:
                err = "The model's answer wasn't valid JSON; try again or add another model to OPENROUTER_MODELS."
        else:
            err = "No readable sources found for this topic in the last few weeks."
        if err:
            notes.append(err)

    by_n = {s["n"]: s["url"] for s in sources}
    if insight:
        for k in ("expert_views", "key_stats", "competitor_moves"):
            items = insight.get(k)
            insight[k] = [dict(x, url=by_n.get(x.get("source"))) for x in items if isinstance(x, dict)] if isinstance(items, list) else []
        for k in ("debates", "buyer_questions", "content_angles"):
            if not isinstance(insight.get(k), list):
                insight[k] = []
    data = {
        "topic": topic, "generated_at": now_iso(), "model": model, "insight": insight, "notes": notes,
        "sources": [{k: s[k] for k in ("n", "url", "title", "date", "via", "chars")} | {"excerpt": s["text"][:400]} for s in sources],
        "radar": {k: radar[k] for k in ("mentions_7d", "mentions_30d", "momentum", "voices", "rising_terms", "brands_in_news", "entities")},
    }
    await db.seo_cache.replace_one({"kind": "deep", "key": key}, {"kind": "deep", "key": key, "at": now_iso(), "data": data}, upsert=True)
    return data
