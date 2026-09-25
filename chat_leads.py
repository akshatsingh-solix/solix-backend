"""Lead capture from Sol conversations.

Any contact detail a visitor types (email, phone, name, company, role) is
saved the moment it appears, not only when a demo is booked:

- extract_contact() reads each visitor message with plain pattern matching,
  so capture never depends on the AI model noticing (or being up at all);
- the model can add what it understands from context through the
  save_visitor_details tool;
- capture() keeps one "chat" submission per conversation in the form inbox,
  and as soon as it holds an email the visitor becomes (or updates) a scored
  lead on the Leads page, with one sales alert.
"""
import asyncio
import logging
import re
import uuid
from typing import Optional

from database import db, now_iso
from emailer import notify_lead
from intent import record_submission

logger = logging.getLogger("solix.chat_leads")

CONTACT_FIELDS = ("name", "email", "phone", "company", "job_title")
EMAIL_RX = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")
PHONE_RX = re.compile(r"(?<![\w@.])\+?\(?\d[\d\s().-]{6,18}\d(?![\w@])")
# Strong lead-ins: whatever follows is the name, in any case.
NAME_STRONG_RX = re.compile(r"\b(?i:my name is|my name's|name is|call me|name\s*:)\s*([A-Za-z][A-Za-z'’-]*(?:\s+[A-Za-z][A-Za-z'’-]*){0,2})")
# Weak lead-ins ("I'm ...", "this is ...") only count when the words are capitalised like a name.
NAME_WEAK_RX = re.compile(r"(?:^|[\s,.!])(?i:i am|i'm|im|this is|it's|its)\s+([A-Z][a-z'’-]+(?:\s+[A-Z][a-z'’-]+){0,2})\b")
COMPANY_RX = re.compile(
    r"\b(?i:i work (?:at|for|with)|i'm (?:at|with)|i am (?:at|with)|working (?:at|for)|our company is|company is|company\s*:|organi[sz]ation is|employer is)\s*"
    r"([A-Z0-9][\w&.'’-]*(?:\s+(?:[A-Z0-9&][\w&.'’-]*|of|and))*)"
)
# "I'm Priya from Acme": a company introduced right after the name.
NAME_THEN_COMPANY_RX = re.compile(r"\s+(?i:from|at|with|of)\s+([A-Z0-9][\w&.'’-]*(?:\s+[A-Z0-9&][\w&.'’-]*){0,3})")
TITLE_RX = re.compile(r"\b(?i:i'm the|i am the|i'm a|i am a|i'm an|i am an|my role is|my title is|i work as an?|working as an?)\s+([A-Za-z][A-Za-z &/-]{2,60}?)(?=\s+(?i:at|for|in|with|from)\b|[.,!?]|$)")
TITLE_WORDS = ("chief", "cio", "cto", "cdo", "ciso", "cfo", "ceo", "coo", "vp", "vice president", "head", "director", "manager", "architect", "engineer",
               "analyst", "lead", "officer", "consultant", "admin", "administrator", "owner", "founder", "president", "specialist", "counsel", "partner")
NOT_NAMES = {
    "looking", "interested", "from", "with", "working", "here", "just", "not", "happy", "good", "fine", "sorry", "trying", "new", "curious", "planning",
    "evaluating", "currently", "also", "a", "an", "the", "in", "on", "at", "sure", "ok", "okay", "yes", "no", "thanks", "thank", "great", "glad",
    "wondering", "asking", "calling", "writing", "reaching", "hoping", "going", "using", "responsible", "based", "located", "part", "on", "back",
    "ready", "done", "sol", "solix", "hi", "hello", "hey", "still", "very", "really", "so", "too", "able", "unable", "free", "busy", "tired",
}
STOP_TAIL = {"from", "at", "and", "with", "working", "here", "of", "in", "for", "the", "i", "my", "we", "our", "to", "is", "am", "a", "an"}
FREE_MAIL = {
    "gmail", "googlemail", "yahoo", "ymail", "outlook", "hotmail", "live", "msn", "icloud", "me", "mac", "aol", "proton", "protonmail", "pm", "gmx",
    "zoho", "yandex", "mail", "qq", "163", "126", "rediffmail", "rediff", "fastmail", "hey", "tutanota", "example", "test",
}


def _words(raw: str, capitalised: bool, person: bool = True) -> Optional[str]:
    kept = []
    for w in raw.split():
        w = w.strip(".,;:!?'’\"")
        if not w or w.lower() in STOP_TAIL:
            break
        kept.append(w)
    if not kept or (person and kept[0].lower() in NOT_NAMES):
        return None
    if capitalised and not all(w[0].isupper() for w in kept):
        return None
    return " ".join(w[:1].upper() + w[1:] for w in kept)[:80]


def _phone(text: str) -> Optional[str]:
    for m in PHONE_RX.finditer(text):
        raw = m.group(0).strip()
        digits = re.sub(r"\D", "", raw)
        # 8-15 digits, and not a year range such as 2019-2024 or a plain big number like 150000.
        if not 8 <= len(digits) <= 15 or re.fullmatch(r"(19|20)\d{2}\s*[-–]\s*(19|20)\d{2}", raw):
            continue
        if not re.search(r"[\s().+-]", raw) and len(digits) < 10:
            continue
        return raw[:30]
    return None


def company_from_email(email: str) -> Optional[str]:
    """acme-corp.com -> "Acme Corp"; nothing for Gmail, Outlook and other free mail."""
    parts = email.split("@", 1)[-1].lower().split(".")
    if len(parts) < 2:
        return None
    label = parts[-3] if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "ac", "gov") else parts[-2]
    if label in FREE_MAIL or len(label) < 2:
        return None
    return " ".join(p.capitalize() for p in re.split(r"[-_]", label) if p)[:80] or None


def extract_contact(text: str) -> dict:
    """Contact details a visitor wrote in one message (empty dict when none)."""
    if not text:
        return {}
    found = {}
    m = EMAIL_RX.search(text)
    if m:
        found["email"] = m.group(0).strip(".").lower()
    scrubbed = EMAIL_RX.sub(" ", text)
    phone = _phone(scrubbed)
    if phone:
        found["phone"] = phone
    m = NAME_STRONG_RX.search(scrubbed)
    name = _words(m.group(1), capitalised=False) if m else None
    end = m.end() if (m and name) else None
    if not name:
        m = NAME_WEAK_RX.search(scrubbed)
        name = _words(m.group(1), capitalised=True) if m else None
        end = m.end() if (m and name) else None
    if name:
        found["name"] = name
        after = NAME_THEN_COMPANY_RX.match(scrubbed[end:])
        if after:
            company = _words(after.group(1), capitalised=True, person=False)
            if company:
                found["company"] = company
    m = COMPANY_RX.search(scrubbed)
    if m:
        company = _words(m.group(1), capitalised=False, person=False)
        if company:
            found["company"] = company
    m = TITLE_RX.search(scrubbed)
    if m and any(t in m.group(1).lower() for t in TITLE_WORDS):
        found["job_title"] = m.group(1).strip()[:80]
        after = NAME_THEN_COMPANY_RX.match(scrubbed[m.end(1):])
        company = _words(after.group(1), capitalised=True, person=False) if after else None
        if company and "company" not in found:
            found["company"] = company
    return found


def _clean(fields: dict) -> dict:
    out = {}
    for k in CONTACT_FIELDS + ("interest", "notes"):
        v = str(fields.get(k) or "").strip()
        if not v:
            continue
        if k == "email":
            m = EMAIL_RX.search(v)
            if not m:
                continue
            v = m.group(0).lower()
        out[k] = v[:300 if k == "notes" else 120]
    return out


async def get_contact(session_id: str) -> Optional[dict]:
    return await db.submissions.find_one({"type": "chat", "source_page": f"chat:{session_id}"}, {"_id": 0})


async def capture(session_id: str, fields: dict, *, trusted: bool, page: Optional[str] = None, language: Optional[str] = None,
                  visitor_id: Optional[str] = None, alert: bool = True) -> dict:
    """Merge contact details into this conversation's lead record.

    `trusted` values (from the model or a booking) replace earlier ones; values
    from pattern matching only fill gaps. Sales is alerted when the first email
    arrives, unless the caller sends its own alert (`alert=False`, bookings).
    Returns the contact fields that changed.
    """
    fields = _clean(fields)
    if not fields:
        return {}
    now = now_iso()
    doc = await get_contact(session_id)
    is_new = doc is None
    if is_new:
        doc = {"id": str(uuid.uuid4()), "type": "chat", "source": "chat", "source_page": f"chat:{session_id}", "session_id": session_id,
               "page": page, "language": language, "created_at": now}
    had_email = bool(doc.get("email"))
    changed = {}
    for k, v in fields.items():
        cur = doc.get(k)
        if k == "notes":
            if v not in (cur or ""):
                changed[k] = f"{cur}\n{v}".strip()[-1000:] if cur else v
        elif not cur or (trusted and cur != v) or (k in ("email", "phone") and cur != v):
            changed[k] = v
    if not doc.get("company") and "company" not in changed:
        email = changed.get("email") or doc.get("email")
        guess = company_from_email(email) if email else None
        if guess:
            changed["company"] = guess
    if not changed:
        return {}
    if changed.get("notes"):
        changed["message"] = changed["notes"]
    if visitor_id:
        doc["visitor_id"] = visitor_id[:64]
    doc.update(changed)
    doc["updated_at"] = now
    doc["captured"] = sorted(set(doc.get("captured") or []) | {k for k in changed if k in CONTACT_FIELDS})
    await db.submissions.update_one({"id": doc["id"]}, {"$set": doc}, upsert=True)
    if doc.get("email"):
        try:
            await record_submission(doc, visitor_id=doc.get("visitor_id"), topics=[doc["interest"]] if doc.get("interest") else None,
                                    extra={"language": doc.get("language")} if doc.get("language") else None, tags=["chat"])
        except Exception:
            logger.exception("lead update failed for chat %s", session_id)
        if not had_email and alert:
            asyncio.create_task(notify_lead(dict(doc)))
    logger.info("chat %s captured %s", session_id, ",".join(sorted(changed)))
    return {k: v for k, v in changed.items() if k in CONTACT_FIELDS}
