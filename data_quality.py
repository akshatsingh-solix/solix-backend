"""Data accuracy for the Radar: where every company fact came from, how likely it is to be right, and how accurate
each source has proven to be.

Nobody can promise that bought or scraped company data is 98% accurate. What this module does instead is make
accuracy *measured* and *enforced*:

1. Provenance. Every value a source reports is kept as an observation: {value, via, match, at, url}, newest per
   source (`companies.facts`). Headcounts keep using `size_candidates` (radar_model.size_view).
2. Measured source accuracy. Each time a person confirms or corrects a field, every source that reported it is
   scored right or wrong in `accuracy_log`. Email verifications, bounces and replies score the email finders the
   same way. A source's trust is its measured precision, shrunk towards a prior until it has enough checks.
3. A probability per value. Independent sources that agree multiply their chances of being wrong; a conflicting
   source counts against. A value is "verified" only when a person confirmed it or its probability reaches the
   accuracy target (98% by default), which is the bar the outreach gate and the "verified only" filter use.
4. An honest headline number. "Measured accuracy" is the share of values *shown in MarkHub* that people found right
   when they checked, with a 95% confidence interval. The audit queue picks values at random so that number is not
   biased towards the ones people happened to look at.
"""
from __future__ import annotations

import math
import random
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional

import cache
import radar_model as rm

TARGET = 0.98
PRIOR_TRUST = {"manual": 0.995, "sec": 0.95, "companies_house": 0.95, "gleif": 0.93, "wikidata": 0.85, "pdl_company": 0.85,
               "hunter_company": 0.8, "intent": 0.75, "theirstack": 0.7, "crustdata": 0.85, "unknown": 0.6}
DEFAULT_PRIOR = 0.8
PRIOR_WEIGHT = 20        # a source needs about this many checks before its measured rate outweighs the prior
NAME_MATCH_PENALTY = 0.85  # a value found by company name (not by the company's own website) is less certain

# Fields an account must have right for the Radar to target it well. Employees is judged by rm.size_view.
FIELDS = {"industry": "Industry", "revenue": "Revenue", "country": "HQ country", "domain": "Website domain", "employees": "Employees"}
CORE_FIELDS = ("industry", "country", "domain", "employees")

# --- Industry taxonomy: free-text industries from every source mapped to the Solix industries --------------------

INDUSTRY_LABELS = {"financial-services": "Financial services", "insurance": "Insurance", "healthcare": "Healthcare", "pharma-biotech": "Pharma & biotech",
                   "manufacturing": "Manufacturing", "public-sector": "Public sector", "retail": "Retail & consumer", "energy": "Energy & utilities",
                   "telecom": "Telecom & media", "technology": "Technology", "other": "Other"}
_INDUSTRY_RX = [  # order matters: the first match wins (insurance before financial services, pharma before healthcare)
    ("insurance", r"insur|reinsur|assurance|underwrit"),
    ("pharma-biotech", r"pharma|biotech|life ?science|drug|biolog|therapeut|vaccin"),
    ("healthcare", r"health|hospital|medical|clinic|patient|care provider|payer"),
    ("financial-services", r"bank|financ|capital market|asset manage|wealth|credit|payment|securit|invest|brokerage|lending|mortgage|fintech|stock exchange"),
    ("public-sector", r"government|public (sector|administration|service)|agency|municipal|federal|county|ministry|department of|defen[cs]e|military|"
                      r"educat|universit|school|college|police|council"),
    ("energy", r"energy|oil|gas|petrol|utilit|power|electric|mining|renewable|nuclear|water supply"),
    ("telecom", r"telecom|telecommunication|wireless|broadband|cable|mobile network|media|broadcast|publishing"),
    ("retail", r"retail|consumer|e-?commerce|grocery|apparel|wholesale|supermarket|restaurant|hospitality|food|beverage"),
    ("manufacturing", r"manufactur|industrial|automotive|aerospace|chemical|machinery|electronic|semiconductor|steel|engineering|construction|"
                      r"logistic|transport|shipping|airline|rail"),
    ("technology", r"software|information technology|\bit services|computer|internet|saas|cloud|data processing|technology"),
]
_INDUSTRY_COMPILED = [(slug, re.compile(rx, re.I)) for slug, rx in _INDUSTRY_RX]


def industry_slug(text: Optional[str]) -> Optional[str]:
    """'Commercial Banking' -> financial-services. None when there's nothing to go on."""
    t = (text or "").strip()
    if not t:
        return None
    if t in INDUSTRY_LABELS:
        return t
    for slug, rx in _INDUSTRY_COMPILED:
        if rx.search(t):
            return slug
    return "other"


# --- Observations -------------------------------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def observation(value, via: str, match: str = "name", *, url: Optional[str] = None, at: Optional[str] = None) -> Optional[dict]:
    if value in (None, "", [], {}):
        return None
    return {"value": value, "via": via, "match": match or "name", **({"url": url} if url else {}), "at": at or _now()}


def observations_from(got: dict, via: str) -> Dict[str, dict]:
    """What one firmographics source said, as observations per field."""
    match = got.get("match") or "name"
    url = got.get("source_url")
    hq = got.get("hq") or {}
    revenue = got.get("revenue_usd") or got.get("revenue_band")
    out = {"industry": observation(got.get("industry"), via, match, url=url), "revenue": observation(revenue, via, match, url=url),
           "country": observation(rm.country_norm(hq["country"]) if hq.get("country") else None, via, match, url=url),
           "domain": observation(rm.domain_of(got["domain"]) if got.get("domain") else None, via, match, url=url)}
    return {k: v for k, v in out.items() if v}


def merge_facts(existing: Optional[dict], new: Dict[str, List[dict]]) -> dict:
    """Newest observation per source per field; at most 8 sources a field."""
    out = {f: {o["via"]: o for o in obs or [] if o} for f, obs in (existing or {}).items()}
    for f, obs in (new or {}).items():
        for o in obs or []:
            if o:
                out.setdefault(f, {})[o["via"]] = o
    return {f: sorted(by.values(), key=lambda o: o.get("at") or "", reverse=True)[:8] for f, by in out.items() if by}


def key_of(field: str, value) -> Optional[str]:
    """A comparable form, so 'Commercial Banking' and 'Banking' agree, and 'US' and 'United States' agree."""
    if value in (None, ""):
        return None
    if field == "industry":
        return industry_slug(str(value))
    if field == "country":
        return rm.country_norm(str(value))
    if field == "domain":
        return rm.domain_of(str(value))
    if field in ("revenue", "employees"):
        n = rm.revenue_of(value) if field == "revenue" else _int(value)
        return str(n) if n else None
    return str(value).strip().lower()


def _int(v) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def agree(field: str, a, b) -> bool:
    ka, kb = key_of(field, a), key_of(field, b)
    if ka is None or kb is None:
        return False
    if field in ("revenue", "employees"):
        x, y = float(ka), float(kb)
        return max(x, y) <= 2 * min(x, y)  # numbers within 2x of each other describe the same company
    return ka == kb


# --- Trust and probability ----------------------------------------------------------------------------------------

def trust_of(via: str, stats: Optional[dict] = None, field: Optional[str] = None) -> float:
    """Measured precision of a source, shrunk towards its prior until it has PRIOR_WEIGHT checks."""
    prior = PRIOR_TRUST.get(via, DEFAULT_PRIOR)
    s = (stats or {}).get(f"{field}:{via}") or (stats or {}).get(f"*:{via}") or {}
    n, ok = s.get("n", 0), s.get("correct", 0)
    return (ok + prior * PRIOR_WEIGHT) / (n + PRIOR_WEIGHT)


def _obs_trust(o: dict, stats: Optional[dict], field: str) -> float:
    t = trust_of(o["via"], stats, field)
    if o.get("match") not in ("domain", "manual", "id"):
        t *= NAME_MATCH_PENALTY
    return min(t, 0.999)


def field_view(field: str, observations: List[dict], *, confirmed: Optional[dict] = None, stats: Optional[dict] = None, target: float = TARGET) -> dict:
    """The value to show for one field, how likely it is right, and why.

    status: verified (a person confirmed it, or independent sources push it past the target), likely (>= 90%),
            unverified (one weak source), conflicting (sources disagree and neither side is clearly right), missing."""
    if confirmed and confirmed.get("value") not in (None, ""):
        return {"field": field, "value": confirmed["value"], "p": 0.995, "status": "verified", "basis": f"Confirmed by {confirmed.get('by') or 'a person'}",
                "sources": ["manual"], "conflicts": []}
    obs = [o for o in observations or [] if o and o.get("value") not in (None, "")]
    if not obs:
        return {"field": field, "value": None, "p": 0.0, "status": "missing", "basis": "No source has reported it", "sources": [], "conflicts": []}
    groups: List[List[dict]] = []
    for o in obs:  # cluster observations that agree with each other
        for g in groups:
            if agree(field, g[0]["value"], o["value"]):
                g.append(o)
                break
        else:
            groups.append([o])
    support = []
    for g in groups:
        wrong = 1.0
        for o in g:
            wrong *= 1 - _obs_trust(o, stats, field)
        support.append(1 - wrong)
    # Each group is a hypothesis with odds support/(1-support); "every source is wrong" has odds 1. With one group this
    # is exactly its support; a conflicting group pulls the winner down in proportion to its own strength.
    odds = [s / max(1e-6, 1 - s) for s in support]
    best_i = max(range(len(groups)), key=lambda i: (odds[i], len(groups[i])))
    p = odds[best_i] / (1 + sum(odds))
    best = groups[best_i]
    top = max(best, key=lambda o: (_obs_trust(o, stats, field), o.get("at") or ""))
    conflicts = [{"value": g[0]["value"], "sources": [o["via"] for o in g]} for i, g in enumerate(groups) if i != best_i]
    if conflicts and p < 0.9:
        status = "conflicting"
    elif p >= target:
        status = "verified"
    elif p >= 0.9:
        status = "likely"
    else:
        status = "unverified"
    names = ", ".join(rm.SOURCE_LABELS.get(o["via"], o["via"]) for o in best)
    basis = f"{len(best)} source{'s' if len(best) > 1 else ''} agree ({names})" if len(best) > 1 else f"Only {names}"
    if conflicts:
        basis += "; " + "; ".join(f"{', '.join(rm.SOURCE_LABELS.get(v, v) for v in c['sources'])} said {c['value']}" for c in conflicts[:2])
    return {"field": field, "value": top["value"], "p": round(p, 4), "status": status, "basis": basis, "sources": [o["via"] for o in best], "conflicts": conflicts}


def employees_view(company: dict, stats: Optional[dict] = None, target: float = TARGET) -> dict:
    """Headcount through the same lens, using the size candidates and rm.size_view's checks."""
    size = rm.size_view(company)
    if size["size_status"] == "verified":
        return {"field": "employees", "value": size["employees"], "p": 0.995, "status": "verified", "basis": "Entered or confirmed by a person", "sources": ["manual"], "conflicts": []}
    cands = [{"value": c["value"], "via": c["via"], "match": c.get("match") or "name", "at": c.get("at")} for c in company.get("size_candidates") or [] if c and c.get("value")]
    view = field_view("employees", cands, stats=stats, target=target)
    if size["size_status"] == "doubtful":
        view.update({"status": "conflicting", "p": min(view["p"], 0.5), "basis": "; ".join(size["size_flags"]) or view["basis"]})
    return view


def account_quality(company: dict, stats: Optional[dict] = None, target: float = TARGET) -> dict:
    """Per-field views and one score for the account: the average chance its core facts are right."""
    facts = company.get("facts") or {}
    confirmed = company.get("confirmed") or {}
    fallback = {"industry": company.get("industry"), "revenue": company.get("revenue_usd") or company.get("revenue_band"),
                "country": (company.get("hq") or {}).get("country"), "domain": (company.get("domains") or [None])[0]}
    views = {}
    for f in ("industry", "revenue", "country", "domain"):
        obs = list(facts.get(f) or [])
        if not obs and fallback[f]:  # recorded before observations were kept: one source of unknown quality
            obs = [{"value": fallback[f], "via": "unknown", "match": "unknown"}]
        views[f] = field_view(f, obs, confirmed=confirmed.get(f), stats=stats, target=target)
    views["employees"] = employees_view(company, stats, target)
    core = [views[f] for f in CORE_FIELDS]
    score = round(100 * sum(v["p"] for v in core) / len(core))
    return {"score": score, "target": round(target * 100, 1), "verified": sum(1 for v in core if v["status"] == "verified"), "core": len(core),
            "meets_target": all(v["status"] == "verified" for v in core),
            "conflicting": [f for f, v in views.items() if v["status"] == "conflicting"], "missing": [f for f in CORE_FIELDS if views[f]["status"] == "missing"],
            "fields": views, "industry_slug": industry_slug(views["industry"]["value"])}


# --- The ledger: measured accuracy ---------------------------------------------------------------------------------

def _db():
    import database
    return database.db


async def log(kind: str, field: str, via: Optional[str], correct: bool, *, how: str, by: str = "system", shown: Optional[bool] = None, **ref) -> None:
    """One check of one source's value. `shown` is set when the value MarkHub displayed was the one checked."""
    await _db().accuracy_log.insert_one({"kind": kind, "field": field, "via": via or "unknown", "correct": bool(correct), "how": how, "by": by,
                                         **({"shown": bool(shown)} if shown is not None else {}), **{k: v for k, v in ref.items() if v}, "at": _now()})
    cache.bump("accuracy")


async def stats(days: int = 365) -> dict:
    """{'field:via': {n, correct}, '*:via': {...}} over the window, for trust_of."""
    async def load():
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        rows = await _db().accuracy_log.aggregate([{"$match": {"at": {"$gte": since}}},
                                                   {"$group": {"_id": {"f": "$field", "v": "$via"}, "n": {"$sum": 1}, "correct": {"$sum": {"$cond": ["$correct", 1, 0]}}}}]).to_list(2000)
        out: dict = {}
        for r in rows:
            f, v = r["_id"]["f"], r["_id"]["v"]
            out[f"{f}:{v}"] = {"n": r["n"], "correct": r["correct"]}
            agg = out.setdefault(f"*:{v}", {"n": 0, "correct": 0})
            agg["n"] += r["n"]
            agg["correct"] += r["correct"]
        return out
    return await cache.memo("accuracy", f"stats{days}", 120, load)


def wilson(correct: int, n: int, z: float = 1.96) -> tuple:
    if not n:
        return (None, None)
    p = correct / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (round(max(0.0, c - m), 4), round(min(1.0, c + m), 4))


async def report(days: int = 90, target: float = TARGET) -> dict:
    """Measured accuracy of what MarkHub shows, by field, and of every source, with 95% intervals."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = await _db().accuracy_log.find({"at": {"$gte": since}}, {"_id": 0}).to_list(50000)
    shown = [r for r in rows if "shown" in r]
    audit = [r for r in shown if r.get("how") == "audit"]

    def block(rs: list) -> dict:
        n, ok = len(rs), sum(1 for r in rs if r.get("shown", r.get("correct")))
        lo, hi = wilson(ok, n)
        return {"n": n, "correct": ok, "rate": round(ok / n, 4) if n else None, "low": lo, "high": hi,
                "meets_target": bool(lo is not None and lo >= target), "estimate_meets_target": bool(n and ok / n >= target)}
    by_field = {}
    for f in sorted({r["field"] for r in shown}):
        by_field[f] = block([r for r in shown if r["field"] == f])
    sources: Dict[str, dict] = {}
    for r in rows:
        s = sources.setdefault(f"{r['kind']}|{r['field']}|{r['via']}", {"kind": r["kind"], "field": r["field"], "via": r["via"], "n": 0, "correct": 0})
        s["n"] += 1
        s["correct"] += 1 if r["correct"] else 0
    st = await stats()
    src_rows = []
    for s in sources.values():
        lo, hi = wilson(s["correct"], s["n"])
        src_rows.append({**s, "label": rm.SOURCE_LABELS.get(s["via"], s["via"]), "rate": round(s["correct"] / s["n"], 4), "low": lo, "high": hi,
                         "trust": round(trust_of(s["via"], st, s["field"]), 4)})
    src_rows.sort(key=lambda s: (s["kind"], s["field"], -(s["rate"] or 0)))
    overall = block(shown)
    needed = None
    if overall["n"] and overall["rate"] is not None and overall["rate"] >= target and not overall["meets_target"]:
        # How many more checks, at the current rate, before the lower bound clears the target.
        n, p = overall["n"], overall["rate"]
        while n < 20000 and (wilson(round(p * n), n)[0] or 0) < target:
            n += 10
        needed = n - overall["n"] if n < 20000 else None
    return {"days": days, "target": target, "overall": overall, "audit": block(audit), "by_field": by_field, "sources": src_rows, "checks_needed": needed,
            "email": block([{"shown": r["correct"]} for r in rows if r["kind"] == "email"]),
            "explain": ("Measured accuracy is the share of values MarkHub showed that a person found right when they checked them. The target is met when "
                        "the lower end of the 95% interval clears it, so a small sample can't claim it.")}


async def record_check(company: dict, field: str, true_value, *, by: str, how: str) -> dict:
    """A person said what `field` really is: score every source that reported it, and whether the shown value was right."""
    st = await stats()
    q = account_quality(company, st)
    view = q["fields"].get(field) or {}
    facts = company.get("facts") or {}
    if field == "employees":
        obs = [{"value": c["value"], "via": c["via"]} for c in company.get("size_candidates") or [] if c and c.get("value")]
    else:
        obs = list(facts.get(field) or [])
    for o in obs:
        await log("company", field, o["via"], agree(field, o["value"], true_value), how=how, by=by, company_id=company.get("id"))
    if view.get("value") not in (None, "") and view.get("sources") != ["manual"]:
        await log("company", field, "shown", agree(field, view["value"], true_value), how=how, by=by, shown=agree(field, view["value"], true_value),
                  company_id=company.get("id"), status=view.get("status"))
    return view


def audit_sample(companies: Iterable[dict], n: int = 20, seed: Optional[int] = None) -> List[dict]:
    """Random (account, field) pairs whose shown value no person has confirmed: an unbiased sample to check."""
    pool = []
    for c in companies:
        q = account_quality(c)
        for f, v in q["fields"].items():
            if v["value"] in (None, "") or v["sources"] == ["manual"]:
                continue
            pool.append({"company_id": c["id"], "company": c["name"], "domain": (c.get("domains") or [None])[0], "field": f, "label": FIELDS[f],
                         "value": v["value"], "status": v["status"], "p": v["p"], "basis": v["basis"]})
    rnd = random.Random(seed)
    rnd.shuffle(pool)
    return pool[:n]
