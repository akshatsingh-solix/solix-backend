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


def test_a_bounce_in_a_timestamp_tie_goes_to_the_later_step(client):
    # Two sends recorded in the same clock tick: the later step is the email that was sent last.
    same = "2026-11-28T12:00:00+00:00"
    base = {"enrollment_id": "h-tie", "contact_id": "h-inst-c", "status": "delivered", "sent_at": same, "provider": "instantly"}
    run(db().sends.delete_many({"enrollment_id": "h-tie"}))
    run(db().sends.update_many({"contact_id": "h-inst-c"}, {"$set": {"sent_at": "2000-01-01T00:00:00+00:00"}}))  # older sends stay out of it
    run(db().sends.insert_many([{**base, "id": "h-tie-2", "step": 2}, {**base, "id": "h-tie-1", "step": 1}]))  # inserted out of order on purpose
    client.post("/api/outbound/webhooks/instantly", json={"event_type": "email_bounced", "lead_email": "inst@initech.example", "timestamp": "2026-11-29T10:00:00Z"}, headers=HOOK)
    rows = sorted(run(db().sends.find({"enrollment_id": "h-tie"}).to_list(10)), key=lambda r: r["step"])
    assert [bool(r.get("bounced")) for r in rows] == [False, True]
    run(db().sends.delete_many({"enrollment_id": "h-tie"}))


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


# --- country policy engine -------------------------------------------------------------------------------------------

import country_policy as cp  # noqa: E402


@pytest.mark.parametrize("country,title,email,ok", [
    ("United States", "Chief Information Officer", "cio@acme.example", True),
    ("France", "Chief Information Officer", "dsi@acme.example", True),          # role-relevant: allowed
    ("France", "Head of Catering", "chef@acme.example", False),                 # not role-relevant: blocked
    ("Netherlands", "Chief Information Officer", "cio@acme.example", False),    # opt-in country
    ("Norway", "Chief Information Officer", "cio@acme.example", False),         # named address
    ("Norway", "Chief Information Officer", "it@acme.example", True),           # generic address
    ("Portugal", "Chief Information Officer", "cio@acme.example", False),       # EU, not reviewed: strictest
    (None, "Chief Information Officer", "cio@acme.example", False),             # unknown country: strictest
])
def test_country_policy_engine(country, title, email, ok):
    contact = {"email": email, "title": title, "persona": outbound.persona_of(title)}
    reasons, _, _ = cp.evaluate(contact, {"name": "Acme Holdings"}, {country} if country else set(), {})
    assert (not reasons) is ok, reasons


def test_uk_needs_a_corporate_subscriber_and_consent_unlocks_opt_in():
    c = {"email": "cio@shop.example", "title": "Chief Information Officer", "persona": "target"}
    reasons, _, _ = cp.evaluate(c, {"name": "Jo's Shop"}, {"United Kingdom"}, {})
    assert any("entity type" in r for r in reasons)
    reasons, _, _ = cp.evaluate(c, {"name": "Jo's Shop", "entity_type": "company"}, {"United Kingdom"}, {})
    assert not reasons
    nl = {**c, "consent": {"at": "2026-11-01", "proof": "https://forms.example/123", "wording": "Yes, email me"}}
    assert not cp.evaluate(nl, {}, {"Netherlands"}, {})[0]


def test_admin_override_changes_a_country_row():
    reasons, _, decision = cp.evaluate({"email": "a@b.example", "title": "CIO", "persona": "target"}, {}, {"Spain"},
                                       {"country_policies": {"Spain": {"policy": "ALLOW_OPT_OUT", "basis": "counsel confirmed"}}})
    assert not reasons and decision["countries"][0]["basis"] == "counsel confirmed"


# --- grounding and approval ----------------------------------------------------------------------------------------

import grounding  # noqa: E402


def test_grounding_rejects_invented_figures_names_and_claims():
    pro = ["ECM Platform Engineer: Administer OpenText Documentum D2 and xPlore.", "Acme is consolidating content platforms."]
    sol = ["Application Retirement keeps retired data searchable; customers cut run cost by up to 70%."]
    ok = grounding.check("Acme is consolidating its content platforms. Solix retires the old ones and cuts run cost by up to 70%.",
                         prospect_evidence=pro, solix_evidence=sol, company="Acme Mutual")
    assert ok["ok"], ok
    wrong_number = grounding.check("Solix cuts run cost by 90%.", prospect_evidence=pro, solix_evidence=sol, company="Acme Mutual")
    invented = grounding.check("Acme is moving to SharePoint Online this quarter.", prospect_evidence=pro, solix_evidence=sol, company="Acme Mutual")
    assert not wrong_number["ok"] and "90%" in wrong_number["problems"][0]
    assert not invented["ok"] and "SharePoint Online" in invented["problems"][0]


def test_an_edit_with_a_wrong_figure_blocks_approval(client):
    run(db().enrollments.insert_one({"id": "h-ground", "contact_id": "h-rep-c", "status": "pending_approval", "opener": "Acme is consolidating content platforms.",
                                     "steps": [{"day": 0, "subject": "s", "body": "Hi"}], "history": [], "company_name": "Acme Mutual",
                                     "evidence": {"prospect": ["Acme is consolidating content platforms."], "solix": ["Solix cuts run cost by up to 70%."]},
                                     "grounding": {"ok": True, "problems": []}}))
    r = client.patch("/api/admin/outbound/enrollments/h-ground", json={"action": "edit", "opener": "Acme is consolidating content platforms; Solix saved peers 95%."})
    assert r.status_code == 200 and r.json()["grounding"]["ok"] is False
    a = client.patch("/api/admin/outbound/enrollments/h-ground", json={"action": "approve"})
    assert a.status_code == 422


# --- reply classification and the review lane ------------------------------------------------------------------------

@pytest.mark.parametrize("text,label,review", [
    ("Interested in learning more, but please remove me from this mailing list.", "unsubscribe", False),
    ("Sounds interesting, but not right now, maybe next year.", None, True),
    ("I'm out of the office until Monday. For anything urgent, reach out to Sam Lee at sam@initech.example", None, True),
    ("Tell me more, sounds timely.", "interested", False),
])
def test_rules_first_classifier_and_review_triggers(text, label, review):
    out = run(outbound.classify(text))
    if label:
        assert out["label"] == label
    assert out["needs_review"] is review


def test_a_held_reply_pauses_then_a_person_resolves_it(client):
    run(db().contacts.insert_one({"id": "h-mix-c", "email": "mix@initech.example", "name": "Mix Ed", "verified_at": "2099-01-01T00:00:00+00:00"}))
    run(db().enrollments.insert_one({"id": "h-mix-e", "contact_id": "h-mix-c", "status": "active", "sequence_id": "s", "variant": "A", "step_index": 1,
                                     "steps": [{"day": 0, "subject": "a", "body": "b"}, {"day": 3, "subject": "c", "body": "d"}], "history": []}))
    rep = client.post("/api/admin/outbound/replies", json={"contact_id": "h-mix-c", "text": "Sounds interesting, but not right now."}).json()
    assert rep["label"] == "needs_review"
    assert run(db().enrollments.find_one({"id": "h-mix-e"}))["status"] == "paused"
    q = client.get("/api/admin/outbound/review").json()
    assert any(i["id"] == rep["id"] for i in q["items"])
    done = client.post(f"/api/admin/outbound/replies/{rep['id']}/resolve", json={"label": "objection"}).json()
    assert done["label"] == "objection"
    assert run(db().reply_log.find_one({"id": rep["id"]}))["review"]["status"] == "resolved"
    assert not any(i["id"] == rep["id"] for i in client.get("/api/admin/outbound/review").json()["items"])


# --- retention -------------------------------------------------------------------------------------------------------

import retention  # noqa: E402


def test_retention_reports_first_and_only_purges_when_switched_on(client):
    old = "2020-01-01T00:00:00+00:00"
    run(db().contacts.insert_many([{"id": "r-silent", "email": "silent@ret.example", "created_at": old, "source": "deep_dive"},
                                   {"id": "r-lead", "email": "lead@ret.example", "created_at": old, "source": "deep_dive", "lead_id": "L1"},
                                   {"id": "r-inbound", "email": "in@ret.example", "created_at": old, "source": "form"}]))
    s = run(compliance.outbound_settings())
    report = run(retention.run({**s, "retention_enforce": False}, record=False))
    assert report["notice_deadline"] >= 1 and not report["enforced"]
    assert run(db().contacts.find_one({"id": "r-silent"}))  # nothing deleted in report mode
    run(retention.run({**s, "retention_enforce": True}))
    assert run(db().contacts.find_one({"id": "r-silent"})) is None
    assert run(db().contacts.find_one({"id": "r-lead"})) and run(db().contacts.find_one({"id": "r-inbound"}))  # leads and inbound are never purged
    assert not run(compliance.is_suppressed("silent@ret.example"))  # a purge is not an objection


# --- A/B ---------------------------------------------------------------------------------------------------------------

def test_ab_needs_a_real_sample_and_a_significant_difference():
    assert 1100 < outbound.ab_sample_size(2.0, 2.0) < 1200
    assert outbound.ab_significant(1200, 24, 1200, 48) and not outbound.ab_significant(1200, 24, 1200, 30)


# --- LinkedIn through Dripify ------------------------------------------------------------------------------------------

import linkedin  # noqa: E402


def _li_enrollment(eid, cid, url):
    run(db().contacts.insert_one({"id": cid, "email": f"{cid}@initech.example", "name": "Link Din", "first_name": "Link", "linkedin_url": url}))
    run(db().enrollments.insert_one({"id": eid, "contact_id": cid, "company_id": None, "sequence_id": "li-seq", "variant": "A", "step_index": 0, "status": "scheduled",
                                     "channel": "linkedin", "provider": "dripify", "campaign": "camp-li", "opener": "Opening.", "steps": [{"day": 0, "subject": "", "body": "x"}],
                                     "history": []}))
    return run(db().enrollments.find_one({"id": eid}, {"_id": 0}))


def test_linkedin_handoff_paces_per_seat_and_pauses_on_a_seat_problem(client, monkeypatch):
    monkeypatch.setenv("OUTBOUND_LIVE_SENDING", "1")
    monkeypatch.setenv("DRIPIFY_API_KEY", "k")
    pushed = []

    async def push(contact, enr, campaign):
        pushed.append(contact["linkedin_url"])
        return "dl-1"

    monkeypatch.setattr(linkedin, "push", push)
    s = {**run(compliance.outbound_settings()), "linkedin_daily_cap": 1, "linkedin_paused": False}
    e1 = _li_enrollment("li-1", "li-c1", "https://www.linkedin.com/in/one-person")
    assert run(outbound.send_step(e1, s)) == "handed_off" and pushed == ["https://www.linkedin.com/in/one-person"]
    assert run(ledger.conflict({"linkedin_url": "https://linkedin.com/in/one-person", "email": "li-c1@initech.example"}, "email"))
    e2 = _li_enrollment("li-2", "li-c2", "https://www.linkedin.com/in/two-person")
    assert run(outbound.send_step(e2, s)) == "held" and len(pushed) == 1  # the seat's daily cap
    run(db().sends.delete_many({"channel": "linkedin"}))

    async def restricted(contact, enr, campaign):
        raise linkedin.SeatProblem("Dripify 423: account restricted")

    monkeypatch.setattr(linkedin, "push", restricted)
    run(db().enrollments.update_one({"id": "li-2"}, {"$set": {"claim_until": None}}))
    assert run(outbound.send_step(run(db().enrollments.find_one({"id": "li-2"}, {"_id": 0})), s)) == "held"
    assert run(compliance.outbound_settings())["linkedin_paused"]
    run(compliance.save_outbound_settings({"linkedin_paused": False, "linkedin_pause_reason": None}))


def test_dripify_reply_webhook_is_authenticated_deduplicated_and_classified(client, monkeypatch):
    monkeypatch.setenv("DRIPIFY_WEBHOOK_SECRET", "dsecret")
    body = {"event": "reply_received", "eventId": "e-1", "lead": {"id": "dl-9", "linkedinProfileUrl": "https://www.linkedin.com/in/one-person/"},
            "message": "Sounds interesting, tell me more."}
    assert client.post("/api/outbound/webhooks/dripify", json=body).status_code == 401
    r = client.post("/api/outbound/webhooks/dripify", json=body, headers={"X-Solix-Webhook-Secret": "dsecret"}).json()
    assert r["matched"]
    assert client.post("/api/outbound/webhooks/dripify", json=body, headers={"X-Solix-Webhook-Secret": "dsecret"}).json().get("duplicate")
    assert run(db().reply_log.find_one({"contact_id": "li-c1", "source": "dripify"}))["label"] == "interested"


# --- Seamless.AI ----------------------------------------------------------------------------------------------------

import seamless  # noqa: E402


def test_seamless_keeps_only_tagged_fields_honours_do_not_email_and_can_be_purged(client):
    run(db().contacts.insert_one({"id": "sm-c", "name": "Sea Mless", "company_id": None}))
    c = run(db().contacts.find_one({"id": "sm-c"}, {"_id": 0}))
    found = seamless._minimal({"id": "s-1", "emails": [{"email": "SEA@Hooli.example", "validation": "valid"}], "title": "CIO", "doNotEmail": True,
                               "linkedInUrl": "https://linkedin.com/in/sea"})
    out = run(seamless.apply(c, found, "admin@example.com"))
    assert out["email"] == "sea@hooli.example" and out["email_status"] == "valid" and out["vendor_fields"]["email"] == "seamless"
    assert run(compliance.is_suppressed("sea@hooli.example")) == "dnc"
    assert run(seamless.purge_vendor("seamless")) == 1
    after = run(db().contacts.find_one({"id": "sm-c"}, {"_id": 0}))
    assert "email" not in after and "title" not in after and after["name"] == "Sea Mless"


# --- watchdog ---------------------------------------------------------------------------------------------------------

def test_watchdog_warns_about_a_model_past_its_shutdown(client):
    import llm_log
    run(llm_log.record("opener", prompt_version="opener@x", provider="openai", model="gpt-4.1-nano", ok=True))
    checks = {c["code"]: c for c in client.get("/api/admin/ops/health").json()["checks"]}
    assert not checks["model_shutdown"]["ok"] and "gpt-4.1-nano" in checks["model_shutdown"]["detail"] and checks["model_shutdown"]["severity"] == "fail"
