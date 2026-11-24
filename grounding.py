"""Per-claim citation gate for outreach copy (spec G5, section 7).

Word overlap alone can't see the failure that matters in a sales email: a datasheet figure or a capability stated
wrongly *in the datasheet's own words*. So every draft is split into claims and each claim is checked against the
evidence it may rest on, the account's signals and approved deep dive for claims about them, and Solix's own
published content for claims about Solix:

1. Facts that must match exactly: every number, percentage, money amount, year and named product or system in a
   claim has to appear in the evidence. A wrong figure or an invented product fails here, whatever the wording.
2. Support: a claim about the prospect must share enough meaning with the signal evidence (stems, not raw words).
3. Optional judge: when a private model is configured (GROUNDING_JUDGE=1), each claim is ruled supported,
   partial or unsupported against its evidence only.

Any unsupported claim rejects the draft; scores are never averaged, because averaging hides the one bad claim.
"""
from __future__ import annotations

import json
import os
import re
from typing import Dict, List, Optional

import semantic

NUM = re.compile(r"(?<![\w.])(?:[$€£]\s?)?\d[\d,.]*\s?(?:%|percent|x|tb|pb|gb|k|m|bn|million|billion|years?|months?|weeks?|days?|hours?)?(?![\w])", re.I)
PROPER = re.compile(r"\b([A-Z][A-Za-z0-9&/+-]*(?:\s+(?:[A-Z][A-Za-z0-9&/+-]*|of|for|and))*)")
ALWAYS_OK = {"Solix", "Solix Technologies", "We", "I", "Our", "You", "Your", "Hi", "Hello", "Dear", "Best", "Thanks", "Regards", "Monday", "Tuesday", "Wednesday",
             "Thursday", "Friday", "January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December",
             "AI", "IT", "CIO", "CTO", "CDO", "VP", "SaaS", "ERP", "CRM", "GDPR", "HIPAA", "SOX"}
SUPPORT_MIN = 0.12  # cosine on stemmed features between a prospect claim and its evidence


def claims(text: str) -> List[str]:
    return [c.strip() for c in re.split(r"(?<=[.!?])\s+(?=[A-Z])", (text or "").strip()) if len(c.strip().split()) >= 3]


def _norm(x: str) -> str:
    return re.sub(r"[\s,]", "", x.lower()).replace("percent", "%")


def _facts(claim: str, allowed_names: set) -> Dict[str, List[str]]:
    nums = [n.strip() for n in NUM.findall(claim) if re.search(r"\d", n)]
    names = []
    for i, m in enumerate(PROPER.finditer(claim)):
        phrase = m.group(1).strip()
        first_word = m.start() == 0 or claim[:m.start()].rstrip().endswith((".", ":", "!", "?"))
        words = [w for w in phrase.split() if w not in ("of", "for", "and")]
        if first_word and len(words) == 1:
            continue  # a capital that only starts the sentence
        if phrase in ALWAYS_OK or all(w in ALWAYS_OK for w in words) or phrase.lower() in allowed_names:
            continue
        names.append(phrase)
    return {"numbers": nums, "names": names}


def check(text: str, *, prospect_evidence: List[str], solix_evidence: List[str], company: Optional[str] = None, person: Optional[str] = None) -> dict:
    """Ground every claim. `prospect_evidence`: signal and deep-dive text; `solix_evidence`: Solix site/product text."""
    allowed = {x.lower() for x in (company, person) if x}
    allowed |= {w.lower() for x in (company, person) if x for w in x.split()}
    pro = " \n".join(e for e in prospect_evidence if e)
    sol = " \n".join(e for e in solix_evidence if e)
    everything = (pro + " \n" + sol).lower()
    flat = _norm(everything)
    out, problems = [], []
    idx = semantic.VectorIndex()
    for e in prospect_evidence:
        if e:
            idx.add(e, {})
    idx.build(max_df=1.0)
    for c in claims(text):
        f = _facts(c, allowed)
        bad_nums = [n for n in f["numbers"] if _norm(n) not in flat and _norm(re.sub(r"[^\d.]", "", n)) not in flat]
        bad_names = [n for n in f["names"] if n.lower() not in everything]
        about_prospect = bool(company and company.split()[0].lower() in c.lower()) or bool(re.search(r"\b(you|your|your team)\b", c, re.I)) and not re.search(r"\bsolix\b", c, re.I)
        support = semantic.best(idx, c) if about_prospect and prospect_evidence else None
        verdict = "supported"
        why = []
        if bad_nums:
            verdict = "unsupported"
            why.append(f"figure(s) not in the evidence: {', '.join(bad_nums)}")
        if bad_names:
            verdict = "unsupported"
            why.append(f"name(s) not in the evidence: {', '.join(bad_names)}")
        if about_prospect and support is not None and support < SUPPORT_MIN and verdict == "supported":
            verdict = "unsupported"
            why.append("says something about them the signals don't show")
        out.append({"claim": c, "verdict": verdict, "why": why, "support": None if support is None else round(support, 3)})
        if verdict != "supported":
            problems.append(f"\"{c[:80]}\": {'; '.join(why)}")
    return {"ok": not problems, "claims": out, "problems": problems, "method": "rules"}


async def judge(result: dict, prospect_evidence: List[str], solix_evidence: List[str]) -> dict:
    """Optional model pass over claims the rules let through. Only on a private provider, only with GROUNDING_JUDGE=1."""
    if os.environ.get("GROUNDING_JUDGE", "").lower() not in ("1", "true", "yes") or not result.get("ok"):
        return result
    import llm
    import privacy
    providers, mode = privacy.providers_for("opener")
    if not providers or mode == "redacted":
        return result
    ev = "\n".join(f"- {e[:600]}" for e in (prospect_evidence + solix_evidence)[:8])
    for row in result["claims"]:
        out = []
        try:
            async for e in llm.stream_completion([{"role": "system", "content": "You check one sentence from a sales email against evidence. Reply JSON only: "
                                                   '{"verdict": "supported" | "partial" | "unsupported", "why": "..."}. Supported means every fact in the sentence is stated in the evidence.'},
                                                  {"role": "user", "content": f"Evidence:\n{ev}\n\nSentence: {row['claim']}"}], max_tokens=120, temperature=0, providers=providers):
                if e["type"] == "text":
                    out.append(e["text"])
            raw = "".join(out)
            data = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
        except Exception:  # noqa: BLE001 - a judge that can't answer leaves the rules' verdict
            continue
        if data.get("verdict") in ("unsupported", "partial"):
            row["verdict"] = data["verdict"]
            row["why"].append(f"judge: {data.get('why') or data['verdict']}")
            result["problems"].append(f"\"{row['claim'][:80]}\": judge says {data['verdict']}")
    result["ok"] = not result["problems"]
    result["method"] = "rules+judge"
    return result
