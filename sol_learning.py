"""How Sol gets to know each visitor and keeps getting smarter.

Visitor profile (personalisation)
    Every message is read for the facts a consultant would note: industry,
    systems in play (SAP ECC, Oracle EBS, mainframe ...), the goal (cost,
    compliance, migration, AI, retirement, privacy), data volume, timeline and
    the buying stage. The model adds what it understands through the
    save_visitor_details tool. The profile is fed back to Sol every turn, so
    answers are about *this* visitor's problem, and a returning visitor (same
    analytics-consented visitor id) is recognised in a new chat.

Knowledge gaps -> learned knowledge (the knowledge base grows from chats)
    A question the site's content can't answer well is logged as a gap with
    how often it's asked and what Sol said. In Admin > Conversations >
    Knowledge, one click turns a gap (or any good answer) into learned
    knowledge that Sol uses from the next message on. Learning goes through
    that review on purpose: anything a visitor types is untrusted, and
    letting it straight into the knowledge base would let anyone teach Sol
    false prices or claims.

Answer cache (latency, consistency, cost)
    A visitor's opening question that's been answered before, against the
    same knowledge, is served from MongoDB instantly, at no token cost, with
    the same answer every time. Anything personal or mid-conversation always
    goes to the model.
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import database
from database import now_iso
import sol_search

logger = logging.getLogger("solix.sol_learning")

PROFILE_FIELDS = ("industry", "systems", "goals", "data_volume", "timeline", "stage", "problem", "role_focus")

INDUSTRIES = {
    "healthcare": r"health ?care|hospital|clinic|patient|payer|provider network|hipaa|medical",
    "life sciences": r"pharma|life sciences?|biotech|clinical trial|gxp|21 cfr",
    "financial services": r"\bbank|banking|financial services|fintech|lending|wealth|capital markets|insurer|insurance|sox\b|finra|sec 17a-4",
    "public sector": r"government|public sector|federal|state agency|municipal|county|ministry|defen[cs]e",
    "retail": r"retail|e-?commerce|consumer goods|cpg|store chain",
    "manufacturing": r"manufactur|factory|plant floor|automotive|industrial",
    "telecom": r"telecom|telco|carrier|mobile operator|isp\b",
    "energy & utilities": r"utility|utilities|energy|oil and gas|oil & gas|power grid",
    "education": r"universit|college|school district|higher ed|education",
    "technology": r"\bsaas\b|software company|tech company|high tech",
}
SYSTEMS = {
    "SAP ECC": r"\becc\b|sap ecc|r/3", "SAP S/4HANA": r"s/?4 ?hana|\bs4\b", "SAP": r"\bsap\b",
    "Oracle E-Business Suite": r"oracle e-?business|\bebs\b|oracle apps", "PeopleSoft": r"peoplesoft", "JD Edwards": r"jd ?edwards|\bjde\b",
    "Siebel": r"siebel", "Salesforce": r"salesforce", "Workday": r"workday", "Microsoft Dynamics": r"dynamics (ax|crm|365|nav|gp)",
    "Mainframe": r"mainframe|\bcobol\b|\bz/os\b|\bvsam\b|\bims\b", "Oracle Database": r"oracle (db|database)", "SQL Server": r"sql server|mssql",
    "DB2": r"\bdb2\b", "Teradata": r"teradata", "Hadoop": r"hadoop|cloudera|hortonworks", "SharePoint": r"sharepoint", "Email": r"exchange server|\bemail archiv|lotus notes",
    "File shares": r"file shares?|\bnas\b|unstructured (data|files)", "Snowflake": r"snowflake", "Databricks": r"databricks", "AWS": r"\baws\b|amazon web services|\bs3\b",
    "Azure": r"\bazure\b", "Google Cloud": r"\bgcp\b|google cloud",
}
GOALS = {
    "cut cost": r"cost|budget|licen[cs]|save money|savings|expensive|tco|storage spend",
    "compliance & retention": r"complian|retention|legal hold|e-?discovery|audit|regulat|records management|sox|hipaa|finra",
    "privacy": r"gdpr|ccpa|dsar|\bpii\b|personal data|privacy|consent|masking|right to be forgotten",
    "retire legacy apps": r"retire|decommission|sunset|shut ?down|legacy app|end of life|eol\b",
    "migration & cloud": r"migrat|move to (the )?cloud|cloud first|s/?4 ?hana move|upgrade|consolidat",
    "AI & analytics": r"\bai\b|genai|\bllm|rag\b|copilot|agents?\b|machine learning|analytics|data lake|lakehouse|insight",
    "performance": r"slow|performance|database growth|bloat|response time|batch window",
    "governance": r"governance|catalog|lineage|data quality|stewardship|metadata",
}
VOLUME = re.compile(r"\b(\d+(?:\.\d+)?)\s?(tb|pb|gb|terabytes?|petabytes?)\b", re.I)
TIMELINE = re.compile(r"\b(this|next) (quarter|month|year)|\bq[1-4]\b|\bby (the )?end of (the )?(year|quarter|month)|\bin (\d+|a few|six|three) (weeks?|months?)|\burgent|\basap\b|\bdeadline|\b20[2-3]\d\b", re.I)
DECISION = re.compile(r"\b(pric|cost|quote|proposal|budget|demo|trial|poc|proof of concept|contract|procure|rfp|timeline|implement|get started|talk to (sales|someone)|next steps?)", re.I)
CONSIDER = re.compile(r"\b(compare|vs\.?|versus|alternative|better than|differen|evaluate|evaluating|shortlist|options)\b", re.I)


def _pick(table: Dict[str, str], text: str) -> List[str]:
    return [name for name, rx in table.items() if re.search(rx, text, re.I)]


def read_message(text: str) -> dict:
    """What a single visitor message says about their situation (pattern matching, no model)."""
    found: dict = {}
    if ind := _pick(INDUSTRIES, text):
        found["industry"] = ind[0]
    if sysm := _pick(SYSTEMS, text):
        # "SAP ECC" makes the bare "SAP" redundant.
        found["systems"] = [s for s in sysm if not (s == "SAP" and any(x.startswith("SAP ") for x in sysm))]
    if goals := _pick(GOALS, text):
        found["goals"] = goals
    if m := VOLUME.search(text):
        found["data_volume"] = m.group(0)
    if m := TIMELINE.search(text):
        found["timeline"] = m.group(0)
    if DECISION.search(text):
        found["stage"] = "decision"
    elif CONSIDER.search(text):
        found["stage"] = "consideration"
    return found


STAGE_RANK = {"awareness": 0, "consideration": 1, "decision": 2}


def merge(profile: dict, found: dict) -> dict:
    out = dict(profile)
    for k, v in found.items():
        if not v:
            continue
        if isinstance(v, list) or k in ("systems", "goals"):
            items = v if isinstance(v, list) else [x.strip() for x in str(v).split(",") if x.strip()]
            out[k] = list(dict.fromkeys([*(out.get(k) or []), *items]))[:8]
        elif k == "stage":
            if STAGE_RANK.get(v, 0) >= STAGE_RANK.get(out.get("stage", "awareness"), 0):
                out[k] = v
        else:
            out[k] = str(v)[:300]
    return out


async def get_profile(session_id: str, visitor_id: Optional[str] = None) -> dict:
    doc = await database.db.sol_profiles.find_one({"session_id": session_id}, {"_id": 0})
    if doc:
        return doc
    if visitor_id:
        # A returning visitor: start from what they told Sol last time.
        prev = await database.db.sol_profiles.find({"visitor_id": visitor_id}, {"_id": 0}).sort("updated_at", -1).to_list(1)
        if prev:
            return {**{k: prev[0][k] for k in PROFILE_FIELDS if prev[0].get(k)}, "returning": True}
    return {}


async def update_profile(session_id: str, found: dict, visitor_id: Optional[str] = None, current: Optional[dict] = None) -> dict:
    current = current if current is not None else await get_profile(session_id, visitor_id)
    merged = merge({k: current.get(k) for k in PROFILE_FIELDS if current.get(k)}, {k: found.get(k) for k in PROFILE_FIELDS if found.get(k)})
    if current.get("returning"):
        merged["returning"] = True
    if merged != {k: current.get(k) for k in merged} or not current.get("session_id"):
        await database.db.sol_profiles.update_one(
            {"session_id": session_id},
            {"$set": {**merged, "visitor_id": visitor_id or current.get("visitor_id"), "updated_at": now_iso()}, "$setOnInsert": {"session_id": session_id, "created_at": now_iso()}},
            upsert=True,
        )
    return merged


def describe_profile(p: dict) -> str:
    """The profile as one line for the system prompt."""
    parts = []
    if p.get("returning"):
        parts.append("returning visitor (this is what they told you before)")
    for k, label in (("problem", "problem"), ("industry", "industry"), ("systems", "systems"), ("goals", "goals"), ("data_volume", "data volume"),
                     ("timeline", "timeline"), ("role_focus", "role"), ("stage", "buying stage")):
        v = p.get(k)
        if v:
            parts.append(f"{label}: {', '.join(v) if isinstance(v, list) else v}")
    return "; ".join(parts)


def next_gap(p: dict) -> Optional[str]:
    """The one discovery question worth asking next, so Sol qualifies like a good consultant."""
    if not p.get("goals") and not p.get("problem"):
        return "what they're trying to achieve (cut cost, compliance, retire legacy apps, migrate, privacy or AI)"
    if not p.get("systems"):
        return "which systems or data sources are involved"
    if not p.get("industry"):
        return "their industry (it changes the regulations and the proof points)"
    if not p.get("data_volume") and not p.get("timeline"):
        return "rough data volume or timeline, to size the solution"
    return None


# --- knowledge gaps and learned knowledge ---
GAP_SCORE = 6.0  # retrieval score below which site content probably doesn't cover the question


def _terms(text: str) -> str:
    """Order- and phrasing-insensitive form of a question ("what's X" == "What is X?")."""
    return " ".join(sorted({t for t in sol_search.tokenize(text.replace("'", " ").replace("’", " ")) if len(t) > 1}))


def question_key(text: str) -> str:
    terms = _terms(text)
    return hashlib.sha1(terms.encode()).hexdigest()[:20]


async def log_gap(question: str, answer: str, top_score: float, session_id: str) -> None:
    if len(sol_search.tokenize(question)) < 2:
        return
    key, now = question_key(question), now_iso()
    await database.db.sol_gaps.update_one(
        {"key": key},
        {"$set": {"question": question[:500], "sample_answer": answer[:2000], "top_score": top_score, "last_at": now, "last_session": session_id},
         "$inc": {"count": 1}, "$setOnInsert": {"id": str(uuid.uuid4()), "key": key, "status": "open", "first_at": now}},
        upsert=True,
    )


async def learned_chunks() -> List[dict]:
    try:
        docs = await database.db.sol_learned.find({"active": {"$ne": False}}, {"_id": 0}).to_list(2000)
    except Exception:
        logger.exception("could not load learned knowledge")
        return []
    return [{"id": f"learned:{d['id']}", "kind": "learned", "title": d["question"][:160], "url": d.get("url") or "",
             "text": f"Approved answer to \"{d['question']}\":\n{d['answer']}"} for d in docs]


async def add_learned(question: str, answer: str, url: str, by: str, source: str = "manual", gap_id: Optional[str] = None) -> dict:
    doc = {"id": str(uuid.uuid4()), "question": question.strip(), "answer": answer.strip(), "url": url.strip(), "source": source,
           "gap_id": gap_id, "active": True, "created_at": now_iso(), "created_by": by}
    await database.db.sol_learned.insert_one(dict(doc))
    if gap_id:
        await database.db.sol_gaps.update_one({"id": gap_id}, {"$set": {"status": "learned", "learned_id": doc["id"], "resolved_at": now_iso(), "resolved_by": by}})
    sol_search.invalidate()
    return doc


# --- answer cache ---
_mem: Dict[str, tuple] = {}
MEM_MAX = 500


def _ordered_terms(text: str) -> str:
    """Word order kept ("migrate from Oracle to SAP" is not "from SAP to Oracle"), negations kept."""
    t = text.lower().replace("’", "'")
    t = re.sub(r"\b(\w+)n't\b", r"\1 not", t).replace("'s ", " is ").replace("'re ", " are ")
    return " ".join(re.findall(r"[a-z0-9&/+.-]+", t)).strip(" .")


def cache_key(message: str, language: str) -> str:
    from knowledge import PROMPT_VERSION  # answers written under an older prompt are never served again
    return hashlib.sha1(f"{language}|{PROMPT_VERSION}|{sol_search.version()}|{_ordered_terms(message)}".encode()).hexdigest()


async def cached_answer(message: str, language: str) -> Optional[dict]:
    if len(sol_search.tokenize(message)) < 2:
        return None  # "hi", "thanks": let the model be conversational
    key = cache_key(message, language)
    hit = _mem.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    try:
        doc = await database.db.sol_answer_cache.find_one({"key": key, "expires_at": {"$gt": datetime.now(timezone.utc)}}, {"_id": 0})
    except Exception:
        return None
    if doc:
        _mem[key] = (doc.get("exp_ts") or time.time() + 3600, doc)
        await database.db.sol_answer_cache.update_one({"key": key}, {"$inc": {"hits": 1}})
    return doc


async def store_answer(message: str, language: str, answer: str, sources: List[dict], provider: str, model: str, hours: int) -> None:
    key = cache_key(message, language)
    expires = datetime.now(timezone.utc) + timedelta(hours=hours)
    doc = {"key": key, "question": message[:500], "language": language, "answer": answer, "sources": sources, "provider": provider, "model": model,
           "created_at": now_iso(), "expires_at": expires, "exp_ts": expires.timestamp(), "hits": 0}
    if len(_mem) >= MEM_MAX:
        _mem.pop(next(iter(_mem)))
    _mem[key] = (expires.timestamp(), doc)
    try:
        await database.db.sol_answer_cache.update_one({"key": key}, {"$set": doc}, upsert=True)
    except Exception:
        logger.exception("could not store a cached answer")


async def clear_cache() -> int:
    _mem.clear()
    res = await database.db.sol_answer_cache.delete_many({})
    return res.deleted_count
