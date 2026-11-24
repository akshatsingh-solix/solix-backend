"""The shared outreach ledger: one row per person, written before any team or channel contacts them.

The engineering spec (G9) asks for a ledger every sender checks, keyed on the person, so nobody gets an email
and a LinkedIn message for the same campaign, and two teams don't work the same prospect. The person key is the
normalised LinkedIn public id when we have it (it survives job changes) and the hashed email otherwise; a row
records the channel stamped on first touch, every touch, and who made it.

Rules:
* the first channel to touch a person owns them for `channel_cooldown_days` (default 90): another channel is
  refused until the cooldown passes or a person reassigns them;
* touches recorded by people outside MarkHub (a rep's own campaign in Instantly, DigiPrise or LinkedIn) come in
  through POST /api/admin/outbound/ledger, so the automated side sees them too.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import database
from database import now_iso

CHANNELS = ("email", "linkedin", "phone", "event")
_LI = re.compile(r"linkedin\.com/(?:in|pub)/([^/?#]+)", re.I)


def _db():
    return database.db


def linkedin_id(url: Optional[str]) -> Optional[str]:
    m = _LI.search(url or "")
    return m.group(1).strip().lower().rstrip("/") if m else None


def keys_for(person: dict) -> List[str]:
    keys = []
    li = linkedin_id(person.get("linkedin_url"))
    if li:
        keys.append(f"li:{li}")
    email = (person.get("email") or "").strip().lower()
    if email:
        keys.append("em:" + hashlib.sha256(email.encode()).hexdigest()[:32])
    return keys


async def entry(person: dict) -> Optional[dict]:
    keys = keys_for(person)
    return await _db().outreach_ledger.find_one({"keys": {"$in": keys}}, {"_id": 0}) if keys else None


async def record(person: dict, channel: str, ref: Optional[str] = None, *, by: str = "system", note: Optional[str] = None) -> dict:
    keys = keys_for(person)
    if not keys:
        return {}
    now = now_iso()
    touch = {"at": now, "channel": channel, "ref": ref, "by": by, "note": note}
    existing = await entry(person)
    if existing:
        await _db().outreach_ledger.update_one({"id": existing["id"]}, {"$addToSet": {"keys": {"$each": keys}, "channels": channel},
                                                                       "$set": {"last_at": now, "last_channel": channel},
                                                                       "$push": {"touches": {"$each": [touch], "$slice": -50}}})
        return {**existing, "last_channel": channel}
    doc = {"id": keys[0], "keys": keys, "first_channel": channel, "channels": [channel], "first_at": now, "last_at": now, "last_channel": channel,
           "owner": by, "touches": [touch]}
    await _db().outreach_ledger.update_one({"id": doc["id"]}, {"$setOnInsert": doc}, upsert=True)
    return doc


async def conflict(person: dict, channel: str, ref: Optional[str] = None) -> Optional[str]:
    """Why this channel may not contact this person now, or None."""
    e = await entry(person)
    if not e or e.get("first_channel") == channel:
        return None
    if e.get("released_at"):
        return None
    import compliance
    days = int((await compliance.outbound_settings()).get("channel_cooldown_days") or 90)
    try:
        last = datetime.fromisoformat(e.get("last_at"))
    except (TypeError, ValueError):
        return None
    if last > datetime.now(timezone.utc) - timedelta(days=days):
        return f"already contacted by {e['first_channel']} on {e['first_at'][:10]}: one channel per person for {days} days"
    return None


async def release(person_key: str, by: str) -> bool:
    res = await _db().outreach_ledger.update_one({"id": person_key}, {"$set": {"released_at": now_iso(), "released_by": by}})
    return bool(res.matched_count)
