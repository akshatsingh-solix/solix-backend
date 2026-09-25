"""Retrieval over the site's own content for Sol.

sol_knowledge.json is generated from the website repo's data files by
frontend/scripts/sol-knowledge.js (copy it here after regenerating).
Published CMS content (the `content` collection) is merged in and refreshed
every few minutes, so articles published in the admin are answerable without
a redeploy; gated items contribute only their summary. Everything is ranked with BM25 (the keyword ranking
behind most search engines), in-process: no embedding API to pay for or rate
limit, no vector database, and it fits easily in a free instance's memory.
"""
import json
import logging
import math
import re
import time
from datetime import datetime, timezone
from collections import Counter
from pathlib import Path
from typing import List, Optional

KNOWLEDGE_FILE = Path(__file__).parent / "sol_knowledge.json"
CMS_REFRESH_SECONDS = 600
logger = logging.getLogger("solix.sol_search")

STOP = set(
    "a an and are as at be but by can do does for from has have how i if in into is it its me my of on or our so that the their them then there these "
    "they this to us was we what when where which who why will with you your about any all also more most not only other some such than too very "
    "would could should tell show give get want need know like just solix sol please hi hello hey".split()
)
# Words visitors use for things the site names differently.
SYNONYMS = {
    "price": "pricing cost", "pricing": "cost", "cost": "savings", "costs": "savings", "cheap": "cost",
    "decommission": "retirement retire", "sunset": "retirement retire", "legacy": "retirement",
    "gdpr": "privacy", "ccpa": "privacy", "dsar": "privacy", "pii": "privacy",
    "llm": "ai", "genai": "ai", "copilot": "ai agents", "chatbot": "ai",
    "job": "careers roles", "jobs": "careers roles", "hiring": "careers roles", "career": "careers roles",
    "office": "offices", "address": "offices headquarters", "phone": "offices contact",
    "partner": "partners", "reseller": "partners distribution", "press": "media",
    "demo": "contact", "trial": "free trial", "ecc": "sap", "s4": "s/4hana", "hana": "s/4hana",
}


def tokenize(text: str) -> List[str]:
    out = []
    for w in re.findall(r"[a-z0-9][a-z0-9/+.-]*[a-z0-9]|[a-z0-9]", text.lower()):
        if w in STOP:
            continue
        # Light stemming so "archives"/"archiving"/"archived" meet.
        for suf in ("ing", "ed", "es", "s"):
            if len(w) > 4 + len(suf) - 1 and w.endswith(suf):
                w = w[: -len(suf)]
                break
        out.append(w)
    return out


class Index:
    def __init__(self, chunks: List[dict], k1: float = 1.4, b: float = 0.7):
        self.chunks = chunks
        self.k1, self.b = k1, b
        # Titles count twice: a chunk titled "Application Retirement" is about it.
        self.docs = [Counter(tokenize(f"{c['title']} {c['title']} {c['text']}")) for c in chunks]
        self.lens = [sum(d.values()) for d in self.docs]
        self.avg = (sum(self.lens) / len(self.lens)) if self.lens else 1
        df = Counter(t for d in self.docs for t in d)
        n = len(chunks)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def _expand(self, query: str) -> List[str]:
        terms = tokenize(query)
        extra = [s for w in query.lower().split() for s in tokenize(SYNONYMS.get(w.strip("?.,!"), ""))]
        return terms + extra

    def search(self, query: str, k: int = 5, kinds: Optional[List[str]] = None) -> List[dict]:
        terms = self._expand(query)
        if not terms:
            return []
        scored = []
        for i, d in enumerate(self.docs):
            if kinds and self.chunks[i]["kind"] not in kinds:
                continue
            s = 0.0
            for t in terms:
                f = d.get(t)
                if f:
                    s += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * self.lens[i] / self.avg))
            if s > 0:
                scored.append((s, i))
        scored.sort(reverse=True)
        return [{**self.chunks[i], "score": round(s, 2)} for s, i in scored[:k]]


_index: Optional[Index] = None
_bundled: Optional[List[dict]] = None
_cms_checked: Optional[float] = None  # monotonic time of the last CMS load


def bundled_chunks() -> List[dict]:
    global _bundled
    if _bundled is None:
        try:
            _bundled = json.loads(KNOWLEDGE_FILE.read_text())["chunks"]
        except (OSError, ValueError, KeyError):
            _bundled = []
    return _bundled


def index() -> Index:
    global _index
    if _index is None:
        _index = Index(bundled_chunks())
    return _index


def cms_chunks(doc: dict) -> List[dict]:
    """Retrieval chunks for one published CMS item: a summary, then one per heading section."""
    slug, title = doc.get("slug"), (doc.get("title") or "").strip()
    if not slug or not title:
        return []
    url, kind = f"/resources/{slug}", doc.get("type") or "article"
    gated = bool(doc.get("gated"))
    header = f"{title} ({kind}{', gated download: visitors unlock it with a short form' if gated else ''}). {doc.get('summary') or ''}"
    out = [{"id": f"cms:{slug}", "kind": "cms", "title": title, "url": url, "text": header.strip()}]
    if gated or not doc.get("body"):
        return out
    body = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", doc["body"])  # drop images
    section, lines, n = title, [], 0
    for line in body.splitlines() + ["# end"]:
        m = re.match(r"^#{1,3}\s+(.*)", line)
        if m:
            text = "\n".join(lines).strip()
            # Long sections are split so no single chunk crowds out the others.
            for start in range(0, len(text), 2400):
                out.append({"id": f"cms:{slug}#{n}", "kind": "cms", "title": f"{title}: {section}", "url": url,
                            "text": f'From "{title}", section "{section}":\n{text[start:start + 2400]}'})
                n += 1
            section, lines = m.group(1).strip(), []
        else:
            lines.append(line)
    return out


async def refresh_cms(db, force: bool = False) -> None:
    """Merge live CMS content into the index; cheap no-op between refreshes."""
    global _index, _cms_checked
    if not force and _cms_checked is not None and time.monotonic() - _cms_checked < CMS_REFRESH_SECONDS:
        return
    _cms_checked = time.monotonic()
    now = datetime.now(timezone.utc).isoformat()
    try:
        docs = await db.content.find(
            {"status": {"$in": ["published", "scheduled"]}, "publish_at": {"$lte": now}},
            {"_id": 0, "slug": 1, "title": 1, "summary": 1, "body": 1, "type": 1, "gated": 1},
        ).to_list(1000)
    except Exception:
        logger.exception("Sol could not load CMS content; answering from site knowledge only")
        return
    cms = [c for d in docs for c in cms_chunks(d)]
    # A CMS item replaces the built-in article at the same address.
    urls = {c["url"] for c in cms}
    _index = Index([c for c in bundled_chunks() if c["url"] not in urls] + cms)


def search(query: str, k: int = 5) -> List[dict]:
    return index().search(query, k)


def format_context(hits: List[dict], max_chars: int = 6000) -> str:
    """Retrieved chunks as a compact, citable block for the prompt."""
    parts, used = [], 0
    for h in hits:
        block = f"[{h['title']}] (page: {h['url']})\n{h['text']}"
        if used + len(block) > max_chars:
            block = block[: max(0, max_chars - used)]
        if not block:
            break
        parts.append(block)
        used += len(block)
    return "\n\n---\n\n".join(parts)
