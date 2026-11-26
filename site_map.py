"""The www.solix.com page and asset map: what each page is, which products and industry it is about, whether it is
gated, and which file it delivers. Scoring reads it so a visit to any solix.com page (not only product pages) counts
toward the right product line.

The map is generated, reviewed by marketing, and committed:
    python -m site_map build            # fetches www.solix.com through its public WordPress API (read-only)
writes integrations/solixcom/site-map.json (what MarkHub reads) and site-map.csv (for people to review or correct).
Corrections go in integrations/solixcom/site-map-overrides.csv (path,products,industry,type,stage) and win over the
generated values, so a rebuild never loses them.

Classification, first match wins:
1. URL rules: the page's own product/solution/industry address (scoring.SOLIXCOM_PATHS, /products/<slug>/...).
2. Vocabulary: product and industry terms in the address, title and opening text, weighted toward the title.
Resources have three addresses that share a slug: /resources/<kind>/<slug>/ (open page), /resources/lg/<kind>/<slug>/
(the form) and /documents/<kind>/<slug>.pdf (the file); all three map to the same entry.
"""
from __future__ import annotations

import csv
import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

import scoring

HERE = Path(__file__).parent / "integrations" / "solixcom"
MAP_FILE = HERE / "site-map.json"
CSV_FILE = HERE / "site-map.csv"
OVERRIDES_FILE = HERE / "site-map-overrides.csv"
SITE = "https://www.solix.com"

# Terms that mean a product. Lower case; matched on word boundaries. Specific names first, broad themes last.
PRODUCT_TERMS: Dict[str, List[str]] = {
    "data-ask": ["data ask"],
    "data-sense": ["data sense"],
    "ai-warehouse": ["ai warehouse"],
    "application-knowledge-graph": ["application knowledge graph", "knowledge graph", "akg"],
    "agentic": ["agentic", "ai agent", "ai agents", "agent ops", "agentic operations"],
    "ai-governance": ["ai governance", "eu ai act", "responsible ai", "ai trust", "model governance"],
    "eai-pharma": ["pharma", "drug discovery", "drug repurposing", "biomedical", "life sciences", "clinical trial", "clinical trials"],
    "ai-healthcare": ["healthcare ai", "clinical decision", "patient data", "ehr", "emr"],
    "enterprise-ai": ["enterprise ai", "generative ai", "genai", "gen ai", "llm", "llms", "machine learning", "rag"],
    "enterprise-edition": ["enterprise edition"],
    "common-data-platform": ["common data platform", "cdp", "hybrid query", "data fabric", "data platform", "data management platform", "data stack", "modern data stack", "cloud data", "data integration", "data pipeline", "data pipelines", "analytics", "data warehouse", "data warehousing", "big data", "hadoop", "cloud database", "data mesh"],
    "enterprise-data-lake": ["data lake", "data lakes", "lakehouse", "data lakehouse", "hudi", "iceberg", "delta lake"],
    "enterprise-content-services": ["enterprise content services", "solix ecs", "ecs", "content services", "document management", "content management", "accounts payable", "invoice", "invoices", "contract management", "workflow automation", "document processing", "idp"],
    "application-retirement": ["application retirement", "app retirement", "decommission", "decommissioning", "legacy application",
                               "legacy applications", "retire", "retiring"],
    "data-preservation": ["data preservation", "m&a", "merger", "mergers", "acquisition", "divestiture"],
    "sap-archiving": ["sap", "s/4hana", "s4hana", "sap ecc", "sap hana"],
    "oracle-oebs-archiving": ["oracle e-business", "oracle ebs", "e-business suite", "oracle applications"],
    "mainframe-archiving": ["mainframe", "zseries", "z/os", "cobol", "vsam", "db2"],
    "email-archiving": ["email archiving", "email archive", "exchange", "office 365", "microsoft 365", "o365"],
    "file-archiving": ["file archiving", "file archive", "file share", "file shares", "unstructured data"],
    "database-archiving": ["database archiving", "database growth", "data growth", "database performance"],
    "enterprise-archiving": ["enterprise archiving", "data archiving", "archiving", "archive"],
    "active-archiving-compliance": ["active archiving", "retention", "records management", "legal hold", "compliance archiving", "compliance", "regulatory", "audit trail", "sec 17a-4", "finra"],
    "enterprise-data-governance": ["data governance", "data catalog", "data classification", "metadata", "data quality", "data lineage", "information governance", "information management", "data management", "master data", "data discovery", "data stewardship"],
    "consumer-data-privacy": ["data privacy", "privacy", "gdpr", "ccpa", "dsar", "data masking", "masking", "pii", "sensitive data", "sensitive information", "data security", "database security", "data breach", "data breaches", "breach", "breaches", "auditing", "encryption", "test data"],
    "ediscovery": ["ediscovery", "e-discovery", "litigation"],
    "services": ["professional services", "managed services", "implementation services"],
}
INDUSTRY_TERMS: Dict[str, List[str]] = {
    "financial-services": ["banking", "bank", "banks", "financial services", "finance", "capital markets", "fintech"],
    "insurance": ["insurance", "insurer", "claims"],
    "healthcare": ["healthcare", "hospital", "hipaa", "patient", "payer", "provider"],
    "pharma-biotech": ["pharma", "pharmaceutical", "biotech", "life sciences", "drug"],
    "public-sector": ["government", "public sector", "federal", "state and local", "agency"],
    "manufacturing": ["manufacturing", "manufacturer", "industrial"],
    "retail": ["retail", "retailer", "ecommerce", "e-commerce", "consumer goods"],
    "energy": ["energy", "utilities", "oil and gas"],
    "telecom": ["telecom", "telecommunications", "telco"],
}
# What kind of page it is, from its address, first match wins. Stage: how close to buying a visit to it suggests.
TYPE_RULES = [
    (r"^/products/[^/]+/answers?/", "answer", "awareness"),
    (r"^/(products/request-demo|company/contact-sales|company/contact-us)(/|$)", "contact", "decision"),
    (r"free-trial|/trial(/|$)|get-started", "trial", "decision"),
    (r"^/resources/(lg/)?datasheets?/", "datasheet", "decision"),
    (r"^/resources/(lg/)?case-stud", "case-study", "consideration"),
    (r"^/resources/(lg/)?white-papers?/", "white-paper", "consideration"),
    (r"^/resources/(lg/)?(e-?books?)/", "ebook", "consideration"),
    (r"^/resources/(lg/)?(on-demand-)?webinars?/", "webinar", "consideration"),
    (r"^/resources/(lg/)?(analyst|reports?)", "analyst-report", "consideration"),
    (r"^/resources/(lg/)?videos?/", "video", "consideration"),
    (r"^/resources/(lg/)?podcasts?/", "podcast", "awareness"),
    (r"^/resources/", "resource", "consideration"),
    (r"^/products/", "product", "consideration"),
    (r"^/solutions/", "solution", "consideration"),
    (r"^/industries/", "industry", "consideration"),
    (r"^/kb/", "kb-article", "awareness"),
    (r"^/glossary/", "glossary", "awareness"),
    (r"^/leadership-lessons/", "leadership-lesson", "awareness"),
    (r"press-releases?|/news(room)?/|in-the-news", "press", "awareness"),
    (r"/events?/|empower", "event", "consideration"),
    (r"^/(company|about)/.*(careers?|jobs?)", "careers", "none"),
    (r"^/(company|about|partners?)/", "company", "awareness"),
    (r"privacy|terms|cookie|legal|gdpr-notice", "legal", "none"),
]
WEIGHT_TITLE, WEIGHT_PATH, WEIGHT_TEXT = 3, 3, 1


def norm_path(url_or_path: str) -> str:
    p = urlparse(url_or_path).path if "://" in (url_or_path or "") else (url_or_path or "")
    p = re.sub(r"^/Website", "", p.split("?")[0].split("#")[0])
    return (p.rstrip("/") or "/").lower()


def _count(terms: List[str], hay: str) -> int:
    return sum(len(re.findall(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])", hay)) for t in terms)


def page_type(path: str):
    for rx, kind, stage in TYPE_RULES:
        if re.search(rx, path):
            return kind, stage
    return "page", "awareness"


def classify(url: str, title: str = "", text: str = "") -> dict:
    """Products (best first, at most 3), industry, type and buying stage for one page."""
    path = norm_path(url)
    kind, stage = page_type(path)
    products: List[str] = []
    direct = scoring.rule_topics(path)  # the page's own product address
    m = re.match(r"^/products/([a-z0-9-]+)", path)
    if direct:
        products = list(direct)
    elif m and scoring.rule_topics(f"/products/{m.group(1)}"):
        products = scoring.rule_topics(f"/products/{m.group(1)}")  # pages under a product (answers, learning, ...)
    path_words = path.replace("/", " ").replace("-", " ")
    t, body = (title or "").lower(), (text or "")[:1500].lower()
    scores = {}
    for slug, terms in PRODUCT_TERMS.items():
        s = WEIGHT_TITLE * _count(terms, t) + WEIGHT_PATH * _count(terms, path_words) + WEIGHT_TEXT * _count(terms, body)
        if s:
            scores[slug] = s
    if products:
        scores = {k: v for k, v in scores.items() if k not in products}
    ranked = [s for s, v in sorted(scores.items(), key=lambda kv: -kv[1]) if v >= 2]
    products = (products + ranked)[:3]
    industry = scoring.rule_industry(path)
    if not industry:
        best = max(((slug, WEIGHT_TITLE * _count(terms, t) + WEIGHT_PATH * _count(terms, path_words) + WEIGHT_TEXT * _count(terms, body))
                    for slug, terms in INDUSTRY_TERMS.items()), key=lambda kv: kv[1])
        industry = best[0] if best[1] >= 2 else None
    lines = list(dict.fromkeys(scoring.PRODUCT_TO_LINE[p] for p in products if p in scoring.PRODUCT_TO_LINE))
    return {"path": path, "type": kind, "stage": stage, "products": products, "lines": lines, "industry": industry,
            "gated": "/resources/lg/" in path + "/"}


def products_for_text(text: str, limit: int = 3) -> List[str]:
    """Products named in free text, e.g. a form's "solution of interest" answer ("Enterprise Content Services")."""
    hay = (text or "").lower()
    scores = {slug: _count(terms, hay) for slug, terms in PRODUCT_TERMS.items()}
    return [s for s, v in sorted(scores.items(), key=lambda kv: -kv[1]) if v][:limit]


def lead_topics(source_page: str = "", interest: str = "", resource_slug: str = "") -> List[str]:
    """Products for a solix.com form submission: what the visitor said they want first, then the page they were on."""
    out: List[str] = []
    for slug in products_for_text(interest) + ((lookup(source_page) or {}).get("products") or []):
        if slug in scoring.PRODUCTS and slug not in out:
            out.append(slug)
    if resource_slug and not out:
        for kind in ("white-papers", "datasheets", "case-studies", "ebooks", "on-demand-webinars"):
            entry = lookup(f"/resources/{kind}/{resource_slug}")
            if entry:
                out = [p for p in entry.get("products") or [] if p in scoring.PRODUCTS]
                break
    return out[:5]


# --- lookup (what scoring uses) ------------------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _loaded() -> dict:
    try:
        return json.loads(MAP_FILE.read_text(encoding="utf-8")).get("pages", {})
    except (OSError, ValueError):
        return {}


def _twin(path: str) -> str:
    """/resources/lg/x/y and /documents/x/y.pdf both point at the open page /resources/x/y."""
    if path.startswith("/resources/lg/"):
        return "/resources/" + path[len("/resources/lg/"):]
    m = re.match(r"^/documents/(.+?)\.pdf$", path)
    return f"/resources/{m.group(1)}" if m else path


def lookup(url_or_path: str) -> Optional[dict]:
    """The map entry for a solix.com page or file, or None."""
    pages = _loaded()
    if not pages or not (url_or_path or "").strip():
        return None
    path = norm_path(url_or_path)
    return pages.get(path) or pages.get(_twin(path))


def reload() -> None:
    _loaded.cache_clear()


# --- build ---------------------------------------------------------------------------------------------------------

def _overrides() -> Dict[str, dict]:
    if not OVERRIDES_FILE.exists():
        return {}
    out = {}
    with OVERRIDES_FILE.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            path = norm_path(row.get("path") or "")
            if not path:
                continue
            o = {}
            if row.get("products"):
                o["products"] = [p.strip() for p in row["products"].replace(";", ",").split(",") if p.strip() in scoring.PRODUCTS][:3]
                o["lines"] = list(dict.fromkeys(scoring.PRODUCT_TO_LINE[p] for p in o["products"]))
            for k in ("industry", "type", "stage"):
                if row.get(k):
                    o[k] = row[k].strip()
            out[path] = o
    return out


async def build(types=("pages", "kb", "glossary", "leadership-lessons")) -> dict:
    """Fetch every published page through the WordPress REST API and classify it (read-only for the site)."""
    import asyncio
    import migrate
    pages: Dict[str, dict] = {}
    async with migrate.new_client() as client:
        for wp_type in types:
            page = 1
            while True:
                rows = await migrate._json(client, f"{SITE}/wp-json/wp/v2/{wp_type}?per_page=100&page={page}&status=publish"
                                                   f"&_fields=link,title,content,modified_gmt")
                if not isinstance(rows, list) or not rows:
                    break
                for item in rows:
                    link = item.get("link") or ""
                    if not link.startswith(SITE):
                        continue
                    html = (item.get("content") or {}).get("rendered", "")
                    title = migrate._text((item.get("title") or {}).get("rendered", ""))
                    entry = classify(link, title, migrate._text(html)[:3000])
                    files = sorted(set(re.findall(r"https?://www\.solix\.com(/documents/[^\"'\s>]+?\.pdf)", html)))
                    entry.update(title=title[:200], url=link, wp_type=wp_type, modified=item.get("modified_gmt"),
                                 assets=files[:5])
                    pages[entry["path"]] = entry
                if len(rows) < 100:
                    break
                page += 1
                await asyncio.sleep(0.4)
    # Gated form pages and their open twins share the file and the classification of whichever saw more text.
    for path, e in pages.items():
        if e["gated"]:
            twin = pages.get(_twin(path))
            if twin:
                e["assets"] = e["assets"] or twin["assets"]
                twin["gated_form"] = e["url"]
                if not e["products"] and twin["products"]:
                    e.update(products=twin["products"], lines=twin["lines"])
    for path, o in _overrides().items():
        if path in pages:
            pages[path].update(o, overridden=True)
    return pages


def write(pages: Dict[str, dict]) -> None:
    HERE.mkdir(parents=True, exist_ok=True)
    compact = {p: {k: e[k] for k in ("type", "stage", "products", "lines", "industry", "gated") if e.get(k) not in (None, [], False)}
               for p, e in sorted(pages.items())}
    MAP_FILE.write_text(json.dumps({"site": SITE, "pages": compact}, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    with CSV_FILE.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["path", "title", "type", "stage", "products", "product_lines", "industry", "gated", "asset_files", "gated_form", "wp_type", "modified", "url"])
        for p, e in sorted(pages.items()):
            w.writerow([p, e.get("title"), e["type"], e["stage"], ";".join(e["products"]), ";".join(e["lines"]), e.get("industry") or "",
                        "yes" if e["gated"] else "", ";".join(e.get("assets") or []), e.get("gated_form") or "", e.get("wp_type"),
                        e.get("modified"), e.get("url")])
    reload()


if __name__ == "__main__":
    import asyncio
    import sys
    os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:1")
    os.environ.setdefault("DB_NAME", "site_map")
    os.environ.setdefault("JWT_SECRET", "unused-by-the-map-builder-" + "x" * 16)
    if sys.argv[1:] != ["build"]:
        sys.exit("usage: python -m site_map build")
    result = asyncio.run(build())
    write(result)
    print(f"{len(result)} pages mapped -> {MAP_FILE.name}, {CSV_FILE.name}")
