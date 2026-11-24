"""Data retention for EchoLocate prospects (spec C-03 and C-15).

* Art. 14(3) notice deadline: a person whose details we collected from somewhere else must hear from us (the first
  email carries the Art. 14 notice) within a month. A sourced contact who hasn't been sent anything by day
  `notice_deadline_days` (default 30) is purged: we may not keep them silently.
* Retention: a prospect who never replied is purged `retention_days` (default 365, set in the LIA) after we first
  collected them. The clock starts at collection (`created_at`) and re-enrichment doesn't restart it.

A purge deletes the contact and their outbound trail, removes the email from send rows, and records only counts.
It does *not* add them to the suppression list: purging isn't an objection, and a hash would stop us ever
re-sourcing them lawfully. People who replied, became leads or asked to be contacted are never purged here.
Runs once a day from the outbound sender (outbound.tick). Ships in report-only mode (`retention_enforce` off):
it counts who *would* be purged so the admin can see the effect before switching it on in Compliance.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import database
from database import now_iso

INBOUND_SOURCES = {"inbound", "form", "chat", "event", "trial"}  # they came to us: no Art. 14 clock


def _db():
    return database.db


async def _purge(ids: list, why: str, enforce: bool = True) -> int:
    if not ids or not enforce:
        return len(ids)
    db = _db()
    import ledger
    people = [c async for c in db.contacts.find({"id": {"$in": ids}}, {"_id": 0, "email": 1, "linkedin_url": 1})]
    emails = [p["email"] for p in people if p.get("email")]
    keys = [k for p in people for k in ledger.keys_for(p)]
    await db.enrollments.delete_many({"contact_id": {"$in": ids}})
    await db.sends.update_many({"contact_id": {"$in": ids}}, {"$set": {"email": None, "purged": why}})
    if keys:
        await db.outreach_ledger.delete_many({"keys": {"$in": keys}})
    res = await db.contacts.delete_many({"id": {"$in": ids}})
    await db.retention_log.insert_one({"at": now_iso(), "why": why, "contacts": res.deleted_count, "emails": len(emails)})
    return res.deleted_count


async def run(settings: dict, now: datetime | None = None, *, record: bool = True) -> dict:
    now = now or datetime.now(timezone.utc)
    db = _db()
    notice_days = int(settings.get("notice_deadline_days") or 30)
    keep_days = int(settings.get("retention_days") or 365)
    notice_cut = (now - timedelta(days=notice_days)).isoformat()
    keep_cut = (now - timedelta(days=keep_days)).isoformat()
    replied = set(await db.reply_log.distinct("contact_id"))
    protected = {"$or": [{"lead_id": {"$ne": None}}, {"source": {"$in": list(INBOUND_SOURCES)}}]}

    silent = [c["id"] async for c in db.contacts.find({"created_at": {"$lt": notice_cut}, "notice_sent_at": None, "id": {"$nin": list(replied)}, "$nor": [protected]},
                                                      {"_id": 0, "id": 1})]
    # Contacts waiting in an approved sequence are about to get the notice: give them a few more days, never forever.
    pending = set(await db.enrollments.distinct("contact_id", {"status": {"$in": ["scheduled", "pending_approval"]}, "created_at": {"$gte": notice_cut}}))
    silent = [i for i in silent if i not in pending]
    stale = [c["id"] async for c in db.contacts.find({"created_at": {"$lt": keep_cut}, "id": {"$nin": list(replied)}, "$nor": [protected]}, {"_id": 0, "id": 1})]
    enforce = bool(settings.get("retention_enforce"))
    out = {"notice_deadline": await _purge(silent, f"no Art. 14 notice within {notice_days} days", enforce),
           "retention": await _purge([i for i in stale if i not in set(silent)], f"no reply within the {keep_days}-day retention period", enforce),
           "enforced": enforce}
    if record:
        await db.settings.update_one({"key": "retention"}, {"$set": {"last_run": now.isoformat(), **out}}, upsert=True)
    return out


async def run_if_due(settings: dict) -> dict | None:
    state = await _db().settings.find_one({"key": "retention"}, {"_id": 0}) or {}
    if (state.get("last_run") or "")[:10] == datetime.now(timezone.utc).date().isoformat():
        return None
    return await run(settings)
