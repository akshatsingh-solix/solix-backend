"""Where each account is in its buying journey, who's on its buying committee, and how much it looks like the accounts
that already said yes.

Buying stage (from evidence, never a guess):
    problem_aware  signals show the problem: competitor or problem jobs, DNS, filings, tenders, news
    exploring      people are researching: website visits, intent data, content reads, an objection reply ("not now")
    evaluating     they're comparing: a qualified website lead (MQL), pricing/demo/trial interest, several people engaged, "interested"
    engaged        a conversation: positive reply, meeting booked, sales-accepted or sales-qualified lead
    buying         an opportunity, or won
Each stage comes with the play that moves it forward: exploration plays (insight, a resource, a question) until they
are evaluating, buying plays (working session, business case, proof) after.

Buying committee: the roles the lead product needs at the table (product_fit line -> personas), which ones we have a
contact for, which have engaged, and which are missing.

Lookalikes (computed weekly in learning.py): accounts whose industry, size, competitors, signal mix and lead product
resemble the accounts that replied positively or turned into pipeline.
"""
from __future__ import annotations

import math
from collections import Counter
from typing import Dict, List, Optional

STAGES = ["problem_aware", "exploring", "evaluating", "engaged", "buying"]
STAGE_LABELS = {"problem_aware": "Problem aware", "exploring": "Exploring", "evaluating": "Evaluating", "engaged": "In conversation", "buying": "Buying"}
PLAYS = {
    "problem_aware": ("exploration", "Earn a look: share an insight on the problem their signals show, with a resource, not a meeting request."),
    "exploring": ("exploration", "Feed the research: send the product page or a short guide for their role, and ask one question about their estate."),
    "evaluating": ("buying", "Offer a 20-minute working session on their estate with a concrete outcome (first-wave shortlist, rough saving)."),
    "engaged": ("buying", "Book the meeting, bring the business case for the lead product, and bring in the missing committee roles."),
    "buying": ("buying", "Run the deal: proof of value, references from their industry, and the next play from the pitch plan."),
}
PROBLEM_TYPES = {"competitor_job", "problem_job", "dns", "cert", "filing", "tender", "news", "linkedin", "spend"}
EXPLORE_TYPES = {"website", "visit", "intent"}
POSITIVE_REPLIES = {"interested", "meeting"}

# Who decides on each product line (messaging.PERSONAS keys).
COMMITTEE = {
    "archiving": ["it_exec", "architecture", "compliance", "infrastructure"],
    "governance": ["data_exec", "compliance", "security"],
    "ai": ["data_exec", "it_exec", "security"],
    "ecs": ["content", "it_exec", "compliance"],
    "platform": ["it_exec", "data_exec", "architecture"],
    "services": ["it_exec", "architecture"],
}


def stage_of(company: dict, signals: List[dict], leads: List[dict], replies: List[dict]) -> dict:
    """The furthest stage the evidence supports, with the evidence and the play."""
    status = company.get("status")
    labels = Counter(r.get("label") for r in replies)
    lead_stages = {l.get("stage") for l in leads}
    types = {s.get("type") for s in signals}
    evidence: Dict[str, List[str]] = {s: [] for s in STAGES}
    if types & PROBLEM_TYPES:
        n = len([s for s in signals if s.get("type") in PROBLEM_TYPES])
        evidence["problem_aware"].append(f"{n} problem signal{'s' if n != 1 else ''}")
    if types & EXPLORE_TYPES:
        evidence["exploring"].append("researching: " + ", ".join(sorted({"website visits" if t in ("website", "visit") else "intent data" for t in types & EXPLORE_TYPES})))
    if leads:
        evidence["exploring"].append(f"{len(leads)} {'people' if len(leads) != 1 else 'person'} on the website")
    if labels.get("objection"):
        evidence["exploring"].append("replied 'not now'")
    if lead_stages & {"mql"}:
        evidence["evaluating"].append("a marketing-qualified lead")
    if len(leads) >= 3:
        evidence["evaluating"].append(f"{len(leads)} people engaged")
    if labels.get("interested"):
        evidence["evaluating"].append("replied with interest")
    if labels.get("meeting") or status in ("meeting_booked",) or lead_stages & {"sal", "sql"}:
        evidence["engaged"].append("meeting booked" if labels.get("meeting") or status == "meeting_booked" else "sales-accepted lead")
    if status in ("opportunity", "won") or lead_stages & {"opportunity", "won"}:
        evidence["buying"].append("an open opportunity" if "won" not in lead_stages and status != "won" else "won")
    stage = next((s for s in reversed(STAGES) if evidence[s]), "problem_aware")
    intent, play = PLAYS[stage]
    return {"stage": stage, "label": STAGE_LABELS[stage], "intent": intent, "play": play, "evidence": [e for s in STAGES[:STAGES.index(stage) + 1] for e in evidence[s]],
            "rank": STAGES.index(stage)}


def committee(company: dict, contacts: List[dict], replies: List[dict]) -> dict:
    """The roles the lead product needs, who we have for each, who engaged, and who's missing."""
    import messaging
    import scoring
    line = scoring.PRODUCT_TO_LINE.get(company.get("pitch_product") or company.get("primary_product") or "") or "archiving"
    needed = COMMITTEE.get(line, COMMITTEE["archiving"])
    replied = {r.get("contact_id") for r in replies}
    by_persona: Dict[str, List[dict]] = {}
    for c in contacts:
        if c.get("suppressed"):
            continue
        by_persona.setdefault(messaging.persona_key(c.get("title")) or "other", []).append(c)
    roles = []
    for p in needed:
        people = by_persona.get(p, [])
        roles.append({"persona": p, "label": messaging.PERSONAS[p]["label"], "have": len(people), "engaged": any(c.get("id") in replied for c in people),
                      "reachable": sum(1 for c in people if c.get("email") and c.get("email_status") != "invalid")})
    have = sum(1 for r in roles if r["have"])
    return {"line": line, "roles": roles, "coverage": round(100 * have / len(roles)), "missing": [r["label"] for r in roles if not r["have"]],
            "engaged": sum(1 for r in roles if r["engaged"])}


# --- Lookalikes -----------------------------------------------------------------------------------------------

POSITIVE_STATUSES = {"replied", "meeting_booked", "opportunity", "won"}
FEATURE_WEIGHTS = {"industry_slug": 3.0, "segment": 2.0, "pitch_product": 2.0, "competitor_keys": 2.5, "signal_types": 1.5, "territory": 0.5}


def _features(c: dict) -> Dict[str, set]:
    out = {}
    for f in FEATURE_WEIGHTS:
        v = c.get(f)
        out[f] = set(v) if isinstance(v, list) else ({v} if v else set())
    return out


def lookalike_model(companies: List[dict], min_positive: int = 3) -> Optional[dict]:
    """Per feature value, how much more common it is among positive accounts than among all of them (smoothed lift)."""
    pos = [c for c in companies if c.get("status") in POSITIVE_STATUSES or (c.get("journey") or {}).get("rank", 0) >= STAGES.index("engaged")]
    if len(pos) < min_positive:
        return None
    n_all, n_pos = len(companies), len(pos)
    all_counts: Dict[str, Counter] = {f: Counter() for f in FEATURE_WEIGHTS}
    pos_counts: Dict[str, Counter] = {f: Counter() for f in FEATURE_WEIGHTS}
    for c in companies:
        for f, vals in _features(c).items():
            all_counts[f].update(vals)
    for c in pos:
        for f, vals in _features(c).items():
            pos_counts[f].update(vals)
    lift = {f: {v: ((pos_counts[f][v] + 0.5) / (n_pos + 1)) / ((all_counts[f][v] + 0.5) / (n_all + 1)) for v in all_counts[f]} for f in FEATURE_WEIGHTS}
    top = sorted(((f, v, l) for f, vs in lift.items() for v, l in vs.items() if pos_counts[f][v] >= 2 and l > 1.2), key=lambda x: -x[2])[:6]
    return {"lift": lift, "positives": n_pos, "accounts": n_all, "traits": [{"feature": f, "value": v, "lift": round(l, 2), "positives": pos_counts[f][v]} for f, v, l in top],
            "positive_ids": [c["id"] for c in pos]}


def lookalike_score(c: dict, model: dict) -> dict:
    """0-100: how strongly this account carries the traits of the accounts that said yes, and which traits."""
    total, weight, why = 0.0, 0.0, []
    for f, vals in _features(c).items():
        w = FEATURE_WEIGHTS[f]
        if not vals:
            continue
        best = max((model["lift"][f].get(v, 1.0), v) for v in vals)
        total += w * math.log(max(best[0], 0.05))
        weight += w
        if best[0] > 1.5:
            why.append(f"{f.replace('_slug', '').replace('_keys', '').replace('_', ' ')}: {str(best[1]).replace('-', ' ')}")
    if not weight:
        return {"score": 0, "why": []}
    avg = total / sum(FEATURE_WEIGHTS.values())  # missing features count as no evidence
    score = round(100 / (1 + math.exp(-2.2 * avg)))
    return {"score": score, "why": why[:3]}
