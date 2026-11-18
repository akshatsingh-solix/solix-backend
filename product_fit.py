"""Which Solix product to pitch to an account first, what to cross-sell and up-sell, and why.

Every recommendation is built from evidence on the account, and each reason names that evidence:
* signals: a competitor product in a job post, DNS record or filing (displacement), a problem term ("decommission",
  "retention", "legal hold"), intent topics, the products their people read about on solix.com;
* firmographics: the industry (regulation and typical estates drive what hurts), size (platforms suit enterprises);
* the relationship: products a customer already owns turn into up-sell and cross-sell plays, never a re-pitch.

Weights are a starting point. Phase 5's weekly review (learning.py) nudges the industry and adjacency weights from
which plays actually earned positive replies and opportunities, within fixed bounds, and logs every change.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

import scoring

PRODUCTS: Dict[str, dict] = {
    "application-retirement": {"label": "Application Retirement", "value": "Retire legacy applications into one compliant, searchable archive and remove their licence and run cost"},
    "enterprise-archiving": {"label": "Enterprise Archiving", "value": "One governed archive for structured and unstructured data, with retention and legal hold"},
    "database-archiving": {"label": "Database Archiving", "value": "Move inactive data out of production databases so they run faster and cost less"},
    "sap-archiving": {"label": "SAP Archiving", "value": "Archive SAP data and retire old SAP instances ahead of S/4HANA"},
    "oracle-oebs-archiving": {"label": "Oracle E-Business Suite Archiving", "value": "Shrink and retire Oracle EBS while keeping records searchable"},
    "mainframe-archiving": {"label": "Mainframe Archiving", "value": "Keep mainframe records accessible after the MIPS are switched off"},
    "email-archiving": {"label": "Email Archiving", "value": "Compliant email and communications archive with supervision and eDiscovery"},
    "file-archiving": {"label": "File Archiving", "value": "Tier and archive file shares with retention rules applied"},
    "data-preservation": {"label": "Data Preservation", "value": "Long-term, tamper-evident preservation for regulated records"},
    "active-archiving-compliance": {"label": "Active Archiving & Compliance", "value": "Regulatory retention (SEC 17a-4, FINRA, HIPAA) on live archives"},
    "enterprise-data-governance": {"label": "Enterprise Data Governance", "value": "Catalogue, classify and govern data across the estate"},
    "consumer-data-privacy": {"label": "Consumer Data Privacy", "value": "Find, mask and delete personal data to meet GDPR, CCPA and DSAR deadlines"},
    "ediscovery": {"label": "eDiscovery", "value": "Search, hold and export across archives for litigation and investigations"},
    "enterprise-ai": {"label": "Enterprise AI", "value": "Put governed enterprise data to work in AI assistants and agents"},
    "ai-governance": {"label": "AI Governance", "value": "Control which data AI models can see and prove it"},
    "ai-healthcare": {"label": "AI for Healthcare", "value": "Governed AI over clinical and operational data"},
    "eai-pharma": {"label": "Enterprise AI for Pharma", "value": "AI over regulated R&D and quality records"},
    "data-sense": {"label": "Data Sense", "value": "Discover and classify sensitive data automatically"},
    "data-ask": {"label": "Data Ask", "value": "Ask questions of archived and live data in plain language"},
    "application-knowledge-graph": {"label": "Application Knowledge Graph", "value": "Map applications, data and owners before retirement or migration"},
    "ai-warehouse": {"label": "AI Warehouse", "value": "A warehouse built for AI workloads on governed data"},
    "agentic": {"label": "Agentic AI", "value": "Agents that act on enterprise data within governance"},
    "common-data-platform": {"label": "Common Data Platform", "value": "One platform for archiving, governance, lake and AI"},
    "enterprise-data-lake": {"label": "Enterprise Data Lake", "value": "A governed lake for analytics on current and historical data"},
    "enterprise-edition": {"label": "Enterprise Edition", "value": "The full Solix platform under one licence"},
    "enterprise-content-services": {"label": "Enterprise Content Services", "value": "Modern content management to replace ageing ECM platforms"},
    "services": {"label": "Professional & Managed Services", "value": "Solix experts plan and run the programme"},
}

# Industry -> products whose pain is typical there, with the reason a rep can say out loud.
INDUSTRY_FIT: Dict[str, List[tuple]] = {
    "financial-services": [("email-archiving", 18, "regulators require communications retention and supervision (SEC 17a-4, FINRA)"),
                           ("active-archiving-compliance", 14, "records must stay retained and producible for years"),
                           ("application-retirement", 12, "mergers leave duplicate core and lending systems running only for their records"),
                           ("consumer-data-privacy", 10, "customer data falls under GDPR, CCPA and GLBA"), ("ediscovery", 8, "frequent litigation and investigations")],
    "insurance": [("mainframe-archiving", 16, "policy and claims history often sits on mainframes"),
                  ("application-retirement", 16, "acquired books of business keep legacy policy admin systems alive"),
                  ("enterprise-data-governance", 10, "regulators expect governed customer and claims data"), ("consumer-data-privacy", 8, "policyholder personal data")],
    "healthcare": [("application-retirement", 18, "legacy EHR and clinical systems are kept running only to keep records for decades"),
                   ("active-archiving-compliance", 12, "HIPAA retention and patient access to records"), ("ai-healthcare", 10, "governed AI over clinical data"),
                   ("consumer-data-privacy", 8, "patient data privacy")],
    "pharma-biotech": [("data-preservation", 16, "GxP and FDA 21 CFR Part 11 records must be preserved and provable"),
                       ("eai-pharma", 12, "AI over R&D and quality records"), ("enterprise-content-services", 10, "regulated document management"),
                       ("application-retirement", 10, "lab and quality systems retired after M&A")],
    "manufacturing": [("sap-archiving", 18, "large SAP estates face the S/4HANA move"), ("oracle-oebs-archiving", 12, "Oracle EBS estates to shrink or retire"),
                      ("application-retirement", 12, "plants and acquisitions leave ERP instances behind"), ("enterprise-data-lake", 6, "operational analytics")],
    "public-sector": [("data-preservation", 14, "public records retention schedules"), ("enterprise-content-services", 14, "ageing ECM platforms to replace"),
                      ("ediscovery", 10, "FOIA and public records requests"), ("application-retirement", 12, "legacy case and benefits systems")],
    "retail": [("consumer-data-privacy", 16, "large volumes of consumer personal data and DSARs"), ("database-archiving", 12, "transaction databases grow fast"),
               ("enterprise-data-lake", 8, "customer analytics")],
    "energy": [("application-retirement", 14, "asset and billing systems outlive their vendors"), ("file-archiving", 10, "engineering file shares"),
               ("enterprise-data-governance", 8, "regulated operational data")],
    "telecom": [("database-archiving", 16, "call and usage records swell production databases"), ("consumer-data-privacy", 12, "subscriber personal data"),
                ("application-retirement", 10, "billing and OSS/BSS consolidation")],
    "technology": [("common-data-platform", 12, "one platform for data and AI"), ("enterprise-ai", 12, "AI on governed data"), ("database-archiving", 8, "fast-growing databases")],
}

PROBLEM_FIT = [  # problem words in signals -> products
    (r"decommission|retir|sunset|legacy (application|system)", "application-retirement", 14, "they're decommissioning legacy systems"),
    (r"\bsap\b|s/?4 ?hana", "sap-archiving", 14, "they're working on SAP"),
    (r"mainframe|cobol|db2|z/os", "mainframe-archiving", 12, "they run mainframe workloads"),
    (r"oracle (e-?business|ebs)|\boebs\b", "oracle-oebs-archiving", 12, "they run Oracle E-Business Suite"),
    (r"retention|records management|record keeping", "data-preservation", 10, "records retention is on their agenda"),
    (r"legal hold|e-?discovery|litigation", "ediscovery", 10, "legal hold and eDiscovery come up in their hiring"),
    (r"gdpr|ccpa|privacy|dsar|personal data", "consumer-data-privacy", 10, "privacy obligations show in their signals"),
    (r"\bai\b|llm|machine learning|genai|generative|rag\b|copilot", "enterprise-ai", 10, "they're investing in AI"),
    (r"data governance|data catalog|data quality|lineage", "enterprise-data-governance", 10, "data governance is a stated priority"),
    (r"email|journal|smarsh|global relay|communications", "email-archiving", 8, "communications archiving is in scope"),
    (r"content management|\becm\b|documentum|sharepoint", "enterprise-content-services", 8, "they manage content on an ECM platform"),
    (r"archiv", "enterprise-archiving", 8, "archiving is in their signals"),
]
_PROBLEM_RX = [(re.compile(rx, re.I), p, w, why) for rx, p, w, why in PROBLEM_FIT]

# After one product lands, what naturally follows (cross-sell across lines, up-sell within a line or to the platform).
ADJACENT: Dict[str, List[tuple]] = {
    "application-retirement": [("enterprise-ai", "retired data becomes AI-ready instead of sitting idle"), ("ediscovery", "retired records stay searchable for legal"),
                               ("application-knowledge-graph", "map the next wave of applications to retire")],
    "enterprise-archiving": [("enterprise-data-governance", "govern what's archived"), ("data-ask", "let people ask questions of the archive")],
    "database-archiving": [("application-retirement", "retire the applications once their data is archived"), ("enterprise-data-lake", "analytics on archived history")],
    "sap-archiving": [("application-retirement", "retire the old SAP instances after the move"), ("enterprise-data-lake", "SAP history for analytics")],
    "oracle-oebs-archiving": [("application-retirement", "retire EBS entirely"), ("enterprise-data-lake", "EBS history for analytics")],
    "mainframe-archiving": [("application-retirement", "retire the remaining mainframe applications"), ("enterprise-ai", "AI over decades of history")],
    "email-archiving": [("ediscovery", "search and hold across communications"), ("active-archiving-compliance", "one retention policy for all records")],
    "file-archiving": [("data-sense", "find sensitive files first"), ("enterprise-archiving", "one archive for files and databases")],
    "data-preservation": [("ediscovery", "produce preserved records on demand"), ("enterprise-ai", "AI over preserved records")],
    "active-archiving-compliance": [("ediscovery", "search what's retained"), ("enterprise-data-governance", "governance across the estate")],
    "enterprise-data-governance": [("ai-governance", "extend governance to AI"), ("consumer-data-privacy", "act on the personal data you find")],
    "consumer-data-privacy": [("data-sense", "discover personal data automatically"), ("enterprise-data-governance", "govern it ongoing")],
    "ediscovery": [("enterprise-archiving", "archive what legal keeps asking for"), ("email-archiving", "communications in the same search")],
    "enterprise-ai": [("ai-governance", "govern what the AI can see"), ("common-data-platform", "one platform under the AI")],
    "enterprise-content-services": [("enterprise-archiving", "archive content that's done being worked on"), ("enterprise-ai", "AI over documents")],
    "common-data-platform": [("enterprise-ai", "AI on the platform"), ("enterprise-data-lake", "a governed lake on the same platform")],
}
PLATFORM = "common-data-platform"


def label(slug: Optional[str]) -> Optional[str]:
    return (PRODUCTS.get(slug) or {}).get("label") or (slug.replace("-", " ").title() if slug else None)


def _signal_text(signals: List[dict]) -> str:
    return " ".join(str(s.get(k) or "") for s in signals for k in ("title", "role_title", "problem_term", "evidence_text", "competitor_product", "record_value"))[:20000]


def recommend(company: dict, signals: Optional[List[dict]] = None, *, lead_products: Optional[Dict[str, float]] = None, weights: Optional[dict] = None,
              limit: int = 5) -> dict:
    """Ranked plays for the account. weights: learned multipliers {"industry:<slug>:<product>": x, "adjacent:<a>:<b>": x} (learning.py)."""
    import data_quality as dq
    import radar_model as rm
    signals = signals or []
    weights = weights or {}
    owned = set(company.get("products_owned") or [])
    is_customer = bool(company.get("is_customer") or owned)
    industry = company.get("industry_slug") or dq.industry_slug(company.get("industry"))
    seg = company.get("segment")
    score: Dict[str, float] = {}
    why: Dict[str, List[str]] = {}

    def add(p: str, pts: float, reason: str, key: Optional[str] = None) -> None:
        if p not in PRODUCTS or p in owned or pts <= 0:
            return
        pts *= float(weights.get(key, 1.0)) if key else 1.0
        score[p] = score.get(p, 0) + pts
        if reason and reason not in why.setdefault(p, []):
            why[p].append(reason)

    # Displacement: a competitor product in their signals is the strongest reason to pitch its Solix replacement.
    for key in company.get("competitor_keys") or []:
        comp = rm.COMPETITOR_BY_KEY.get(key)
        for slug in (comp or {}).get("slugs") or []:
            add(slug, 30, f"they use {comp['product']} (seen in {', '.join(sorted({rm.SIGNAL_LABELS.get(s['type'], s['type']).lower() for s in signals if s.get('competitor_key') == key})) or 'signals'})")
    text = _signal_text(signals)
    for rx, slug, pts, reason in _PROBLEM_RX:
        if rx.search(text):
            add(slug, pts, reason)
    for slug, pts, reason in INDUSTRY_FIT.get(industry or "", []):
        add(slug, pts, f"{dq.INDUSTRY_LABELS.get(industry, industry)}: {reason}", f"industry:{industry}:{slug}")
    for slug, pts in sorted((lead_products or {}).items(), key=lambda kv: -kv[1])[:4]:
        add(slug, min(20, pts), "their people read about it on solix.com")
    if seg == "enterprise" and score:
        add(PLATFORM, 6 + 0.15 * max(score.values()), "an enterprise with several needs gets more from one platform than point products")
    elif seg == "smb":
        for p in (PLATFORM, "enterprise-edition", "enterprise-data-lake"):
            if p in score:
                score[p] *= 0.5
    # Customers: what follows what they own. Prospects: what follows the lead pitch (land, then expand).
    anchors = sorted(owned) if is_customer else ([max(score, key=score.get)] if score else [])
    expand: Dict[str, str] = {}
    for a in anchors:
        for slug, reason in ADJACENT.get(a, []):
            pts = 16 if is_customer else 6
            add(slug, pts, f"follows {label(a)}: {reason}", f"adjacent:{a}:{slug}")
            expand.setdefault(slug, a)
        if is_customer and scoring.PRODUCT_TO_LINE.get(a) != "platform":
            add(PLATFORM, 10, f"they already run {label(a)}; the platform adds the rest without another vendor", f"adjacent:{a}:{PLATFORM}")
            expand.setdefault(PLATFORM, a)
    ranked = sorted(score.items(), key=lambda kv: -kv[1])[:limit]
    top = ranked[0][1] if ranked else 1
    plays = []
    for i, (slug, pts) in enumerate(ranked):
        line = scoring.PRODUCT_TO_LINE.get(slug)
        if is_customer:
            kind = "up_sell" if (slug == PLATFORM or (expand.get(slug) and scoring.PRODUCT_TO_LINE.get(expand[slug]) == line)) else "cross_sell"
        else:
            kind = "lead" if i == 0 else "cross_sell"
        plays.append({"product": slug, "label": label(slug), "line": line, "line_label": (scoring.PRODUCT_LINES.get(line) or {}).get("label"), "kind": kind,
                      "fit": round(100 * pts / top), "points": round(pts, 1), "reasons": why.get(slug, [])[:4], "value": PRODUCTS[slug]["value"],
                      "page": f"/products/{slug}", "follows": label(expand[slug]) if slug in expand else None})
    return {"plays": plays, "industry": industry, "customer": is_customer, "owned": [{"product": p, "label": label(p)} for p in sorted(owned)],
            "summary": _summary(plays, is_customer)}


def _summary(plays: List[dict], customer: bool) -> Optional[str]:
    if not plays:
        return None
    first = plays[0]
    lead = f"{'Expand with' if customer else 'Lead with'} {first['label']}" + (f" because {first['reasons'][0]}" if first["reasons"] else "")
    nxt = [p["label"] for p in plays[1:3]]
    return lead + (f"; then {' and '.join(nxt)}." if nxt else ".")
