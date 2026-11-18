"""Run the Radar's AI on free model tiers without sending them personal data.

Free tiers (Gemini free, Groq, Mistral Experiment, OpenRouter ":free" and others) may keep or train on prompts, so
prospect personal data must not reach them. Most of what the Radar asks a model about is company-level: job
postings, news, filings, the company's own pages, industry and product. This module strips what identifies a
person from that text before it leaves, and puts it back in the answer:

* email addresses, phone numbers and personal profile links (LinkedIn /in/, X, GitHub, etc.) become [EMAIL_1],
  [PHONE_1], [PROFILE_1];
* the names of people we know about (contacts, people found on leadership pages, the person who replied) become
  [PERSON_1], and so do "Firstname Lastname" pairs that sit next to a job title;
* the text after a sign-off ("Best,", "Regards", "--") is dropped from replies: signatures are mostly contact details.

What is never sent to a free tier at all: leadership pages (they are lists of people), and contact names or exact
job titles. For those jobs the Radar uses its rules (radar_intel.people_from_text) on our own server instead.

Providers cleared for personal data (LLM_PII_PROVIDERS: OpenAI, your own or a company endpoint) get the full text,
as before. Free mode is on by default; set RADAR_FREE_AI=off to use no free tier for Radar work at all.
"""
from __future__ import annotations

import os
import re
from typing import Iterable, List, Optional, Tuple

_EMAIL = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<![\w/])(?:\+?\d[\d\s().-]{7,}\d)(?![\w/])")
_PROFILE = re.compile(r"https?://(?:[a-z]{2,3}\.)?(?:linkedin\.com/in|twitter\.com|x\.com|github\.com|facebook\.com|instagram\.com)/[^\s)\"'>]+", re.I)
_TITLE_WORDS = (r"chief|officer|president|director|head|manager|lead|vp|vice president|cio|cto|cdo|ciso|cfo|ceo|coo|counsel|architect|engineer|"
                r"administrator|analyst|recruiter|partner|founder|owner|specialist|coordinator")
_NAME = r"[A-Z][a-z]+(?:[-'][A-Z][a-z]+)?(?: [A-Z]\.)? [A-Z][a-z]+(?:[-'][A-Z][a-z]+)?"
# "Jane Doe, Chief Information Officer" / "Jane Doe - CIO" / "CIO Jane Doe" / "contact Jane Doe". Names must be capitalised;
# only the title words ignore case.
_T = rf"(?i:{_TITLE_WORDS})"
_NAME_PATTERNS = [
    re.compile(rf"\b({_NAME})(?=\s*(?:,|-|–|—|\||\(|\bis\b|\bwas\b|\bas\b)\s*(?:(?i:the|our|a)\s+)?(?:\w+\s+){{0,3}}{_T}\b)"),
    re.compile(rf"\b{_T}[,:]?\s+({_NAME})\b"),
    re.compile(rf"\b(?i:contact|ask|email|call)\s+({_NAME})\b"),
]
_SIGNOFF = re.compile(r"^[ \t]*(?:--+|—|best( regards| wishes)?|kind regards|warm regards|regards|thanks|thank you|many thanks|cheers|sincerely|sent from my)\b[^\n]{0,25}$", re.I | re.M)
NOT_NAMES = {"united states", "new york", "san francisco", "los angeles", "north america", "data governance", "legal hold", "enterprise archiving",
             "application retirement", "information technology", "human resources", "general counsel", "vice president", "press release"}


def free_ai_enabled() -> bool:
    return (os.environ.get("RADAR_FREE_AI") or "on").strip().lower() not in ("off", "0", "false", "no")


class Redactor:
    """Replaces personal identifiers with numbered placeholders and puts them back afterwards."""

    def __init__(self, names: Optional[Iterable[str]] = None):
        self.map: dict = {}
        self.rev: dict = {}
        self.counts: dict = {}
        self.names = sorted({re.sub(r"\s+", " ", n).strip() for n in names or [] if n and len(str(n).split()) >= 2}, key=len, reverse=True)

    def _token(self, kind: str, value: str) -> str:
        key = (kind, value.lower())
        if key not in self.map:
            self.counts[kind] = self.counts.get(kind, 0) + 1
            tok = f"[{kind}_{self.counts[kind]}]"
            self.map[key] = tok
            self.rev[tok] = value
        return self.map[key]

    def redact(self, text: Optional[str], *, strip_signature: bool = False) -> str:
        t = text or ""
        if strip_signature:
            m = next((m for m in _SIGNOFF.finditer(t) if m.start() > 20), None)  # a short sign-off line after the message, not "Thanks, ..." opening it
            if m:
                t = t[:m.start()]
        t = _PROFILE.sub(lambda m: self._token("PROFILE", m.group(0)), t)
        t = _EMAIL.sub(lambda m: self._token("EMAIL", m.group(0)), t)
        t = _PHONE.sub(lambda m: self._token("PHONE", m.group(0)) if sum(ch.isdigit() for ch in m.group(0)) >= 9 else m.group(0), t)
        for name in self.names:
            t = re.sub(rf"\b{re.escape(name)}\b", lambda m: self._token("PERSON", m.group(0)), t, flags=re.I)
            first = name.split()[0]
            if len(first) >= 3:  # "Hi Jordan," after the full name was seen
                t = re.sub(rf"\b((?:hi|hello|dear|thanks|thank you)\s+)({re.escape(first)})\b", lambda m: m.group(1) + self._token("PERSON", m.group(2)), t, flags=re.I)

        for rx in _NAME_PATTERNS:
            def near(m):
                val = m.group(1)
                if val.lower() in NOT_NAMES or any(w.lower() in ("solix", "the", "our") for w in val.split()) or val.startswith("["):
                    return m.group(0)
                a, b = m.span(1)
                return m.group(0)[:a - m.start()] + self._token("PERSON", val) + m.group(0)[b - m.start():]
            t = rx.sub(near, t)
        return t

    def restore(self, text):
        if isinstance(text, str):
            return re.sub(r"\[(?:EMAIL|PHONE|PROFILE|PERSON)_\d+\]", lambda m: self.rev.get(m.group(0), m.group(0)), text)
        if isinstance(text, list):
            return [self.restore(x) for x in text]
        if isinstance(text, dict):
            return {k: self.restore(v) for k, v in text.items()}
        return text

    def leaks(self, text: str) -> List[str]:
        """Anything that still looks like an email or phone number after redaction (should be empty)."""
        return _EMAIL.findall(text or "") + [p for p in _PHONE.findall(text or "") if sum(ch.isdigit() for ch in p) >= 9]


INSTRUCTION = ("Some personal details were replaced with placeholders such as [PERSON_1] or [EMAIL_1]. Keep any placeholder exactly as written "
               "if you need to refer to it, never guess what it stands for, and never invent names or contact details.")


def providers_for(task: str) -> Tuple[list, str]:
    """(providers, mode) for a Radar AI task. mode: "private" (cleared for personal data), "redacted" (free tier, personal
    data removed first) or "off". Every task here works on company-level text, so redacted mode is safe for all of them."""
    import llm
    private = llm.pii_providers()
    if private:
        return private, "private"
    if free_ai_enabled():
        free = llm.configured_providers()
        if free:
            return free, "redacted"
    return [], "off"


def status() -> dict:
    """What the Live sources card shows."""
    import llm
    private = [p.name for p in llm.pii_providers()]
    free = [p.name for p in llm.configured_providers()]
    if private:
        return {"on": True, "mode": "private", "providers": private, "detail": "Cleared for personal data: " + ", ".join(private)}
    if free_ai_enabled() and free:
        return {"on": True, "mode": "redacted", "providers": free,
                "detail": "Free tiers (" + ", ".join(free) + "): names, emails, phone numbers and profile links are removed before anything is sent, "
                          "and leadership pages never leave the server"}
    return {"on": False, "mode": "off", "providers": [], "detail": "No model configured" if not free else "Free mode switched off (RADAR_FREE_AI=off)"}
