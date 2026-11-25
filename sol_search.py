"""Retrieval over the site's own content for Sol.

sol_knowledge.json is generated from the website repo's data files by
frontend/scripts/sol-knowledge.js, and every website deploy publishes a fresh
copy at <site>/sol_knowledge.json. Set SITE_KNOWLEDGE_URL to that address and
the API keeps itself in sync with the live site (re-checked every few hours);
the copy committed here is the fallback when the site cannot be reached.
Published CMS content (the `content` collection) is merged in and refreshed
every few minutes, so articles published in the admin are answerable without
a redeploy; gated items (lead magnets) contribute their summary, topics and
form path, never their body. Admin-approved learned answers (sol_learning.py)
are indexed too. Publishing in the admin bumps the "content" cache
generation, which rebuilds the index on the next message, so new content is
answerable at once; a background loop also refreshes every few minutes so
every worker and scheduled items catch up. Everything is ranked with BM25 (the keyword ranking
behind most search engines), in-process: no embedding API to pay for or rate
limit, no vector database, and it fits easily in a free instance's memory.
"""
import asyncio
import json
import logging
import math
import os
import re
import time
from datetime import datetime, timezone
from collections import Counter
from pathlib import Path
from typing import List, Optional

KNOWLEDGE_FILE = Path(__file__).parent / "sol_knowledge.json"
CMS_REFRESH_SECONDS = 600
SITE_REFRESH_SECONDS = 6 * 3600
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
    "cloud": "cloud migration solixcloud", "moving": "migration", "move": "migration", "migrate": "migration", "lift": "migration",
    "merger": "m&a acquisition preservation", "mergers": "m&a acquisition preservation", "acquisition": "m&a merger preservation",
    "acquired": "m&a merger preservation", "divestiture": "m&a preservation",
    "vendors": "why solix differentiators", "vendor": "why solix differentiators", "competitors": "why solix differentiators",
    "hipaa": "healthcare", "hospital": "healthcare", "patient": "healthcare", "bank": "financial services", "banking": "financial services",
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
        self.lens = []
        # Postings: term -> (chunk ids, frequencies) as compact arrays, so a query touches only chunks that share a
        # term. Arrays, not lists of (chunk, frequency) tuples: a tuple per posting cost about 10x the memory and,
        # with a few thousand CMS articles, ran the API out of its 512 MB.
        from array import array
        self.postings: dict = {}
        for i, c in enumerate(chunks):
            d = Counter(tokenize(f"{c['title']} {c['title']} {c['text']}"))  # one chunk at a time: nothing per chunk is kept
            self.lens.append(sum(d.values()))
            for t, f in d.items():
                p = self.postings.get(t)
                if p is None:
                    p = self.postings[t] = (array("i"), array("H"))
                p[0].append(i)
                p[1].append(min(f, 65535))
        self.avg = (sum(self.lens) / len(self.lens)) if self.lens else 1
        n = len(chunks)
        self.idf = {t: math.log(1 + (n - len(p[0]) + 0.5) / (len(p[0]) + 0.5)) for t, p in self.postings.items()}
        self._build_shards()

    # --- Sharded semantic layer -----------------------------------------------------------------------------
    # Chunks are split into shards by what they are (products, solutions, industries, resources, company,
    # services, learned answers). Each shard has its own vector index and a centroid; a query is matched
    # against the centroids first and searches only the closest shards, so the semantic pass stays fast as
    # the CMS grows, and a product question isn't diluted by hundreds of blog chunks.
    def _build_shards(self) -> None:
        import semantic
        by: dict = {}
        for i, c in enumerate(self.chunks):
            by.setdefault(shard_of(c), []).append(i)
        self.shards = {}
        for name, ids in by.items():
            # Stemmed words and domain concepts only (no word pairs, no character n-grams), first 600 characters:
            # the semantic pass is for meaning, BM25 already covers every word, and the API runs in 512 MB.
            vi = semantic.VectorIndex(ngrams=False, pairs=False)
            for i in ids:
                c = self.chunks[i]
                vi.add(f"{c['title']} {c['title']} {c['text'][:600]}", {"i": i})
            vi.build(max_df=0.7, centroid=True, min_df=2 if len(ids) > 50 else 1)
            self.shards[name] = (vi, vi.centroid)

    def semantic_search(self, query: str, k: int = 8, shards: int = 3, include: tuple = ()) -> List[tuple]:
        import semantic
        if not getattr(self, "shards", None):
            return []
        ranked = []
        for name, (vi, cen) in self.shards.items():
            ranked.append((semantic.cosine(vi.query_vector(query), cen), name))
        ranked.sort(reverse=True)
        out = []
        names = [n for _, n in ranked[:shards]] + [n for n in include if n in self.shards]
        for name in dict.fromkeys(names):
            out += [(s, it["i"]) for s, it in self.shards[name][0].search(query, k)]
        out.sort(reverse=True)
        return out[:k]

    def hybrid(self, query: str, k: int = 5) -> List[dict]:
        """BM25 plus the sharded semantic pass, fused on normalised scores (0.65 keyword, 0.35 meaning).

        Score fusion rather than rank fusion: one strong exact match ("mainframe" -> the Mainframe Archiving
        page) must not be outvoted by several weak hits that merely appear in both lists. The shard holding
        the best keyword hit is always searched too. A chunk found only by meaning keeps a modest score (under
        the 'well covered' bar), so gap logging and the 'Related' chips stay keyword-backed."""
        kw = self.search(query, max(k, 8))
        top_kw = kw[0]["score"] if kw else 0.0
        include = (shard_of(kw[0]),) if kw else ()
        sem = self.semantic_search(query, max(k, 8), include=include)
        top_sem = sem[0][0] if sem else 0.0
        fused: dict = {}
        by_id: dict = {}
        for h in kw:
            by_id[h["id"]] = h
            fused[h["id"]] = 0.85 * (h["score"] / top_kw if top_kw else 0.0)
        for sc, i in sem:
            c = self.chunks[i]
            by_id.setdefault(c["id"], {**c, "score": round(min(5.5, sc * 12), 2)})
            fused[c["id"]] = fused.get(c["id"], 0.0) + 0.15 * (sc / top_sem if top_sem else 0.0)
        ranked = [cid for cid, _ in sorted(fused.items(), key=lambda kv: -kv[1])]
        if top_kw >= CONFIDENT_KEYWORD_SCORE:
            # The visitor used the site's own words: keep the keyword order and let meaning add what it missed.
            ranked = [h["id"] for h in kw[:3]] + [cid for cid in ranked if cid not in {h["id"] for h in kw[:3]}]
        return [by_id[cid] for cid in ranked[:k]]

    def _expand(self, query: str) -> List[str]:
        terms = tokenize(query)
        extra = [s for w in query.lower().split() for s in tokenize(SYNONYMS.get(w.strip("?.,!"), ""))]
        return terms + extra

    def search(self, query: str, k: int = 5, kinds: Optional[List[str]] = None) -> List[dict]:
        terms = self._expand(query)
        if not terms:
            return []
        scores: dict = {}
        for t in terms:
            idf = self.idf.get(t)
            if idf is None:
                continue
            ids, freqs = self.postings[t]
            for i, f in zip(ids, freqs):
                scores[i] = scores.get(i, 0.0) + idf * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * self.lens[i] / self.avg))
        scored = sorted(((s, i) for i, s in scores.items() if s > 0 and (not kinds or self.chunks[i]["kind"] in kinds)), reverse=True)
        return [{**self.chunks[i], "score": round(s, 2)} for s, i in scored[:k]]


_index: Optional[Index] = None
_bundled: Optional[List[dict]] = None
_cms_checked: Optional[float] = None  # monotonic time of the last CMS load
_site_checked: Optional[float] = None  # monotonic time of the last live-site fetch
_content_gen: Optional[int] = None  # cache.generation("content") the index was built at
_version = 0  # bumps on every rebuild; the answer cache keys on it
_cms_items = 0
_learned_items = 0
_built_at: Optional[str] = None
_lock: Optional[asyncio.Lock] = None
_rebuild_must_wait = False  # the running rebuild carries a publish/edit, so answers must wait for it


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


def version() -> int:
    return _version


def invalidate() -> None:
    """Rebuild on the next message (new content published, an answer learned)."""
    global _cms_checked
    _cms_checked = None


def stats() -> dict:
    chunks = index().chunks
    kinds = Counter(c["kind"] for c in chunks)
    return {
        "chunks": len(chunks),
        "documents": len({c["url"] or c["id"] for c in chunks}),
        "site_chunks": len(bundled_chunks()),
        "cms_items": _cms_items,
        "learned": _learned_items,
        "by_kind": dict(kinds.most_common()),
        "built_at": _built_at,
        "version": _version,
    }


def cms_chunks(doc: dict) -> List[dict]:
    """Retrieval chunks for one published CMS item: a summary, then one per heading section."""
    slug, title = doc.get("slug"), (doc.get("title") or "").strip()
    if not slug or not title:
        return []
    url, kind = f"/resources/{slug}", doc.get("type") or "article"
    gated = bool(doc.get("gated"))
    header = f"{title} ({kind}{', gated download: visitors unlock it with a short form on the page' if gated else ''}). {doc.get('summary') or ''}"
    facets = [
        doc.get("tag") and f"Topic: {doc['tag']}",
        doc.get("products") and f"Related products: {', '.join(doc['products'])}",
        doc.get("industries") and f"Industries: {', '.join(doc['industries'])}",
        doc.get("file_id") and "Includes a downloadable file" + (" (lead magnet: offer it, the form unlocks it)" if gated else ""),
        doc.get("event_date") and f"Event date: {doc['event_date']}",
        doc.get("seo_description") if doc.get("seo_description") and doc.get("seo_description") != doc.get("summary") else None,
    ]
    header = "\n".join([header.strip(), *[f for f in facets if f]])
    out = [{"id": f"cms:{slug}", "kind": "cms", "title": title, "url": url, "text": header}]
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


async def refresh_site(force: bool = False) -> bool:
    """Swap in the knowledge file the live website publishes, if SITE_KNOWLEDGE_URL is set.

    Returns True when new site knowledge was loaded. Any failure keeps what is
    already loaded (the committed file at first), so Sol never goes blank.
    """
    global _bundled, _site_checked
    url = os.environ.get("SITE_KNOWLEDGE_URL", "").strip()
    if not url or (not force and _site_checked is not None and time.monotonic() - _site_checked < SITE_REFRESH_SECONDS):
        return False
    _site_checked = time.monotonic()
    try:
        import httpx

        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            r = await client.get(url)
            r.raise_for_status()
            chunks = r.json()["chunks"]
        if not isinstance(chunks, list) or not chunks or not all(isinstance(c, dict) and c.get("url") and c.get("text") for c in chunks):
            raise ValueError("no usable chunks")
    except Exception as e:  # network, HTTP, JSON or shape problems
        logger.warning("Sol could not load site knowledge from %s (%s); keeping the current copy", url, e)
        return False
    _bundled = chunks
    return True


async def refresh_cms(db, force: bool = False) -> None:
    """Merge live CMS content and learned answers into the index; cheap no-op between refreshes."""
    global _index, _cms_checked, _content_gen, _version, _cms_items, _learned_items, _built_at, _lock, _rebuild_must_wait
    import cache

    if await refresh_site():
        force = True
    gen = cache.generation("content")
    if gen != _content_gen:
        force = True  # something was published, edited or unpublished in this process
    loop = asyncio.get_running_loop()
    if _lock is None or getattr(_lock, "_solix_loop", None) is not loop:
        _lock = asyncio.Lock()  # one per event loop: a lock from another loop can't be awaited
        _lock._solix_loop = loop
    if _lock.locked():
        # A rebuild is under way (just published, or the background refresh): wait for it rather than answer
        # from the index it is replacing. Checked before the freshness test, which the rebuild has already reset.
        async with _lock:
            return
    if not force and _cms_checked is not None and time.monotonic() - _cms_checked < CMS_REFRESH_SECONDS:
        return
    async with _lock:
        _rebuild_must_wait = _index is None or gen != _content_gen
        _cms_checked, _content_gen = time.monotonic(), gen
        now = datetime.now(timezone.utc).isoformat()
        try:
            docs = await db.content.find(
                {"status": {"$in": ["published", "scheduled"]}, "publish_at": {"$lte": now}},
                {"_id": 0, "slug": 1, "title": 1, "summary": 1, "body": 1, "type": 1, "gated": 1, "tag": 1, "products": 1, "industries": 1,
                 "file_id": 1, "event_date": 1, "seo_description": 1},
            ).to_list(5000)
        except Exception:
            logger.exception("Sol could not load CMS content; answering from site knowledge only")
            return
        cms = [c for d in docs for c in cms_chunks(d)]
        import sol_learning

        learned = await sol_learning.learned_chunks()
        # A CMS item replaces the built-in article at the same address.
        urls = {c["url"] for c in cms}
        chunks = [c for c in bundled_chunks() if c["url"] not in urls] + cms + learned
        # Tokenising a few thousand chunks takes a moment: off the event loop, so other visitors aren't held up.
        built = await asyncio.get_running_loop().run_in_executor(None, Index, chunks)
        _index, _cms_items, _learned_items = built, len(docs), len(learned)
        _version += 1
        _built_at = now
        _rebuild_must_wait = False
        logger.info("Sol knowledge rebuilt: %d chunks (%d site, %d CMS items, %d learned)", len(chunks), len(bundled_chunks()), len(docs), len(learned))


async def ensure_fresh(db) -> None:
    """What a visitor's message waits for: the first build, or a rebuild that carries a publish or edit.

    The routine refresh (server.py, every CMS_REFRESH_SECONDS) runs in the background. On a small instance a
    rebuild takes several seconds, and waiting for it pushed the chat's first byte past the widget's timeout,
    which then switched the visitor to the scripted concierge for the rest of the session.
    """
    import cache

    if _index is None or cache.generation("content") != _content_gen:
        await refresh_cms(db)
    elif _lock is not None and _lock.locked() and _rebuild_must_wait:
        async with _lock:
            return


SHARDS = (("learned", lambda c: c.get("kind") == "learned" or not c.get("url")), ("products", lambda c: c["url"].startswith("/products")),
          ("solutions", lambda c: c["url"].startswith("/solutions") or c["url"].startswith("/platform")),
          ("industries", lambda c: c["url"].startswith("/industries")), ("services", lambda c: c["url"].startswith("/services")),
          ("resources", lambda c: c["url"].startswith("/resources") or c.get("kind") in ("cms", "article", "resource")),
          ("news", lambda c: c["url"].startswith("/newsroom")))


CONFIDENT_KEYWORD_SCORE = 7.0  # a BM25 top score this high means the question uses the site's own terms


def shard_of(chunk: dict) -> str:
    for name, test in SHARDS:
        try:
            if test(chunk):
                return name
        except (KeyError, AttributeError, TypeError):
            continue
    return "company"


def search(query: str, k: int = 5) -> List[dict]:
    return index().search(query, k)


def hybrid_search(query: str, k: int = 5) -> List[dict]:
    """Keyword + sharded semantic retrieval (what Sol answers from)."""
    return index().hybrid(query, k)


def format_context(hits: List[dict], max_chars: int = 6000) -> str:
    """Retrieved chunks as a compact, citable block for the prompt."""
    parts, used = [], 0
    for h in hits:
        where = f"(page: {h['url']})" if h.get("url") else "(approved answer)"
        block = f"[{h['title']}] {where}\n{h['text']}"
        if used + len(block) > max_chars:
            block = block[: max(0, max_chars - used)]
        if not block:
            break
        parts.append(block)
        used += len(block)
    return "\n\n---\n\n".join(parts)
