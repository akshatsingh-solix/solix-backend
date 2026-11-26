"""Sol's knowledge of www.solix.com (or any WordPress site), kept in sync through the site's public REST API.

SOL_WORDPRESS_SITES lists the sites (comma-separated, e.g. https://www.solix.com). Their pages, posts and custom
post types named in SOL_WORDPRESS_TYPES are fetched, cleaned to text (the CMS importer's cleaner: no scripts, forms,
navigation or images) and stored in the `site_pages` collection, so a restart doesn't refetch thousands of pages.
Sol indexes them next to the website's own knowledge and cites them by their full address.

Every day only pages modified since the last sync are fetched; once a week a full pass also drops pages that were
unpublished. Each page contributes a summary chunk and at most a few section chunks (SOL_WORDPRESS_MAX_CHARS of text),
and SOL_WORDPRESS_MAX_PAGES caps the total, so the index stays inside a 512 MB instance.

solix.com's ~27,000 auto-generated "AI Answer" pages (post type `answer`) are left out by default: they would swamp
the index and repeat the product pages. Add `answer` to SOL_WORDPRESS_TYPES to include them.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from html import unescape
from typing import List, Optional
from urllib.parse import urlparse

import cache
import database

logger = logging.getLogger("solix.site_knowledge")

DEFAULT_TYPES = "pages,posts,kb,glossary,leadership-lessons"
SYNC_EVERY = timedelta(hours=24)
FULL_EVERY = timedelta(days=7)
PER_PAGE = 100
SECTION_CHARS = 2400
# Pages that are forms, legal boilerplate, or listings of other pages: nothing Sol should quote.
# The auto-generated SEO Q&A pages under /products/<product>/answers/ (about 2,000 of them) would crowd the product
# pages out of Sol's results, like the `answer` post type; SOL_WORDPRESS_ANSWERS=1 keeps them.
SKIP_PATH = re.compile(r"/(thank-you|thankyou|thanks|privacy-policy|terms|cookie|sitemap|search|login|wp-|feed)(/|$)|/page/\d+/?$", re.I)
ANSWERS_PATH = re.compile(r"/answers?/", re.I)
# Gated resources exist twice: /resources/lg/<kind>/<slug>/ (the form) and /resources/<kind>/<slug>/ (the page).
LEADGEN = re.compile(r"/resources/lg/")

_chunks: Optional[List[dict]] = None
_chunks_gen: Optional[int] = None
_running = False


def sites() -> List[str]:
    return [s.strip().rstrip("/") for s in (os.environ.get("SOL_WORDPRESS_SITES") or "").split(",") if s.strip().startswith("http")]


def types() -> List[str]:
    raw = os.environ.get("SOL_WORDPRESS_TYPES") or DEFAULT_TYPES
    return [t.strip() for t in raw.split(",") if re.match(r"^[a-z0-9_-]+$", t.strip())]


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name) or default))
    except ValueError:
        return default


def max_pages() -> int:
    return _int_env("SOL_WORDPRESS_MAX_PAGES", 4000)


def max_chars() -> int:
    return _int_env("SOL_WORDPRESS_MAX_CHARS", 4000)


def page_text(html: str, url: str) -> str:
    """A page body as plain Markdown-ish text: headings kept (they become sections), images and chrome dropped."""
    import migrate
    cleaned, _ = migrate.clean_html(html or "", url)
    md = migrate.to_markdown(cleaned)
    md = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", md)  # images
    md = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", md)  # keep link text, drop addresses
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()


def to_doc(item: dict, site: str, wp_type: str) -> Optional[dict]:
    url = (item.get("link") or "").split("#")[0]
    path = urlparse(url).path
    if not url.startswith(site) or SKIP_PATH.search(path):
        return None
    if ANSWERS_PATH.search(path) and os.environ.get("SOL_WORDPRESS_ANSWERS") != "1":
        return None
    import migrate
    title = unescape(migrate._text((item.get("title") or {}).get("rendered", "")))
    text = page_text((item.get("content") or {}).get("rendered", ""), url)
    summary = unescape(migrate._text((item.get("excerpt") or {}).get("rendered", "")))[:500]
    if not title or len(text) < 80:
        return None  # empty shells (redirect stubs, form-only pages)
    return {"url": url, "site": site, "type": wp_type, "title": title[:200], "summary": summary,
            "text": text[: max_chars()], "modified": item.get("modified_gmt") or item.get("modified")}


def chunks_for(doc: dict) -> List[dict]:
    """A summary chunk, then one per heading section (like CMS items), within the page's text budget."""
    url, title = doc["url"], doc["title"]
    text = doc.get("text") or ""
    label = {"kb": "knowledge base article", "glossary": "glossary entry", "posts": "blog post",
             "leadership-lessons": "leadership lesson", "answer": "answer page"}.get(doc.get("type"), "page")
    head = f"{title} (solix.com {label}). {doc.get('summary') or ''}".strip()
    out = [{"id": f"wp:{url}", "kind": "solixcom", "title": title, "url": url, "text": f"{head}\n{text[:1200]}"}]
    rest = text[1200:]
    if not rest:
        return out
    section, lines, n = title, [], 0
    for line in rest.splitlines() + ["# end"]:
        m = re.match(r"^#{1,3}\s+(.*)", line)
        if not m:
            lines.append(line)
            continue
        body = "\n".join(lines).strip()
        for start in range(0, len(body), SECTION_CHARS):
            if len(body) - start < 120:
                break
            out.append({"id": f"wp:{url}#{n}", "kind": "solixcom", "title": f"{title}: {section}", "url": url,
                        "text": f'From "{title}" on solix.com, section "{section}":\n{body[start:start + SECTION_CHARS]}'})
            n += 1
        section, lines = m.group(1).strip(), []
    return out


def dedupe(docs: List[dict]) -> List[dict]:
    """Drop a gated /resources/lg/ page when its ungated twin is there too (same content, one citation)."""
    urls = {d["url"] for d in docs}
    return [d for d in docs if not (LEADGEN.search(d["url"]) and LEADGEN.sub("/resources/", d["url"]) in urls)]


async def chunks() -> List[dict]:
    """Every stored page as Sol chunks; rebuilt only after a sync changed something."""
    global _chunks, _chunks_gen
    gen = cache.generation("site_pages")
    if _chunks is not None and _chunks_gen == gen:
        return _chunks
    if not sites():
        _chunks, _chunks_gen = [], gen
        return _chunks
    docs = await database.db.site_pages.find({"site": {"$in": sites()}}, {"_id": 0}).sort("modified", -1).to_list(max_pages())
    _chunks = [c for d in dedupe(docs) for c in chunks_for(d)]
    _chunks_gen = gen
    return _chunks


async def _fetch_type(client, site: str, wp_type: str, since: Optional[str], log) -> tuple[list, bool]:
    """(docs, complete) for one post type; complete is False when a page of results couldn't be read."""
    import migrate
    docs, page = [], 1
    query = f"&modified_after={since}" if since else ""
    while True:
        url = (f"{site}/wp-json/wp/v2/{wp_type}?per_page={PER_PAGE}&page={page}&status=publish"
               f"&_fields=link,title,content,excerpt,modified_gmt{query}")
        rows = await migrate._json(client, url)
        if rows is None and page == 1:
            log.append(f"{wp_type}: not available")
            return docs, False
        if not isinstance(rows, list):
            # WordPress answers past-the-end pages with a 400 error, which reads as None here.
            return docs, rows is None and page > 1
        if not rows:
            return docs, True
        for item in rows:
            d = to_doc(item, site, wp_type)
            if d:
                docs.append(d)
        if len(rows) < PER_PAGE:
            return docs, True
        page += 1
        await asyncio.sleep(0.5)  # be gentle with the live site


async def sync(full: bool = False) -> dict:
    """Fetch new and changed pages (all of them when `full`); returns what happened."""
    global _running
    if _running:
        return {"status": "already running"}
    if not sites():
        return {"status": "off", "detail": "SOL_WORDPRESS_SITES is not set"}
    _running = True
    started = datetime.now(timezone.utc)
    state = await database.db.settings.find_one({"key": "site_knowledge"}, {"_id": 0}) or {}
    last = state.get("last_sync")
    last_full = state.get("last_full_sync")
    full = full or not last or not last_full or started - datetime.fromisoformat(last_full) > FULL_EVERY
    since = None if full else (datetime.fromisoformat(last) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
    log, stored, removed, complete = [], 0, 0, True
    try:
        import migrate
        async with migrate.new_client() as client:
            for site in sites():
                seen = set()
                for wp_type in types():
                    docs, ok = await _fetch_type(client, site, wp_type, since, log)
                    complete = complete and ok
                    for d in docs:
                        if len(seen) >= max_pages():
                            break
                        seen.add(d["url"])
                        await database.db.site_pages.update_one({"url": d["url"]}, {"$set": {**d, "synced_at": started.isoformat()}}, upsert=True)
                        stored += 1
                    log.append(f"{site} {wp_type}: {len(docs)} page(s)")
                # Only a complete full pass may delete: a timeout halfway must not wipe Sol's knowledge.
                if full and complete and seen:
                    r = await database.db.site_pages.delete_many({"site": site, "url": {"$nin": list(seen)}})
                    removed += r.deleted_count
        update = {"key": "site_knowledge", "last_sync": started.isoformat(), "last_result": log[-20:],
                  "pages": await database.db.site_pages.count_documents({}), "complete": complete}
        if full and complete:
            update["last_full_sync"] = started.isoformat()
        await database.db.settings.update_one({"key": "site_knowledge"}, {"$set": update}, upsert=True)
        if stored or removed:
            cache.bump("site_pages")
            cache.bump("content")  # Sol rebuilds its index on the next message
        logger.info("site knowledge sync (%s): %d stored, %d removed", "full" if full else "changes", stored, removed)
        return {"status": "ok", "full": full, "stored": stored, "removed": removed, "complete": complete, "log": log}
    except Exception as e:  # noqa: BLE001 - a failed sync keeps what Sol already knows
        logger.exception("site knowledge sync failed")
        return {"status": "error", "detail": str(e)[:300], "log": log}
    finally:
        _running = False


async def status() -> dict:
    state = await database.db.settings.find_one({"key": "site_knowledge"}, {"_id": 0}) or {}
    return {"sites": sites(), "types": types(), "max_pages": max_pages(), "running": _running,
            "pages": await database.db.site_pages.count_documents({}), "chunks": len(await chunks()),
            **{k: state.get(k) for k in ("last_sync", "last_full_sync", "last_result", "complete")}}


async def loop() -> None:
    """Daily sync in the background, started with the API when SOL_WORDPRESS_SITES is set."""
    await asyncio.sleep(90)  # let the API warm up first
    while True:
        try:
            state = await database.db.settings.find_one({"key": "site_knowledge"}, {"_id": 0}) or {}
            last = state.get("last_sync")
            if not last or datetime.now(timezone.utc) - datetime.fromisoformat(last) > SYNC_EVERY:
                await sync()
        except Exception:  # noqa: BLE001
            logger.exception("site knowledge loop")
        await asyncio.sleep(3600)
