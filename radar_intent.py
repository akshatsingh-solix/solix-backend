"""Third-party intent: companies researching a competitor or the problem, as Radar "intent" signals.

Nobody can see who visits another company's website except that company. The lawful sources of "who is
researching a competitor or a solution" are B2B intent providers that collect it with consent across
their own networks, and their exports land here:

* G2 Buyer Intent: companies viewing competitor profiles, comparison and category pages on G2;
* Bombora Company Surge: companies whose content consumption on a topic ("data archiving",
  "OpenText Documentum") is above their normal level (also resold inside many CRMs and data tools);
* TrustRadius, Gartner Digital Markets (Capterra, GetApp), 6sense, Demandbase, ZoomInfo intent.

Upload their CSV export in Admin > Radar > Setup, or POST rows from an automation. Column names are matched
loosely (see COLUMNS): a company or domain, and a topic are required; score, date, country, employees and
activity are used when present. A topic that names a competitor product becomes that competitor's signal,
so the account gets the right Solix product line and the rep rule (never name the competitor) applies.

The same signal type also covers research seen on the Solix site itself: radar_visitors marks visitors from
an identified organisation who read pages or searched for a competitor's product.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional

import radar_model as rm
import radar_service as rs

COLUMNS: Dict[str, List[str]] = {
    "company": ["company", "company name", "account", "account name", "organization", "organisation", "organization name", "business name", "name"],
    "domain": ["domain", "company domain", "website", "company website", "website url", "url", "company url"],
    "topic": ["topic", "topics", "intent topic", "topic name", "keyword", "keywords", "category", "product", "product name", "competitor", "research topic",
              "surging topics", "intent signal"],
    "activity": ["activity", "activity type", "action", "signal", "signal type", "page", "page type", "description", "behavior", "behaviour"],
    "score": ["score", "intent score", "composite score", "surge score", "buyer intent score", "intensity", "strength", "intent strength"],
    "date": ["date", "week", "week of", "date seen", "last seen", "last activity", "activity date", "timestamp", "first seen", "created at"],
    "country": ["country", "hq country", "company country", "location country", "visitor country"],
    "employees": ["employees", "employee count", "company size", "size", "headcount", "number of employees"],
    "source": ["source", "provider", "vendor", "data source"],
}


def _norm(header: str) -> str:
    return re.sub(r"[\s_\-]+", " ", (header or "").strip().lower())


def parse(text: str) -> List[dict]:
    """A CSV export as rows keyed by the canonical names above (unknown columns are dropped)."""
    text = (text or "").lstrip("﻿")
    reader = csv.DictReader(io.StringIO(text))
    lookup = {}
    for h in reader.fieldnames or []:
        n = _norm(h)
        for key, aliases in COLUMNS.items():
            if n in aliases and key not in lookup.values():
                lookup[h] = key
                break
    rows = []
    for raw in reader:
        row = {}
        for h, key in lookup.items():
            v = (raw.get(h) or "").strip()
            if v and key not in row:
                row[key] = v
        if row:
            rows.append(row)
    return rows


def _date(value) -> str:
    if not value:
        return datetime.now(timezone.utc).date().isoformat()
    s = str(value).strip()
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return "-".join(m.groups())
    for fmt in ("%m/%d/%Y", "%d/%m/%Y", "%b %d, %Y", "%d %b %Y", "%B %d, %Y", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return datetime.now(timezone.utc).date().isoformat()


def _number(value) -> Optional[float]:
    try:
        return float(re.sub(r"[^\d.]", "", str(value))) if value not in (None, "") and re.search(r"\d", str(value)) else None
    except ValueError:
        return None


def signals_for(row: dict, source: str) -> List[dict]:
    """The intent signals one row stands for: one per competitor the topic names, else one for the problem or topic."""
    company = (row.get("company") or "").strip()
    domain = rm.domain_of(row.get("domain")) if row.get("domain") else None
    topic = (row.get("topic") or row.get("activity") or "").strip()
    if not (company or domain) or not topic:
        return []
    src = (row.get("source") or source or "Intent data").strip()[:60]
    when = _date(row.get("date"))
    score = _number(row.get("score"))
    activity = (row.get("activity") or "").strip()
    text = f"{topic} {activity}"
    base = {"company": company or domain.split(".")[0].title(), "domain": domain, "type": "intent", "source": re.sub(r"[^a-z0-9]+", "_", src.lower()).strip("_") or "intent",
            "posted_at": when, "country": row.get("country") or None, "value": score, "employees": _number(row.get("employees")),
            "meta": {"provider": src, "topic": topic[:200], "score": score, **({"activity": activity[:200]} if activity else {})}}
    detail = f"{src}: researching “{topic[:160]}”" + (f" ({activity[:120]})" if activity and activity.lower() != topic.lower() else "") + (f", score {score:g}" if score is not None else "")
    key = domain or rm.company_key(company)
    comps = rm.find_competitors(text)
    if comps:
        return [{**base, "competitor_key": c["key"], "competitor_product": c["product"], "title": f"Researching {c['product']} ({src})", "evidence_text": detail[:400],
                 "source_ref": f"{src}|{key}|{c['key']}|{when}"} for c in comps]
    prob = rm.find_problem(text)
    return [{**base, "problem_term": prob["term"] if prob else None, "title": f"Researching {topic[:120]} ({src})", "evidence_text": detail[:400],
             "source_ref": f"{src}|{key}|{topic.lower()[:120]}|{when}"}]


async def import_rows(rows: List[dict], source: str, by: str) -> dict:
    """Store intent rows as signals on their accounts (created when new), in bulk, idempotently."""
    resolver = await rs.Resolver.load()
    ops, touched, skipped = [], set(), 0
    for row in rows:
        sigs = signals_for(row, source)
        if not sigs:
            skipped += 1
            continue
        for sig in sigs:
            company = resolver.resolve(sig["company"], sig.get("domain"), "intent")
            if sig.get("employees") and not company.get("employees"):
                resolver.set(company, {"employees": int(sig["employees"])})
            ops.append(rs._signal_op(company, {**sig, "meta": {**sig["meta"], "imported_by": by}}))
            touched.add(company["id"])
    await resolver.flush()
    new = await rs.save_signals(ops)
    await rs.recompute_many(touched)
    return {"rows": len(rows), "signals_new": new, "signals_seen": len(ops) - new, "accounts": len(touched), "skipped": skipped}
