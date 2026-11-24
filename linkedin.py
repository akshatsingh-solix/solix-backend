"""LinkedIn channel through Dripify's Open API (spec: dripify-export, campaign-sync, section 6).

How it works, and the limits that shape it:
* Dripify campaigns can't be created or switched on by API: a person builds and starts each campaign in the
  Dripify UI (on their own LinkedIn seat, with their agreement), then pastes its id into a MarkHub sequence
  whose channel is LinkedIn. MarkHub only feeds approved people into that running campaign.
* The same gates as email apply (C-11: LinkedIn messages are electronic mail under PECR): suppression, country
  policy, LIA, approval, the overlap ledger. A person needs a LinkedIn profile URL.
* Pacing per seat, well under Dripify's own ceiling (LinkedIn publishes none; community estimates put ~100
  invitations a week at the edge): default 15 a day and 80 a rolling week per seat.
* A restriction lands on a real person's account, so any 401/403/423/429 from Dripify pauses the LinkedIn channel
  at once and alerts; it never retries into a restricted seat.
* Replies arrive on the per-campaign webhook configured in Dripify's UI (POST /api/outbound/webhooks/dripify with
  the X-Solix-Webhook-Secret header). Conversations are fetched only for a lead whose webhook fired, because each
  read is live LinkedIn activity on the seat and the key allows 5,000 calls a day.

Endpoints and field names follow Dripify's published Open API v1.0.0 (api.dripify.com); confirm them on the live
account before switching the channel on (DRIPIFY_API_KEY + a campaign id on a sequence).
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

import database
from database import now_iso

BASE = os.environ.get("DRIPIFY_API_URL", "https://api.dripify.com/v1").rstrip("/")
SEAT_PROBLEM = {401, 403, 423, 429}


class SeatProblem(RuntimeError):
    pass


def ready() -> bool:
    return bool(os.environ.get("DRIPIFY_API_KEY"))


async def request(method: str, path: str, **kw) -> dict:
    key = os.environ.get("DRIPIFY_API_KEY")
    if not key:
        raise RuntimeError("DRIPIFY_API_KEY is not set")
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.request(method, f"{BASE}{path}", headers={"X-Api-Key": key}, **kw)
    if r.status_code in SEAT_PROBLEM:
        raise SeatProblem(f"Dripify {r.status_code}: {r.text[:200]}")
    if r.status_code >= 400:
        raise RuntimeError(f"Dripify {r.status_code}: {r.text[:200]}")
    return r.json() if r.content else {}


async def seat_room(campaign_id: str, settings: dict) -> Optional[str]:
    """Why this seat can't take another person today, or None."""
    db = database.db
    day = datetime.now(timezone.utc).date().isoformat()
    week = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    sent_day = await db.sends.count_documents({"channel": "linkedin", "campaign": campaign_id, "sent_at": {"$gte": day}})
    sent_week = await db.sends.count_documents({"channel": "linkedin", "campaign": campaign_id, "sent_at": {"$gte": week}})
    if sent_day >= int(settings.get("linkedin_daily_cap") or 15):
        return f"seat for campaign {campaign_id} has reached today's cap ({sent_day})"
    if sent_week >= int(settings.get("linkedin_weekly_cap") or 80):
        return f"seat for campaign {campaign_id} has reached the 7-day cap ({sent_week})"
    return None


async def push(contact: dict, enrollment: dict, campaign_id: str) -> str:
    """Add one approved person to a running Dripify campaign. Returns Dripify's lead id."""
    res = await request("POST", f"/campaigns/{campaign_id}/leads", json={"leads": [{
        "linkedinProfileUrl": contact["linkedin_url"], "firstName": contact.get("first_name"), "lastName": contact.get("last_name"),
        "company": enrollment.get("company_name"), "customFields": {"opener": enrollment.get("opener"), "solix_enrollment": enrollment["id"],
                                                                     "solix_contact": contact["id"]}}]})
    leads = res.get("leads") or res.get("data") or []
    if res.get("duplicateCount") and not leads:
        return "duplicate"
    return (leads[0].get("id") if leads else None) or res.get("id") or "dripify"


async def blacklist(linkedin_url: str) -> None:
    await request("POST", "/blacklist", json={"items": [linkedin_url]})


async def conversation(lead_id: str) -> str:
    res = await request("GET", f"/leads/{lead_id}/conversation")
    msgs = res.get("messages") or res.get("data") or []
    inbound = [m for m in msgs if (m.get("direction") or m.get("type") or "").lower() in ("inbound", "received", "incoming")]
    return (inbound[-1].get("text") or inbound[-1].get("body") or "") if inbound else ""


async def pause_channel(reason: str) -> None:
    import compliance
    import ops
    await compliance.save_outbound_settings({"linkedin_paused": True, "linkedin_pause_reason": reason})
    await ops.alert("linkedin_seat", f"LinkedIn channel paused: {reason}. Check the seat in Dripify before resuming (a restriction lands on a real person's account).",
                    severity="fail", dedupe_hours=2)


def suggest_channel(company: Optional[dict], contact: dict) -> str:
    """Tier routing from the spec: Priority (Strong) accounts go to LinkedIn first when there's a profile; others email."""
    if (company or {}).get("strength") == "Strong" and contact.get("linkedin_url") and ready():
        return "linkedin"
    return "email"
