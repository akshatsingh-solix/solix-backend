"""EchoLocate hardening from the engineering spec review: sends happen exactly once, webhook retries and
double labels don't double-count, bounce and complaint rates are per domain and need enough data, one channel
per person, and the first touch carries the Art. 14 notice. In-memory Mongo; invented people only."""
# flake8: noqa: F811
import asyncio
import os
import pytest


from test_radar_echolocate import _valid, run  # noqa: F401

import compliance
import database
import deliverability as dl
import ledger
import outbound

HOOK = {"X-Solix-Webhook-Secret": "hook-secret"}


@pytest.fixture(scope="module")
def client(isolated_app):
    return isolated_app[0]


def db():
    return database.db


def _enrollment(eid, cid, email, provider="dry_run", steps=2, **extra):
    run(db().contacts.update_one({"id": cid}, {"$setOnInsert": {"id": cid, "email": email, "name": "Hard Ening", "verified_at": "2099-01-01T00:00:00+00:00",
                                                               "email_status": "valid"}}, upsert=True))
    doc = {"id": eid, "contact_id": cid, "company_id": None, "sequence_id": "seq-h", "variant": "A", "step_index": 0, "status": "active", "provider": provider,
           "opener": "Opening line.", "steps": [{"subject": f"s{i}", "body": f"b{i}", "day": i * 3} for i in range(steps)], "history": [], **extra}
    run(db().enrollments.insert_one(dict(doc)))
    return doc


# --- exactly-once sending ------------------------------------------------------------------------------------------

def test_a_send_that_may_have_gone_out_is_never_repeated(client, monkeypatch):
    import emailer
    sent = []

    async def fake_send(**kw):
        sent.append(kw["to"])
        return "m1"

    monkeypatch.setattr(emailer, "send_email", fake_send)
    monkeypatch.setenv("OUTBOUND_LIVE_SENDING", "1")
    enr = _enrollment("h-crash", "h-crash-c", "crash@initech.example", provider="smtp")
    # A previous run wrote the row and died before recording the provider's answer.
    run(db().sends.insert_one({"id": "pre", "idem_key": dl.idem_key("h-crash", 1), "enrollment_id": "h-crash", "status": "sending", "step": 1}))
    s = {**run(compliance.outbound_settings()), "provider": "smtp"}
    assert run(outbound.send_step(dict(enr), s)) == "skipped"
    assert sent == [] and run(db().sends.count_documents({"enrollment_id": "h-crash"})) == 1


def test_a_refused_send_can_be_retried_once_on_the_same_row(client, monkeypatch):
    import emailer
    calls = []

    async def flaky(**kw):
        calls.append(kw["to"])
        if len(calls) == 1:
            raise RuntimeError("SMTP 451 try later")
        return "m2"

    monkeypatch.setattr(emailer, "send_email", flaky)
    monkeypatch.setenv("OUTBOUND_LIVE_SENDING", "1")
    enr = _enrollment("h-retry", "h-retry-c", "retry@initech.example", provider="smtp")
    s = {**run(compliance.outbound_settings()), "provider": "smtp"}
    with pytest.raises(RuntimeError):
        run(outbound.send_step(dict(enr), s))
    assert run(db().sends.find_one({"enrollment_id": "h-retry"}))["status"] == "failed"
    run(db().enrollments.update_one({"id": "h-retry"}, {"$set": {"claim_until": None}}))
    assert run(outbound.send_step(run(db().enrollments.find_one({"id": "h-retry"}, {"_id": 0})), s)) == "sent"
    rows = run(db().sends.find({"enrollment_id": "h-retry"}).to_list(5))
    assert len(rows) == 1 and rows[0]["status"] == "sent" and len(calls) == 2


def test_instantly_gets_a_stable_idempotency_key(client, monkeypatch):
    seen = []

    async def fake(method, path, **kw):
        seen.append((path, kw["json"]))
        return {"created_leads": [{"index": 0, "id": "inst-1", "email": kw["json"]["leads"][0]["email"]}]}

    monkeypatch.setattr(outbound, "instantly_request", fake)
    monkeypatch.setenv("OUTBOUND_LIVE_SENDING", "1")
    enr = _enrollment("h-inst", "h-inst-c", "inst@initech.example", provider="instantly")
    s = {**run(compliance.outbound_settings()), "provider": "instantly", "instantly_campaign_id": "camp-1"}
    assert run(outbound.send_step(dict(enr), s)) == "handed_off"
    path, body = seen[0]
    assert path == "/leads/add" and body["idempotency_key"] == dl.instantly_key("camp-1", "h-inst-c", enr) and body["skip_if_in_workspace"]
    assert set(body["leads"][0]["custom_variables"]) >= {"solix_contact", "solix_enrollment", "opener"}


# --- webhooks ------------------------------------------------------------------------------------------------------

def test_webhook_retries_and_sent_events_count_each_email_once(client):
    run(db().sends.delete_many({"enrollment_id": "h-inst"}))  # start from exactly one hand-off row
    run(db().sends.insert_one({"id": "h-ho", "enrollment_id": "h-inst", "contact_id": "h-inst-c", "status": "handed_off", "step": 1, "sent_at": "2099-01-01T00:00:00+00:00"}))
    ev = {"event_type": "email_sent", "lead_email": "inst@initech.example", "timestamp": "2026-11-24T10:00:00Z", "campaign_id": "camp-1", "step": 1,
          "email_account": "jo@mail.solix.example"}
    assert client.post("/api/outbound/webhooks/instantly", json=ev, headers=HOOK).json()["matched"]
    assert client.post("/api/outbound/webhooks/instantly", json=ev, headers=HOOK).json().get("duplicate")  # Instantly retried
    rows = run(db().sends.find({"enrollment_id": "h-inst"}).to_list(10))
    assert len(rows) == 1 and rows[0]["status"] == "delivered" and rows[0]["sending_domain"] == "mail.solix.example"
    step2 = {**ev, "step": 2, "timestamp": "2026-11-27T10:00:00Z"}
    client.post("/api/outbound/webhooks/instantly", json=step2, headers=HOOK)
    assert run(db().sends.count_documents({"enrollment_id": "h-inst"})) == 2


def test_a_bounce_marks_only_the_email_that_bounced(client):
    client.post("/api/outbound/webhooks/instantly", json={"event_type": "email_bounced", "lead_email": "inst@initech.example", "timestamp": "2026-11-28T10:00:00Z"}, headers=HOOK)
    rows = sorted(run(db().sends.find({"enrollment_id": "h-inst"}).to_list(10)), key=lambda r: r.get("step") or 0)
    assert [bool(r.get("bounced")) for r in rows] == [False, True]


def test_instantly_label_after_our_classification_doesnt_act_twice(client, monkeypatch):
    monkeypatch.setattr(outbound, "verify_email", _valid)
    run(db().contacts.insert_one({"id": "h-rep-c", "email": "rep@initech.example", "name": "Re Ply"}))
    client.post("/api/outbound/webhooks/instantly", json={"event_type": "reply_received", "lead_email": "rep@initech.example", "timestamp": "t1",
                                                          "reply_text": "Sounds interesting, tell me more."}, headers=HOOK)
    client.post("/api/outbound/webhooks/instantly", json={"event_type": "lead_interested", "lead_email": "rep@initech.example", "timestamp": "t2"}, headers=HOOK)
    logs = run(db().reply_log.find({"contact_id": "h-rep-c"}).to_list(5))
    assert len(logs) == 1 and logs[0]["label"] == "interested" and logs[0]["instantly_label"] == "interested"
    # ...but an opt-out label always takes effect.
    client.post("/api/outbound/webhooks/instantly", json={"event_type": "lead_unsubscribed", "lead_email": "rep@initech.example", "timestamp": "t3"}, headers=HOOK)
    assert run(compliance.is_suppressed("rep@initech.example"))


# --- deliverability ------------------------------------------------------------------------------------------------

def test_rates_need_enough_sends_and_are_per_domain(client):
    async def seed():
        await db().sends.delete_many({})
        for i in range(20):  # 1 bounce in 20 is 5%, but 20 sends say nothing yet
            await db().sends.insert_one({"id": f"d{i}", "sent_at": "2099-02-01T00:00:00+00:00", "status": "sent", "bounced": i == 0, "sending_domain": "a.example"})
        await compliance.save_outbound_settings({"paused": False, "enroll_hold": False})
        return await dl.health(await compliance.outbound_settings())
    t = run(seed())
    assert t["a.example"]["bounce_pct"] == 5.0 and not t["a.example"]["enough_data"] and t["a.example"]["state"] == "ok"

    async def more():
        for i in range(20, 80):
            await db().sends.insert_one({"id": f"d{i}", "sent_at": "2099-02-01T00:00:00+00:00", "status": "sent", "bounced": False, "sending_domain": "a.example"})
        await db().sends.insert_one({"id": "dz", "sent_at": "2099-02-01T00:00:00+00:00", "status": "sent", "sending_domain": "a.example"})
        return await dl.enforce(await compliance.outbound_settings())
    t = run(more())
    assert t["a.example"]["state"] == "warn"  # 1 of 81 = 1.2%: hold new enrollments, keep sending
    s = run(compliance.outbound_settings())
    assert s["enroll_hold"] and not s["paused"]
    r = client.post("/api/admin/outbound/enrollments", json={"contact_ids": ["h-rep-c"], "sequence_id": "nope"})
    assert r.status_code in (404, 422)
    run(compliance.save_outbound_settings({"enroll_hold": False}))


def test_complaints_pause_before_the_yahoo_line(client):
    async def seed():
        await db().sends.delete_many({})
        for i in range(300):
            await db().sends.insert_one({"id": f"c{i}", "sent_at": "2099-03-01T00:00:00+00:00", "status": "sent", "sending_domain": "b.example"})
        await db().suppression.insert_one({"email_hash": "x1", "reason": "complaint", "added_at": "2099-03-01T00:00:00+00:00", "sending_domain": "b.example"})
        await compliance.save_outbound_settings({"paused": False, "enroll_hold": False})
        return await dl.enforce(await compliance.outbound_settings())
    t = run(seed())
    assert t["b.example"]["complaint_pct"] == 0.333 and t["b.example"]["state"] == "pause"
    assert run(compliance.outbound_settings())["paused"]
    run(compliance.save_outbound_settings({"paused": False, "pause_reason": None, "enroll_hold": False}))
    run(db().sends.delete_many({}))
    run(db().suppression.delete_many({"email_hash": "x1"}))


# --- one channel per person ------------------------------------------------------------------------------------------

def test_ledger_keeps_one_channel_per_person():
    person = {"email": "dual@initech.example", "linkedin_url": "https://www.linkedin.com/in/Dual-Person/"}
    run(ledger.record(person, "linkedin", "dripify-1", by="rep@example.com"))
    clash = run(ledger.conflict({"email": "dual@initech.example"}, "email"))
    assert clash and "linkedin" in clash
    assert run(ledger.conflict({"linkedin_url": "linkedin.com/in/dual-person"}, "linkedin")) is None
    assert ledger.linkedin_id("https://uk.linkedin.com/in/Dual-Person?trk=x") == "dual-person"


# --- Art. 14 ------------------------------------------------------------------------------------------------------------

def test_first_touch_carries_the_art14_notice_with_the_source():
    s = run(compliance.outbound_settings())
    first = outbound.footer_html("x@initech.example", s, contact={"source": "seamless"}, first=True)
    later = outbound.footer_html("x@initech.example", s, contact={"source": "seamless"}, first=False)
    assert "Seamless.AI" in first and "legitimate interest" in first and "privacy notice" in first
    assert "Seamless.AI" not in later and "Unsubscribe" in later
