"""Sol's protection layer: keeps the concierge a Solix concierge.

People will try to use a public chatbot as a free general-purpose LLM (write
my code, my essay), to pull out its instructions, or to get at the keys and
models behind it. Three layers stop that, none of which relies on the model
behaving:

1. screen(): a pattern check on every visitor message *before* any model is
   called. Coding requests, prompt-injection and jailbreak attempts, probes for
   keys, models or infrastructure, hacking requests and "write my essay" style
   free-LLM use get a short, polite, canned reply that steers back to Solix.
   No provider is called, so no tokens are spent and no key is exposed.
2. The system prompt (knowledge.py) tells the model the same rules and carries
   a canary string.
3. StreamGuard: every streamed reply passes through it before it reaches the
   visitor or the database. It removes code blocks, redacts anything shaped
   like an API key, token or connection string (and the exact values of this
   server's secret environment variables), and cuts the reply if the system
   prompt's canary ever appears in it.

Rate limits, the per-visitor daily cap and the concurrency cap live in chat.py.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import List, Optional

# Appears only inside the system prompt. If it ever shows up in a reply, the model is
# repeating its instructions, and the reply is cut.
CANARY = "SOLX-CNRY-8E41"

_I = re.I

INJECTION = re.compile(
    r"(ignore|disregard|forget|override|bypass)\b.{0,40}\b(previous|prior|above|earlier|all|your|the|these|system)\b.{0,30}\b(instruction|prompt|rule|guideline|direction|polic)"
    r"|\b(system|hidden|initial|original|developer)\s+(prompt|message|instruction)"
    r"|\b(reveal|print|show|repeat|output|display|leak|dump|tell me)\b.{0,40}\b(your|the)\b.{0,20}\b(prompt|instructions|rules|configuration|config)\b"
    r"|\bdeveloper mode\b|\bjail ?break|\bDAN\b|\bdo anything now\b|\bgod mode\b|\bunfiltered\b|\buncensored\b"
    r"|\b(you are|you're) (now|no longer)\b|\bfrom now on,? (you|act|respond|answer)\b|\bpretend (to be|you are|you're)\b"
    r"|\bact as (an? |my )?(?!solix)(\w+ ){0,2}(assistant|ai|gpt|chatbot|model|terminal|interpreter|compiler|linux|python|shell|developer|hacker)\b"
    r"|<\|im_start\|>|<\|system\|>|\[/?INST\]|###\s*(system|instruction)|<\s*/?\s*system\s*>",
    _I,
)

SECRETS = re.compile(
    r"\b(give|share|send|tell|show|reveal|print|leak|dump|expose|list|what('?s| is| are))\b.{0,30}\b(api[\s_-]?keys?|apikeys?|access tokens?|secret keys?|"
    r"passwords?|credentials?|env(ironment)? var(iable)?s?|\.env|connection strings?|mongo(db)? ?(url|uri)s?)\b"
    r"|\b(your|sol'?s|this (bot|chat)'?s?|openai|groq|openrouter|gemini|anthropic|hugging ?face)\s+(api[\s_-]?)?(keys?|tokens?|credentials?|secrets?)\b"
    r"|\b(what|which)\b.{0,15}\b(llm|ai model|language model|model|provider|gpt)s?\b.{0,20}\b(are you|do you use|powers? (you|this|sol)|is behind|runs? (you|this|sol)|are behind|you run on|you based on)"
    r"|\b(are you|is this|r u)\s+(an? )?(gpt|chatgpt|claude|gemini|llama|mistral|groq|openai|deepseek|qwen)\b"
    r"|\byour (own )?(backend|servers?|infrastructure|source code|admin panel|tech stack|hosting|keys|tokens)\b|(?<![\w/])\.env\b",
    _I,
)

_NOUN = (r"(code|coding|script|scripts|function|program|class|snippet|regex|regexp|sql (query|queries|statement)|stored procedure|dockerfile|algorithm|"
         r"macro|vba|excel formula|terraform|yaml file|bash command|shell command|powershell|api call|curl command|unit tests?|leetcode|boilerplate)")
_LANG = r"(python|javascript|typescript|java|c\+\+|c#|golang|rust|php|ruby|bash|shell|sql|html|css|react|node(\.js)?|kotlin|swift|scala|perl|r)"
CODE_ASK = re.compile(
    r"\b(write|generate|create|give|make|build|fix|debug|refactor|optimi[sz]e|convert|complete|implement|code up|port)\b(\s+(me|us))?(\s+\S+){0,3}?\s+" + _NOUN + r"\b"
    r"|\b" + _LANG + r"\s+(code|script|program|function|snippet|class|query)\b"
    r"|\b(fix|debug|correct)\s+(my|this|the following)\s+(code|script|query|function|program|error|stack ?trace)\b"
    r"|\bhow (do|can|would|to) (i|you|we)?\s*(code|program|implement)\b.{0,40}\b(in|using|with) " + _LANG + r"\b",
    _I,
)
# Pasted code: fences, or lines that only code looks like.
CODE_PASTE = re.compile(
    r"```|^\s*(def |class \w+[:(]|import \w|from \S+ import |function\s*\w*\(|const \w+\s*=|let \w+\s*=|var \w+\s*=|public (static )?\w|#include|package \w|SELECT\s.+\sFROM\s|INSERT INTO|UPDATE \w+ SET|"
    r"CREATE (TABLE|PROCEDURE|FUNCTION)|<\?php|<script|\$\w+\s*=|for\s*\(.*;.*;)",
    _I | re.M,
)

HACKING = re.compile(
    r"\b(how (do|can|to|would)( i| we| you)?|help me|teach me|write|create|build|give me|show me|steps to|way to)\b.{0,40}\b(hack|exploit (a|the|this|their|your)|sql injection|sqli|xss|csrf|ddos|"
    r"malware|ransomware|keylogger|phishing (email|page|site|kit)|brute[- ]?force|crack (a |the |their |your )?password|bypass (the )?(auth|login|security|paywall|gate|form|rate limit)|"
    r"reverse shell|privilege escalation|zero[- ]day|exfiltrat|steal|scrape (all|your|the whole))",
    _I,
)

FREE_LLM = re.compile(
    r"\b(write|compose|draft|generate)\b(\s+(me|us))?(\s+\S+){0,3}?\s+(essay|poem|story|song|lyrics|novel|joke|cover letter|resume|cv|homework|assignment|thesis|tweet|caption|speech|"
    r"birthday (message|wish)|love letter|blog post about (?!solix))\b"
    r"|\bsolve (this|the|my) (equation|math|problem set|homework)|\bdo my (homework|assignment)\b",
    _I,
)
# Pasting a long text to translate or summarise is free-LLM use; asking Sol to sum up its own answer is not.
LONG_TEXT_TASK = re.compile(r"\b(translate|summari[sz]e|paraphrase|rewrite|proofread)\b", _I)

REPLIES = {
    "code": (
        "I can't help with writing or debugging code here: I'm Sol, Solix's concierge, and I stick to Solix and enterprise data questions. "
        "If the code is part of a data problem, though, tell me what you're trying to achieve (archiving, retiring an application, privacy, "
        "governance, getting data ready for AI) and I'll show you how Solix handles it, or connect you with a Solix engineer."
    ),
    "injection": (
        "I'll keep doing what I'm here for: helping with Solix and enterprise data questions. "
        "What are you working on: archiving, application retirement, data privacy, governance or enterprise AI?"
    ),
    "secrets": (
        "I don't share details about the systems, models or credentials behind this chat. "
        "Happy to help with anything about Solix, though: what problem are you looking to solve?"
    ),
    "hacking": (
        "That's not something I can help with. If you're working on securing or governing sensitive data, "
        "I can walk you through how Solix handles access control, masking, audit and compliance."
    ),
    "free_llm": (
        "I'm Sol, Solix's concierge, so I'll leave that one to a general-purpose assistant. "
        "Where I can really help is enterprise data: archiving, application retirement, privacy, governance and AI on governed data. What's on your plate?"
    ),
}


@dataclass
class Verdict:
    kind: str
    reply: str


def screen(message: str, *, block_code: bool = True) -> Optional[Verdict]:
    """A canned reply for a message no model should see, or None to let it through."""
    text = message.strip()
    if INJECTION.search(text):
        return Verdict("injection", REPLIES["injection"])
    if SECRETS.search(text):
        return Verdict("secrets", REPLIES["secrets"])
    if HACKING.search(text):
        return Verdict("hacking", REPLIES["hacking"])
    if block_code and (CODE_ASK.search(text) or len(CODE_PASTE.findall(text)) >= 2 or (CODE_PASTE.search(text) and len(text) > 300)):
        return Verdict("code", REPLIES["code"])
    if FREE_LLM.search(text) or (len(text) > 500 and LONG_TEXT_TASK.search(text[:200])):
        return Verdict("free_llm", REPLIES["free_llm"])
    return None


# --- output side ---
KEY_SHAPES = re.compile(
    r"\b(sk-(?:proj-|or-v1-|ant-)?[A-Za-z0-9_-]{16,}|gsk_[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|hf_[A-Za-z0-9]{20,}|nvapi-[A-Za-z0-9_-]{20,}"
    r"|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[abpr]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})"
    r"|(mongodb(\+srv)?|postgres(ql)?|mysql|redis)://\S+|\bBearer\s+[A-Za-z0-9._-]{20,}",
)
SECRET_ENV = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|MONGO_URL|DSN|PASS)$", re.I)
REDACTED = "[redacted]"
CODE_NOTE = "\n_(Code left out: I don't write code in this chat.)_\n"


def _secret_values() -> List[str]:
    vals = {v for k, v in os.environ.items() if SECRET_ENV.search(k) and v and len(v) >= 12}
    return sorted(vals, key=len, reverse=True)


def redact(text: str) -> str:
    for v in _secret_values():
        if v in text:
            text = text.replace(v, REDACTED)
    return KEY_SHAPES.sub(REDACTED, text)


class StreamGuard:
    """Cleans streamed text on the way out.

    Text is released up to the last whitespace, so a key or fence is always
    seen whole before any of it is sent; the unfinished word waits for the
    next chunk (or flush()).
    """

    MAX_HOLD = 400

    def __init__(self, block_code: bool = True):
        self.block_code = block_code
        self.pending = ""
        self.in_code = False
        self.noted = False
        self.stopped = False
        self.events: List[str] = []

    def _clean(self, text: str) -> str:
        if CANARY in text or CANARY in self.pending:
            self.stopped = True
            self.events.append("canary")
            return "\n\nI can't share that. Is there anything about Solix I can help with?"
        text = redact(text)
        if not self.block_code:
            return text
        out, parts = [], text.split("```")
        for i, part in enumerate(parts):
            if i > 0:
                self.in_code = not self.in_code
                if self.in_code and not self.noted:
                    out.append(CODE_NOTE)
                    self.noted = True
                    self.events.append("code_removed")
            if not self.in_code:
                out.append(part)
        return "".join(out)

    def feed(self, chunk: str) -> str:
        if self.stopped:
            return ""
        self.pending += chunk
        cut = max(self.pending.rfind(" "), self.pending.rfind("\n"), self.pending.rfind("\t"))
        if cut < 0 and len(self.pending) < self.MAX_HOLD:
            return ""
        if cut < 0:
            cut = len(self.pending) - 1
        ready, self.pending = self.pending[: cut + 1], self.pending[cut + 1:]
        return self._clean(ready)

    def flush(self) -> str:
        if self.stopped:
            return ""
        ready, self.pending = self.pending, ""
        out = self._clean(ready) if ready else ""
        if self.in_code:
            self.in_code = False
        return out
