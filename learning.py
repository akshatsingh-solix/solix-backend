"""MarkHub's weekly self-review: learn from what happened, tune what's safe to tune, and say what it found.

Every Monday (after outbound.learn re-weights signal types) and on demand from Intelligence:

1. Learn. Positive reply rates by persona, seniority, industry, pitched product, opener source and opener quality;
   measured data accuracy by source; which segments and territories hold strong accounts nobody is working.
2. Tune, within bounds. Product-fit weights per (industry, product) move towards the observed reply lift: at most
   +-0.2 a week, never outside 0.5-1.5, and only with enough sends behind them. Every change is written to
   `tuning_log` with its evidence and can be reverted. With autotune off, changes are proposed, not applied.
3. Track. A weekly snapshot of the numbers that matter (accounts, signals, positive replies, accuracy, coverage),
   so the brief can say what moved.
4. Report. A short brief of wins, risks and opportunities, each with the action to take, shown in MarkHub and
   posted to the pipeline channel.
"""
from __future__ import annotations

import logging
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import cache
import ops
import radar_service as rs

logger = logging.getLogger("solix.learning")

MIN_SENDS = 8          # enrollments behind a (industry, product) pair before its weight moves
MAX_STEP = 0.2         # largest weekly change of one weight
BOUNDS = (0.5, 1.5)
POSITIVE = {"interested", "meeting"}
LOOKALIKE_MIN = 60     # same bar as the Radar's Lookalikes tab
DEFAULT_SETTINGS = {"autotune": True, "min_sends": MIN_SENDS}


def _db():
    import database
    return database.db


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(d: datetime) -> str:
    return d.isoformat()


async def settings() -> dict:
    doc = await _db().settings.find_one({"key": "intelligence"}, {"_id": 0}) or {}
    return {**DEFAULT_SETTINGS, **{k: v for k, v in doc.items() if k in DEFAULT_SETTINGS}}


async def save_settings(values: dict) -> dict:
    clean = {k: v for k, v in values.items() if k in DEFAULT_SETTINGS}
    await _db().settings.update_one({"key": "intelligence"}, {"$set": clean}, upsert=True)
    return await settings()


# --- 1. Learn ---------------------------------------------------------------------------------------------------

async def outcomes(days: int = 120) -> List[dict]:
    """One row per enrollment with what it was (persona, industry, product, opener) and whether it earned a positive reply."""
    since = _iso(_now() - timedelta(days=days))
    enr = await _db().enrollments.find({"created_at": {"$gte": since}, "status": {"$nin": ["pending_approval", "cancelled", "rejected"]}},
                                       {"_id": 0, "id": 1, "contact_id": 1, "persona": 1, "seniority_band": 1, "industry_slug": 1, "pitch_product": 1,
                                        "opener_provider": 1, "opener_quality": 1, "variant": 1, "sequence_name": 1}).to_list(50000)
    replies = await _db().reply_log.find({"received_at": {"$gte": since}}, {"_id": 0, "enrollment_id": 1, "contact_id": 1, "label": 1}).to_list(50000)
    pos_enr = {r.get("enrollment_id") for r in replies if r.get("label") in POSITIVE}
    any_enr = {r.get("enrollment_id") for r in replies}
    out = []
    for e in enr:
        q = e.get("opener_quality")
        out.append({**e, "positive": e["id"] in pos_enr, "replied": e["id"] in any_enr,
                    "quality_band": None if q is None else ("80+" if q >= 80 else "60-79" if q >= 60 else "<60")})
    return out


def rates(rows: List[dict], key: str, min_n: int = 5) -> List[dict]:
    """Positive reply rate per value of `key`, with its lift over the overall rate."""
    n_all = len(rows)
    base = sum(1 for r in rows if r["positive"]) / n_all if n_all else 0
    groups: Dict[str, list] = defaultdict(list)
    for r in rows:
        if r.get(key):
            groups[r[key]].append(r)
    out = []
    for value, rs_ in groups.items():
        if len(rs_) < min_n:
            continue
        pos = sum(1 for r in rs_ if r["positive"])
        rate = pos / len(rs_)
        out.append({"value": value, "n": len(rs_), "positive": pos, "rate": round(rate, 4), "lift": round(rate / base, 2) if base else None,
                    "replied": round(sum(1 for r in rs_ if r["replied"]) / len(rs_), 4)})
    return sorted(out, key=lambda x: -(x["rate"]))


async def coverage() -> dict:
    """Where the Radar sees demand that nobody is working: the scoping and scaling view."""
    rows = await _db().companies.find({"excluded_reason": None}, {"_id": 0, "strength": 1, "owner": 1, "status": 1, "segment": 1, "industry_slug": 1,
                                                                   "territory": 1, "surge": 1, "contact_count": 1, "next_action": 1, "data_quality": 1}).to_list(None)
    strong = [c for c in rows if c.get("strength") == "Strong"]
    unworked = [c for c in strong if c.get("status") in ("new", "ready", "researching")]
    by = lambda key, items: dict(sorted(((k or "unknown", n) for k, n in _count(items, key).items()), key=lambda kv: -kv[1]))  # noqa: E731
    return {"accounts": len(rows), "strong": len(strong), "strong_unworked": len(unworked), "strong_unowned": sum(1 for c in strong if not c.get("owner")),
            "surging_unworked": sum(1 for c in unworked if c.get("surge")), "by_industry": by("industry_slug", unworked), "by_territory": by("territory", unworked),
            "by_segment": by("segment", unworked), "verified_share": round(sum(1 for c in rows if (c.get("data_quality") or {}).get("meets_target")) / len(rows), 4) if rows else None}


def _count(items: List[dict], key: str) -> Dict[str, int]:
    out: Dict[str, int] = defaultdict(int)
    for c in items:
        out[c.get(key)] += 1
    return out


# --- 2. Tune ----------------------------------------------------------------------------------------------------

def proposals(rows: List[dict], current: Dict[str, float], min_sends: int = MIN_SENDS) -> List[dict]:
    """Product-fit weight changes the evidence supports: move each (industry, product) weight towards its reply lift."""
    n_all = len(rows)
    base = sum(1 for r in rows if r["positive"]) / n_all if n_all else 0
    if not base:
        return []
    groups: Dict[tuple, list] = defaultdict(list)
    for r in rows:
        if r.get("industry_slug") and r.get("pitch_product"):
            groups[(r["industry_slug"], r["pitch_product"])].append(r)
    out = []
    for (ind, prod), rs_ in groups.items():
        if len(rs_) < min_sends:
            continue
        rate = sum(1 for r in rs_ if r["positive"]) / len(rs_)
        target = max(BOUNDS[0], min(BOUNDS[1], rate / base))
        key = f"industry:{ind}:{prod}"
        before = float(current.get(key, 1.0))
        step = max(-MAX_STEP, min(MAX_STEP, target - before))
        after = round(before + step, 3)
        if abs(after - before) >= 0.05:
            out.append({"key": key, "before": before, "after": after, "evidence": {"sends": len(rs_), "positive_rate": round(rate, 4), "overall_rate": round(base, 4)},
                        "reason": f"{prod.replace('-', ' ')} pitched to {ind.replace('-', ' ')} accounts: {round(rate * 100, 1)}% positive replies vs {round(base * 100, 1)}% overall "
                                  f"({len(rs_)} sends)"})
    return sorted(out, key=lambda p: -abs(p["after"] - p["before"]))


async def apply(props: List[dict], *, by: str, review_id: str) -> int:
    if not props:
        return 0
    cfg = await rs.radar_settings()
    weights = dict(cfg.get("product_weights") or {})
    for p in props:
        weights[p["key"]] = p["after"]
        await _db().tuning_log.insert_one({"id": str(uuid.uuid4()), "review_id": review_id, "kind": "product_weight", **p, "by": by, "at": _iso(_now()), "reverted": False})
    await rs.save_radar_settings({"product_weights": weights})
    await rs.recompute_all()
    return len(props)


async def revert(change_id: str, by: str) -> dict:
    ch = await _db().tuning_log.find_one({"id": change_id}, {"_id": 0})
    if not ch or ch.get("reverted"):
        return {"reverted": False}
    cfg = await rs.radar_settings()
    weights = dict(cfg.get("product_weights") or {})
    if ch["before"] == 1.0:
        weights.pop(ch["key"], None)
    else:
        weights[ch["key"]] = ch["before"]
    await rs.save_radar_settings({"product_weights": weights})
    await _db().tuning_log.update_one({"id": change_id}, {"$set": {"reverted": True, "reverted_by": by, "reverted_at": _iso(_now())}})
    await rs.recompute_all()
    return {"reverted": True, "key": ch["key"], "weight": ch["before"]}


# --- 3. Track ---------------------------------------------------------------------------------------------------

async def snapshot() -> dict:
    import data_quality as dq
    now = _now()
    week = _iso(now - timedelta(days=7))
    db = _db()
    acc = await dq.report(90)
    return {"at": _iso(now), "accounts": await db.companies.count_documents({}), "new_accounts_7d": await db.companies.count_documents({"created_at": {"$gte": week}}),
            "signals_7d": await db.company_signals.count_documents({"first_seen_at": {"$gte": week}}),
            "sends_7d": await db.sends.count_documents({"sent_at": {"$gte": week}}),
            "positive_7d": await db.reply_log.count_documents({"received_at": {"$gte": week}, "label": {"$in": list(POSITIVE)}}),
            "accuracy": acc["overall"]["rate"], "accuracy_low": acc["overall"]["low"], "accuracy_checks": acc["overall"]["n"],
            "email_accuracy": acc["email"]["rate"]}


# --- 4. Report --------------------------------------------------------------------------------------------------

def _pct(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{round(100 * x, 1)}%"


def insights(rows: List[dict], cov: dict, snap: dict, prev: Optional[dict], acc: dict, props: List[dict], applied: bool) -> List[dict]:
    """The brief: what worked, what's at risk, where to go next. Each item says what to do about it."""
    out: List[dict] = []
    if prev:
        for key, label in (("positive_7d", "positive replies"), ("signals_7d", "new signals"), ("new_accounts_7d", "new accounts")):
            a, b = snap.get(key) or 0, prev.get(key) or 0
            if b and abs(a - b) / b >= 0.25:
                out.append({"kind": "win" if a > b else "risk", "title": f"{label.capitalize()} {'up' if a > b else 'down'} {round(100 * abs(a - b) / b)}% week on week",
                            "detail": f"{a} this week vs {b} last week.", "action": None})
    for dim, label in (("persona", "role"), ("industry_slug", "industry"), ("pitch_product", "pitched product")):
        rs_ = rates(rows, dim)
        if len(rs_) >= 2 and rs_[0]["lift"] and rs_[0]["lift"] >= 1.5:
            top = rs_[0]
            out.append({"kind": "win", "title": f"Best {label}: {top['value'].replace('_', ' ').replace('-', ' ')} ({_pct(top['rate'])} positive, {top['lift']}x average)",
                        "detail": f"{top['positive']} positive replies from {top['n']} sends.", "action": f"Prioritise {label}s like this in the next enrolment batch."})
        if len(rs_) >= 2 and rs_[-1]["n"] >= 10 and rs_[-1]["rate"] == 0:
            low = rs_[-1]
            out.append({"kind": "risk", "title": f"No positive replies from {low['value'].replace('_', ' ').replace('-', ' ')} ({low['n']} sends)",
                        "detail": "Same message to this group keeps missing.", "action": "Change the angle or pause this group."})
    q = rates(rows, "quality_band", 5)
    hi, lo = next((x for x in q if x["value"] == "80+"), None), next((x for x in q if x["value"] == "<60"), None)
    if hi and lo and lo["rate"] < hi["rate"]:
        out.append({"kind": "win", "title": f"Personal openers work: {_pct(hi['rate'])} positive when the opener scores 80+, {_pct(lo['rate'])} below 60",
                    "detail": "Opener quality is checked on every enrolment.", "action": "Keep deep dives approved so openers have a real trigger."})
    if cov.get("strong_unworked"):
        top_ind = next(iter(cov["by_industry"].items()), None)
        n = cov["strong_unworked"]
        out.append({"kind": "opportunity", "title": f"{n} strong account{'s' if n != 1 else ''} not yet worked ({cov['surging_unworked']} surging)",
                    "detail": f"Most are in {top_ind[0].replace('-', ' ')} ({top_ind[1]})." if top_ind else "",
                    "action": "Open the Radar To-do tab and assign or enrol the surging ones first.", "link": "/admin/radar?tab=todo&strength=Strong"})
    if cov.get("strong_unowned"):
        n = cov["strong_unowned"]
        out.append({"kind": "risk", "title": f"{n} strong account{'s have' if n != 1 else ' has'} no owner", "detail": "Nobody will act on them.",
                    "action": "Route them in Radar, or let reps claim them.", "link": "/admin/radar?owner=unassigned&strength=Strong"})
    ov = acc.get("overall") or {}
    if ov.get("n"):
        meets = ov.get("meets_target")
        out.append({"kind": "win" if meets else "data", "title": f"Measured data accuracy {_pct(ov['rate'])} (95% interval {_pct(ov['low'])} to {_pct(ov['high'])}, {ov['n']} check{'s' if ov['n'] != 1 else ''})",
                    "detail": f"Target {_pct(acc.get('target'))}: " + ("met with confidence." if meets else
                                                                       "not yet proven" + (f"; about {acc['checks_needed']} more audit checks would prove it at this rate." if acc.get("checks_needed") else ".")),
                    "action": None if meets else "Run 15 audit checks in Radar > Data accuracy.", "link": "/admin/radar?tab=accuracy"})
    else:
        out.append({"kind": "data", "title": "Data accuracy hasn't been measured yet", "detail": "Nobody has checked a random sample of shown values.",
                    "action": "Run 15 audit checks in Radar > Data accuracy.", "link": "/admin/radar?tab=accuracy"})
    weak = [s for s in acc.get("sources") or [] if s["n"] >= 10 and s["rate"] < 0.9 and s["via"] not in ("shown",)]
    for s in weak[:2]:
        out.append({"kind": "data", "title": f"{s['label']} is right {_pct(s['rate'])} of the time on {s['field'].replace('_', ' ')} ({s['n']} checks)",
                    "detail": "MarkHub now trusts it less: its values need a second source before they count as verified.", "action": None})
    for p in props[:3]:
        out.append({"kind": "tuning", "title": ("Tuned: " if applied else "Suggested: ") + p["reason"],
                    "detail": f"Product-fit weight {p['before']} -> {p['after']}.", "action": None if applied else "Turn on autotune or apply it in Intelligence."})
    return out


async def review(by: str = "system") -> dict:
    """The weekly self-review. Safe to run any time; a second run the same day just refreshes the brief."""
    import data_quality as dq
    async with ops.run("intelligence_review", by) as run:
        cfg = await settings()
        rows = await outcomes()
        cov = await coverage()
        snap = await snapshot()
        prev = await _db().intelligence.find_one({"at": {"$lt": _iso(_now() - timedelta(days=5))}}, {"_id": 0, "snapshot": 1}, sort=[("at", -1)])
        acc = await dq.report(90)
        radar_cfg = await rs.radar_settings()
        props = proposals(rows, radar_cfg.get("product_weights") or {}, int(cfg.get("min_sends") or MIN_SENDS))
        review_id = str(uuid.uuid4())
        applied = 0
        if cfg.get("autotune"):
            applied = await apply(props, by=by, review_id=review_id)
        brief = insights(rows, cov, snap, (prev or {}).get("snapshot"), acc, props, bool(cfg.get("autotune")))
        looks = await score_lookalikes()
        brief = journey_insights(await journey_stats()) + committee_insights(await committee_gaps()) + lookalike_insights(looks) + brief
        narrative = await narrate(brief, snap)
        doc = {"id": review_id, "at": _iso(_now()), "by": by, "snapshot": snap, "coverage": cov, "insights": brief, "proposals": props, "applied": applied,
               "narrative": narrative, "lookalikes": {k: v for k, v in (looks or {}).items() if k != "lift"} if looks else None,
               "learned": {"persona": rates(rows, "persona"), "seniority": rates(rows, "seniority_band"), "industry": rates(rows, "industry_slug"),
                           "product": rates(rows, "pitch_product"), "opener": rates(rows, "opener_provider"), "quality": rates(rows, "quality_band"),
                           "outcomes": len(rows), "positive": sum(1 for r in rows if r["positive"])}}
        await _db().intelligence.insert_one(dict(doc))
        run["counts"].update({"outcomes": len(rows), "insights": len(brief), "proposals": len(props), "applied": applied})
        cache.bump("intelligence")
        heads = [i["title"] for i in brief if i["kind"] in ("win", "risk", "opportunity")][:3]
        if heads:
            await ops.notify("pipeline", "MarkHub weekly brief:\n- " + "\n- ".join(heads), event="intelligence_brief", key=f"brief:{doc['at'][:10]}")
    doc.pop("_id", None)
    return doc


# --- Buying stages, committees, lookalikes, and a written summary --------------------------------------------------

async def journey_stats(days: int = 7) -> dict:
    """Accounts per buying stage, who moved forward this week, and who has sat in evaluating too long."""
    import account_intel as ai
    since = _iso(_now() - timedelta(days=days))
    stale_before = _iso(_now() - timedelta(days=21))
    rows = await _db().companies.find({"excluded_reason": None}, {"_id": 0, "id": 1, "name": 1, "journey": 1, "journey_moves": 1}).to_list(None)
    by_stage = Counter((c.get("journey") or {}).get("stage") or "problem_aware" for c in rows)
    moved = []
    for c in rows:
        for m in c.get("journey_moves") or []:
            if m.get("at", "") >= since and ai.STAGES.index(m["to"]) > ai.STAGES.index(m["from"]):
                moved.append({"id": c["id"], "name": c["name"], "to": m["to"]})
    stalled = [c for c in rows if (c.get("journey") or {}).get("stage") == "evaluating" and ((c.get("journey") or {}).get("since") or "") < stale_before]
    return {"by_stage": {s: by_stage.get(s, 0) for s in ai.STAGES}, "moved": moved, "stalled": [{"id": c["id"], "name": c["name"]} for c in stalled]}


def journey_insights(j: dict) -> List[dict]:
    import account_intel as ai
    out = []
    if j["moved"]:
        to = Counter(m["to"] for m in j["moved"])
        names = ", ".join(m["name"] for m in j["moved"][:3])
        out.append({"kind": "win", "title": f"{len(j['moved'])} account{'s' if len(j['moved']) != 1 else ''} moved forward this week: "
                                            + ", ".join(f"{n} to {ai.STAGE_LABELS[s].lower()}" for s, n in to.most_common()),
                    "detail": f"Including {names}.", "action": "Switch them to the buying play: offer the working session.", "link": "/admin/radar?stage=evaluating"})
    if j["stalled"]:
        out.append({"kind": "risk", "title": f"{len(j['stalled'])} account{'s' if len(j['stalled']) != 1 else ''} stuck in evaluating for 3+ weeks",
                    "detail": ", ".join(s["name"] for s in j["stalled"][:3]) + ".", "action": "Bring in a missing committee role or send a business case.",
                    "link": "/admin/radar?stage=evaluating"})
    return out


async def committee_gaps() -> dict:
    """Which roles are most often missing on accounts worth working (strong, or past problem-aware)."""
    rows = await _db().companies.find({"excluded_reason": None, "$or": [{"strength": "Strong"}, {"journey.rank": {"$gte": 1}}]},
                                      {"_id": 0, "committee": 1}).to_list(None)
    missing = Counter(m for c in rows for m in (c.get("committee") or {}).get("missing") or [])
    none = sum(1 for c in rows if (c.get("committee") or {}).get("coverage") == 0)
    return {"accounts": len(rows), "missing": dict(missing.most_common(4)), "no_contacts": none}


def committee_insights(g: dict) -> List[dict]:
    if not g["accounts"] or not g["missing"]:
        return []
    role, n = next(iter(g["missing"].items()))
    return [{"kind": "opportunity", "title": f"{n} of {g['accounts']} accounts worth working have no contact in {role.split(' (')[0]}",
             "detail": f"{g['no_contacts']} have nobody from the buying committee yet." if g["no_contacts"] else "",
             "action": "Use Find people on those accounts; the drawer shows which roles are missing.", "link": "/admin/radar?strength=Strong"}]


async def score_lookalikes() -> Optional[dict]:
    """Score every account against the traits of the accounts that said yes; stored as companies.lookalike."""
    from pymongo import UpdateOne
    import account_intel as ai
    rows = await _db().companies.find({}, {"_id": 0, "id": 1, "name": 1, "status": 1, "journey": 1, "excluded_reason": 1,
                                           **{f: 1 for f in ai.FEATURE_WEIGHTS}}).to_list(None)
    model = ai.lookalike_model(rows)
    if not model:
        return None
    positive = set(model["positive_ids"])
    writes, top = [], []
    for c in rows:
        if c["id"] in positive:
            writes.append(UpdateOne({"id": c["id"]}, {"$set": {"lookalike": None}}))
            continue
        s = ai.lookalike_score(c, model)
        writes.append(UpdateOne({"id": c["id"]}, {"$set": {"lookalike": s}}))
        if not c.get("excluded_reason"):
            top.append({"id": c["id"], "name": c["name"], **s})
    if writes:
        await _db().companies.bulk_write(writes, ordered=False)
        rs.companies_changed()
    top.sort(key=lambda x: -x["score"])
    return {**model, "top": top[:10], "strong": sum(1 for t in top if t["score"] >= LOOKALIKE_MIN)}


def lookalike_insights(looks: Optional[dict]) -> List[dict]:
    if not looks:
        return [{"kind": "data", "title": "Lookalikes start after 3 accounts reply positively or reach a meeting",
                 "detail": "MarkHub then finds accounts with the same industry, size, competitors and signals.", "action": None}]
    traits = ", ".join(f"{t['feature'].replace('_slug', '').replace('_keys', '').replace('_', ' ')} {str(t['value']).replace('-', ' ')} ({t['lift']}x)"
                       for t in looks["traits"][:3])
    if not looks["strong"]:
        return []
    return [{"kind": "opportunity", "title": f"{looks['strong']} account{'s look' if looks['strong'] != 1 else ' looks'} like your {looks['positives']} best ones",
             "detail": (f"What your best accounts share: {traits}. " if traits else "") + "Top: " + ", ".join(t["name"] for t in looks["top"][:3]) + ".",
             "action": "Work the Lookalikes tab in Radar first.", "link": "/admin/radar?tab=lookalikes"}]


async def narrate(brief: List[dict], snap: dict) -> Optional[dict]:
    """A four-sentence summary for leadership, written by whatever model is available. It sees only aggregate numbers
    and the brief's own titles (no people), so a free tier is fine; without a model the brief stands on its own."""
    import llm
    import privacy
    providers, _ = privacy.providers_for("brief")
    if not providers or not brief:
        return None
    facts = "\n".join(f"- {i['kind']}: {i['title']}" for i in brief[:12])
    nums = ", ".join(f"{k.replace('_', ' ')}: {v}" for k, v in snap.items() if k != "at" and v is not None)
    red = privacy.Redactor()
    prompt = red.redact(f"This week's numbers: {nums}\nFindings:\n{facts}")
    out = []
    try:
        async for ev in llm.stream_completion([{"role": "system", "content": "You write a four-sentence weekly summary for a B2B marketing and sales leadership team. "
                                                                          "Use only the facts given. Lead with what changed and why it matters, then the single most "
                                                                          "important action. Plain English, no bullet points, no hype, no numbers that aren't given."},
                                               {"role": "user", "content": prompt}], max_tokens=260, temperature=0.3, providers=providers):
            if ev["type"] == "text":
                out.append(ev["text"])
            elif ev["type"] == "meta":
                provider = ev.get("provider")
    except llm.ProviderError:
        return None
    text = red.restore(" ".join("".join(out).split()))
    return {"text": text, "provider": locals().get("provider")} if text else None


async def latest() -> Optional[dict]:
    return await _db().intelligence.find_one({}, {"_id": 0}, sort=[("at", -1)])


async def weekly_if_due() -> None:
    """Called from the outbound tick: once each Monday after 06:00 UTC."""
    now = _now()
    if now.weekday() != 0 or now.hour < 6:
        return
    last = await latest()
    if last and last["at"][:10] == now.date().isoformat():
        return
    try:
        await review()
    except Exception:  # noqa: BLE001
        logger.exception("weekly intelligence review failed")
