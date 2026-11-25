"""New accounts must keep arriving and deep dives must keep happening on a host that restarts mid-run.

* collect saves each source as it finishes: a restart loses at most the source in progress, never the whole day's
  signals and new accounts, and a resumed run skips the sources already saved;
* a run from the previous daily slot is closed, not resumed (resuming it skipped today's discovery);
* competitors and Solix itself are never prospects: their signals are dropped and existing vendor accounts are excluded;
* deep dives cut off mid-way go back in the queue, and accounts already waiting don't use up the day's slots."""
import os

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "solix_test")

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from mongomock_motor import AsyncMongoMockClient

import database
import ops
import radar_collectors as rc
import radar_model as rm
import radar_research
import radar_run
import radar_service as rs
import radar_sources as srcs


@pytest.fixture()
def db(monkeypatch):
    fake = AsyncMongoMockClient()["discovery_test"]
    monkeypatch.setattr(database, "db", fake)
    monkeypatch.setenv("RADAR_DAILY_UTC", "05:00")

    async def nothing():
        return None
    monkeypatch.setattr(radar_run, "sync_provider_accounts", nothing)
    return fake


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _only(*names):
    return {"collectors": {k: k in names for k in rs.DEFAULT_RADAR["collectors"]}}


def _filing(company, ref):
    return {"company": company, "type": "filing", "source": "edgar", "source_ref": ref, "title": "10-K mentions records retention"}


# --- collect: saved per source -----------------------------------------------------------------

def test_a_cut_off_collect_keeps_the_sources_it_finished(db, monkeypatch):
    run(rs.save_radar_settings(_only("filings", "public_sector")))

    async def filings(client):
        return [_filing("Acme Corp", "f1"), _filing("Smarsh Inc", "f2")]

    async def cut_off(client):
        raise asyncio.CancelledError()  # the instance restarts while this source is running
    monkeypatch.setattr(rc, "edgar_signals", filings)
    monkeypatch.setattr(srcs, "public_sector", cut_off)
    saved = []

    async def on_saved(name):
        saved.append(name)
    the_run = {"id": "r1", "counts": {}, "notes": []}
    done: set = set()
    with pytest.raises(asyncio.CancelledError):
        run(radar_run.collect(the_run, done, set(), on_saved=on_saved))
    names = [c["name"] for c in run(db.companies.find({}).to_list(None))]
    assert names == ["Acme Corp"]                               # the new account survived the cut-off...
    assert run(db.company_signals.count_documents({})) == 1     # ...with its signal
    assert saved == ["filings"] and done == {"filings"}         # and the source is checkpointed
    assert the_run["counts"]["vendor_signals_dropped"] == 1     # Smarsh is a competitor, not a prospect

    calls = {"filings": 0}

    async def filings_again(client):
        calls["filings"] += 1
        return []

    async def public_sector(client):
        return [{**_filing("Ministry of Records", "p1"), "type": "tender", "public_sector": True}]
    monkeypatch.setattr(rc, "edgar_signals", filings_again)
    monkeypatch.setattr(srcs, "public_sector", public_sector)
    resumed = {"id": "r2", "counts": {}, "notes": []}
    run(radar_run.collect(resumed, done, set()))
    assert calls["filings"] == 0                                # not read twice
    assert resumed["counts"]["companies_new"] == 1
    assert run(db.companies.count_documents({})) == 2


def test_resolver_flush_can_run_after_every_source(db):
    r = run(rs.Resolver.load())
    acme = r.resolve("Acme Corp", "acme.com")
    run(r.flush())
    again = r.resolve("Acme Corp")
    r.set(again, {"industry": "Retail"})
    run(r.flush())
    rows = run(db.companies.find({}, {"_id": 0}).to_list(None))
    assert len(rows) == 1 and rows[0]["id"] == acme["id"] and rows[0]["industry"] == "Retail"  # updated, not inserted twice


# --- resume stays within today's slot ------------------------------------------------------------

def test_yesterdays_cut_off_run_is_closed_not_resumed(db):
    now = datetime.now(timezone.utc)
    slot = ops.radar_slot(now)
    old_start = slot - timedelta(hours=2)  # in the previous slot, still inside the last 24 hours
    run(db.ops_runs.insert_one({"id": "old", "job": "radar_daily", "status": "running", "started_at": old_start.isoformat(),
                                "progress": {"steps_done": ["migration", "collect", "enrich"], "started_at": old_start.isoformat()}}))
    today = {"id": "new", "started_at": now.isoformat(), "counts": {}}
    assert run(radar_run._resume_point(today)) == {}              # today's discovery is not skipped
    assert run(db.ops_runs.find_one({"id": "old"}))["status"] == "interrupted"


# --- vendors ------------------------------------------------------------------------------------

@pytest.mark.parametrize("name,domains,vendor", [
    ("Smarsh Inc", [], True), ("OpenText Corporation", [], True), ("Open Text", [], True), ("Proofpoint, Inc.", [], True),
    ("Global Relay Communications Inc", [], True), ("Informatica LLC", [], True), ("Solix Technologies", [], True),
    ("Some Bank", ["careers.mimecast.com"], True),
    ("Veritas Capital", [], False), ("Proofpoint Bank of Ohio", [], False), ("Kiewit Corp", [], False), ("Acme", ["acme.com"], False),
])
def test_vendors_are_recognised_exactly(name, domains, vendor):
    assert rm.is_vendor(name, domains) is vendor


def test_a_vendor_posting_is_not_a_signal():
    assert rc.classify_posting("Smarsh", "Smarsh Support Engineer", "Support Smarsh archiving customers", url="https://x", source="t") == []
    assert rc.classify_posting("Acme Corp", "Records Engineer", "Migrate off Smarsh", url="https://x", source="t")


def test_an_existing_vendor_account_is_excluded_on_recompute(db):
    c = rs.new_company_doc("Proofpoint, Inc.", rm.company_key("Proofpoint, Inc."), "proofpoint.com", None, "radar")
    run(db.companies.insert_one(dict(c)))
    out = run(rs.recompute(c["id"]))
    assert out["excluded_reason"] == rs.VENDOR_REASON and out["status"] == "excluded"


# --- deep dives ---------------------------------------------------------------------------------

def _job(job_id, company_id, status, minutes_ago, attempts=1):
    return {"id": job_id, "company_id": company_id, "status": status, "attempts": attempts, "requested_by": "system",
            "created_at": (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago + 5)).isoformat(),
            "started_at": (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()}


def test_a_dive_cut_off_mid_way_goes_back_in_the_queue(db):
    run(db.radar_research.insert_many([_job("stuck", "c1", "running", 120), _job("live", "c2", "running", 5),
                                       _job("hopeless", "c3", "running", 120, attempts=radar_research.MAX_ATTEMPTS)]))
    assert run(radar_research.requeue_stale()) == 2
    status = {j["id"]: j["status"] for j in run(db.radar_research.find({}, {"_id": 0}).to_list(None))}
    assert status == {"stuck": "queued", "live": "running", "hopeless": "failed"}


def test_waiting_accounts_dont_use_up_the_days_slots(db, monkeypatch):
    hot = {"strength": "Strong", "signal_types": ["competitor_job", "dns"], "signal_count": 4, "excluded_reason": None, "status": "researching"}
    run(db.companies.insert_many([
        {"id": "waiting", "name": "Waiting Co", "score": 99, **hot},
        {"id": "smarsh", "name": "Smarsh", "score": 98, **hot, "status": "new"},
        {"id": "fresh", "name": "Fresh Co", "score": 50, **hot, "status": "new"},
        {"id": "never", "name": "Never Co", "score": 10, "strength": "Weak", "signal_types": ["problem_job"], "signal_count": 1,
         "excluded_reason": None, "status": "new"},
    ]))
    run(db.radar_research.insert_one(_job("w", "waiting", "queued", 0)))
    run(rs.save_radar_settings({"research_per_run": 1, "research_backlog_per_run": 1}))
    queued = []

    async def queue(company, by="system"):
        queued.append(company["id"])

    async def run_queue(limit=5, only=None):
        return {"done": 0}
    monkeypatch.setattr(radar_research, "queue", queue)
    monkeypatch.setattr(radar_research, "run_queue", run_queue)
    the_run = {"counts": {}, "notes": []}
    run(radar_run.research(the_run))
    assert queued == ["fresh", "never"]   # not the account already waiting, never the competitor; plus one never researched
    assert the_run["counts"]["research_queued"] == 1 and the_run["counts"]["research_backlog_queued"] == 1


def test_the_system_does_not_research_a_vendor(db, monkeypatch):
    c = {"id": "v", "name": "Smarsh", "excluded_reason": rs.VENDOR_REASON, "status": "excluded"}
    run(db.companies.insert_one(dict(c)))
    run(db.radar_research.insert_one({**_job("j", "v", "queued", 0), "started_at": None}))

    async def boom(client, company):
        raise AssertionError("a vendor was researched")

    async def recompute(cid):
        return {}
    monkeypatch.setattr(radar_research, "research", boom)
    monkeypatch.setattr(rs, "recompute", recompute)
    counts = run(radar_research.run_queue(limit=5))
    assert counts["skipped"] == 1 and run(db.radar_research.find_one({"id": "j"}))["status"] == "skipped"


# --- crt.sh: never parsed on the event loop, never read past the cap -----------------------------

def test_an_oversized_certificate_list_is_dropped_and_a_normal_one_parsed():
    import httpx
    big = b"[" + b'{"name_value":"a.acme.com"},' * 500 + b'{"name_value":"b.acme.com"}]'

    def handler(request):
        return httpx.Response(200, content=big)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            dropped = await rc._capped_get(client, "https://crt.sh/", max_bytes=1000)
            body = await rc._capped_get(client, "https://crt.sh/")
        return dropped, await asyncio.to_thread(rc._cert_names, body, "acme.com")
    dropped, names = run(go())
    assert dropped is None and names == ["a.acme.com", "b.acme.com"]
