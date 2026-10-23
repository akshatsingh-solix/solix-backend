"""The Radar's daily run: collect -> score -> research -> digest.

Runs three ways, all writing the same `ops_runs` heartbeat:
  * GitHub Actions on a schedule:  python radar_run.py daily
  * Admin > Radar > "Run now" (in the API process)
  * By hand:                       python radar_run.py collect|score|research|digest

Logs print counts only - never company or person names - because workflow logs
on a public repository are public.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from html import escape
from typing import Iterable, Optional

import httpx
from pymongo import UpdateOne

import database
import ops
import radar_collectors as rc
import radar_model as rm
import radar_service as rs
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
        if on.get("crustdata") and os.environ.get("CRUSTDATA_API_KEY"):
            signals += await guarded("crustdata", rc.crustdata(client, settings, budget))
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
        if on.get("tenders"):
            signals += await guarded("tenders", rc.ted_tenders(client, settings))
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
        ops.append(rs._signal_op(company, sig, run["id"]))
        touched.add(company["id"])
    await resolver.flush()
    counts["signals_new"] = await rs.save_signals(ops)
    counts["signals_seen"] = len(ops) - counts["signals_new"]
    counts["companies_touched"] = len(touched)
    return touched


async def research(run: dict, per_run: Optional[int] = None) -> dict:
    import radar_research
    settings = await rs.radar_settings()
    n = settings.get("research_per_run", 5) if per_run is None else per_run
    queued = 0
    if n:
        now = now_iso()
        candidates = await _db().companies.find({"excluded_reason": None, "status": {"$in": ["new", "researching", "ready"]}}, {"_id": 0}).sort("score", -1).to_list(500)
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


async def daily(steps: Iterable[str] = ("collect", "score", "research", "digest"), by: str = "system") -> dict:
    steps = tuple(steps)
    async with ops.run("radar_daily", by=by) as run:
        run["counts"]["steps"] = ",".join(steps)
        touched: set = set()
        if "collect" in steps:
            touched = await collect(run)
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
    if not os.environ.get("MONGO_URL"):
        print("MONGO_URL is not set; nothing to do.")
        return 0
    cmd = argv[1] if len(argv) > 1 else "daily"
    steps = {"daily": ("collect", "score", "research", "digest"), "collect": ("collect", "score"), "score": ("score",), "research": ("research",), "digest": ("digest",)}.get(cmd)
    if not steps:
        print("usage: python radar_run.py [daily|collect|score|research|digest]")
        return 2
    counts = asyncio.run(daily(steps, by="scheduler"))
    print("radar run finished:", {k: v for k, v in counts.items() if not isinstance(v, str) or k == "steps"})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
