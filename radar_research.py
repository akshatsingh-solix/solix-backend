"""Master Prospector: an evidence-backed deep dive on one Radar account.

Every claim must cite a numbered source; the first-touch email never names the
competitor (the rep rule) and never quotes prices. Drafts wait in the review
queue until a person approves them - only approved deep dives mark an account
"Ready for outreach". With no AI provider configured, a rules-based brief built
from the signals themselves keeps the queue moving.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from typing import List, Optional

import httpx

import database
import radar_model as rm
import radar_service as rs
from database import now_iso

logger = logging.getLogger("solix.radar.research")
MAX_SOURCES = 14

SYSTEM = """You are Master Prospector, a B2B research analyst for Solix Technologies (enterprise data archiving, application retirement and legacy decommissioning, data governance, enterprise AI readiness, enterprise content services).
You write an account deep dive for a sales rep from ONLY the numbered sources given. Rules:
- Cite every factual claim with its source number in square brackets, e.g. [3]. Never invent facts, people, numbers or dates.
- Contacts: only people named in the sources, with the title the source gives. If none are named, return an empty list.
- The first-touch email must NOT name any competitor or competitor product, must not mention pricing or discounts, must be under 120 words, plain text, start with "Hi {first name}," and end with "Best,\\n{rep name}". Talk about the problem the evidence points to.
- "Why Solix wins" may use the Solix knowledge excerpts (cite them as [K1], [K2]...) for Solix facts.
- score: 0-10 confidence that this account has a real, current need Solix solves. 6 or more means "Ready for Outreach", 4-5 "Research More", under 4 "Not a Fit".
Reply with one JSON object only, no prose, with keys: score (int), verdict, headline, summary, trigger, why_solix_wins, contacts ([{name, title}]), first_touch ({subject, body}), gaps."""


def _db():
    return database.db


async def queue(company: dict, by: str = "system") -> dict:
    existing = await _db().radar_research.find_one({"company_id": company["id"], "status": {"$in": ["queued", "running"]}}, {"_id": 0})
    if existing:
        return existing
    version = await _db().radar_research.count_documents({"company_id": company["id"]}) + 1
    doc = {"id": str(uuid.uuid4()), "company_id": company["id"], "company": company["name"], "version": version, "status": "queued", "requested_by": by, "created_at": now_iso()}
    await _db().radar_research.insert_one(dict(doc))
    await rs.add_history(company["id"], by, "research", "Deep dive requested")
    await rs.recompute(company["id"])
    return doc


async def run_queue(limit: int = 5, only: Optional[str] = None) -> dict:
    counts = {"done": 0, "failed": 0, "rules": 0}
    q = {"status": "queued", **({"id": only} if only else {})}
    jobs = await _db().radar_research.find(q, {"_id": 0}).sort("created_at", 1).to_list(limit)
    async with httpx.AsyncClient(follow_redirects=True, headers={"User-Agent": "SolixRadar/1.0 (+https://www.solix.com)"}) as client:
        for job in jobs:
            await _db().radar_research.update_one({"id": job["id"]}, {"$set": {"status": "running", "started_at": now_iso()}})
            company = await _db().companies.find_one({"id": job["company_id"]}, {"_id": 0})
            try:
                brief = await research(client, company)
                brief["checks"] = validate(brief, company)
                await _db().radar_research.update_one({"id": job["id"]}, {"$set": {**brief, "status": "draft", "finished_at": now_iso()}})
                counts["done"] += 1
                if brief.get("provider") == "rules":
                    counts["rules"] += 1
                await rs.add_history(company["id"], "system", "research", f"Deep dive drafted (score {brief.get('score')}) - waiting for review")
            except Exception as exc:  # noqa: BLE001 - one bad account must not stop the queue
                logger.exception("research failed for %s", job.get("company"))
                await _db().radar_research.update_one({"id": job["id"]}, {"$set": {"status": "failed", "error": str(exc)[:300], "finished_at": now_iso()}})
                counts["failed"] += 1
            await rs.recompute(job["company_id"])
    return counts


async def gather(client: httpx.AsyncClient, company: dict) -> List[dict]:
    """Numbered sources: the account's strongest signals first, then recent news."""
    sources: List[dict] = []
    signals = await _db().company_signals.find({"company_id": company["id"]}, {"_id": 0}).to_list(500)
    signals.sort(key=lambda s: (rm.STRENGTH_RANK.get(s.get("role_strength") or "Unrated", 3), -len(s.get("evidence_text") or "")))
    seen_urls = set()
    for s in signals:
        url = s.get("evidence_url")
        if url in seen_urls:
            continue
        seen_urls.add(url)
        label = {"competitor_job": "Job posting", "problem_job": "Job posting", "dns": "DNS record", "cert": "Certificate record", "linkedin": "LinkedIn activity",
                 "news": "News", "filing": "SEC filing", "tender": "Public tender"}.get(s["type"], s["type"])
        sources.append({"title": f"{label}: {s.get('title') or s.get('role_title') or ''}".strip(), "url": url, "date": str(s.get("posted_at") or "")[:10],
                        "text": s.get("evidence_text") or "", "kind": s["type"]})
        if len(sources) >= 8:
            break
    try:
        import seo
        news = await seo._news(client, f'"{company["name"]}"')
        for n in news[:10]:
            if n.get("url") in seen_urls:
                continue
            sources.append({"title": f"{n.get('source') or 'News'}: {n.get('title')}", "url": n.get("url"), "date": (n.get("at") or "")[:10], "text": n.get("title") or "", "kind": "news"})
            if len(sources) >= MAX_SOURCES:
                break
    except Exception:  # noqa: BLE001
        logger.info("news lookup failed for %s", company["name"])
    for i, s in enumerate(sources):
        s["n"] = i + 1
    return sources


def _kb(company: dict) -> str:
    try:
        import sol_search
        q = " ".join(filter(None, [company.get("primary_product") or "", "application retirement archiving", " ".join(company.get("competitors") or []), company.get("industry") or ""]))
        hits = sol_search.search(q, k=4)
        return "\n\n".join(f"[K{i + 1}] {h['title']} ({h['url']})\n{h['text'][:900]}" for i, h in enumerate(hits))
    except Exception:  # noqa: BLE001
        return ""


async def _complete(system: str, user: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    import llm
    if not llm.configured_providers():
        return None, None, None
    text, meta = [], {}
    try:
        async for ev in llm.stream_completion([{"role": "system", "content": system}, {"role": "user", "content": user}], max_tokens=2200, temperature=0.3):
            if ev["type"] == "text":
                text.append(ev["text"])
            elif ev["type"] == "meta":
                meta = ev
    except llm.ProviderError as e:
        logger.warning("research model unavailable: %s", e)
        return None, None, None
    return "".join(text), meta.get("provider"), meta.get("model")


def _parse_json(text: str) -> Optional[dict]:
    start, end = (text or "").find("{"), (text or "").rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


async def research(client: httpx.AsyncClient, company: dict) -> dict:
    sources = await gather(client, company)
    kb = _kb(company)
    facts = (f"Account: {company['name']}\nDomains: {', '.join(company.get('domains') or []) or 'unknown'}\nTerritory: {company.get('territory')}\n"
             f"Competitor products seen: {', '.join(company.get('competitors') or []) or 'none named'}\nSignal types: {', '.join(company.get('signal_types') or [])}\n"
             f"Primary Solix product fit: {company.get('primary_product') or 'unknown'}")
    src_block = "\n\n".join(f"[{s['n']}] {s['title']} ({s.get('date') or 'undated'}) {s.get('url') or ''}\n{(s.get('text') or '')[:700]}" for s in sources)
    user = f"{facts}\n\nSOURCES\n{src_block or '(no sources)'}\n\nSOLIX KNOWLEDGE\n{kb or '(none)'}\n\nReturn the JSON object now."
    text, provider, model = await _complete(SYSTEM, user)
    data = _parse_json(text or "") if text else None
    if not data:
        return rules_brief(company, sources)
    try:
        score = max(0, min(10, int(round(float(data.get("score", 0))))))
    except (TypeError, ValueError):
        score = 0
    verdict = data.get("verdict") or ("Ready for Outreach" if score >= rm.READY_SCORE else "Research More" if score >= 4 else "Not a Fit")
    ft = data.get("first_touch") or {}
    return {
        "score": score, "verdict": verdict, "headline": str(data.get("headline") or "")[:500], "summary": str(data.get("summary") or "")[:6000],
        "trigger": str(data.get("trigger") or "")[:3000], "why_solix_wins": str(data.get("why_solix_wins") or "")[:4000],
        "contacts": [{"name": str(c.get("name"))[:120], "title": str(c.get("title") or "")[:200], "linkedin_url": None} for c in (data.get("contacts") or []) if isinstance(c, dict) and c.get("name")][:5],
        "first_touch": {"subject": str(ft.get("subject") or "")[:200], "body": str(ft.get("body") or "")[:3000]}, "gaps": str(data.get("gaps") or "")[:3000],
        "evidence": [{"n": s["n"], "title": s["title"], "url": s.get("url"), "date": s.get("date")} for s in sources], "provider": provider, "model": model,
    }


def rules_brief(company: dict, sources: List[dict]) -> dict:
    """No model available: an honest, signal-only brief that a rep can still use."""
    jobs = [s for s in sources if s["kind"] in ("competitor_job", "problem_job")]
    kinds = sorted({s["kind"] for s in sources})
    score = min(5, 2 + len(kinds) + (1 if company.get("strength") == "Strong" else 0))
    cites = " ".join(f"[{s['n']}]" for s in jobs[:4])
    comps = ", ".join(company.get("competitors") or [])
    problem = "running and retiring legacy content and archive platforms" if comps else "archiving, retention and legacy application retirement"
    return {
        "score": score, "verdict": "Research More", "headline": f"{company['name']} shows {len(sources)} live signal(s) around {comps or 'archiving and retirement work'}.",
        "summary": (f"{company['name']} is hiring for roles that work with {comps} {cites}." if comps and cites else f"{company['name']} has {len(sources)} recent signals.") +
                   (" Recent coverage: " + "; ".join(f"{s['title']} [{s['n']}]" for s in sources if s["kind"] == "news")[:600] if any(s["kind"] == "news" for s in sources) else ""),
        "trigger": f"Active hiring around {problem} {cites}".strip(),
        "why_solix_wins": "Solix retires legacy applications into one compliant, searchable archive with retention and legal hold, removing license and infrastructure cost.",
        "contacts": [],
        "first_touch": {"subject": "Keeping retired systems' records without keeping the systems",
                        "body": "Hi {first name},\n\nTeams like yours often keep older applications running only because the records inside them must be retained. "
                                "Solix moves that data into one compliant, searchable archive with retention and legal hold, so the old platforms can be switched off.\n\n"
                                "Would a 20-minute conversation in the next two weeks be useful?\n\nBest,\n{rep name}"},
        "gaps": "Drafted without an AI model (no provider key, or every free tier was busy). Confirm the decision makers and the current initiative before outreach.",
        "evidence": [{"n": s["n"], "title": s["title"], "url": s.get("url"), "date": s.get("date")} for s in sources], "provider": "rules", "model": None,
    }


_PRICE = re.compile(r"(\$\s?\d|€\s?\d|£\s?\d|\bprice\b|\bpricing\b|\bdiscount\b|\bcost[s]? (?:only|just)\b)", re.I)


def validate(brief: dict, company: dict) -> List[dict]:
    checks = []
    ft = brief.get("first_touch") or {}
    email = f"{ft.get('subject') or ''}\n{ft.get('body') or ''}"
    named = rm.find_competitors(email)
    if named:
        checks.append({"level": "block", "message": f"First touch names a competitor ({', '.join(sorted({n['product'] for n in named}))}). Talk about the problem instead."})
    if _PRICE.search(email):
        checks.append({"level": "warn", "message": "First touch mentions pricing or cost figures."})
    if len((ft.get("body") or "").split()) > 160:
        checks.append({"level": "warn", "message": "First touch is longer than about 120 words."})
    ns = {e.get("n") for e in brief.get("evidence") or []}
    cited = {int(x) for x in re.findall(r"\[(\d+)\]", " ".join(str(brief.get(k) or "") for k in ("summary", "trigger", "why_solix_wins", "headline")))}
    bad = sorted(cited - ns)
    if bad:
        checks.append({"level": "block", "message": f"Cites sources that don't exist: {', '.join(f'[{b}]' for b in bad)}."})
    if brief.get("summary") and not cited:
        checks.append({"level": "warn", "message": "The summary has no source citations."})
    score = brief.get("score")
    if not isinstance(score, int) or not 0 <= score <= 10:
        checks.append({"level": "block", "message": "Score must be a whole number from 0 to 10."})
    for c in brief.get("contacts") or []:
        if c.get("name") and not any(c["name"].split()[-1].lower() in json.dumps(e).lower() for e in brief.get("evidence") or []):
            checks.append({"level": "warn", "message": f"{c['name']} isn't named in the listed sources; confirm before outreach."})
    return checks
