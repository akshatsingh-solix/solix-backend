"""First-touch messaging shaped to the person: their role, their seniority, their industry and what the account's
signals say, so the first email earns a look ("exploration intent") and later touches ask for time ("buying intent").

The plan for one contact (`brief`):
* persona: what this role answers for, what keeps it up at night, and the words it uses;
* seniority: executives get outcome and risk in three short lines; managers get the programme; practitioners the how;
* industry: the regulation or estate that makes the pain real there (product_fit.INDUSTRY_FIT);
* product: the account's lead play (product_fit.recommend), described in the persona's terms;
* hook: the account's own trigger (a signal or the approved deep dive), never a competitor's name;
* the ask: touch 1 offers something to read or a question to answer (exploration); touch 3 offers a short working
  session (buying); nothing asks for a demo on day one.

`check` scores a draft against that plan and lists what's missing, so a weak or generic opener is replaced by the
rule-based one before anyone sees it. Phase 5 (learning.py) tracks reply rates by persona, industry and play.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

PERSONAS: Dict[str, dict] = {
    "it_exec": {
        "team": "IT leaders", "label": "IT leadership (CIO, CTO, VP IT)", "rx": r"\b(cio|cto|chief (information|technology|digital) officer|vp (of )?(it|information|technology|infrastructure)|"
                                                       r"(head|director|svp|evp) of (it|information technology|technology|infrastructure))\b",
        "cares": "run cost, risk and the roadmap", "pain": "budget tied up keeping old systems alive that no one uses day to day",
        "outcome": "cut the run cost of legacy applications and free budget for the roadmap", "words": ["cost", "risk", "roadmap", "portfolio"],
        "explore": "a one-page view of what retiring the long tail of applications typically frees up",
    },
    "data_exec": {
        "team": "data leaders", "label": "Data leadership (CDO, head of data)", "rx": r"\b(cdo|chief data|chief analytics|head of data|vp (of )?data|director (of )?data|data (governance|strategy) (lead|head|director))\b",
        "cares": "trusted, governed data that AI and analytics can use", "pain": "historical data scattered across systems that can't be governed or used",
        "outcome": "bring historical data under one governance model and make it usable for AI", "words": ["governed", "trusted", "AI-ready", "lineage"],
        "explore": "a short note on how teams make archived data AI-ready without moving it twice",
    },
    "security": {
        "team": "security teams", "label": "Security (CISO, head of security)", "rx": r"\b(ciso|chief (information )?security|head of security|vp (of )?security|security (director|architect))\b",
        "cares": "attack surface and sensitive data exposure", "pain": "unpatched legacy systems kept online only for their data",
        "outcome": "take legacy systems offline and keep their sensitive data locked down", "words": ["exposure", "attack surface", "sensitive data"],
        "explore": "a checklist for taking legacy systems offline without losing access to their records",
    },
    "compliance": {
        "team": "compliance and records teams", "label": "Compliance, legal, records and privacy", "rx": r"\b(compliance|general counsel|legal|records (manager|management)|information governance|privacy|dpo|"
                                                                r"data protection|e-?discovery|retention)\b",
        "cares": "retention, legal hold and proving it to regulators", "pain": "records spread across systems with different retention rules and no single place to search",
        "outcome": "apply one retention policy and one search across every record, including retired systems", "words": ["retention", "legal hold", "defensible", "audit"],
        "explore": "a retention and legal hold checklist for records that live in old systems",
    },
    "architecture": {
        "team": "architecture teams", "label": "Enterprise and application architecture", "rx": r"\b(enterprise architect|solution architect|chief architect|application (portfolio|owner|manager)|"
                                                                r"head of applications|director of applications|it portfolio)\b",
        "cares": "a simpler application estate", "pain": "applications that can't be retired because no one knows where their data should go",
        "outcome": "retire applications in repeatable waves and keep their data", "words": ["portfolio", "rationalisation", "decommission"],
        "explore": "the pattern other architecture teams use to retire applications in waves",
    },
    "infrastructure": {
        "team": "infrastructure teams", "label": "Infrastructure, databases and platforms", "rx": r"\b(dba|database|infrastructure|platform (engineer|lead|manager)|storage|systems (engineer|manager)|"
                                                                  r"sap basis|mainframe|data engineer)\b",
        "cares": "performance, storage and keeping the lights on", "pain": "production databases slowed by data no one queries",
        "outcome": "move inactive data out of production and keep it queryable", "words": ["performance", "storage", "upgrade"],
        "explore": "how teams take 40-60% of data out of production databases and still query it",
    },
    "finance": {
        "team": "finance teams", "label": "Finance and procurement", "rx": r"\b(cfo|chief financial|finance director|controller|procurement|vendor management)\b",
        "cares": "spend and contract renewals", "pain": "licence and support renewals for systems kept only for their records",
        "outcome": "end renewals for systems that only hold old records", "words": ["licence", "renewal", "savings"],
        "explore": "a simple model of what ending a legacy licence saves over three years",
    },
    "content": {
        "team": "content teams", "label": "Content and ECM", "rx": r"\b(ecm|content (management|services)|document management|sharepoint|documentum)\b",
        "cares": "content platforms that users and auditors can live with", "pain": "an ageing content platform that's expensive to keep and hard to leave",
        "outcome": "move to modern content services and archive the rest", "words": ["content", "migration", "users"],
        "explore": "what a staged move off an ageing ECM platform looks like",
    },
}
DEFAULT_PERSONA = {"team": "teams like yours", "label": "Other roles", "cares": "keeping systems and records under control", "pain": "old systems kept running only for their records",
                   "outcome": "keep the records and switch the old systems off", "words": [], "explore": "a short overview of how teams retire legacy systems"}
_PERSONA_RX = [(k, re.compile(v["rx"], re.I)) for k, v in PERSONAS.items()]

SENIORITY_STYLE = {
    "executive": {"label": "Executive", "max_words": 90, "style": "three short lines: the trigger, the outcome in business terms, one easy question"},
    "manager": {"label": "Manager", "max_words": 120, "style": "the trigger, the programme they'd run and its outcome, one easy question"},
    "individual": {"label": "Practitioner", "max_words": 130, "style": "the trigger, how it works in practice, one practical question"},
}

STEP_OBJECTIVES = [
    {"step": 1, "intent": "exploration", "ask": "Offer something useful to read or one question about their situation. No meeting request."},
    {"step": 2, "intent": "exploration", "ask": "A different angle on the same pain, with one proof point. Still no meeting request."},
    {"step": 3, "intent": "buying", "ask": "Offer a 20-minute working session on their specific estate, with a clear outcome."},
    {"step": 4, "intent": "close", "ask": "Close the loop politely and ask who owns it if not them."},
]

CLICHES = re.compile(r"hope (this|you).{0,20}(well|finds)|just (checking|following) (in|up)|touch(ing)? base|circle back|synerg|revolutioni[sz]e|game.?changer|"
                     r"best.in.class|cutting.edge|I wanted to reach out|quick question|leverage our", re.I)


def persona_key(title: Optional[str]) -> Optional[str]:
    t = title or ""
    for key, rx in _PERSONA_RX:
        if rx.search(t):
            return key
    return None


def seniority_key(title: Optional[str]) -> str:
    import scoring
    t = title or ""
    if re.search(r"\b(chief|c[etiofd]o|ciso|president|vp|vice president|svp|evp|general counsel|head of)\b", t, re.I):
        return "executive"
    if scoring.SENIOR_RX.search(t) or scoring.MANAGER_RX.search(t):
        return "manager"
    return "individual"


def brief(contact: dict, company: Optional[dict], *, signal: Optional[dict] = None, research: Optional[dict] = None, site_url: str = "https://www.solix.com",
          signals: Optional[List[dict]] = None) -> dict:
    """Everything the first touch should be built from, for this person at this account."""
    import data_quality as dq
    import product_fit
    import radar_model as rm
    company = company or {}
    pk = persona_key(contact.get("title"))
    persona = PERSONAS.get(pk, DEFAULT_PERSONA)
    sk = seniority_key(contact.get("title"))
    industry = company.get("industry_slug") or dq.industry_slug(company.get("industry"))
    fit = product_fit.recommend(company, signals or ([signal] if signal else []))
    play = (fit["plays"] or [None])[0]
    if not play and company.get("primary_product"):
        play = {"product": company["primary_product"], "label": product_fit.label(company["primary_product"]), "reasons": [], "value": "",
                "page": f"/products/{company['primary_product']}"}
    industry_reason = next((r for p, _, r in product_fit.INDUSTRY_FIT.get(industry or "", []) if play and p == play["product"]), None) or \
        next((r for _, _, r in product_fit.INDUSTRY_FIT.get(industry or "", [])[:1]), None)
    hook, hook_line = None, None
    name = company.get("name") or "your team"
    if research and research.get("trigger"):
        hook = re.sub(r"\s*\[\d+\]", "", research["trigger"]).strip().rstrip(".")
        hook_line = f"{hook}."
    elif signal:
        title = (signal.get("role_title") or signal.get("title") or "").strip()
        term = signal.get("problem_term")
        clean = title and not rm.find_competitors(title) and len(title) <= 80
        if signal.get("type") in ("competitor_job", "problem_job"):
            hook = f"hiring {('an ' if title[:1].lower() in 'aeiou' else 'a ') + title}" if clean else "hiring for its archive and records platform"
            hook_line = f"I noticed {name} is {hook}."
        elif signal.get("type") in ("dns", "cert"):
            hook = f"new {term or 'archive'} infrastructure"
            hook_line = f"I noticed {name} is standing up {hook}."
        elif signal.get("type") in ("news", "filing", "tender") and clean:
            hook = title
            hook_line = f"I read about {name}: {title.rstrip('.')}."
        elif clean:
            hook = title
            hook_line = f"I noticed {name}'s recent {rm.SIGNAL_LABELS.get(signal.get('type'), 'activity').lower()}: {title.rstrip('.')}."
    for c in rm.find_competitors(hook or "") + rm.find_competitors(hook_line or ""):  # never name a competitor in a first touch
        hook = re.sub(re.escape(c["term"]), "their current platform", hook or "", flags=re.I) or None
        hook_line = re.sub(re.escape(c["term"]), "their current platform", hook_line or "", flags=re.I) or None
    link = f"{site_url.rstrip('/')}{play['page']}" if play else site_url
    return {
        "persona": pk or "other", "persona_label": persona["label"], "cares": persona["cares"], "pain": persona["pain"], "outcome": persona["outcome"],
        "words": persona["words"], "seniority": sk, "style": SENIORITY_STYLE[sk]["style"], "max_words": SENIORITY_STYLE[sk]["max_words"],
        "industry": industry, "industry_label": dq.INDUSTRY_LABELS.get(industry or "", None), "industry_reason": industry_reason,
        "product": (play or {}).get("product"), "product_label": (play or {}).get("label"), "product_reasons": (play or {}).get("reasons", [])[:2],
        "hook": hook, "hook_line": hook_line, "team": persona["team"], "explore_offer": persona["explore"], "resource_link": link,
        "explore_cta": f"Would {persona['explore']} be useful? Happy to send it.",
        "buy_cta": f"Worth a 20-minute working session on {company.get('name') or 'your estate'} to see where {(play or {}).get('label') or 'this'} would land first?",
        "steps": STEP_OBJECTIVES, "company": company.get("name"),
    }


def prompt(b: dict) -> str:
    """The writer's instructions for touch 1: grounded, persona-shaped, exploration only."""
    lines = [
        f"Prospect role: {b['persona_label']} ({b['seniority']}). They care about {b['cares']}.",
        f"Their likely pain: {b['pain']}.",
        f"Industry: {b['industry_label'] or 'unknown'}" + (f" ({b['industry_reason']})" if b.get("industry_reason") else "") + ".",
        f"What Solix offers them: {b['product_label'] or 'archiving and application retirement'}: {b['outcome']}.",
        f"Trigger at {b['company'] or 'their company'}: {b['hook'] or 'none known; do not invent one'}.",
        f"Style: {b['style']}. Use their words where natural: {', '.join(b['words']) or 'plain English'}.",
    ]
    return "\n".join(lines)


OPENER_SYSTEM = ("You write the first two sentences of a B2B cold email for Solix Technologies (archiving, application retirement, data governance, enterprise AI). "
                 "Sentence 1 connects the prospect's trigger to what their role cares about. Sentence 2 names the outcome Solix gives people in their role and industry. "
                 "Ground everything ONLY in the facts given; never invent numbers, customers or events. Never name any software vendor or competitor product. "
                 "Never mention pricing. No greeting, no sign-off, no flattery, no questions, no clichés. Maximum 2 sentences, under 45 words. Reply with the two sentences only.")


def rule_opener(b: dict) -> str:
    """A persona-shaped opener with no model: the account's trigger, then what it means for this role and the outcome."""
    team = b.get("team") or "teams like yours"
    if b.get("hook_line"):
        first = b["hook_line"]
    elif b.get("industry_reason"):
        first = f"In {(b.get('industry_label') or 'your industry').lower()}, {b['industry_reason']}."
    else:
        first = f"Most {team} we speak to carry {b['pain']}."
        return f"{first} Solix helps them {b['outcome']}."
    return f"{first} For {team}, that usually means {b['pain']}; Solix helps them {b['outcome']}."


def check(text: str, b: dict, *, step: int = 1) -> dict:
    """How personal and on-brief a draft is (0-100), and what's missing. Used to reject generic or risky copy."""
    import radar_model as rm
    t = text or ""
    low = t.lower()
    issues, points = [], 0
    if rm.find_competitors(t):
        issues.append("Names a competitor product")
    if CLICHES.search(t):
        issues.append("Uses a stock phrase (" + CLICHES.search(t).group(0) + ")")
    words = len(t.split())
    if words > (b.get("max_words") or 120):
        issues.append(f"{words} words; keep it under {b.get('max_words')} for this reader")
    else:
        points += 15
    if b.get("company") and b["company"].lower().split()[0] in low:
        points += 15
    else:
        issues.append("Doesn't mention the company")
    hook_words = {w for w in re.findall(r"[a-z]{5,}", (b.get("hook") or "").lower())} - {"their", "which", "about", "posting", "signal", "record"}
    if hook_words and any(w in low for w in hook_words):
        points += 25
    elif b.get("hook"):
        issues.append("Doesn't use the account's trigger")
    persona_words = [w.lower() for w in b.get("words") or []] + re.findall(r"[a-z]{6,}", (b.get("pain") or "").lower())[:4]
    if any(w in low for w in persona_words):
        points += 20
    else:
        issues.append("Nothing speaks to what this role cares about")
    if b.get("product_label") and any(w in low for w in re.findall(r"[a-z]{5,}", b["product_label"].lower())) or \
       any(w in low for w in re.findall(r"[a-z]{6,}", (b.get("outcome") or "").lower())[:4]):
        points += 15
    else:
        issues.append("Doesn't say what Solix would do for them")
    asks = len(re.findall(r"\?", t))
    if step <= 2 and re.search(r"\b(demo|call|meeting|calendar|book|15.minute|30.minute)\b", low):
        issues.append("Asks for a meeting in an early touch: offer something to read or a question instead")
    else:
        points += 10
    if asks > 1:
        issues.append("More than one question")
    hard = [i for i in issues if i.startswith(("Names a competitor", "Uses a stock"))]
    score = max(0, min(100, points - 15 * len(hard)))
    return {"score": score, "ok": score >= 60 and not hard, "issues": issues}
