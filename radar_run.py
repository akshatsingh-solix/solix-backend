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


async def sync_provider_accounts() -> None:
    """Once a run: take Hunter's own usage figures, so its allowance bar matches what Hunter has counted."""
    import os
    if os.environ.get("HUNTER_API_KEY"):
        try:
            async with httpx.AsyncClient() as client:
                await srcs.hunter_account(client)
        except Exception:  # noqa: BLE001 - the counter just stays on MarkHub's own count
            pass


COLLECT_SOURCES = ("ats", "ats_xray", "jobs_api", "crustdata", "visitors", "filings", "tenders", "public_sector", "news", "dns", "subdomains")


async def collect(run: dict, done: Optional[set] = None, touched: Optional[set] = None, on_saved=None) -> set:
    """Every signal source in turn; each source's signals are saved (new accounts created, signals upserted) as soon as
    that source finishes, then checkpointed through `on_saved(name)`.

    Saving per source matters on a small host: the run used to hold the whole day's signals in memory and write them
    only at the very end, so a restart anywhere in collect (Render's health check, a deploy, memory) threw away every
    signal and every new account, and the resumed run started collect from scratch and hit the same wall. Now a
    restart costs at most the source in progress, and a resumed run skips the sources already saved."""
    done = done if done is not None else set()
    touched = touched if touched is not None else set()
    await sync_provider_accounts()
    settings = await rs.radar_settings()
    on = settings["collectors"]
    budget = {"credits": 0.0, "cap": float(settings.get("credit_cap") or 0)}
    counts = run["counts"]
    vendors = settings.get("vendor_names") or ()
    resolver = await rs.Resolver.load()  # once per run; postings are resolved in memory and written in bulk per source

    async def save(name: str, signals: list) -> None:
        """Resolve the source's signals to accounts and write them; competitors and Solix itself are never accounts."""
        writes = []
        for sig in signals:
            await ops.breathe()
            if not sig.get("company"):
                continue
            if rm.is_vendor(sig["company"], [sig["domain"]] if sig.get("domain") else (), vendors):
                counts["vendor_signals_dropped"] = counts.get("vendor_signals_dropped", 0) + 1
                continue
            company = resolver.resolve(sig["company"], sig.get("domain"), sig.get("source") or "radar")
            if sig.get("public_sector") and not company.get("industry"):
                resolver.set(company, {"industry": "Public sector"})
            # A headcount from a job listing counts only when the listing's company domain is this account's
            # (never on a name match: "RS Ltd" with 11 staff must not size "RS Group plc").
            if sig.get("employees") and sig.get("domain") and rm.domain_of(sig["domain"]) in (company.get("domains") or []):
                cand = rm.size_candidate(sig["employees"], sig.get("source") or "jobs", "domain")
                resolver.set(company, {"size_candidates": rm.merge_candidates(company.get("size_candidates"), [cand])})
            writes.append(rs._signal_op(company, sig, run["id"]))
            touched.add(company["id"])
        created = len(resolver.created)
        await resolver.flush()
        new = await rs.save_signals(writes)
        counts["signals_new"] = counts.get("signals_new", 0) + new
        counts["signals_seen"] = counts.get("signals_seen", 0) + len(writes) - new
        counts["companies_new"] = counts.get("companies_new", 0) + created
        counts["companies_touched"] = len(touched)
        done.add(name)
        if on_saved:
            await on_saved(name)

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

        async def ats():
            out = []
            for t in settings.get("targets") or []:
                fn = rc.ATS.get((t.get("ats") or "").lower())
                if fn and (t.get("board") or t.get("workday_url")):
                    out += await guarded("ats", fn(client, t))
                    await ops.breathe()
            return out

        async def crustdata():
            got = await guarded("crustdata", rc.crustdata(client, settings, budget))
            await pv.succeeded("crustdata", int(budget["credits"] / 0.03))
            return got

        async def dns():
            domains = set(settings.get("dns_domains") or [])
            owners = {}
            async for c in _db().companies.find({"domains.0": {"$exists": True}}, {"_id": 0, "name": 1, "domains": 1}):
                for d in c["domains"]:
                    owners[d] = c["name"]
                    domains.add(d)
            domains = {d for d in domains if not rm.is_vendor(None, [d])}  # a vendor's own DNS fingerprints itself
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
                    await ops.breathe()
                    found = rc.dns_signals_from(owners.get(d) or d.split(".")[0].title(), d, records[0], snaps.get(d))
                    snap_ops.append(UpdateOne({"domain": d}, {"$set": {**{f"records.{t}": v for t, v in records[0].items()}, "scanned_at": now_iso()}}, upsert=True))
                    counts["dns_found"] = counts.get("dns_found", 0) + len(found)
                    counts["dns_changes"] = counts.get("dns_changes", 0) + sum(1 for x in found if x.get("action") in ("added", "removed"))
                    return found
            out = []
            for got in await asyncio.gather(*(one(d) for d in scan)):
                out += got
            if snap_ops:
                await _db().radar_dns.bulk_write(snap_ops, ordered=False)
            return out

        async def news():
            out = []
            hot = await _db().companies.find({"strength": "Strong", "excluded_reason": None}, {"_id": 0, "name": 1, "domains": 1}).sort("score", -1).to_list(15)
            for c in hot:
                out += await guarded("news", rc.news_signals(client, c["name"], (c.get("domains") or [None])[0]))
            return out

        async def visitors():
            import radar_visitors
            return await guarded("visitors", radar_visitors.signals(int(settings.get("visitor_days") or 30)))

        # Cheapest and richest in new accounts first (job boards and the job market), the slow DNS sweeps last.
        plan = {
            "ats": (on.get("ats"), ats),
            "ats_xray": (on.get("ats_xray") and pv.any_configured("web_search") and await pv.enabled("ats_xray"),
                         lambda: guarded("ats_xray", xray_boards(client, settings, run))),
            "jobs_api": (on.get("jobs_api") and pv.any_configured("jobs", list(srcs.JOB_ADAPTERS)), lambda: guarded("jobs_api", market_jobs(client, settings, run))),
            "crustdata": (on.get("crustdata") and os.environ.get("CRUSTDATA_API_KEY") and await pv.enabled("crustdata"), crustdata),
            "visitors": (on.get("visitors"), visitors),
            "filings": (on.get("filings"), lambda: guarded("filings", rc.edgar_signals(client))),
            "tenders": (on.get("tenders") and await pv.enabled("ted"), lambda: guarded("tenders", rc.ted_tenders(client, settings))),
            "public_sector": (on.get("public_sector"), lambda: guarded("public_sector", srcs.public_sector(client))),
            "news": (on.get("news"), news),
            "dns": (on.get("dns"), dns),
            "subdomains": (on.get("subdomains") and pv.any_configured("dns_history"), lambda: guarded("subdomains", subdomain_scan(client, settings))),
        }
        for name in COLLECT_SOURCES:
            wanted, produce = plan[name]
            if not wanted or name in done:
                continue
            await save(name, await produce())
    counts["credits_used"] = counts.get("credits_used", 0) + budget["credits"]
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
    """Postings across the job market that name a competitor or describe the problem (TheirStack, JSearch,
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
            if not company or rm.is_staffing(company) or rm.is_vendor(company, (), settings.get("vendor_names") or ()):
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
    """Fill what the account lacks from the firmographics chain. Headcounts are added as candidates with their source;
    the account shows the most trustworthy one (rm.size_view), so a better source can replace a weaker one, a doubtful
    one shows as unverified, and a value a person set is never replaced."""
    import radar_intel
    domain = (c.get("domains") or [None])[0]
    f = await srcs.firmographics(client, c["name"], domain, (c.get("hq") or {}).get("country"), bulk=bulk, problems=problems)
    sets: dict = {"firmo_checked_at": now_iso()}
    if f:
        sets["firmographics"] = {**{k: f.get(k) for k in ("employees", "revenue_usd", "revenue_band", "industry", "domain", "hq", "ids", "sources", "legal_name",
                                                          "linkedin_url", "public", "fields_from", "candidates") if f.get(k) not in (None, "", {}, [])}, "at": now_iso()}
        if f.get("candidates"):
            sets["size_candidates"] = rm.merge_candidates(c.get("size_candidates"), f["candidates"])
        if f.get("observations"):
            import data_quality as dq
            sets["facts"] = dq.merge_facts(c.get("facts"), f["observations"])
        for k in ("revenue_usd", "revenue_band", "industry"):
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
    due = {"excluded_reason": None, "$or": [{"firmo_checked_at": None}, {"firmo_checked_at": {"$lt": stale}}]}
    # Doubtful and unverified sizes first: a wrong size misfiles an account; a missing one only leaves it unsorted.
    cands = await _db().companies.find({**due, "size_status": {"$in": ["doubtful", "unverified"]}}, {"_id": 0, "history": 0}).sort("score", -1).to_list(n)
    if len(cands) < n:
        seen = {c["id"] for c in cands}
        more = await _db().companies.find(due, {"_id": 0, "history": 0}).sort("score", -1).to_list(n)
        cands += [c for c in more if c["id"] not in seen][:n - len(cands)]
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
    """Queue the day's deep dives, then run the queue (oldest first, so dives requested earlier aren't overtaken).

    Two lanes: `research_per_run` hot accounts (strong, two or more signal types, or surging) as before, plus
    `research_backlog_per_run` accounts that have never had a deep dive at all, best score first. Accounts that already
    have a dive waiting are skipped and not counted: they used to take the day's slots, so nothing new was queued."""
    import radar_research
    settings = await rs.radar_settings()
    n = int(settings.get("research_per_run", 5) if per_run is None else per_run)
    backlog = int(settings.get("research_backlog_per_run", 0) or 0) if per_run is None else 0
    await radar_research.requeue_stale()  # dives cut off by a restart go back in the queue before anything new is added
    pending = set(await _db().radar_research.distinct("company_id", {"status": {"$in": ["queued", "running"]}}))
    queued = queued_backlog = 0
    if n or backlog:
        now = now_iso()
        candidates = await _db().companies.find({"excluded_reason": None, "status": {"$in": ["new", "researching", "ready"]}}, {"_id": 0, "history": 0, "search_text": 0}
                                                ).sort("score", -1).to_list(500)
        # Target segments (enterprise and mid-market by default) first; an account of unknown size still qualifies.
        targets = set(settings.get("target_segments") or [])
        candidates.sort(key=lambda c: 1 if targets and c.get("segment") and c["segment"] not in targets else 0)
        candidates = [c for c in candidates if c["id"] not in pending and not rm.is_vendor(c.get("name"), c.get("domains") or (), settings.get("vendor_names") or ())]
        picked: set = set()
        for c in candidates:
            if queued >= n:
                break
            if rm.is_research_candidate(c, now, settings.get("refresh_days", 30)):
                await radar_research.queue(c)
                picked.add(c["id"])
                queued += 1
        for c in candidates:  # never researched: no brief, no overview, no signal search yet
            if queued_backlog >= backlog:
                break
            if c["id"] in picked or c.get("research_at") or not (c.get("signal_count") or 0):
                continue
            if targets and c.get("segment") and c["segment"] not in targets:
                continue
            await radar_research.queue(c)
            queued_backlog += 1
    done = await radar_research.run_queue(limit=max(n + backlog, 1))
    never = await _db().companies.count_documents({"excluded_reason": None, "research_at": None, "signal_count": {"$gt": 0}})
    run["counts"].update({"research_queued": queued, "research_backlog_queued": queued_backlog, "research_waiting": len(pending),
                          "never_researched": never, **{f"research_{k}": v for k, v in done.items()}})
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
            # Saved after each email, so a run resumed mid-digest never emails the same person twice.
            await _db().settings.update_one({"key": "radar_digest"}, {"$set": {f"last_sent.{owner.replace('.', '·')}": last[owner.replace('.', '·')]}}, upsert=True)
        except Exception as exc:  # noqa: BLE001
            run["notes"].append(f"digest failed: {type(exc).__name__}")
    await _db().settings.update_one({"key": "radar_digest"}, {"$set": {"last_sent": last, "updated_at": now_iso()}}, upsert=True)
    run["counts"]["digests_sent"] = sent
    return sent


async def _resume_point(run: dict) -> dict:
    """Progress of today's run if it was cut off: the completed steps and sources, the accounts touched, the accounts
    already scored. The cut-off record is marked 'interrupted' so the day's history shows what happened.

    Only a run started in the current daily slot is resumed. A run from the previous day that was cut off before
    today's slot is closed, not continued: inheriting its "collect done" would skip today's discovery entirely."""
    started = datetime.fromisoformat(run["started_at"])
    slot = ops.radar_slot(started)
    since = (slot or started - timedelta(hours=24)).isoformat()
    stale = {"job": "radar_daily", "status": "running", "id": {"$ne": run["id"]}}
    old = await _db().ops_runs.update_many({**stale, "started_at": {"$lt": since}}, {"$set": {"status": "interrupted", "finished_at": now_iso(), "resumed_by": None}})
    if old.modified_count:
        run["counts"]["closed_earlier_runs"] = old.modified_count
    prev = await _db().ops_runs.find_one({**stale, "started_at": {"$gte": since}}, {"_id": 0}, sort=[("started_at", -1)])
    if not prev:
        return {}
    await _db().ops_runs.update_one({"id": prev["id"]}, {"$set": {"status": "interrupted", "finished_at": now_iso(), "resumed_by": run["id"]}})
    return prev.get("progress") or {}


PULSE_SECONDS = 60


async def _pulse(run: dict) -> None:
    """A heartbeat every minute for as long as the run is alive, whatever step it is in. Without it a long step (the
    job market, a DNS sweep, a deep dive) went 15 minutes without a beat and read as cut off."""
    while True:
        await asyncio.sleep(PULSE_SECONDS)
        try:
            await ops.heartbeat(run)
        except Exception:  # noqa: BLE001 - a missed beat is retried next minute
            logger.info("radar heartbeat failed", exc_info=True)


SCORE_CHUNK = 50  # accounts scored between checkpoints


async def _score(run: dict, ids: list, done: set) -> int:
    """Score accounts in small chunks with a checkpoint after each, skipping those already scored today."""
    todo = [i for i in sorted(set(ids)) if i not in done]
    n = 0
    for i in range(0, len(todo), SCORE_CHUNK):
        chunk = todo[i:i + SCORE_CHUNK]
        n += await rs.recompute_many(chunk)
        done.update(chunk)
        await ops.heartbeat(run, scored=sorted(done))
    return n


async def daily(steps: Iterable[str] = ("collect", "enrich", "score", "research", "digest"), by: str = "system") -> dict:
    """The Radar's day: collect, enrich, score, research, digest.

    Resumable: progress is checkpointed after each step and every 50 accounts scored. If the instance restarts mid-run
    (a deploy, or the free tier's tiny CPU), the next attempt continues from the last checkpoint, so the run always
    completes and nothing is done twice (the digest, for instance, is never emailed twice)."""
    steps = tuple(steps)
    async with ops.run("radar_daily", by=by) as run:
        run["counts"]["steps"] = ",".join(steps)
        prior = await _resume_point(run)
        done_steps = set(prior.get("steps_done") or [])
        touched: set = set(prior.get("touched") or [])
        scored: set = set(prior.get("scored") or [])
        since_start = prior.get("started_at") or run["started_at"]  # "new today" counts from the first attempt
        if prior:
            run["counts"]["resumed_after"] = ",".join(sorted(done_steps)) or "start"
            run["progress"] = dict(prior)

        collected: set = set(prior.get("collect_done") or [])
        pulse = asyncio.create_task(_pulse(run))

        async def checkpoint(step: Optional[str] = None) -> None:
            if step:
                done_steps.add(step)
            await ops.heartbeat(run, steps_done=sorted(done_steps), collect_done=sorted(collected), touched=sorted(touched), started_at=since_start)

        async def source_saved(name: str) -> None:
            await checkpoint()

        try:
            await _daily_steps(run, steps, prior, done_steps, touched, scored, collected, since_start, checkpoint, source_saved)
        finally:
            pulse.cancel()
    return run["counts"]


async def _daily_steps(run, steps, prior, done_steps, touched, scored, collected, since_start, checkpoint, source_saved) -> None:
    """The steps of `daily`, each skipped when a resumed run already finished it."""
    if "migration" not in done_steps:
        repaired = await rs.ensure_size_migration()
        if repaired:
            run["counts"].update({f"migration_{k}": v for k, v in repaired.items()})
        await checkpoint("migration")
    if "collect" in steps and "collect" not in done_steps:
        touched |= await collect(run, collected, touched, on_saved=source_saved)
        await checkpoint("collect")
    if "enrich" in steps and "enrich" not in done_steps:
        touched |= await enrich(run)
        await checkpoint("enrich")
    if "score" in steps and "score" not in done_steps:
        if "collect" in steps:
            # The accounts with new signals first, then the rest: decay moves every score a little each day.
            n = await _score(run, list(touched), scored)
            n += await _score(run, [c["id"] async for c in _db().companies.find({"id": {"$nin": list(touched)}}, {"_id": 0, "id": 1})], scored)
        else:
            n = await _score(run, [c["id"] async for c in _db().companies.find({}, {"_id": 0, "id": 1})], scored)
        run["counts"]["recomputed"] = n + int(prior.get("recomputed") or 0)
        strong_new = await _db().companies.count_documents({"strength": "Strong", "created_at": {"$gte": since_start}})
        if strong_new:
            await ops.notify("pipeline", f"Radar: {strong_new} new strong account(s) today.", event="radar_new_strong",
                             key=f"radar_new_strong:{run['started_at'][:10]}")
        await checkpoint("score")
    if "research" in steps and "research" not in done_steps:
        await research(run)  # each deep dive is saved as it finishes, so a resume only picks up the rest
        await checkpoint("research")
    if "digest" in steps and "digest" not in done_steps:
        await digest(run)
        await checkpoint("digest")


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
