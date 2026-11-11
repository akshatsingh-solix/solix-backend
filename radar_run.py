"""The Radar's daily run: collect -> enrich -> score -> research -> digest.

  collect  every signal source (ATS boards, the job market, DNS and certificates, news, filings,
           tenders and public spend, website visits), each provider paced to its free allowance
  enrich   size, HQ and domain for the accounts that lack them (mid-market vs enterprise, routing),
           and public job boards detected for accounts with a website

Runs three ways, all writing the same `ops_runs` heartbeat:
  * Daily inside the API:  ops.radar_schedule, at RADAR_DAILY_UTC (05:00 by default)
  * Admin > Radar > "Run now" (in the API process)
  * By hand:               python radar_run.py collect|score|research|digest (or the manual GitHub workflow)

Logs print counts only - never company or person names - because workflow logs
on a public repository are public.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Iterable, List, Optional

import httpx
from pymongo import UpdateOne

import database
import ops
import providers as pv
import radar_collectors as rc
import radar_model as rm
import radar_service as rs
import radar_sources as srcs
from database import now_iso

logger = logging.getLogger("solix.radar.run")


def _db():
    return database.db


async def _as_list(coro) -> list:
    """A lookup's result as a one-item list, so `guarded` can count it and swallow its errors."""
    result = await coro
    return [result] if result else []


async def collect(run: dict) -> set:
    settings = await rs.radar_settings()
    on = settings["collectors"]
    budget = {"credits": 0.0, "cap": float(settings.get("credit_cap") or 0)}
    signals: list = []
    counts = run["counts"]
    async with httpx.AsyncClient(headers=rc.UA, follow_redirects=True) as client:
        async def guarded(name, coro):
            try:
                got = await coro
                counts[f"{name}_found"] = counts.get(f"{name}_found", 0) + len(got)
                return got
            except Exception as exc:  # noqa: BLE001 - a dead source costs only its own signals
                counts[f"{name}_errors"] = counts.get(f"{name}_errors", 0) + 1
                run["notes"].append(f"{name}: {type(exc).__name__}")
                return []

        if on.get("ats"):
            for t in settings.get("targets") or []:
                fn = rc.ATS.get((t.get("ats") or "").lower())
                if fn and (t.get("board") or t.get("workday_url")):
                    signals += await guarded("ats", fn(client, t))
        if on.get("ats_xray") and pv.any_configured("web_search") and await pv.enabled("ats_xray"):
            signals += await guarded("ats_xray", xray_boards(client, settings, run))
        if on.get("jobs_api") and pv.any_configured("jobs", list(srcs.JOB_ADAPTERS)):
            signals += await guarded("jobs_api", market_jobs(client, settings, run))
        if on.get("crustdata") and os.environ.get("CRUSTDATA_API_KEY") and await pv.enabled("crustdata"):
            signals += await guarded("crustdata", rc.crustdata(client, settings, budget))
            await pv.succeeded("crustdata", int(budget["credits"] / 0.03))
        if on.get("dns"):
            domains = set(settings.get("dns_domains") or [])
            owners = {}
            async for c in _db().companies.find({"domains.0": {"$exists": True}}, {"_id": 0, "name": 1, "domains": 1}):
                for d in c["domains"]:
                    owners[d] = c["name"]
                    domains.add(d)
            sem = asyncio.Semaphore(8)
            scan = sorted(domains)[:800]
            # Each scan is compared with the last one, so a fingerprint that appears or disappears is a signal of its own.
            snaps = {x["domain"]: x.get("records") or {} async for x in _db().radar_dns.find({"domain": {"$in": scan}}, {"_id": 0})}
            snap_ops = []

            async def one(d):
                async with sem:
                    records = await guarded("dns_lookup", _as_list(rc.dns_records(client, d, use_crtsh=len(domains) <= 200)))
                    if not records:
                        return []
                    found = rc.dns_signals_from(owners.get(d) or d.split(".")[0].title(), d, records[0], snaps.get(d))
                    snap_ops.append(UpdateOne({"domain": d}, {"$set": {**{f"records.{t}": v for t, v in records[0].items()}, "scanned_at": now_iso()}}, upsert=True))
                    counts["dns_found"] = counts.get("dns_found", 0) + len(found)
                    counts["dns_changes"] = counts.get("dns_changes", 0) + sum(1 for x in found if x.get("action") in ("added", "removed"))
                    return found
            for got in await asyncio.gather(*(one(d) for d in scan)):
                signals += got
            if snap_ops:
                await _db().radar_dns.bulk_write(snap_ops, ordered=False)
        if on.get("news"):
            hot = await _db().companies.find({"strength": "Strong", "excluded_reason": None}, {"_id": 0, "name": 1, "domains": 1}).sort("score", -1).to_list(15)
            for c in hot:
                signals += await guarded("news", rc.news_signals(client, c["name"], (c.get("domains") or [None])[0]))
        if on.get("filings"):
            signals += await guarded("filings", rc.edgar_signals(client))
        if on.get("tenders") and await pv.enabled("ted"):
            signals += await guarded("tenders", rc.ted_tenders(client, settings))
        if on.get("public_sector"):
            signals += await guarded("public_sector", srcs.public_sector(client))
        if on.get("subdomains") and pv.any_configured("dns_history"):
            signals += await guarded("subdomains", subdomain_scan(client, settings))
        if on.get("visitors"):
            import radar_visitors
            signals += await guarded("visitors", radar_visitors.signals(int(settings.get("visitor_days") or 30)))
    counts["credits_used"] = budget["credits"]
    touched = set()
    resolver = await rs.Resolver.load()  # every posting saved in bulk: a round trip each adds up against Atlas
    ops = []
    for sig in signals:
        if not sig.get("company"):
            continue
        company = resolver.resolve(sig["company"], sig.get("domain"), sig.get("source") or "radar")
        if sig.get("public_sector") and not company.get("industry"):
            resolver.set(company, {"industry": "Public sector"})
        if sig.get("employees") and not company.get("employees"):
            resolver.set(company, {"employees": int(sig["employees"])})
        ops.append(rs._signal_op(company, sig, run["id"]))
        touched.add(company["id"])
    await resolver.flush()
    counts["signals_new"] = await rs.save_signals(ops)
    counts["signals_seen"] = len(ops) - counts["signals_new"]
    counts["companies_touched"] = len(touched)
    return touched


# --- Free job-market and ATS discovery -------------------------------------------------------

PROBLEM_TERMS = ["application retirement", "data archiving", "legacy decommissioning"]


def _rotate(items: list) -> list:
    """Start the list at a different place each day, so every phrase gets the day's fullest allowances in turn."""
    if not items:
        return items
    k = datetime.now(timezone.utc).timetuple().tm_yday % len(items)
    return items[k:] + items[:k]


def job_terms(settings: dict) -> List[str]:
    out, seen = [], set()
    for t in [c["terms"][0] for c in rm.COMPETITORS] + PROBLEM_TERMS + list(settings.get("job_terms") or []):
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return _rotate(out)


async def market_jobs(client: httpx.AsyncClient, settings: dict, run: dict) -> list:
    """Postings across the job market that name a competitor or describe the problem (TheirStack, Adzuna, JSearch,
    Jooble, Reed, USAJOBS), smaller than mid-market dropped when the source knows the headcount."""
    min_emp = int(settings.get("min_employees") or 0)
    out = []
    for term in job_terms(settings):
        for p in await srcs.job_postings(client, term, settings, per_term=int(settings.get("jobs_per_term") or 2), problems=run["notes"]):
            if not p.get("company") or (p.get("employees") and min_emp and int(p["employees"]) < min_emp):
                continue
            sigs = rc.classify_posting(p["company"], p["title"], p["text"], url=p["url"], location=p["location"], country=p["country"], state=p["state"],
                                       posted=p["posted"], source=p["source"], source_ref=p["source_ref"], domain=p.get("domain"),
                                       matched_term=term if term not in PROBLEM_TERMS else None)
            for sg in sigs:
                if p.get("employees"):
                    sg["employees"] = p["employees"]
            out += sigs
    return out


async def board_company(client: httpx.AsyncClient, board: dict) -> str:
    if board["ats"] == "greenhouse":
        try:
            r = await client.get(f"https://boards-api.greenhouse.io/v1/boards/{board['board']}", timeout=15)
            if r.status_code == 200 and r.json().get("name"):
                return r.json()["name"]
        except (httpx.HTTPError, ValueError):
            pass
    stem = board["board"].split(".")[0] if board["ats"] == "workday" else board["board"]
    return " ".join(w.capitalize() for w in stem.replace("-", " ").replace("_", " ").split())


async def xray_boards(client: httpx.AsyncClient, settings: dict, run: dict) -> list:
    """Public ATS boards with a competitor-named posting, found through search engines; new ones are read now (free)
    and join the daily ATS scan, so coverage compounds."""
    pick = _rotate([c["terms"][0] for c in rm.COMPETITORS])[:int(settings.get("xray_terms_per_run") or 5)]
    targets = list(settings.get("targets") or [])
    known = {((t.get("ats") or "").lower(), (t.get("board") or t.get("workday_url") or "").lower()) for t in targets}
    added = []
    for term in pick:
        for b in await srcs.ats_xray(client, term, problems=run["notes"]):
            key = (b["ats"], b["board"].lower())
            if key in known:
                continue
            known.add(key)
            company = await board_company(client, b)
            if not company or rm.is_staffing(company):
                continue
            added.append({"company": company, "ats": b["ats"], "board": b["board"], **({"workday_url": b["workday_url"]} if b.get("workday_url") else {}),
                          **({"region": b["region"]} if b.get("region") else {}), "source": "ats_xray", "added_at": now_iso()})
    signals = []
    for t in added[:25]:
        try:
            signals += await rc.ATS[t["ats"]](client, t)
        except Exception:  # noqa: BLE001 - one board failing costs only its own postings
            run["counts"]["ats_xray_errors"] = run["counts"].get("ats_xray_errors", 0) + 1
    if added and settings.get("auto_targets", True):
        await rs.save_radar_settings({"targets": (targets + added)[-3000:]})
    run["counts"]["ats_boards_added"] = len(added)
    return signals


async def subdomain_scan(client: httpx.AsyncClient, settings: dict) -> list:
    """Hosts named for a competitor's product, and CNAMEs into its service, from passive DNS and certificate logs;
    a batch of account domains a day, least recently checked first."""
    owners = {}
    async for c in _db().companies.find({"domains.0": {"$exists": True}, "excluded_reason": None}, {"_id": 0, "name": 1, "domains": 1}):
        for d in c["domains"]:
            owners.setdefault(d, c["name"])
    snaps = {x["domain"]: x async for x in _db().radar_dns.find({"domain": {"$in": list(owners)}}, {"_id": 0, "domain": 1, "sub_scanned_at": 1, "records.CERT": 1})}
    batch = sorted(owners, key=lambda d: (snaps.get(d) or {}).get("sub_scanned_at") or "")[:int(settings.get("subdomain_batch") or 40)]
    sem = asyncio.Semaphore(4)

    async def one(d):
        async with sem:
            got = await srcs.subdomains(client, d)
            await _db().radar_dns.update_one({"domain": d}, {"$set": {"sub_scanned_at": now_iso(), "sub_hosts": len(got["hosts"])}}, upsert=True)
            seen = set(((snaps.get(d) or {}).get("records") or {}).get("CERT") or [])  # already fingerprinted by the certificate scan
            return rc.dns_signals_from(owners[d], d, {"SUB": [h for h in got["hosts"] if h not in seen][:3000], "PDNS": got["targets"][:500]}, None)
    out = []
    for got in await asyncio.gather(*(one(d) for d in batch)):
        out += got
    return out


# --- Enrichment: size (mid-market vs enterprise), HQ and domain ------------------------------------

async def enrich_company(client: httpx.AsyncClient, c: dict, *, bulk: bool = False, problems: Optional[list] = None) -> dict:
    """Fill what the account lacks from the firmographics chain; never overwrites a value a person set."""
    import radar_intel
    domain = (c.get("domains") or [None])[0]
    f = await srcs.firmographics(client, c["name"], domain, (c.get("hq") or {}).get("country"), bulk=bulk, problems=problems)
    sets: dict = {"firmo_checked_at": now_iso()}
    if f:
        sets["firmographics"] = {**{k: f.get(k) for k in ("employees", "revenue_usd", "revenue_band", "industry", "domain", "hq", "ids", "sources", "legal_name",
                                                          "linkedin_url", "public", "fields_from") if f.get(k) not in (None, "", {}, [])}, "at": now_iso()}
        for k in ("employees", "revenue_usd", "revenue_band", "industry"):
            if f.get(k) not in (None, "") and c.get(k) in (None, ""):
                sets[k] = f[k]
        hq = {k: v for k, v in (c.get("hq") or {}).items() if v}
        if f.get("hq") and not hq.get("country"):
            new = {k: v for k, v in f["hq"].items() if v}
            if new.get("city"):
                new.setdefault("location", new["city"])
            sets["hq"] = {**new, **hq}
    await _db().companies.update_one({"id": c["id"]}, {"$set": sets})
    if f.get("domain") and not c.get("domains") and radar_intel.domain_matches(f["domain"], c["name"]):
        await _db().companies.update_one({"id": c["id"]}, {"$addToSet": {"domains": f["domain"]}})
        await rs.link_by_domain({**c, "domains": [f["domain"]]}, [f["domain"]])
    rs.companies_changed()
    return sets


async def enrich(run: dict, per_run: Optional[int] = None) -> set:
    settings = await rs.radar_settings()
    n = int(settings.get("enrich_per_run", 40) if per_run is None else per_run)
    stale = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    touched: set = set()
    if not n:
        return touched
    cands = await _db().companies.find({"excluded_reason": None, "$or": [{"firmo_checked_at": None}, {"firmo_checked_at": {"$lt": stale}}]},
                                       {"_id": 0, "history": 0}).sort("score", -1).to_list(n)
    enriched = sized = 0
    async with httpx.AsyncClient(headers=srcs.UA, follow_redirects=True) as client:
        for c in cands:
            try:
                sets = await enrich_company(client, c, bulk=True, problems=run["notes"])
            except Exception as exc:  # noqa: BLE001 - one account must not stop the batch
                run["notes"].append(f"enrich: {type(exc).__name__}")
                continue
            touched.add(c["id"])
            enriched += 1 if sets.get("firmographics") else 0
            sized += 1 if sets.get("employees") or sets.get("revenue_usd") else 0
        if settings.get("auto_targets", True) and settings["collectors"].get("ats") and pv.keyless_on():
            run["counts"]["ats_boards_detected"] = await detect_boards(client, settings, limit=20)
    run["counts"].update({"enriched": enriched, "sized": sized, "enrich_checked": len(cands)})
    return touched


async def detect_boards(client: httpx.AsyncClient, settings: dict, limit: int = 20) -> int:
    """Public ATS boards for accounts with a website and no board yet (free probes), added to the daily ATS scan."""
    targets = list(settings.get("targets") or [])
    have = {(t.get("domain") or "").lower() for t in targets} | {rm.company_key(t.get("company") or "") for t in targets}
    found = []
    tried = 0
    async for c in _db().companies.find({"domains.0": {"$exists": True}, "excluded_reason": None, "board_checked_at": None}, {"_id": 0, "id": 1, "name": 1, "domains": 1}).sort("score", -1):
        if tried >= limit:
            break
        if c["domains"][0] in have or rm.company_key(c["name"]) in have:
            continue
        tried += 1
        b = await rc.detect_board(client, c["name"], c["domains"][0])
        await _db().companies.update_one({"id": c["id"]}, {"$set": {"board_checked_at": now_iso()}})
        if b:
            found.append({**b, "source": "detected", "added_at": now_iso()})
    if found:
        await rs.save_radar_settings({"targets": (targets + found)[-3000:]})
    return len(found)


async def research(run: dict, per_run: Optional[int] = None) -> dict:
    import radar_research
    settings = await rs.radar_settings()
    n = settings.get("research_per_run", 5) if per_run is None else per_run
    queued = 0
    if n:
        now = now_iso()
        candidates = await _db().companies.find({"excluded_reason": None, "status": {"$in": ["new", "researching", "ready"]}}, {"_id": 0}).sort("score", -1).to_list(500)
        # Target segments (enterprise and mid-market by default) first; an account of unknown size still qualifies.
        targets = set(settings.get("target_segments") or [])
        candidates.sort(key=lambda c: 1 if targets and c.get("segment") and c["segment"] not in targets else 0)
        for c in candidates:
            if queued >= n:
                break
            if rm.is_research_candidate(c, now, settings.get("refresh_days", 30)):
                await radar_research.queue(c)
                queued += 1
    done = await radar_research.run_queue(limit=max(n, 1))
    run["counts"].update({"research_queued": queued, **{f"research_{k}": v for k, v in done.items()}})
    return done


async def digest_html(owner: str, since: Optional[str] = None) -> tuple[str, int]:
    q = {"owner": owner}
    if since:
        q["$or"] = [{"latest_signal_at": {"$gte": since[:10]}}, {"created_at": {"$gte": since}}, {"research_at": {"$gte": since}}]
    rows = await _db().companies.find(q, {"_id": 0, "history": 0}).sort("score", -1).to_list(60)
    rows = [r for r in rows if r.get("status") not in ("not_a_fit", "excluded", "lost")]
    if not rows:
        return "", 0
    site = os.environ.get("SITE_URL", "").rstrip("/")
    items = []
    for c in rows[:40]:
        tag = "Ready for outreach" if c.get("status") == "ready" else ("Surging" if c.get("surge") else c.get("strength"))
        link = f"{site}/admin/radar?account={c['id']}" if site else ""
        name = f'<a href="{escape(link)}" style="color:#0a6b78">{escape(c["name"])}</a>' if link else escape(c["name"])
        comps = escape(", ".join(c.get("competitors") or [])[:120])
        items.append(f"<tr><td style='padding:6px 8px;border-bottom:1px solid #eee'><b>{name}</b><br><span style='color:#667;font-size:12px'>{comps}</span></td>"
                     f"<td style='padding:6px 8px;border-bottom:1px solid #eee'>{escape(str(tag))}</td><td style='padding:6px 8px;border-bottom:1px solid #eee;text-align:right'>{c.get('signal_count') or 0}</td></tr>")
    html = (f"<div style='font-family:Arial,sans-serif;font-size:14px;color:#14222c'><h2 style='margin:0 0 8px'>Your Radar: {len(rows)} account(s) with something new</h2>"
            "<p style='color:#556'>Rep rule: never name the competitor in the first message. Talk about the problem the signal points to.</p>"
            "<table style='border-collapse:collapse;width:100%'><tr><th align='left'>Account</th><th align='left'>Status</th><th align='right'>Signals</th></tr>"
            + "".join(items) + "</table></div>")
    return html, len(rows)


async def digest(run: dict) -> int:
    from emailer import email_provider, send_email
    if not email_provider():
        run["notes"].append("digest skipped: no email transport configured")
        return 0
    state = await _db().settings.find_one({"key": "radar_digest"}, {"_id": 0}) or {}
    last = state.get("last_sent") or {}
    sent = 0
    owners = [o for o in await _db().companies.distinct("owner") if o]
    for owner in owners:
        html, n = await digest_html(owner, last.get(owner.replace(".", "·")))
        if not n:
            continue
        try:
            await send_email(to=owner, subject=f"[Solix Radar] {n} account(s) with new signals", html=html)
            last[owner.replace(".", "·")] = now_iso()
            sent += 1
        except Exception as exc:  # noqa: BLE001
            run["notes"].append(f"digest failed: {type(exc).__name__}")
    await _db().settings.update_one({"key": "radar_digest"}, {"$set": {"last_sent": last, "updated_at": now_iso()}}, upsert=True)
    run["counts"]["digests_sent"] = sent
    return sent


async def daily(steps: Iterable[str] = ("collect", "enrich", "score", "research", "digest"), by: str = "system") -> dict:
    steps = tuple(steps)
    async with ops.run("radar_daily", by=by) as run:
        run["counts"]["steps"] = ",".join(steps)
        touched: set = set()
        if "collect" in steps:
            touched = await collect(run)
        if "enrich" in steps:
            touched |= await enrich(run)
        if "score" in steps:
            run["counts"]["recomputed"] = await (rs.recompute_many(touched) if "collect" in steps else rs.recompute_all())
            # Decay moves every score a little each day, so refresh the rest too.
            if "collect" in steps:
                run["counts"]["recomputed"] += await rs.recompute_many([c["id"] async for c in _db().companies.find({"id": {"$nin": list(touched)}}, {"_id": 0, "id": 1})])
            strong_new = await _db().companies.count_documents({"strength": "Strong", "created_at": {"$gte": run["started_at"]}})
            if strong_new:
                await ops.notify("pipeline", f"Radar: {strong_new} new strong account(s) today.", event="radar_new_strong")
        if "research" in steps:
            await research(run)
        if "digest" in steps:
            await digest(run)
    return run["counts"]


def main(argv: list) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cmd = argv[1] if len(argv) > 1 else "daily"
    if cmd == "sources":
        print(pv.report())
        return 0
    if not os.environ.get("MONGO_URL"):
        print("MONGO_URL is not set; nothing to do.")
        return 0
    steps = {"daily": ("collect", "enrich", "score", "research", "digest"), "collect": ("collect", "score"), "enrich": ("enrich", "score"), "score": ("score",),
             "research": ("research",), "digest": ("digest",)}.get(cmd)
    if not steps:
        print("usage: python radar_run.py [daily|collect|enrich|score|research|digest|sources]")
        return 2
    counts = asyncio.run(daily(steps, by="scheduler"))
    print("radar run finished:", {k: v for k, v in counts.items() if not isinstance(v, str) or k == "steps"})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
