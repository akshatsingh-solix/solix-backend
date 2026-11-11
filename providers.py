"""Free-tier-first data providers for the Radar: failover, cooldowns and quota pacing.

Every Radar function (web search, page reading, news, job postings, company size, people,
email finding and checking, website-visitor companies, DNS and certificate history, public
tenders) has a chain of providers here, most of them free. A call walks its chain in order:

* a provider without its key (or switched off) is skipped;
* one that was rate limited, ran out of credits or rejected its key rests for a while
  (60 seconds to 6 hours, by the kind of failure) and the next one answers;
* one that has used its free allowance for the month is skipped until the month turns,
  one whose one-time credits are used or past their expiry date is skipped for good (until a new
  grant is entered), and one-time credits are spread over the days to their expiry,
  and scheduled (bulk) work spreads the month's allowance over the days left, so the
  daily run never spends a whole month of credits on day one. A person clicking a
  button is never paced, only capped.

Usage is counted per provider and month in `provider_usage` (so a restart or the GitHub
runner shares the count), and Admin > Radar > Data sources shows every chain: which
providers are set up, what each has left and why one is resting. The order, a provider's
free allowance and switching one off are editable there (settings key "providers").

Keyless providers (public registries, RSS, DNS-over-HTTPS) cost nothing and need no
sign-up; RADAR_KEYLESS_SOURCES=off turns the new ones off (the test suites do this).
RADAR_PROVIDERS_DISABLED=name,name switches named providers off from the environment.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from calendar import monthrange
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger("solix.providers")


@dataclass(frozen=True)
class Spec:
    name: str                      # stable id, e.g. "tavily"
    capability: str                # chain it belongs to, e.g. "web_search"
    label: str                     # shown on the admin screen
    env: Tuple[str, ...] = ()      # every variable must be set; () means keyless
    free: str = ""                 # what the free tier gives, in words
    monthly: Optional[int] = None  # soft cap on units a calendar month (None: no known cap; errors decide)
    daily: Optional[int] = None    # soft cap on units a UTC day
    grant: Optional[int] = None    # one-time credits (not monthly); an expiry date can be set in Data sources
    signup: str = ""
    pool: str = ""                 # providers that share one allowance (Hunter searches) share a pool
    always: bool = False           # keyless and part of the original Radar: not affected by RADAR_KEYLESS_SOURCES
    note: str = ""

    @property
    def keyless(self) -> bool:
        return not self.env

    @property
    def quota_pool(self) -> str:
        return self.pool or self.name


@dataclass
class Billed:
    """An adapter's result with the units it used (a job search billed per job returned)."""
    value: Any
    units: int = 1


class Skip(Exception):
    """The provider can't answer this kind of request (a UK registry asked about a US company). Not a failure."""


class OutOfCredits(Exception):
    """The provider answered 200 but said its allowance is used up."""


CAPABILITIES: Dict[str, dict] = {
    "web_search": {"label": "Web search", "why": "Deep-dive sources, leadership pages, LinkedIn X-ray, ATS discovery"},
    "page_read": {"label": "Page reading", "why": "Full text of leadership pages and postings found by search"},
    "news": {"label": "News and triggers", "why": "Acquisitions, migrations, decommissioning, fines"},
    "jobs": {"label": "Job postings (market-wide)", "why": "Companies hiring for a competitor's product, found across the job market"},
    "firmographics": {"label": "Company size and HQ", "why": "Mid-market vs enterprise, HQ for routing, the company's domain"},
    "people": {"label": "People at the account", "why": "Decision makers who work there now"},
    "email_find": {"label": "Email finder", "why": "A named person's work address"},
    "email_verify": {"label": "Email verification", "why": "Mailbox exists before anything is sent"},
    "ip_company": {"label": "Website visitor companies", "why": "Which organisation an anonymous visitor's network belongs to"},
    "dns_history": {"label": "DNS and certificate history", "why": "Subdomains and records that fingerprint a competitor's product"},
    "public_sector": {"label": "Tenders and public spend", "why": "Public bodies buying archiving or paying a competitor"},
}

_H = "https://"
SPECS: List[Spec] = [
    # --- web search (keyword engines first: they accept site: and quoted phrases) ---
    Spec("firecrawl", "web_search", "Firecrawl", ("FIRECRAWL_API_KEY",), "Free credits each month (2 per search of up to 10 results)", signup=_H + "firecrawl.dev", pool="firecrawl"),
    Spec("tavily", "web_search", "Tavily", ("TAVILY_API_KEY",), "1,000 credits a month (1 per basic search)", monthly=1000, signup=_H + "tavily.com"),
    Spec("serper", "web_search", "Serper (Google results)", ("SERPER_API_KEY",), "2,500 free queries on sign-up (one-time)", grant=2500, signup=_H + "serper.dev",
         note="The free queries are a one-time grant; set its allowance in Data sources once you know your balance."),
    Spec("brave", "web_search", "Brave Search", ("BRAVE_API_KEY",), "$5 of credit a month (about 1,000 searches)", monthly=950, signup=_H + "brave.com/search/api/"),
    Spec("google_cse", "web_search", "Google Programmable Search", ("GOOGLE_CSE_KEY", "GOOGLE_CSE_CX"), "100 queries a day", daily=100,
         signup=_H + "developers.google.com/custom-search/v1/overview", note="Closed to new sign-ups; works with an existing key."),
    Spec("serpapi", "web_search", "SerpApi", ("SERPAPI_API_KEY",), "250 searches a month", monthly=250, signup=_H + "serpapi.com"),
    Spec("searxng", "web_search", "SearXNG (self-hosted)", ("SEARXNG_URL",), "Unlimited: your own metasearch server", signup=_H + "docs.searxng.org"),
    Spec("jina", "web_search", "Jina Search", ("JINA_API_KEY",), "Free token grant (about 10,000 tokens a search)", signup=_H + "jina.ai/reader",
         note="Check Jina's terms for commercial use of the free grant."),
    Spec("parallel", "web_search", "Parallel Search", ("PARALLEL_API_KEY",), "$5 of free credit a month", signup=_H + "parallel.ai"),
    # --- page reading ---
    Spec("firecrawl_read", "page_read", "Firecrawl", ("FIRECRAWL_API_KEY",), "1 credit a page", pool="firecrawl", signup=_H + "firecrawl.dev"),
    Spec("jina_reader", "page_read", "Jina Reader", (), "Keyless (20 pages a minute); JINA_API_KEY raises the limit", signup=_H + "jina.ai/reader"),
    Spec("tavily_extract", "page_read", "Tavily Extract", ("TAVILY_API_KEY",), "Shares Tavily's 1,000 credits", pool="tavily", signup=_H + "tavily.com"),
    Spec("parallel_extract", "page_read", "Parallel Extract", ("PARALLEL_API_KEY",), "Shares Parallel's monthly credit", pool="parallel", signup=_H + "parallel.ai"),
    Spec("wayback", "page_read", "Wayback Machine copy", (), "Keyless: the latest archived copy, for pages that block direct reading"),
    Spec("direct_read", "page_read", "Direct fetch + text extraction", (), "Keyless: the page itself, read locally"),
    # --- news ---
    Spec("google_news", "news", "Google News RSS", (), "Keyless", always=True),
    Spec("bing_news", "news", "Bing News RSS", (), "Keyless"),
    Spec("gdelt", "news", "GDELT DOC API", (), "Keyless, worldwide news in 65 languages", signup=_H + "www.gdeltproject.org"),
    Spec("newsdata", "news", "NewsData.io", ("NEWSDATA_API_KEY",), "200 credits a day", daily=190, signup=_H + "newsdata.io"),
    Spec("web_news", "news", "Web search (past year)", (), "Uses the web-search chain's allowance", always=True),
    # --- job postings across the market ---
    Spec("theirstack", "jobs", "TheirStack", ("THEIRSTACK_API_KEY",), "200 API credits a month (1 per job returned)", monthly=200, signup=_H + "theirstack.com"),
    Spec("jsearch", "jobs", "JSearch (Google for Jobs, LinkedIn, Indeed)", ("RAPIDAPI_KEY",), "About 200 requests a month on RapidAPI's free plan", monthly=150,
         signup=_H + "rapidapi.com/letscrawl/api/jsearch"),
    Spec("jooble", "jobs", "Jooble", ("JOOBLE_API_KEY",), "Free API key on request", signup=_H + "jooble.org/api/about"),
    Spec("reed", "jobs", "Reed.co.uk (UK)", ("REED_API_KEY",), "Free API key", signup=_H + "www.reed.co.uk/developers"),
    Spec("usajobs", "jobs", "USAJOBS (US federal)", ("USAJOBS_API_KEY", "USAJOBS_EMAIL"), "Free API key", signup=_H + "developer.usajobs.gov"),
    Spec("ats_xray", "jobs", "ATS X-ray (search engines + free ATS APIs)", (), "Uses the web-search chain's allowance; postings read free from the ATS", always=True),
    Spec("crustdata", "jobs", "Crustdata (paid)", ("CRUSTDATA_API_KEY",), "Paid credits, capped per run in Radar settings", signup=_H + "crustdata.com"),
    # --- company size, HQ and domain ---
    Spec("wikidata", "firmographics", "Wikidata", (), "Keyless: employees, revenue, HQ, website, industry for larger companies"),
    Spec("gleif", "firmographics", "GLEIF LEI register", (), "Keyless: legal name and HQ address for 2.8M legal entities"),
    Spec("sec", "firmographics", "SEC EDGAR", (), "Keyless: US-listed companies' HQ, industry (SIC) and website"),
    Spec("companies_house", "firmographics", "UK Companies House", ("COMPANIES_HOUSE_API_KEY",), "Free key, 600 requests per 5 minutes",
         signup=_H + "developer.company-information.service.gov.uk"),
    Spec("pdl_company", "firmographics", "People Data Labs", ("PDL_API_KEY",), "100 company lookups a month", monthly=95, pool="pdl_company", signup=_H + "peopledatalabs.com"),
    Spec("hunter_company", "firmographics", "Hunter (company)", ("HUNTER_API_KEY",), "Shares Hunter's free searches", pool="hunter", signup=_H + "hunter.io"),
    # --- people who work at the account now ---
    Spec("web_people", "people", "Leadership pages (web search + reading)", (), "Uses the web-search chain", always=True),
    Spec("linkedin_xray", "people", "LinkedIn X-ray (search engines)", (), "Uses the web-search chain; public profile titles show the current employer", always=True),
    Spec("hunter", "people", "Hunter", ("HUNTER_API_KEY",), "25 searches a month", monthly=25, pool="hunter", signup=_H + "hunter.io"),
    Spec("tomba", "people", "Tomba", ("TOMBA_KEY", "TOMBA_SECRET"), "25 searches a month", monthly=25, pool="tomba", signup=_H + "tomba.io"),
    Spec("snov", "people", "Snov.io", ("SNOV_CLIENT_ID", "SNOV_CLIENT_SECRET"), "50 credits a month", monthly=50, pool="snov", signup=_H + "snov.io"),
    Spec("pdl_person", "people", "People Data Labs", ("PDL_API_KEY",), "100 person records a month (contact details hidden on free)", monthly=95,
         pool="pdl_person", signup=_H + "peopledatalabs.com"),
    Spec("apollo", "people", "Apollo.io", ("APOLLO_API_KEY",), "People search with an Apollo API key", signup=_H + "apollo.io",
         note="Apollo's API access depends on your Apollo plan."),
    Spec("companies_house_officers", "people", "UK Companies House officers", ("COMPANIES_HOUSE_API_KEY",), "Free: directors with active appointments",
         signup=_H + "developer.company-information.service.gov.uk"),
    # --- a named person's work email ---
    Spec("hunter_find", "email_find", "Hunter Email Finder", ("HUNTER_API_KEY",), "Shares Hunter's 25 searches", monthly=25, pool="hunter", signup=_H + "hunter.io"),
    Spec("tomba_find", "email_find", "Tomba Email Finder", ("TOMBA_KEY", "TOMBA_SECRET"), "Shares Tomba's 25 searches", monthly=25, pool="tomba", signup=_H + "tomba.io"),
    Spec("snov_find", "email_find", "Snov.io Email Finder", ("SNOV_CLIENT_ID", "SNOV_CLIENT_SECRET"), "Shares Snov's 50 credits", monthly=50, pool="snov", signup=_H + "snov.io"),
    Spec("prospeo", "email_find", "Prospeo", ("PROSPEO_API_KEY",), "75 credits a month", monthly=70, signup=_H + "prospeo.io"),
    Spec("skrapp", "email_find", "Skrapp", ("SKRAPP_API_KEY",), "Free monthly email credits", signup=_H + "skrapp.io"),
    Spec("pattern_verify", "email_find", "Address patterns + verification", (), "Keyless when a mailbox verifier is set up", always=True),
    # --- does the mailbox exist ---
    Spec("reacher", "email_verify", "Reacher (self-hosted)", ("REACHER_URL",), "Unlimited on your own server", signup=_H + "reacher.email"),
    Spec("mailboxvalidator", "email_verify", "MailboxValidator", ("MAILBOXVALIDATOR_API_KEY",), "300 API credits on the free plan, which can expire", grant=300,
         signup=_H + "www.mailboxvalidator.com", note="Set the date your credits expire under Allowance."),
    Spec("zerobounce", "email_verify", "ZeroBounce", ("ZEROBOUNCE_API_KEY",), "100 checks a month (business domain sign-up)", monthly=95, signup=_H + "www.zerobounce.net"),
    Spec("abstract_email", "email_verify", "Abstract Email Validation", ("ABSTRACT_EMAIL_API_KEY",), "100 checks a month", monthly=95, signup=_H + "www.abstractapi.com"),
    Spec("hunter_verify", "email_verify", "Hunter Email Verifier", ("HUNTER_API_KEY",), "50 verifications a month", monthly=50, pool="hunter_verify", signup=_H + "hunter.io"),
    Spec("tomba_verify", "email_verify", "Tomba Verifier", ("TOMBA_KEY", "TOMBA_SECRET"), "50 verifications a month", monthly=50, pool="tomba_verify", signup=_H + "tomba.io"),
    # --- which organisation a website visitor's network belongs to ---
    Spec("ipapi_is", "ip_company", "ipapi.is", ("IPAPI_IS_KEY",), "1,000 lookups a day; company name, domain and network type", daily=950, signup=_H + "ipapi.is"),
    Spec("ipinfo_lite", "ip_company", "IPinfo Lite", ("IPINFO_TOKEN",), "Unlimited (network owner and its domain)", signup=_H + "ipinfo.io/lite",
         note="Free with attribution to IPinfo."),
    Spec("ipregistry", "ip_company", "Ipregistry", ("IPREGISTRY_KEY",), "100,000 free lookups on sign-up (one-time)", grant=100000, signup=_H + "ipregistry.co"),
    Spec("rdap", "ip_company", "RDAP (regional internet registries)", (), "Keyless: who the address block is registered to"),
    Spec("ptr", "ip_company", "Reverse DNS", (), "Keyless: the address's host name"),
    # --- subdomains and DNS history ---
    Spec("crtsh", "dns_history", "crt.sh certificate transparency", (), "Keyless", always=True),
    Spec("certspotter", "dns_history", "Cert Spotter", (), "Keyless (CERTSPOTTER_API_KEY optional for more)", signup=_H + "sslmate.com/certspotter/api/"),
    Spec("hackertarget", "dns_history", "HackerTarget host search", (), "Keyless, 50 lookups a day", daily=45),
    Spec("otx", "dns_history", "AlienVault OTX passive DNS", (), "Keyless (OTX_API_KEY optional)", signup=_H + "otx.alienvault.com"),
    Spec("urlscan", "dns_history", "urlscan.io", ("URLSCAN_API_KEY",), "Free key", signup=_H + "urlscan.io"),
    # --- public sector ---
    Spec("ted", "public_sector", "EU TED tenders", (), "Keyless", always=True),
    Spec("sam_gov", "public_sector", "SAM.gov opportunities (US federal)", ("SAM_GOV_API_KEY",), "Free key, 1,000 requests a day", daily=900, signup=_H + "sam.gov"),
    Spec("contracts_finder", "public_sector", "UK Contracts Finder", (), "Keyless"),
    Spec("usaspending", "public_sector", "USAspending.gov awards", (), "Keyless: US agencies already paying a competitor"),
]
SPEC_BY_NAME: Dict[str, Spec] = {s.name: s for s in SPECS}

# --- runtime state -------------------------------------------------------------------------

_cool: Dict[str, Tuple[float, str, float]] = {}      # key -> (monotonic until, reason, wall-clock until)
_strikes: Dict[str, List[float]] = {}                # key -> recent 429 times
_last: Dict[str, dict] = {}                          # name -> {"ok_at", "error", "error_at"}
_usage: Dict[str, dict] = {}                         # pool -> {"month", "units", "days": {dd: n}, "loaded": monotonic}
USAGE_TTL = 60


def reset() -> None:
    """Forget cooldowns and cached usage (tests, or after keys change)."""
    _cool.clear()
    _strikes.clear()
    _last.clear()
    _usage.clear()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _db():
    import database
    return database.db


def keyless_on() -> bool:
    return os.environ.get("RADAR_KEYLESS_SOURCES", "on").strip().lower() not in ("off", "0", "false", "no")


def _env_disabled() -> set:
    return {x.strip() for x in os.environ.get("RADAR_PROVIDERS_DISABLED", "").split(",") if x.strip()}


def configured(spec: Spec) -> bool:
    if spec.keyless:
        return spec.always or keyless_on()
    return all(os.environ.get(v, "").strip() for v in spec.env)


def _kid(spec: Spec) -> str:
    """Cooldowns belong to a provider *and* its key: a new key starts fresh."""
    raw = "|".join(os.environ.get(v, "") for v in spec.env)
    return spec.name + ":" + hashlib.sha1(raw.encode()).hexdigest()[:8]


async def config() -> dict:
    import cache

    async def load():
        try:
            doc = await _db().settings.find_one({"key": "providers"}, {"_id": 0}) or {}
        except Exception:  # noqa: BLE001 - a settings read must never stop a lookup
            doc = {}
        return {"order": doc.get("order") or {}, "disabled": list(doc.get("disabled") or []), "limits": doc.get("limits") or {}}
    return await cache.memo("providers_cfg", "cfg", 60, load)


async def save_config(values: dict) -> dict:
    import cache
    from database import now_iso
    clean = {}
    if "order" in values:
        clean["order"] = {cap: [n for n in names if n in SPEC_BY_NAME and SPEC_BY_NAME[n].capability == cap]
                          for cap, names in (values.get("order") or {}).items() if cap in CAPABILITIES}
    if "disabled" in values:
        clean["disabled"] = sorted({n for n in values.get("disabled") or [] if n in SPEC_BY_NAME})
    if "limits" in values:
        # monthly / daily allowances, or one-time credits (`total`) with an optional `expires` date. A grant keeps its own
        # counter from the day it is entered (`starts`), so entering a new grant (a renewal, the real balance) starts at zero.
        current = (await config())["limits"]
        out = {}
        for pool, lim in (values.get("limits") or {}).items():
            lim = lim or {}
            new = {k: int(v) for k, v in lim.items() if k in ("monthly", "daily", "total") and v not in (None, "") and int(v) >= 0}
            if lim.get("expires"):
                new["expires"] = date.fromisoformat(str(lim["expires"])[:10]).isoformat()
            if "total" in new:
                prev = current.get(pool) or {}
                same = prev.get("total") == new["total"] and prev.get("expires") == new.get("expires") and prev.get("starts")
                new["starts"] = prev["starts"] if same else _now().date().isoformat()
            if new:
                out[pool] = new
        clean["limits"] = out
    await _db().settings.update_one({"key": "providers"}, {"$set": {**clean, "updated_at": now_iso()}}, upsert=True)
    cache.bump("providers_cfg")
    return await config()


async def plan(capability: str) -> List[Spec]:
    """The capability's providers in the configured order, minus those switched off."""
    cfg = await config()
    default = [s for s in SPECS if s.capability == capability]
    names = [n for n in cfg["order"].get(capability, []) if n in SPEC_BY_NAME and SPEC_BY_NAME[n].capability == capability]
    ordered = [SPEC_BY_NAME[n] for n in names] + [s for s in default if s.name not in names]
    off = set(cfg["disabled"]) | _env_disabled()
    return [s for s in ordered if s.name not in off]


# --- usage and quotas ----------------------------------------------------------------------

def _month(now: Optional[datetime] = None) -> str:
    return (now or _now()).strftime("%Y-%m")


async def usage(pool: str, month: Optional[str] = None) -> dict:
    """Units counted this calendar month, or under a one-time grant's counter (`month` = "grant:<start date>")."""
    month = month or _month()
    key = f"{pool}|{month}" if month.startswith("grant:") else pool
    u = _usage.get(key)
    if u and u["month"] == month and time.monotonic() - u["loaded"] < USAGE_TTL:
        return u
    try:
        doc = await _db().provider_usage.find_one({"pool": pool, "month": month}, {"_id": 0}) or {}
    except Exception:  # noqa: BLE001
        doc = {}
    u = {"month": month, "units": int(doc.get("units") or 0), "days": dict(doc.get("days") or {}), "loaded": time.monotonic()}
    if key in _usage and _usage[key]["month"] == month:  # keep counts this process added since the last read
        u["units"] = max(u["units"], _usage[key]["units"])
        for d, n in _usage[key]["days"].items():
            u["days"][d] = max(int(u["days"].get(d) or 0), n)
    _usage[key] = u
    return u


async def charge(spec: Spec, units: int = 1) -> None:
    if units <= 0:
        return
    pool, now = spec.quota_pool, _now()
    g = await grant(spec)
    day = now.strftime("%d")
    for month in [None] + ([g["month"]] if g else []):
        u = await usage(pool, month)
        u["units"] += units
        u["days"][day] = int(u["days"].get(day) or 0) + units
        try:
            await _db().provider_usage.update_one({"pool": pool, "month": u["month"]},
                                                  {"$inc": {"units": units, f"days.{day}": units}, "$set": {"updated_at": now.isoformat()}}, upsert=True)
        except Exception:  # noqa: BLE001 - counting must never fail a lookup
            logger.debug("usage write failed for %s", pool)


async def limits(spec: Spec) -> Tuple[Optional[int], Optional[int]]:
    over = (await config())["limits"].get(spec.quota_pool) or {}
    return over.get("monthly", spec.monthly), over.get("daily", spec.daily)


GRANT_HORIZON_DAYS = 60  # one-time credits with no expiry date are spread as if they had to last this long


async def grant(spec: Spec) -> Optional[dict]:
    """One-time credits for the provider's pool: entered in Data sources, else the provider's built-in sign-up grant."""
    over = (await config())["limits"].get(spec.quota_pool) or {}
    total = over.get("total", spec.grant)
    if not total:
        return None
    starts = over.get("starts") if "total" in over else None
    return {"total": int(total), "expires": over.get("expires"), "starts": starts, "month": f"grant:{starts or 'signup'}"}


def days_until(iso_date: str, now: Optional[datetime] = None) -> int:
    return (date.fromisoformat(iso_date[:10]) - (now or _now()).date()).days


def pace(cap: int, used_before_today: int, now: Optional[datetime] = None) -> int:
    """Units a scheduled job may spend today so the month's allowance lasts to its end."""
    now = now or _now()
    days_left = monthrange(now.year, now.month)[1] - now.day + 1
    return max(1, math.ceil(max(0, cap - used_before_today) / days_left))


async def headroom(spec: Spec, bulk: bool = False) -> Tuple[bool, str]:
    monthly, daily = await limits(spec)
    g = await grant(spec)
    if monthly is None and daily is None and not g:
        return True, ""
    u = await usage(spec.quota_pool)
    today = int(u["days"].get(_now().strftime("%d")) or 0)
    if g:
        if g["expires"] and days_until(g["expires"]) < 0:
            return False, f"credits expired on {g['expires']}"
        used = (await usage(spec.quota_pool, g["month"]))["units"]
        if used >= g["total"]:
            return False, f"one-time credits used ({used}/{g['total']})"
        if bulk:
            days_left = days_until(g["expires"]) + 1 if g["expires"] else GRANT_HORIZON_DAYS
            if today >= max(1, math.ceil((g["total"] - (used - today)) / max(days_left, 1))):
                return False, "paced: today's share of the remaining credits is used"
    if monthly is not None and u["units"] >= monthly:
        return False, f"free allowance used for {u['month']} ({u['units']}/{monthly})"
    if daily is not None and today >= daily:
        return False, f"daily allowance used ({today}/{daily})"
    if bulk and monthly is not None and today >= pace(monthly, u["units"] - today):
        return False, "paced: today's share of the month is used"
    return True, ""


def cooling(spec: Spec) -> Optional[str]:
    c = _cool.get(_kid(spec))
    if c and c[0] > time.monotonic():
        return c[1]
    return None


async def ready(spec: Spec, bulk: bool = False) -> Tuple[bool, str]:
    if not configured(spec):
        return False, "not set up"
    why = cooling(spec)
    if why:
        return False, why
    return await headroom(spec, bulk)


# --- failures --------------------------------------------------------------------------------

def classify(exc: BaseException, spec: Spec) -> Tuple[float, str]:
    """Seconds to rest a provider after this failure, and why. 0 = the request was at fault, not the provider."""
    if isinstance(exc, OutOfCredits):
        return 6 * 3600, "out of credits"
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code == 402:
            return 6 * 3600, "out of credits"
        if code in (401, 403):
            return 3600, f"key rejected ({code})"
        if code == 429:
            k = _kid(spec)
            now = time.monotonic()
            hits = [t for t in _strikes.get(k, []) if now - t < 600] + [now]
            _strikes[k] = hits
            return (900 if len(hits) >= 3 else 75), "rate limited"
        if code >= 500:
            return 120, f"provider error ({code})"
        return 0, f"rejected the request ({code})"
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return 60, "unreachable"
    return 30, type(exc).__name__


def _rest(spec: Spec, seconds: float, reason: str) -> None:
    if seconds > 0:
        _cool[_kid(spec)] = (time.monotonic() + seconds, reason, time.time() + seconds)


def _remember(spec: Spec, ok: bool, error: str = "") -> None:
    rec = _last.setdefault(spec.name, {})
    stamp = _now().isoformat()
    if ok:
        rec["ok_at"] = stamp
    else:
        rec.update({"error": error[:300], "error_at": stamp})


Call = Callable[[], Awaitable[Any]]
OnError = Optional[Callable[[Spec, BaseException], None]]


async def _attempt(spec: Spec, call: Call, on_error: OnError) -> Tuple[bool, Any]:
    try:
        out = await call()
    except Skip:
        return False, None
    except Exception as exc:  # noqa: BLE001 - any provider failure moves on to the next
        seconds, reason = classify(exc, spec)
        _rest(spec, seconds, reason)
        _remember(spec, False, f"{reason}: {str(exc)[:200]}")
        logger.info("provider %s failed (%s), trying the next", spec.name, reason)
        if on_error:
            on_error(spec, exc)
        return False, None
    units = 1
    if isinstance(out, Billed):
        out, units = out.value, out.units
    await charge(spec, units)
    _remember(spec, True)
    return True, out


async def first(capability: str, calls: Dict[str, Call], *, accept: Callable[[Any], bool] = bool, on_error: OnError = None,
                bulk: bool = False, max_empty: int = 2) -> Tuple[Any, Optional[str]]:
    """The first acceptable answer along the chain, and the provider that gave it.

    `calls` maps provider names to zero-argument coroutines; providers without an entry are not
    asked. A provider that answers but finds nothing counts toward `max_empty`: after that many
    empty answers the chain stops, so an obscure company doesn't drain every allowance."""
    empty, last = 0, None
    for spec in await plan(capability):
        call = calls.get(spec.name)
        if call is None:
            continue
        ok, why = await ready(spec, bulk)
        if not ok:
            continue
        answered, out = await _attempt(spec, call, on_error)
        if not answered:
            continue
        if accept(out):
            return out, spec.name
        last = out
        empty += 1
        if empty >= max_empty:
            break
    return last, None


async def gather(capability: str, calls: Dict[str, Call], *, on_error: OnError = None, bulk: bool = False, limit: Optional[int] = None) -> List[Tuple[str, Any]]:
    """Every ready provider's answer, asked together (people from several sources are merged)."""
    chosen = []
    for spec in await plan(capability):
        if spec.name in calls and (await ready(spec, bulk))[0]:
            chosen.append(spec)
            if limit and len(chosen) >= limit:
                break
    results = await asyncio.gather(*(_attempt(s, calls[s.name], on_error) for s in chosen))
    return [(s.name, out) for s, (ok, out) in zip(chosen, results) if ok]


async def enabled(name: str) -> bool:
    """For providers a caller runs itself (the original collectors): set up and not switched off."""
    spec = SPEC_BY_NAME.get(name)
    return bool(spec and configured(spec) and spec in await plan(spec.capability))


async def usable(name: str, bulk: bool = False) -> bool:
    """`enabled`, not resting and with allowance left."""
    spec = SPEC_BY_NAME.get(name)
    return bool(spec and spec in await plan(spec.capability) and (await ready(spec, bulk))[0])


def failed(name: str, exc: BaseException) -> None:
    """Record a failure from a provider a caller runs itself, so it rests like a chained one."""
    spec = SPEC_BY_NAME.get(name)
    if spec:
        seconds, reason = classify(exc, spec)
        _rest(spec, seconds, reason)
        _remember(spec, False, f"{reason}: {str(exc)[:200]}")


async def succeeded(name: str, units: int = 1) -> None:
    spec = SPEC_BY_NAME.get(name)
    if spec:
        await charge(spec, units)
        _remember(spec, True)


async def available(capability: str, names: Optional[List[str]] = None) -> List[str]:
    """Providers of a capability that could answer now (set up, not resting, allowance left)."""
    out = []
    for spec in await plan(capability):
        if (names is None or spec.name in names) and (await ready(spec))[0]:
            out.append(spec.name)
    return out


def any_configured(capability: str, names: Optional[List[str]] = None, *, keyed_only: bool = False) -> bool:
    return any(configured(s) and (not keyed_only or not s.keyless) for s in SPECS
               if s.capability == capability and (names is None or s.name in names) and s.name not in _env_disabled())


# --- the admin screen -----------------------------------------------------------------------

async def status() -> dict:
    cfg = await config()
    caps = []
    for cap, meta in CAPABILITIES.items():
        rows = []
        order = await plan(cap)
        disabled = [s for s in SPECS if s.capability == cap and s not in order]
        for spec in order + disabled:
            monthly, daily = await limits(spec)
            u = await usage(spec.quota_pool)
            today = int(u["days"].get(_now().strftime("%d")) or 0)
            g = await grant(spec)
            g_used = (await usage(spec.quota_pool, g["month"]))["units"] if g else None
            is_on = spec in order
            ok, why = await ready(spec) if is_on else (False, "switched off")
            c = _cool.get(_kid(spec))
            rows.append({
                "name": spec.name, "label": spec.label, "keyless": spec.keyless, "env": list(spec.env), "free": spec.free, "signup": spec.signup, "note": spec.note,
                "configured": configured(spec), "enabled": is_on, "ready": ok, "reason": why, "pool": spec.quota_pool,
                "monthly_cap": monthly, "daily_cap": daily, "used_month": u["units"], "used_today": today,
                "grant_total": g["total"] if g else None, "grant_used": g_used, "grant_left": max(0, g["total"] - g_used) if g else None,
                "expires": g["expires"] if g else None, "expires_in_days": days_until(g["expires"]) if g and g["expires"] else None,
                "left_month": None if monthly is None else max(0, monthly - u["units"]),
                "resting_until": datetime.fromtimestamp(c[2], tz=timezone.utc).isoformat() if c and c[0] > time.monotonic() else None,
                **{k: v for k, v in (_last.get(spec.name) or {}).items()},
            })
        live = sum(1 for r in rows if r["ready"])
        caps.append({"key": cap, **meta, "providers": rows, "ready": live, "configured": sum(1 for r in rows if r["configured"] and r["enabled"])})
    return {"capabilities": caps, "keyless_on": keyless_on(), "config": cfg, "month": _month()}


def report() -> str:
    """Which providers each chain has set up on this machine (names only, never key values), for checking Render or
    GitHub secrets without the admin: `python providers.py`. Allowances and cooldowns are on the Data sources screen."""
    lines = []
    for cap, meta in CAPABILITIES.items():
        specs = [s for s in SPECS if s.capability == cap]
        on = [s.label for s in specs if configured(s) and s.name not in _env_disabled()]
        off = [f"{s.label} ({' + '.join(s.env)})" for s in specs if not configured(s) and s.env]
        lines.append(f"{meta['label']}: {len(on)} of {len(specs)} set up" + (f" - {', '.join(on)}" if on else ""))
        if off:
            lines.append(f"    not set: {', '.join(off)}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(report())
