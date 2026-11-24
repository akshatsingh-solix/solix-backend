"""Fast in-process semantic matching for MarkHub: no embedding API, no vector database, sub-millisecond.

Used by the tool router (tool_registry.py) and by Sol's sharded hybrid knowledge index (sol_search.py).

How a text becomes a vector
---------------------------
Each text is broken into features that capture meaning beyond exact words:

* stemmed words ("archiving" == "archived" == "archive"),
* word pairs ("legal hold", "application retirement"), which carry most of the meaning in B2B text,
* character 4-grams inside each word, so typos, plurals and compound words still meet
  ("decom" matches "decommission"),
* concept tags from a small domain thesaurus (CONCEPTS), so "sunset an app" and
  "retire an application" land on the same feature.

Features are hashed into a sparse vector, weighted by TF-IDF over the indexed corpus and L2-normalised,
so the score is cosine similarity. Search walks only the postings of the query's features, which keeps
a query in the tens of microseconds over a few hundred tools and a few milliseconds over thousands of
knowledge chunks.

Why not a neural embedding model: a router must answer in well under 15 ms on a 512 MB instance, cost
nothing per call and be deterministic, so the same question always routes the same way. The concept
thesaurus and the examples attached to each tool supply the "semantic" part; evals/tool_router_golden.json
measures it.
"""
from __future__ import annotations

import math
import re
import zlib
from collections import Counter, defaultdict
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

DIM = 1 << 20  # hash space; collisions are negligible at this size

STOP = set("""a an and are as at be been being but by can could did do does doing for from had has have having he her his how i if in into is it
its just me my myself no nor of on once or our ours out over own same she should so some such than that the their them then there these they this
those through to too under until up very was we were what when where which while who whom why will with would you your yours please hi hello hey
thanks thank want need like get got show give tell let lets us me""".split())

# Domain thesaurus: every word on the right maps to the concept on the left. Concepts are features too.
CONCEPTS: Dict[str, Tuple[str, ...]] = {
    "retire": ("retire", "retirement", "decommission", "sunset", "shutdown", "switch-off", "legacy", "eol", "end-of-life"),
    "archive": ("archive", "archiving", "archival", "cold", "offload", "tiering"),
    "privacy": ("privacy", "gdpr", "ccpa", "dsar", "pii", "personal", "consent", "erasure", "forget"),
    "lead": ("lead", "leads", "prospect", "prospects", "mql", "sql", "sal", "funnel", "pipeline"),
    "account": ("account", "accounts", "company", "companies", "org", "organisation", "organization", "firm", "radar"),
    "contact": ("contact", "contacts", "person", "people", "persona", "buyer", "stakeholder"),
    "email": ("email", "emails", "mail", "inbox", "mailbox"),
    "send": ("send", "sending", "sent", "outreach", "sequence", "sequences", "cadence", "campaign", "outbound"),
    "reply": ("reply", "replies", "responded", "response", "answered", "interested", "objection"),
    "approve": ("approve", "approval", "approvals", "sign-off", "accept", "authorise", "authorize", "okay"),
    "pause": ("pause", "hold", "stop", "halt", "freeze", "suspend"),
    "suppress": ("suppress", "suppression", "unsubscribe", "opt-out", "optout", "dnc", "blocklist", "blacklist", "do-not-contact"),
    "delete": ("delete", "remove", "erase", "purge", "wipe", "destroy"),
    "signal": ("signal", "signals", "trigger", "intent", "posting", "job", "jobs", "hiring", "surge"),
    "score": ("score", "scoring", "rank", "tier", "priority", "hot", "strongest", "top"),
    "metric": ("kpi", "kpis", "metric", "metrics", "rate", "rates", "performance", "stats", "statistics", "report", "dashboard", "numbers"),
    "health": ("health", "watchdog", "status", "broken", "failing", "alert", "alerts", "uptime", "stalled"),
    "content": ("content", "blog", "article", "whitepaper", "datasheet", "case-study", "webinar", "ebook", "publish", "post"),
    "demo": ("demo", "meeting", "call", "walkthrough", "trial", "book"),
    "human": ("expert", "human", "specialist", "sales", "salesperson", "rep", "engineer", "consultant", "someone"),
    "identity": ("name", "email", "phone", "company", "role", "title", "job-title", "work"),
    "chat": ("chat", "chats", "conversation", "conversations", "transcript", "sol", "concierge"),
    "gap": ("gap", "gaps", "unanswered", "missed", "unknown", "learn", "learned", "teach"),
    "compliance": ("compliance", "lia", "legitimate", "lawful", "gate", "country", "legal"),
    "research": ("research", "deep-dive", "brief", "dossier", "investigate"),
    "owner": ("owner", "assign", "assigned", "route", "routing", "territory", "rep"),
}
_CONCEPT_OF = {w: c for c, words in CONCEPTS.items() for w in words}

_WORD = re.compile(r"[a-z0-9][a-z0-9&/+-]*[a-z0-9]|[a-z0-9]")


def stem(w: str) -> str:
    for suf in ("ization", "isation", "ations", "ation", "ments", "ment", "ings", "ing", "ies", "ied", "ers", "er", "ed", "es", "s"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[: -len(suf)] + ("y" if suf in ("ies", "ied") else "")
    return w


def words(text: str) -> List[str]:
    return [w for w in _WORD.findall((text or "").lower()) if w not in STOP]


def features(text: str, ngrams: bool = True, pairs: bool = True) -> Counter:
    """Weighted features of one text (before IDF). `ngrams=False` skips character 4-grams: they are most of the
    features, so large corpora (Sol's knowledge) leave them out to stay small; the tool router keeps them."""
    ws = words(text)
    out: Counter = Counter()
    stems = [stem(w) for w in ws]
    for w, s in zip(ws, stems):
        out["w:" + s] += 1.0
        c = _CONCEPT_OF.get(w) or _CONCEPT_OF.get(s)
        if c:
            out["c:" + c] += 1.0
        if ngrams and len(w) >= 5:
            padded = f"<{w}>"
            for i in range(len(padded) - 3):
                out["g:" + padded[i:i + 4]] += 0.25
    if pairs:
        for a, b in zip(stems, stems[1:]):
            out["b:" + a + "_" + b] += 1.5
    return out


def _h(feat: str) -> int:
    return zlib.crc32(feat.encode()) & (DIM - 1)


class VectorIndex:
    """Sparse TF-IDF cosine index with postings. `add` everything, then `build()`, then `search()`.

    Memory matters (the API runs in 512 MB): after build() only the postings are kept, as compact arrays, plus a
    centroid when asked for; the per-document vectors and raw counts are dropped."""

    def __init__(self, ngrams: bool = True, pairs: bool = True) -> None:
        self.ngrams, self.pairs = ngrams, pairs
        self.items: List[dict] = []
        self._raw: Optional[List[str]] = []
        self.idf: Dict[int, float] = {}
        self.postings: Dict[int, tuple] = {}
        self.centroid: Dict[int, float] = {}

    def add(self, text: str, item: dict) -> None:
        """Queue a text. Its features are computed in build(), twice (document frequencies, then weights), so no
        per-document table is ever held for the whole corpus."""
        self.items.append(item)
        self._raw.append(text)

    def _hashed(self, text: str) -> Dict[int, float]:
        raw: Dict[int, float] = {}
        for f, w in features(text, self.ngrams, self.pairs).items():
            h = _h(f)
            raw[h] = raw.get(h, 0.0) + w
        return raw

    def build(self, max_df: float = 0.5, centroid: bool = False, min_df: int = 1) -> "VectorIndex":
        from array import array
        texts = self._raw or []
        n = max(1, len(texts))
        df: Counter = Counter()
        for t in texts:
            df.update(self._hashed(t).keys())
        # Features in more than max_df of documents carry no signal and only lengthen postings.
        # min_df > 1 drops features seen in a single document (mostly one-off word pairs): they can't connect two
        # texts, and on a large corpus they are most of the index.
        self.idf = {h: math.log(1 + n / (1 + d)) for h, d in df.items() if (n < 8 or d / n <= max_df) and d >= min_df}
        del df
        docs: Dict[int, array] = {}
        weights: Dict[int, array] = {}
        acc: Dict[int, float] = defaultdict(float)
        for i, t in enumerate(texts):
            raw = self._hashed(t)
            v = {h: (1 + math.log(w)) * self.idf[h] if w >= 1 else w * self.idf[h] for h, w in raw.items() if h in self.idf}
            norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
            for h, x in v.items():
                x /= norm
                if h not in docs:
                    docs[h], weights[h] = array("i"), array("f")
                docs[h].append(i)
                weights[h].append(x)
                if centroid:
                    acc[h] += x
        self.postings = {h: (docs[h], weights[h]) for h in docs}
        if centroid:
            cn = math.sqrt(sum(x * x for x in acc.values())) or 1.0
            self.centroid = {h: x / cn for h, x in acc.items()}
        self._raw = None  # the texts were only needed to build
        return self

    def query_vector(self, text: str) -> Dict[int, float]:
        raw = self._hashed(text)
        v = {h: (1 + math.log(w)) * self.idf[h] if w >= 1 else w * self.idf[h] for h, w in raw.items() if h in self.idf}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        return {h: x / norm for h, x in v.items()}

    def search(self, text: str, k: int = 5, where=None) -> List[Tuple[float, dict]]:
        q = self.query_vector(text)
        scores: Dict[int, float] = defaultdict(float)
        for h, qx in q.items():
            p = self.postings.get(h)
            if p:
                for i, x in zip(p[0], p[1]):
                    scores[i] += qx * x
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        out = []
        for i, s in ranked:
            if where is None or where(self.items[i]):
                out.append((round(s, 4), self.items[i]))
                if len(out) >= k:
                    break
        return out


def rrf(rankings: Sequence[Sequence[str]], k: int = 60) -> List[Tuple[str, float]]:
    """Reciprocal Rank Fusion: merges keyword and vector rankings without comparing their scores."""
    total: Dict[str, float] = defaultdict(float)
    for ranking in rankings:
        for rank, key in enumerate(ranking):
            total[key] += 1.0 / (k + rank + 1)
    return sorted(total.items(), key=lambda kv: -kv[1])


def centroid(vecs: Iterable[Dict[int, float]]) -> Dict[int, float]:
    acc: Dict[int, float] = defaultdict(float)
    n = 0
    for v in vecs:
        n += 1
        for h, x in v.items():
            acc[h] += x
    norm = math.sqrt(sum(x * x for x in acc.values())) or 1.0
    return {h: x / norm for h, x in acc.items()} if n else {}


def cosine(a: Dict[int, float], b: Dict[int, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(x * b.get(h, 0.0) for h, x in a.items())


def best(index: Optional[VectorIndex], text: str) -> float:
    if not index or not index.items:
        return 0.0
    hit = index.search(text, 1)
    return hit[0][0] if hit else 0.0
