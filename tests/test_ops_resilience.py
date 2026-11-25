"""Background jobs must not take the web server down on a 0.1-CPU instance (Render free, 25 Nov 2026): a Radar run
cut off by a restart is retried at most twice a day, and long loops give the event loop room to answer health checks."""
import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest
from mongomock_motor import AsyncMongoMockClient

import database
import ops


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


def test_cut_off_run_is_retried_once_then_alerts(db):
    now = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)
    stale = (now - timedelta(hours=4)).isoformat()
    run(db.ops_runs.insert_one({"job": "radar_daily", "status": "running", "started_at": stale}))
    assert run(ops.radar_due(now)) is True          # first cut-off: retry
    run(db.ops_runs.insert_one({"job": "radar_daily", "status": "running", "started_at": (now - timedelta(hours=3, minutes=30)).isoformat()}))
    assert run(ops.radar_due(now)) is False         # cut off again: stop, and say so
    assert db.alerts == ["radar_cut_off"]


def test_a_finished_or_fresh_run_is_not_repeated(db):
    now = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)
    run(db.ops_runs.insert_one({"job": "radar_daily", "status": "running", "started_at": (now - timedelta(minutes=20)).isoformat()}))
    assert run(ops.radar_due(now)) is False


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
    assert run(main()) < 0.2  # answered within ~one slice, not after the whole job
