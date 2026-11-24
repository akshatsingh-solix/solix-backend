"""Country policy engine for cold B2B outreach (spec requirements C-08 to C-12).

There is no single EU rule: the ePrivacy Directive protects natural-person subscribers and leaves legal persons
to national law, so the right default is *allow only where the country's rule is known*, not "block Germany".
Each country gets one policy:

    ALLOW_OPT_OUT           cold B2B email to a named work address is allowed with a working opt-out
    ALLOW_IF_ROLE_RELEVANT  allowed only when the pitch relates to the person's job (a logged role-relevance basis)
    GENERIC_ADDRESS_ONLY    only generic role addresses (info@, it@...); named people need consent
    BLOCK_UNLESS_CONSENT    opt-in: blocked unless a consent record exists (who, when, wording, channel, proof)

Plus the UK corporate-subscriber rule (PECR reg. 22): only companies, LLPs and public bodies; sole traders,
partnerships and anyone we can't place are treated as individuals and blocked.

The defaults below come from the engineering spec's country table (section 9). Only the UK, Germany, France
and Poland rows were checked against a regulator page or current secondary source; the rest rest on a January
2024 secondary table and MUST be confirmed by local counsel before live sending. Every row is editable in
Admin -> Outbound -> Compliance, and unknown countries take the strictest rule.
"""
from __future__ import annotations

import re
from typing import Dict, Optional, Tuple

ALLOW_OPT_OUT = "ALLOW_OPT_OUT"
ALLOW_IF_ROLE_RELEVANT = "ALLOW_IF_ROLE_RELEVANT"
GENERIC_ADDRESS_ONLY = "GENERIC_ADDRESS_ONLY"
BLOCK_UNLESS_CONSENT = "BLOCK_UNLESS_CONSENT"
POLICIES = (ALLOW_OPT_OUT, ALLOW_IF_ROLE_RELEVANT, GENERIC_ADDRESS_ONLY, BLOCK_UNLESS_CONSENT)
STRICTNESS = {ALLOW_OPT_OUT: 0, ALLOW_IF_ROLE_RELEVANT: 1, GENERIC_ADDRESS_ONLY: 2, BLOCK_UNLESS_CONSENT: 3}

# country -> (policy, basis, verified against a primary/current source)
DEFAULT_TABLE: Dict[str, Tuple[str, str, bool]] = {
    "United Kingdom": (ALLOW_OPT_OUT, "PECR reg. 22: opt-out for corporate subscribers; sole traders and partnerships are opt-in (ICO)", True),
    "Germany": (BLOCK_UNLESS_CONSENT, "UWG s.7(2) no. 2: prior express consent, B2B included", True),
    "France": (ALLOW_IF_ROLE_RELEVANT, "CNIL: opt-out if the pitch relates to the recipient's profession", True),
    "Poland": (BLOCK_UNLESS_CONSENT, "PKE Art. 398: opt-in", True),
    "Ireland": (ALLOW_IF_ROLE_RELEVANT, "Opt-out if role-relevant; third-party lists opt-in (2024 table: confirm)", False),
    "Sweden": (ALLOW_IF_ROLE_RELEVANT, "Opt-out if role-relevant (2024 table: confirm)", False),
    "Finland": (ALLOW_IF_ROLE_RELEVANT, "Opt-in for individual addresses unless the role is essentially related and verifiable (2024 table: confirm)", False),
    "Norway": (GENERIC_ADDRESS_ONLY, "Opt-in for individual addresses; opt-out for generic ones (2024 table: confirm)", False),
    "Netherlands": (BLOCK_UNLESS_CONSENT, "Opt-in (2024 table: confirm)", False),
    "Belgium": (GENERIC_ADDRESS_ONLY, "Opt-in for individual B2B addresses (2024 table: confirm)", False),
    "Spain": (BLOCK_UNLESS_CONSENT, "Opt-in (2024 table: confirm)", False),
    "Italy": (BLOCK_UNLESS_CONSENT, "Opt-in (2024 table: confirm)", False),
    "Austria": (BLOCK_UNLESS_CONSENT, "Opt-in (2024 table: confirm)", False),
    "Switzerland": (BLOCK_UNLESS_CONSENT, "Opt-in (2024 table: confirm)", False),
    "Denmark": (BLOCK_UNLESS_CONSENT, "Opt-in (2024 table: confirm)", False),
    "United States": (ALLOW_OPT_OUT, "CAN-SPAM: opt-out, sender identity and postal address", True),
    "Canada": (ALLOW_IF_ROLE_RELEVANT, "CASL: implied consent only for conspicuously published, role-relevant addresses (confirm)", False),
    "Australia": (ALLOW_IF_ROLE_RELEVANT, "Spam Act: inferred consent for published, role-relevant addresses (confirm)", False),
}
EU_EEA = {"Austria", "Belgium", "Bulgaria", "Croatia", "Cyprus", "Czechia", "Czech Republic", "Denmark", "Estonia", "Finland", "France", "Germany", "Greece",
          "Hungary", "Ireland", "Italy", "Latvia", "Lithuania", "Luxembourg", "Malta", "Netherlands", "Poland", "Portugal", "Romania", "Slovakia", "Slovenia",
          "Spain", "Sweden", "Iceland", "Liechtenstein", "Norway", "Switzerland"}
UK_NAMES = {"United Kingdom", "England", "Scotland", "Wales", "Northern Ireland", "UK", "Great Britain"}
GENERIC_LOCAL = re.compile(r"^(info|contact|hello|enquiries|inquiries|sales|it|office|admin|team|support|procurement|purchasing|marketing|privacy|legal|compliance)$", re.I)
UK_CORPORATE = ("company", "llp", "plc", "public_body", "limited")
_CORP_NAME = re.compile(r"\b(ltd|limited|plc|llp|group|holdings|bank|insurance|council|nhs|university|authority|trust|corporation|corp|inc)\b", re.I)


def table(overrides: Optional[dict] = None) -> Dict[str, dict]:
    out = {c: {"policy": p, "basis": b, "verified": v, "source": "default"} for c, (p, b, v) in DEFAULT_TABLE.items()}
    for c, row in (overrides or {}).items():
        if isinstance(row, dict) and row.get("policy") in POLICIES:
            out[c] = {"policy": row["policy"], "basis": row.get("basis") or "Set by an admin", "verified": bool(row.get("verified")), "source": "admin",
                      "by": row.get("by"), "at": row.get("at")}
    return out


def policy_for(country: Optional[str], overrides: Optional[dict] = None, unknown: str = BLOCK_UNLESS_CONSENT) -> Tuple[str, str]:
    """(policy, basis) for one country. Unknown or unlisted EU/EEA countries take the strictest rule."""
    t = table(overrides)
    if not country:
        return unknown, "Country unknown: the strictest rule applies until it is set"
    c = "United Kingdom" if country in UK_NAMES else country
    if c in t:
        return t[c]["policy"], t[c]["basis"]
    if c in EU_EEA:
        return BLOCK_UNLESS_CONSENT, f"{c} is not in the reviewed table: EU/EEA defaults to opt-in until counsel confirms"
    return ALLOW_IF_ROLE_RELEVANT, f"{c} is not in the reviewed table: role-relevant B2B only, pending review"


def role_relevance(contact: dict) -> dict:
    """An explicit, logged basis tying what Solix sells to the person's job (C-10). Target personas (IT, data,
    records, compliance and application leadership) are relevant; anything else needs a person to say why."""
    if isinstance(contact.get("role_relevance"), dict) and contact["role_relevance"].get("set_by"):
        return contact["role_relevance"]  # a person's decision stands
    title = contact.get("title") or ""
    relevant = contact.get("persona") == "target"
    return {"relevant": relevant, "basis": (f"Title '{title}' owns the systems and records Solix archives, retires and governs" if relevant
                                            else f"Title '{title or 'unknown'}' is outside the roles Solix's offer relates to"), "set_by": None}


def uk_entity_ok(company: Optional[dict]) -> Tuple[bool, str]:
    """PECR reg. 22: corporate subscribers only. If unsure, treat as an individual (ICO)."""
    company = company or {}
    et = (company.get("entity_type") or "").lower()
    if et:
        return (et in UK_CORPORATE, f"Entity type is {et}")
    if _CORP_NAME.search(company.get("name") or "") or (company.get("employees") or 0) >= 50:
        return True, "Inferred corporate body (name or size): confirm the entity type on the account"
    return False, "UK entity type unknown: confirm it's a company, LLP or public body (sole traders and partnerships need consent)"


def evaluate(contact: dict, company: Optional[dict], countries: set, settings: dict) -> Tuple[list, list, dict]:
    """(blocking reasons, warnings, decision) for this recipient across every country they may be in."""
    overrides = settings.get("country_policies") or {}
    unknown = settings.get("unknown_country_policy") or BLOCK_UNLESS_CONSENT
    reasons, warnings = [], []
    checked = sorted(countries) or [None]
    decisions = []
    email = (contact.get("email") or "").strip().lower()
    local = email.split("@")[0] if "@" in email else ""
    consent = contact.get("consent") or {}
    rr = role_relevance(contact)
    for c in checked:
        pol, basis = policy_for(c, overrides, unknown)
        decisions.append({"country": c, "policy": pol, "basis": basis})
        where = c or "an unknown country"
        if pol == BLOCK_UNLESS_CONSENT:
            if not (consent.get("at") and consent.get("proof")):
                reasons.append(f"Outbound to {where} needs recorded consent ({basis})")
        elif pol == GENERIC_ADDRESS_ONLY:
            if not GENERIC_LOCAL.match(local) and not (consent.get("at") and consent.get("proof")):
                reasons.append(f"Only generic addresses can be emailed cold in {where} ({basis})")
        elif pol == ALLOW_IF_ROLE_RELEVANT:
            if not rr["relevant"]:
                reasons.append(f"{where} allows cold B2B email only when it relates to the person's role: {rr['basis']}")
        if c in UK_NAMES or c == "United Kingdom":
            ok, why = uk_entity_ok(company)
            if not ok:
                reasons.append(why)
            elif "Inferred" in why:
                warnings.append(why)
    return reasons, warnings, {"countries": decisions, "role_relevance": rr}
