"""Intent scoring: how visitor behaviour and form data turn into a lead that
is tagged to a product line and promoted to MQL.

Model
-----
* Behaviour score per product (0..n): every tracked signal adds points to
  the products it is about (a product page view, a datasheet download, a
  chat question). Weights are set here, never by the browser. Points decay
  with a configurable half-life so interest from last quarter fades.
* Line score: product scores roll up into product lines (Archiving, Enterprise
  AI, ...). The top line is the lead's primary interest.
* Fit score (0..40): business email, seniority, company size, phone.
* Stage: known lead -> MQL when (top line behaviour + fit) >= threshold, or
  immediately on a hand-raise (demo request, contact sales, trial sign-up).
  Stages after MQL (SAL, SQL, opportunity, won, lost, disqualified) are set by
  people in the admin and are never overwritten automatically.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Dict, Iterable, Optional

PRODUCT_LINES: Dict[str, dict] = {
    "platform": {
        "label": "Enterprise Edition & Common Data Platform",
        "products": ["enterprise-edition", "common-data-platform", "enterprise-data-lake"],
    },
    "archiving": {
        "label": "Archiving & Application Retirement",
        "products": [
            "enterprise-archiving", "application-retirement", "data-preservation", "sap-archiving",
            "oracle-oebs-archiving", "mainframe-archiving", "email-archiving", "file-archiving",
            "database-archiving", "active-archiving-compliance",
        ],
    },
    "governance": {
        "label": "Data Governance, Privacy & eDiscovery",
        "products": ["enterprise-data-governance", "consumer-data-privacy", "ediscovery"],
    },
    "ai": {
        "label": "Enterprise AI",
        "products": [
            "enterprise-ai", "data-sense", "data-ask", "application-knowledge-graph", "ai-warehouse",
            "agentic", "ai-governance", "ai-healthcare", "eai-pharma",
        ],
    },
    "ecs": {"label": "Enterprise Content Services (ECS)", "products": ["enterprise-content-services"]},
    "services": {"label": "Professional & Managed Services", "products": ["services"]},
}
PRODUCT_TO_LINE = {p: line for line, spec in PRODUCT_LINES.items() for p in spec["products"]}
PRODUCTS = set(PRODUCT_TO_LINE)

INDUSTRIES = {
    "financial-services", "healthcare", "manufacturing", "public-sector", "pharma-biotech",
    "retail", "energy", "telecom", "insurance",
}

STAGES = ["lead", "mql", "sal", "sql", "opportunity", "won", "lost", "disqualified"]
AUTO_STAGES = {"lead", "mql"}  # everything else is owned by people

# Points per tracked signal. `topics` on an event say which products it's about.
EVENT_WEIGHTS: Dict[str, float] = {
    "page_view": 3,          # product / solution page viewed
    "engaged": 5,            # 45s+ of active reading on a topic page
    "deep_scroll": 2,        # read to 75%+ of a topic page
    "resource_view": 6,      # opened a resource about the topic
    "resource_download": 15, # unlocked / downloaded gated material
    "chat_topic": 6,         # asked the concierge about the topic
    "cta_click": 8,          # clicked demo / trial / contact from a topic page
    "pricing_intent": 10,    # asked about pricing
    "search": 2,             # searched resources for the topic
}
# Points for form submissions, added to the submission's interest.
SUBMISSION_WEIGHTS: Dict[str, float] = {
    "demo": 30, "trial": 25, "event": 20, "contact": 15, "download": 15, "partner": 5, "newsletter": 5, "career": 0,
}
HAND_RAISE = {"demo", "trial", "contact"}
EVENT_TYPES = set(EVENT_WEIGHTS) | {"session_start", "cta", "form_view"}

DEFAULT_SETTINGS = {"mql_threshold": 45, "half_life_days": 30}

FREE_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "yahoo.co.in", "hotmail.com", "outlook.com",
    "live.com", "msn.com", "aol.com", "icloud.com", "me.com", "mac.com", "proton.me", "protonmail.com",
    "gmx.com", "gmx.de", "web.de", "mail.com", "yandex.com", "yandex.ru", "zoho.com", "rediffmail.com",
    "qq.com", "163.com", "free.fr", "orange.fr", "laposte.net", "libero.it", "t-online.de",
}
SENIOR_RX = re.compile(r"\b(chief|c[etiops]o|cdo|cio|ciso|founder|owner|president|vp|vice president|svp|evp|head|director|partner)\b", re.I)
MANAGER_RX = re.compile(r"\b(manager|lead|principal|architect|gerente|responsable|leiter|leitung)\b", re.I)


def clean_topics(topics: Optional[Iterable[str]]) -> list[str]:
    """Keep only known product slugs, deduplicated, max 5."""
    out: list[str] = []
    for t in topics or []:
        t = str(t).strip().lower()
        if t in PRODUCTS and t not in out:
            out.append(t)
        if len(out) == 5:
            break
    return out


def topics_for_path(path: str) -> list[str]:
    """Server-side mapping of site paths to products (authoritative for page views)."""
    path = (path or "").split("?")[0].rstrip("/")
    path = re.sub(r"^/Website", "", path)
    m = re.match(r"^/products/([a-z0-9-]+)$", path)
    if m and m.group(1) in PRODUCTS:
        return [m.group(1)]
    if path == "/services-support":
        return ["services"]
    if path == "/platform":
        return ["enterprise-edition", "common-data-platform"]
    if path in ("/signup", "/signin", "/ai/signup", "/ai/signin"):
        return ["enterprise-content-services"]
    return []


def industry_for_path(path: str) -> Optional[str]:
    m = re.match(r"^/industries/([a-z0-9-]+)$", re.sub(r"^/Website", "", (path or "").split("?")[0].rstrip("/")))
    return m.group(1) if m and m.group(1) in INDUSTRIES else None


def decay_factor(since_iso: Optional[str], half_life_days: float, now: Optional[datetime] = None) -> float:
    if not since_iso:
        return 1.0
    try:
        then = datetime.fromisoformat(since_iso)
    except ValueError:
        return 1.0
    now = now or datetime.now(timezone.utc)
    days = max(0.0, (now - then).total_seconds() / 86400)
    return 0.5 ** (days / max(half_life_days, 1))


def decay_scores(scores: Dict[str, float], since_iso: Optional[str], half_life_days: float) -> Dict[str, float]:
    f = decay_factor(since_iso, half_life_days)
    return {k: round(v * f, 2) for k, v in (scores or {}).items() if v * f >= 0.05}


def add_points(scores: Dict[str, float], topics: Iterable[str], points: float) -> None:
    topics = list(topics)
    if not topics or points <= 0:
        return
    share = points / len(topics)
    for t in topics:
        scores[t] = round(scores.get(t, 0) + share, 2)


def line_scores(product_scores: Dict[str, float]) -> Dict[str, float]:
    lines: Dict[str, float] = {}
    for slug, v in (product_scores or {}).items():
        line = PRODUCT_TO_LINE.get(slug)
        if line:
            lines[line] = round(lines.get(line, 0) + v, 2)
    return lines


def fit_score(lead: dict) -> tuple[int, dict]:
    breakdown = {}
    email = (lead.get("email") or "").lower()
    domain = email.split("@")[-1] if "@" in email else ""
    if domain and domain not in FREE_EMAIL_DOMAINS:
        breakdown["business_email"] = 10
    title = lead.get("job_title") or ""
    if SENIOR_RX.search(title):
        breakdown["seniority"] = 15
    elif MANAGER_RX.search(title):
        breakdown["seniority"] = 8
    size = (lead.get("company_size") or "").replace(",", "")
    nums = [int(n) for n in re.findall(r"\d+", size)]
    if nums:
        top = max(nums)
        if top >= 1000:
            breakdown["company_size"] = 10
        elif top >= 200:
            breakdown["company_size"] = 6
        elif top >= 50:
            breakdown["company_size"] = 3
    if lead.get("phone"):
        breakdown["phone"] = 5
    return sum(breakdown.values()), breakdown


def channel_for(touch: Optional[dict]) -> str:
    """Marketing channel from first-touch UTM / referrer."""
    touch = touch or {}
    medium = (touch.get("utm_medium") or "").lower()
    source = (touch.get("utm_source") or "").lower()
    ref = (touch.get("referrer") or "").lower()
    if touch.get("chat"):
        return "chat"
    if medium in ("cpc", "ppc", "paid", "paidsearch", "paid_social", "display", "cpm"):
        return "paid"
    if medium in ("email", "newsletter") or source in ("email", "newsletter", "hubspot", "marketo"):
        return "email"
    if medium in ("social", "social-organic") or any(s in (source + ref) for s in ("linkedin", "twitter", "x.com", "facebook", "t.co", "youtube", "instagram")):
        return "social"
    if any(s in ref for s in ("google.", "bing.", "duckduckgo.", "yahoo.", "baidu.", "yandex.", "ecosia.")):
        return "organic_search"
    if medium in ("referral", "partner") or (ref and "solix" not in ref and "github.io" not in ref):
        return "referral"
    return "direct"


def evaluate_stage(lead: dict, settings: dict) -> tuple[str, Optional[str]]:
    """Automatic stage for a known lead. People-set stages are kept."""
    current = lead.get("stage") or "lead"
    if current not in AUTO_STAGES:
        return current, lead.get("stage_reason")
    if current == "mql":
        return "mql", lead.get("stage_reason")
    hand = sorted(set(lead.get("submission_types") or []) & HAND_RAISE)
    if hand:
        return "mql", f"Hand-raiser: {', '.join(hand)} request"
    if (lead.get("score") or 0) >= settings.get("mql_threshold", DEFAULT_SETTINGS["mql_threshold"]):
        return "mql", f"Score {round(lead.get('score') or 0)} reached the MQL threshold of {settings.get('mql_threshold')}"
    return "lead", None


def summarize(product_scores: Dict[str, float], fit: int) -> dict:
    """Primary product, primary line and the lead score (top line + fit)."""
    lines = line_scores(product_scores)
    primary_line = max(lines, key=lines.get) if lines else None
    primary_product = None
    if primary_line:
        in_line = {p: v for p, v in product_scores.items() if PRODUCT_TO_LINE.get(p) == primary_line}
        primary_product = max(in_line, key=in_line.get) if in_line else None
    behaviour = lines.get(primary_line, 0) if primary_line else 0
    return {
        "line_scores": lines,
        "primary_line": primary_line,
        "primary_product": primary_product,
        "behaviour_score": round(behaviour, 1),
        "score": round(behaviour + fit, 1),
    }


def is_finite(n) -> bool:
    return isinstance(n, (int, float)) and math.isfinite(n)
