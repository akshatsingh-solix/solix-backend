"""Background jobs on a 0.1-CPU instance (Render free, 25 Nov 2026): the daily Radar run must always complete. If the
instance restarts mid-run, the run is resumed from its last checkpoint (never skipped, never started over, nothing
done twice), and long loops give the event loop room to answer health checks."""
import os

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "solix_test")

import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest
from mongomock_motor import AsyncMongoMockClient

import database
import ops
import radar_run
import radar_service as rs


@pytest.fixture()
def db(monkeypatch):
    fake = AsyncMongoMockClient()["ops_test"]
    monkeypatch.setattr(database, "db", fake)
    monkeypatch.setenv("RADAR_DAILY_UTC", "05:00")
    alerts = []

    async def fake_alert(code, message, **kw):
        alerts.append(code)
        return True
    monkeypatch.setattr(ops, "alert", fake_alert)
    fake.alerts = alerts
    return fake


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


NOW = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)


def _running(db, minutes_since_heartbeat, started_hours_ago=4):
    run(db.ops_runs.insert_one({"id": f"r{minutes_since_heartbeat}-{started_hours_ago}", "job": "radar_daily", "status": "running",
                                "started_at": (NOW - timedelta(hours=started_hours_ago)).isoformat(),
                                "heartbeat_at": (NOW - timedelta(minutes=minutes_since_heartbeat)).isoformat()}))


def test_a_live_run_is_left_alone(db):
    _running(db, minutes_since_heartbeat=2)  # started hours ago but still beating: a long run, not a dead one
    assert run(ops.radar_due(NOW)) is False


def test_a_silent_run_is_resumed_however_many_times_it_takes(db):
    for i in range(5):
        _running(db, minutes_since_heartbeat=20 + i, started_hours_ago=4 + i)
        assert run(ops.radar_due(NOW)) is True  # never given up on
    assert "radar_interrupted" in db.alerts      # ...but someone is told it keeps getting cut off


def test_a_finished_run_is_not_repeated(db):
    run(db.ops_runs.insert_one({"job": "radar_daily", "status": "ok", "started_at": (NOW - timedelta(hours=6)).isoformat()}))
    assert run(ops.radar_due(NOW)) is False


def test_resume_continues_from_the_checkpoint(db, monkeypatch):
    """A run cut off after collecting and scoring some accounts: the next attempt skips what's done and finishes."""
    calls = {"collect": 0, "enrich": 0, "scored": [], "research": 0, "digest": 0}

    async def collect(r):
        calls["collect"] += 1
        return {"c1", "c2"}

    async def enrich(r):
        calls["enrich"] += 1
        return set()

    async def recompute_many(ids):
        calls["scored"] += list(ids)
        return len(list(ids))

    async def research(r):
        calls["research"] += 1

    async def digest(r):
        calls["digest"] += 1

    async def nothing():
        return {}
    monkeypatch.setattr(radar_run, "collect", collect)
    monkeypatch.setattr(radar_run, "enrich", enrich)
    monkeypatch.setattr(radar_run, "research", research)
    monkeypatch.setattr(radar_run, "digest", digest)
    monkeypatch.setattr(rs, "recompute_many", recompute_many)
    monkeypatch.setattr(rs, "ensure_size_migration", nothing)
    run(db.companies.insert_many([{"id": f"c{i}"} for i in range(1, 6)]))
    now = datetime.now(timezone.utc)
    started = now - (now - ops.radar_slot(now)) / 2  # earlier today, inside the current daily slot
    run(db.ops_runs.insert_one({"id": "cut", "job": "radar_daily", "status": "running", "started_at": started.isoformat(),
                                "heartbeat_at": (started + timedelta(minutes=5)).isoformat(),
                                "progress": {"steps_done": ["collect", "enrich", "migration"], "touched": ["c1", "c2"], "scored": ["c1", "c2", "c3"],
                                             "started_at": started.isoformat()}}))
    counts = run(radar_run.daily(by="schedule"))
    assert calls["collect"] == 0 and calls["enrich"] == 0        # not redone
    assert sorted(calls["scored"]) == ["c4", "c5"]                # only the accounts not yet scored
    assert calls["research"] == 1 and calls["digest"] == 1        # and the run completes
    assert counts["resumed_after"] == "collect,enrich,migration"
    assert run(db.ops_runs.find_one({"id": "cut"}))["status"] == "interrupted"
    final = run(db.ops_runs.find_one({"job": "radar_daily", "status": "ok"}))
    assert set(final["progress"]["steps_done"]) >= {"collect", "enrich", "score", "research", "digest"}


def test_breathe_lets_requests_in_during_a_long_loop():
    served = []

    async def request():
        await asyncio.sleep(0)
        served.append(time.monotonic())

    async def job():
        end = time.monotonic() + 0.4
        while time.monotonic() < end:  # busy work, as when scoring hundreds of accounts
            sum(range(2000))
            await ops.breathe(every_ms=50)

    async def main():
        t = asyncio.create_task(job())
        await asyncio.sleep(0)
        r = asyncio.create_task(request())
        start = time.monotonic()
        await r
        waited = served[0] - start
        await t
        return waited
    assert run(main()) < 0.2  # answered within about one slice, not after the whole job
