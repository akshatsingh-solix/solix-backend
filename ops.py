"""Operational plumbing shared by the Radar and EchoLocate.

* Runs: every scheduled job writes an `ops_runs` record (start, finish, counts,
  error), which doubles as its heartbeat.
* Outbox: anything leaving the system - a Slack ping, a Digiprise push, an AE
  email - is queued in `ops_outbox` and delivered with retries, so a slow or
  broken receiver never blocks the pipeline and nothing is lost on a restart.
* Watchdog: catches the silent stall (a job that simply stops running), not
  just crashes, and raises one alert per problem per half day.
* Background loop: runs inside the API process while it is awake (the warm-up
  ping keeps it awake); `POST /api/ops/tick` lets an external scheduler drive
  the same work when it is not.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

import database
from auth import get_current_admin, require_roles
from database import now_iso

logger = logging.getLogger("solix.ops")
router = APIRouter(prefix="/api/admin/ops", tags=["ops"], dependencies=[Depends(get_current_admin)])
public = APIRouter(prefix="/api/ops", tags=["ops"])
admin_only = Depends(require_roles("admin"))

RETRY_MINUTES = [1, 5, 15, 60, 240, 720]
SLACK_CHANNELS = ("replies", "pipeline", "conflicts", "watchdog")


def _db():
    return database.db


def _ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


# --- Runs -------------------------------------------------------------------------

@asynccontextmanager
async def run(job: str, by: str = "system"):
    """`async with ops.run("radar_collect") as r: r["counts"]["new"] += 1`"""
    rec = {"id": str(uuid.uuid4()), "job": job, "by": by, "status": "running", "started_at": now_iso(), "counts": {}, "notes": []}
    await _db().ops_runs.insert_one(dict(rec))
    try:
        yield rec
    except Exception as exc:
        await _db().ops_runs.update_one({"id": rec["id"]}, {"$set": {"status": "failed", "finished_at": now_iso(), "counts": rec["counts"], "notes": rec["notes"][-50:], "error": str(exc)[:500]}})
        logger.exception("job %s failed", job)
        raise
    else:
        await _db().ops_runs.update_one({"id": rec["id"]}, {"$set": {"status": rec.get("status_override") or "ok", "finished_at": now_iso(), "counts": rec["counts"], "notes": rec["notes"][-50:]}})


async def last_run(job: str, status: Optional[str] = "ok") -> Optional[dict]:
    q = {"job": job}
    if status:
        q["status"] = status
    return await _db().ops_runs.find_one(q, {"_id": 0}, sort=[("started_at", -1)])


# --- Integrations settings --------------------------------------------------------

async def integrations() -> dict:
    doc = await _db().settings.find_one({"key": "integrations"}, {"_id": 0}) or {}
    slack = {c: (doc.get("slack") or {}).get(c) or os.environ.get(f"SLACK_WEBHOOK_{c.upper()}") for c in SLACK_CHANNELS}
    return {
        "slack": slack,
        "digiprise_url": doc.get("digiprise_url") or os.environ.get("DIGIPRISE_URL"),
        "digiprise_enabled": bool(doc.get("digiprise_enabled", bool(os.environ.get("DIGIPRISE_URL")))),
    }


# --- Outbox -----------------------------------------------------------------------

async def enqueue(target: str, event: str, payload: dict, key: Optional[str] = None) -> Optional[dict]:
    """Queue one outbound delivery. `key` makes it idempotent (same key = sent once)."""
    if key and await _db().ops_outbox.find_one({"key": key}, {"_id": 1}):
        return None
    doc = {"id": str(uuid.uuid4()), "target": target, "event": event, "payload": payload, "key": key, "status": "pending", "attempts": 0,
           "next_attempt_at": now_iso(), "created_at": now_iso()}
    await _db().ops_outbox.insert_one(dict(doc))
    return doc


async def notify(channel: str, text: str, *, event: str = "message", key: Optional[str] = None, data: Optional[dict] = None) -> None:
    """Slack message to one of the four channels (#replies, #pipeline, #conflicts, #watchdog), if configured."""
    await enqueue("slack", event, {"channel": channel, "text": text, "data": data or {}}, key)


async def digiprise(event: str, data: dict, key: Optional[str] = None) -> None:
    """One-way push to the reps' CRM view. Postgres-free: the event is the contract."""
    cfg = await integrations()
    if cfg["digiprise_enabled"] and cfg["digiprise_url"]:
        await enqueue("digiprise", event, data, key)


async def _deliver(client: httpx.AsyncClient, item: dict, cfg: dict) -> str:
    target, payload = item["target"], item["payload"]
    if target == "slack":
        url = cfg["slack"].get(payload.get("channel") or "pipeline")
        if not url:
            return "skipped"
        r = await client.post(url, json={"text": payload.get("text", "")}, timeout=20)
        r.raise_for_status()
        return "sent"
    if target == "digiprise":
        if not cfg["digiprise_url"]:
            return "skipped"
        headers = {"Content-Type": "application/json", "Idempotency-Key": item.get("key") or item["id"]}
        if os.environ.get("DIGIPRISE_TOKEN"):
            headers["Authorization"] = f"Bearer {os.environ['DIGIPRISE_TOKEN']}"
        r = await client.post(cfg["digiprise_url"], json={"event": item["event"], "data": payload, "id": item["id"], "sent_at": now_iso()}, headers=headers, timeout=30)
        r.raise_for_status()
        return "sent"
    if target == "email":
        from emailer import email_provider, send_email
        if not email_provider() or not payload.get("to"):
            return "skipped"
        await send_email(to=payload["to"], subject=payload.get("subject", "Solix"), html=payload.get("html", ""), reply_to=payload.get("reply_to"))
        return "sent"
    if target == "instantly_block":
        from outbound import instantly_block
        await instantly_block(payload["email"])
        return "sent"
    if target == "instantly_delete":
        from outbound import instantly_delete_lead
        await instantly_delete_lead(payload["email"])
        return "sent"
    return "skipped"


async def process_outbox(limit: int = 50) -> dict:
    cfg = await integrations()
    counts = {"sent": 0, "skipped": 0, "retry": 0, "failed": 0}
    items = await _db().ops_outbox.find({"status": "pending", "next_attempt_at": {"$lte": now_iso()}}, {"_id": 0}).sort("created_at", 1).to_list(limit)
    if not items:
        return counts
    async with httpx.AsyncClient() as client:
        for item in items:
            # Claim it first so overlapping runs (in-API loop, external tick, "Run now") deliver it once.
            lease = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
            if not await _db().ops_outbox.find_one_and_update(
                    {"id": item["id"], "status": "pending", "$or": [{"lease_until": None}, {"lease_until": {"$lt": now_iso()}}]}, {"$set": {"lease_until": lease}}):
                continue
            try:
                result = await _deliver(client, item, cfg)
                await _db().ops_outbox.update_one({"id": item["id"]}, {"$set": {"status": result, "done_at": now_iso(), "attempts": item["attempts"] + 1}})
                counts[result] = counts.get(result, 0) + 1
            except Exception as exc:  # noqa: BLE001 - any receiver failure is retried
                n = item["attempts"] + 1
                if n >= len(RETRY_MINUTES) + 1:
                    await _db().ops_outbox.update_one({"id": item["id"]}, {"$set": {"status": "failed", "attempts": n, "error": str(exc)[:300], "done_at": now_iso()}})
                    counts["failed"] += 1
                else:
                    nxt = (datetime.now(timezone.utc) + timedelta(minutes=RETRY_MINUTES[n - 1])).isoformat()
                    await _db().ops_outbox.update_one({"id": item["id"]}, {"$set": {"attempts": n, "error": str(exc)[:300], "next_attempt_at": nxt, "lease_until": None}})
                    counts["retry"] += 1
    return counts


# --- Alerts and watchdog ----------------------------------------------------------

async def alert(code: str, message: str, *, channel: str = "watchdog", severity: str = "warn", dedupe_hours: float = 12) -> bool:
    """Record an alert and ping Slack + the alert inbox, at most once per `dedupe_hours`."""
    if await _db().ops_alerts.find_one({"code": code, "created_at": {"$gte": _ago(dedupe_hours)}}, {"_id": 1}):
        return False
    await _db().ops_alerts.insert_one({"id": str(uuid.uuid4()), "code": code, "message": message, "severity": severity, "created_at": now_iso()})
    await notify(channel, f":rotating_light: {message}", event="alert")
    try:
        from emailer import get_alert_recipient
        to = await get_alert_recipient()
        if to:
            await enqueue("email", "alert", {"to": to, "subject": f"[Solix ops] {message[:90]}", "html": f"<p>{message}</p><p>Open Admin → Outbound → Health for details.</p>"})
    except Exception:  # noqa: BLE001
        logger.exception("alert email could not be queued")
    return True


async def watchdog() -> list:
    """Health checks; each failing check raises a deduplicated alert."""
    db = _db()
    checks = []

    async def check(code, ok, label, detail, severity="warn"):
        checks.append({"code": code, "ok": ok, "label": label, "detail": detail, "severity": "ok" if ok else severity})
        if not ok:
            await alert(code, f"{label}: {detail}", severity=severity)

    collect = await last_run("radar_daily", None)
    if collect:
        good = await last_run("radar_daily")
        fresh = bool(good and good["started_at"] >= _ago(26))
        await check("radar_stale", fresh, "Radar daily run", f"last successful run {good['started_at'][:16] if good else 'never'} UTC" + ("" if fresh else " (over 26 hours ago)"))
    else:
        checks.append({"code": "radar_stale", "ok": True, "label": "Radar daily run", "detail": "not scheduled yet", "severity": "ok"})

    out = await db.settings.find_one({"key": "outbound"}, {"_id": 0}) or {}
    if out.get("sending_enabled") and not out.get("paused"):
        tick = await last_run("outbound_tick")
        fresh = bool(tick and tick["started_at"] >= _ago(0.75))
        await check("outbound_stale", fresh, "Outbound sender", f"last tick {tick['started_at'][:16] if tick else 'never'} UTC" + ("" if fresh else " (sending is on but the sender has gone quiet)"), "fail")
    else:
        checks.append({"code": "outbound_stale", "ok": True, "label": "Outbound sender", "detail": "paused" if out.get("paused") else "sending is off", "severity": "ok"})
    if out.get("paused") and out.get("pause_reason"):
        await check("outbound_paused", False, "Outbound paused", out["pause_reason"], "fail")

    unrouted = await db.companies.count_documents({"strength": "Strong", "owner": {"$in": [None, ""]}, "created_at": {"$lte": _ago(24)}, "excluded_reason": None})
    await check("unassigned", unrouted == 0, "Unassigned strong accounts", f"{unrouted} strong account(s) without an owner for over 24h")
    conflicts = await db.companies.count_documents({"route_basis": "needs_routing", "strength": {"$in": ["Strong", "Medium"]}})
    await check("needs_routing", conflicts == 0, "Accounts needing routing", f"{conflicts} account(s) the territory rules couldn't place")
    failed = await db.ops_outbox.count_documents({"status": "failed", "done_at": {"$gte": _ago(24)}})
    await check("outbox_failed", failed == 0, "Deliveries", f"{failed} Slack/Digiprise/email deliveries failed in the last 24h")
    return checks


# --- Background loop ---------------------------------------------------------------

_loop_task: Optional[asyncio.Task] = None


async def tick(jobs: tuple = ("outbox", "outbound", "watchdog")) -> dict:
    out = {}
    if "outbox" in jobs:
        out["outbox"] = await process_outbox()
    if "outbound" in jobs:
        import outbound
        out["outbound"] = await outbound.tick()
    if "watchdog" in jobs:
        async with run("watchdog") as r:
            checks = await watchdog()
            r["counts"] = {"failing": sum(1 for c in checks if not c["ok"])}
        out["watchdog"] = r["counts"]
    return out


async def background_loop() -> None:
    """Outbox every minute, sender every 10 minutes, watchdog every 30 minutes."""
    n = 0
    await asyncio.sleep(20)
    while True:
        try:
            jobs = ["outbox"]
            if n % 10 == 0:
                jobs.append("outbound")
            if n % 30 == 0:
                jobs.append("watchdog")
            await tick(tuple(jobs))
        except Exception:  # noqa: BLE001
            logger.exception("ops loop iteration failed")
        n += 1
        await asyncio.sleep(60)


def start_background() -> None:
    global _loop_task
    if os.environ.get("OPS_LOOP", "1") != "0" and (_loop_task is None or _loop_task.done()):
        _loop_task = asyncio.create_task(background_loop())


# --- API --------------------------------------------------------------------------

@router.get("/health")
async def health():
    checks = await watchdog()
    runs = await _db().ops_runs.find({}, {"_id": 0, "notes": 0}).sort("started_at", -1).to_list(40)
    pending = await _db().ops_outbox.count_documents({"status": "pending"})
    failed = await _db().ops_outbox.count_documents({"status": "failed"})
    alerts = await _db().ops_alerts.find({}, {"_id": 0}).sort("created_at", -1).to_list(20)
    return {"checks": checks, "runs": runs, "outbox": {"pending": pending, "failed": failed}, "alerts": alerts, "integrations": await integrations()}


@router.get("/runs")
async def list_runs(job: Optional[str] = None, limit: int = Query(50, le=200)):
    q = {"job": job} if job else {}
    return await _db().ops_runs.find(q, {"_id": 0}).sort("started_at", -1).to_list(limit)


@router.get("/outbox")
async def list_outbox(status: Optional[str] = None, limit: int = Query(50, le=200)):
    q = {"status": status} if status else {}
    return await _db().ops_outbox.find(q, {"_id": 0}).sort("created_at", -1).to_list(limit)


@router.post("/outbox/{item_id}/retry", dependencies=[admin_only])
async def retry_outbox(item_id: str):
    res = await _db().ops_outbox.update_one({"id": item_id}, {"$set": {"status": "pending", "next_attempt_at": now_iso(), "attempts": 0}})
    if not res.matched_count:
        raise HTTPException(status_code=404, detail="Delivery not found")
    return await process_outbox()


class IntegrationsIn(BaseModel):
    slack: dict = Field(default_factory=dict)
    digiprise_url: Optional[str] = Field(default=None, max_length=500)
    digiprise_enabled: bool = False


@router.get("/integrations")
async def get_integrations():
    return {**await integrations(), "env": {
        "instantly": bool(os.environ.get("INSTANTLY_API_KEY")), "instantly_webhook_secret": bool(os.environ.get("INSTANTLY_WEBHOOK_SECRET")),
        "digiprise_token": bool(os.environ.get("DIGIPRISE_TOKEN")), "reacher": bool(os.environ.get("REACHER_URL")),
        "crustdata": bool(os.environ.get("CRUSTDATA_API_KEY")), "ops_token": bool(os.environ.get("OPS_TOKEN")),
    }}


@router.put("/integrations", dependencies=[admin_only])
async def put_integrations(body: IntegrationsIn):
    slack = {c: (body.slack.get(c) or "").strip() or None for c in SLACK_CHANNELS}
    for url in [u for u in slack.values() if u] + ([body.digiprise_url] if body.digiprise_url else []):
        if not url.startswith("https://"):
            raise HTTPException(status_code=422, detail="Webhook addresses must start with https://")
    await _db().settings.update_one({"key": "integrations"}, {"$set": {"slack": slack, "digiprise_url": body.digiprise_url, "digiprise_enabled": body.digiprise_enabled, "updated_at": now_iso()}}, upsert=True)
    return await get_integrations()


@router.post("/integrations/test", dependencies=[admin_only])
async def test_integrations():
    for c in SLACK_CHANNELS:
        await notify(c, f"Test message for #{c} from the Solix admin ({now_iso()[:16]} UTC).", event="test")
    await digiprise("test", {"message": "Test event from the Solix admin"}, None)
    return await process_outbox()


@router.post("/tick", dependencies=[admin_only])
async def manual_tick():
    return await tick()


@public.post("/tick")
async def external_tick(x_ops_token: Optional[str] = Header(default=None)):
    """For an external scheduler (GitHub Actions, cron-job.org) when the API sleeps."""
    expected = os.environ.get("OPS_TOKEN")
    if not expected or not x_ops_token or not hmac.compare_digest(expected, x_ops_token):
        raise HTTPException(status_code=401, detail="Invalid token")
    return await tick()
