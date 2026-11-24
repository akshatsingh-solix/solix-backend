"""Deliverability guardrails and send idempotency for EchoLocate.

Two jobs:

1. Never send the same step twice. Every send gets an idempotency key (enrollment + step) and its row is
   written *before* the provider is called, under a unique index. A crash between "provider accepted" and
   "we recorded it" leaves a row in `sending`, which blocks a resend: an uncertain send is held for a person
   to check, never retried blind. Instantly also receives a deterministic `idempotency_key`.

2. Measure bounce and complaint rates honestly, and act in stages.
   Rates are computed per sending domain (and overall) over a rolling 7 days, only once there are enough sends
   to mean something (at 30 emails a day, one bounce is 3.3%). Thresholds follow the engineering spec
   (section 10), not the 3% outer edge:

   * hard bounce >= 1%:  hold new enrollments and re-verify queued addresses (warn)
   * hard bounce >= 2%:  pause all sending (the old 3% is an incident, not a target)
   * complaints  >= 0.1%: alert;  >= 0.3%: pause (Yahoo's published line; Gmail's target is under 0.1%)
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

import database
from database import now_iso

logger = logging.getLogger("solix.deliverability")

COUNTED = ("sent", "delivered", "handed_off")  # an email that left (simulated sends are never counted as delivered)
DEFAULTS = {"bounce_warn_pct": 1.0, "bounce_pause_pct": 2.0, "complaint_warn_pct": 0.1, "complaint_pause_pct": 0.3, "health_min_sends": 30}


def _db():
    return database.db


def idem_key(enrollment_id: str, step: int) -> str:
    return f"{enrollment_id}:{int(step)}"


def instantly_key(campaign: str, contact_id: str, enrollment: dict) -> str:
    """Deterministic per campaign, contact and approved content: a retry is recognised, an edited draft is not."""
    digest = hashlib.sha256("|".join([campaign, contact_id, enrollment["id"], enrollment.get("opener") or "",
                                      *[s.get("subject", "") + s.get("body", "") for s in enrollment.get("steps") or []]]).encode()).hexdigest()
    return f"solix-{digest[:48]}"


def domain_of(address: Optional[str]) -> Optional[str]:
    if not address or "@" not in address:
        return None
    return address.rsplit("@", 1)[1].strip().lower().rstrip(">")


async def claim_send(doc: dict) -> Optional[dict]:
    """Insert the send row before the provider call. Returns None when this step already has a row that isn't a
    clean failure (it went out, or might have): the caller must not send."""
    doc = {**doc, "idem_key": idem_key(doc["enrollment_id"], doc["step"]), "status": "sending", "created_at": now_iso()}
    try:
        await _db().sends.insert_one(dict(doc))
        return doc
    except Exception as e:  # noqa: BLE001 - duplicate key
        if "duplicate" not in str(e).lower() and "E11000" not in str(e):
            raise
    prior = await _db().sends.find_one({"idem_key": doc["idem_key"]}, {"_id": 0})
    if prior and prior.get("status") == "failed":
        # The provider refused it last time (nothing went out): this attempt may proceed, on the same row.
        await _db().sends.update_one({"idem_key": doc["idem_key"], "status": "failed"}, {"$set": {"status": "sending", "retry_at": now_iso(), "id": doc["id"]}})
        return {**prior, **doc}
    return None


async def finish_send(key: str, status: str, **fields) -> None:
    await _db().sends.update_one({"idem_key": key}, {"$set": {"status": status, "done_at": now_iso(), **fields}})


async def uncertain(minutes: int = 30) -> list:
    """Sends stuck in `sending`: the process died mid-call. Shown to a person; never retried automatically."""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    return await _db().sends.find({"status": "sending", "created_at": {"$lt": cutoff}}, {"_id": 0}).to_list(200)


async def health(settings: dict, days: int = 7) -> Dict[str, dict]:
    """Bounce and complaint rates per sending domain plus an `all` row, over a rolling window."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = await _db().sends.find({"sent_at": {"$gte": since}, "status": {"$in": list(COUNTED)}}, {"_id": 0, "sending_domain": 1, "bounced": 1, "email": 1}).to_list(200000)
    complaints = await _db().suppression.find({"reason": "complaint", "added_at": {"$gte": since}}, {"_id": 0, "domain": 1, "sending_domain": 1}).to_list(20000)
    cfg = {**DEFAULTS, **{k: settings[k] for k in DEFAULTS if settings.get(k) is not None}}
    out: Dict[str, dict] = {}
    for r in rows:
        for key in ("all", r.get("sending_domain") or "unknown"):
            d = out.setdefault(key, {"sends": 0, "bounces": 0, "complaints": 0})
            d["sends"] += 1
            d["bounces"] += bool(r.get("bounced"))
    for c in complaints:
        for key in ("all", c.get("sending_domain") or "unknown"):
            out.setdefault(key, {"sends": 0, "bounces": 0, "complaints": 0})["complaints"] += 1
    for key, d in out.items():
        enough = d["sends"] >= int(cfg["health_min_sends"])
        d["bounce_pct"] = round(100 * d["bounces"] / d["sends"], 2) if d["sends"] else None
        d["complaint_pct"] = round(100 * d["complaints"] / d["sends"], 3) if d["sends"] else None
        d["enough_data"] = enough
        state = "ok"
        if enough:
            if (d["bounce_pct"] or 0) >= cfg["bounce_pause_pct"] or (d["complaint_pct"] or 0) >= cfg["complaint_pause_pct"]:
                state = "pause"
            elif (d["bounce_pct"] or 0) >= cfg["bounce_warn_pct"] or (d["complaint_pct"] or 0) >= cfg["complaint_warn_pct"]:
                state = "warn"
        d["state"] = state
    return out


async def enforce(settings: dict) -> dict:
    """Act on health(): hold enrollments at 'warn', pause sending at 'pause'. Returns the health table."""
    import compliance
    import ops
    table = await health(settings)
    worst = [(k, v) for k, v in table.items() if v["state"] == "pause"]
    warn = [(k, v) for k, v in table.items() if v["state"] == "warn"]
    if worst and not settings.get("paused"):
        k, v = worst[0]
        why = (f"Bounce rate {v['bounce_pct']}% on {k} over 7 days ({v['sends']} sends)" if (v["bounce_pct"] or 0) >= (settings.get("bounce_pause_pct") or DEFAULTS["bounce_pause_pct"])
               else f"Complaint rate {v['complaint_pct']}% on {k} over 7 days ({v['sends']} sends)")
        await compliance.save_outbound_settings({"paused": True, "pause_reason": why + ": sending paused. Check list quality and domain health before resuming."})
        await ops.alert("bounce_pause", f"Outbound paused automatically. {why}.", severity="fail", dedupe_hours=1)
    if warn and not settings.get("enroll_hold"):
        k, v = warn[0]
        await compliance.save_outbound_settings({"enroll_hold": True, "enroll_hold_reason": f"{k}: bounce {v['bounce_pct']}%, complaints {v['complaint_pct']}% (7 days). "
                                                                                           "New enrollments are held; queued addresses older than 30 days are re-verified."})
        await ops.alert("bounce_warn", f"Deliverability warning on {k}: bounce {v['bounce_pct']}%, complaints {v['complaint_pct']}%. New enrollments held.", dedupe_hours=6)
    elif not warn and not worst and settings.get("enroll_hold") and settings.get("enroll_hold_auto", True):
        await compliance.save_outbound_settings({"enroll_hold": False, "enroll_hold_reason": None})
    return table
