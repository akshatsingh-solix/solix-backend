"""Website migration: bring content from an existing website into the CMS.

Sources
- WordPress: the site's REST API (/wp-json/wp/v2/...), posts, pages or any
  custom post type, with featured images, authors and categories.
- Sitemap: a sitemap.xml (or sitemap index); every page is fetched and its
  main content extracted.
- RSS / Atom feed.
- A list of page URLs.
- A CSV or JSON export (parsed in the admin, sent here as records).

Each item keeps its original publish date and source URL, is mapped to a
content type (by URL, category and title, or one fixed type), and lands as a
draft or published. Images and linked PDFs can be copied into the CMS file
store so nothing depends on the old site staying up. Runs as a background job
with progress, a log and cancel; a preview shows what would be imported first.
Afterwards, the redirect map (old URL -> new URL) can be downloaded for the
old web server.

Only public http(s) addresses are fetched (no private or internal networks),
with size limits and timeouts.
"""
from __future__ import annotations

import asyncio
import csv
import io
import ipaddress
import json
import logging
import re
import socket
import uuid
from html import unescape
from typing import AsyncIterator, List, Literal, Optional
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Comment
from defusedxml import ElementTree
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from markdownify import markdownify
from pydantic import BaseModel, Field

import scoring
from auth import get_current_admin, require_roles
from content import CONTENT_TYPES, MAX_FILE, _changed, slugify, store_file, upsert_imported
import database
from database import now_iso
from delivery import site_url

logger = logging.getLogger("solix.migrate")
router = APIRouter(prefix="/api/admin/migrations", tags=["migration"], dependencies=[Depends(get_current_admin)])
can_run = Depends(require_roles("admin", "editor"))

# A normal browser identity: many site firewalls turn away unknown bots.
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36 SolixSiteMigrator/1.1"
MAX_PAGE = 5 * 1024 * 1024
MAX_ITEMS = 2000
CONCURRENCY = 4
LOG_KEEP = 300
_running: dict[str, asyncio.Task] = {}

# URL / category / title patterns -> content type, first match wins.
TYPE_RULES = [
    (r"press[-_ ]?release|/news(room)?/|announce", "news"),
    (r"white[-_ ]?paper", "whitepaper"),
    (r"data[-_ ]?sheet", "datasheet"),
    (r"case[-_ ]?stud|customer[-_ ]?(story|success)|success[-_ ]?stor", "casestudy"),
    (r"e[-_ ]?book", "ebook"),
    (r"webinar|on[-_ ]?demand", "webinar"),
    (r"podcast", "podcast"),
    (r"solution[-_ ]?brief|/briefs?/", "brief"),
    (r"/events?/", "event"),
    (r"brochure|collateral|infographic", "collateral"),
    (r"leadership", "leadership"),
]


def detect_type(*texts: str) -> str:
    hay = " ".join(t for t in texts if t).lower()
    for rx, kind in TYPE_RULES:
        if re.search(rx, hay):
            return kind
    return "blog"


_PRODUCT_NAMES = {slug: slug.replace("-", " ") for slug in scoring.PRODUCTS}


def detect_products(*texts: str) -> list:
    hay = " ".join(t for t in texts if t).lower()
    return [slug for slug, name in _PRODUCT_NAMES.items() if len(name) > 5 and name in hay][:5]


# --- Safe fetching ----------------------------------------------------------------

def _public_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return False
    return True


async def check_url(url: str) -> str:
    u = urlparse((url or "").strip())
    if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password:
        raise ValueError(f"Not a public web address: {url!r}")
    if not await asyncio.to_thread(_public_host, u.hostname):
        raise ValueError(f"Address is not on the public internet: {u.hostname}")
    return u.geturl()


async def fetch(client: httpx.AsyncClient, url: str, max_bytes: int = MAX_PAGE) -> tuple[bytes, str, str]:
    """GET a public URL, following up to 5 redirects (each re-checked). Returns (body, content_type, final_url)."""
    for _ in range(6):
        url = await check_url(url)
        async with client.stream("GET", url, follow_redirects=False) as resp:
            if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                url = urljoin(url, resp.headers["location"])
                continue
            if resp.status_code >= 400:
                raise ValueError(f"HTTP {resp.status_code} for {url}")
            chunks, size = [], 0
            async for chunk in resp.aiter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError(f"Larger than {max_bytes // (1024 * 1024)} MB: {url}")
                chunks.append(chunk)
            return b"".join(chunks), resp.headers.get("content-type", ""), str(resp.url)
    raise ValueError(f"Too many redirects: {url}")


def new_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(25, connect=10), headers={
        "User-Agent": UA, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.8",
    })


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", BeautifulSoup(html or "", "html.parser").get_text(" ")).strip()


# --- HTML -> Markdown -----------------------------------------------------------------

NOISE = re.compile(r"(share|social|related|comment|newsletter|cookie|breadcrumb|sidebar|widget|subscribe|author-bio|pagination|nav)", re.I)
VIDEO = re.compile(r"(youtube\.com/embed/|youtube-nocookie\.com/embed/|player\.vimeo\.com/video/)([\w-]+)")


def clean_html(html: str, base_url: str) -> tuple[str, Optional[str]]:
    """Strip page chrome and scripts; absolutise links; tables become lists.
    Returns (html, first embedded video url)."""
    soup = BeautifulSoup(html or "", "html.parser")
    video = None
    for frame in soup.find_all("iframe"):
        m = VIDEO.search(frame.get("src") or "")
        if m and not video:
            video = f"https://www.youtube.com/watch?v={m.group(2)}" if "youtube" in m.group(1) else f"https://vimeo.com/{m.group(2)}"
    for tag in soup(["script", "style", "noscript", "iframe", "form", "svg", "button", "input", "select", "textarea", "nav", "footer", "header", "aside", "object", "embed"]):
        tag.decompose()
    for c in soup.find_all(string=lambda s: isinstance(s, Comment)):
        c.extract()
    for el in soup.find_all(True):
        if el.attrs is None:
            continue
        cls = " ".join(el.get("class") or []) + " " + (el.get("id") or "")
        if el.name in ("div", "section", "ul", "aside") and NOISE.search(cls):
            el.decompose()
    for a in soup.find_all("a", href=True):
        a["href"] = urljoin(base_url, a["href"])
    for img in soup.find_all("img"):
        src = img.get("data-src") or img.get("data-lazy-src") or img.get("src") or ""
        if img.get("srcset") and not src:
            src = img["srcset"].split(",")[0].split()[0]
        if not src or src.startswith("data:"):
            img.decompose()
            continue
        img["src"] = urljoin(base_url, src)
    for table in soup.find_all("table"):
        ul = soup.new_tag("ul")
        for tr in table.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            cells = [c for c in cells if c]
            if cells:
                li = soup.new_tag("li")
                li.string = " · ".join(cells)
                ul.append(li)
        table.replace_with(ul)
    return str(soup), video


def to_markdown(html: str) -> str:
    md = markdownify(html, heading_style="ATX", bullets="-", strip=["span", "div", "section", "article", "figure", "figcaption", "u"])
    md = re.sub(r"^#\s+", "## ", md, flags=re.M)
    md = re.sub(r"^#{4,6}\s+", "### ", md, flags=re.M)
    # Images must stand on their own line to render as figures.
    md = re.sub(r"\s*(!\[[^\]]*\]\([^)\s]+(?:\s+\"[^\"]*\")?\))\s*", r"\n\n\1\n\n", md)
    md = re.sub(r'(!\[[^\]]*\]\([^)\s]+)\s+"[^"]*"\)', r"\1)", md)
    md = re.sub(r"\[\s*\]\([^)]*\)", "", md)  # empty links
    md = re.sub(r"[ \t]+\n", "\n", md)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()


# --- Page extraction --------------------------------------------------------------------

BODY_SELECTORS = ["[itemprop=articleBody]", "article .entry-content", ".entry-content", ".post-content", ".article-content", ".article-body",
                  ".blog-content", ".post-body", ".content-body", "article", "main", "[role=main]", "#content", ".content"]


def _meta(soup: BeautifulSoup, *names: str) -> Optional[str]:
    for n in names:
        tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n}) or soup.find("meta", attrs={"itemprop": n})
        if tag and tag.get("content"):
            return tag["content"].strip()
    return None


def _jsonld(soup: BeautifulSoup) -> dict:
    found: dict = {}

    def walk(node):
        if isinstance(node, list):
            for n in node:
                walk(n)
        elif isinstance(node, dict):
            for key in ("datePublished", "headline", "description", "articleSection"):
                if key in node and key not in found and isinstance(node[key], (str, list)):
                    found[key] = node[key] if isinstance(node[key], str) else (node[key][0] if node[key] else None)
            if "author" in node and "author" not in found:
                a = node["author"]
                a = a[0] if isinstance(a, list) and a else a
                if isinstance(a, dict) and a.get("name"):
                    found["author"] = a["name"]
                elif isinstance(a, str):
                    found["author"] = a
            if "image" in node and "image" not in found:
                img = node["image"]
                img = img[0] if isinstance(img, list) and img else img
                found["image"] = img.get("url") if isinstance(img, dict) else img if isinstance(img, str) else None
            for v in node.values():
                if isinstance(v, (dict, list)):
                    walk(v)

    for s in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            walk(json.loads(s.string or ""))
        except (ValueError, TypeError):
            continue
    return found


def extract_page(html: str, url: str) -> dict:
    """Pull the article out of a web page: title, date, summary, author, cover, body."""
    soup = BeautifulSoup(html, "html.parser")
    ld = _jsonld(soup)
    title = _meta(soup, "og:title", "twitter:title") or ld.get("headline")
    if not title:
        h1 = soup.find("h1")
        title = h1.get_text(" ", strip=True) if h1 else (soup.title.get_text(strip=True) if soup.title else "")
    title = re.sub(r"\s+[|\-–—]\s+[^|\-–—]{2,60}$", "", unescape(title or "")).strip()
    date = _meta(soup, "article:published_time", "datePublished", "date", "dc.date", "DC.date.issued") or ld.get("datePublished")
    if not date:
        t = soup.find("time", attrs={"datetime": True})
        date = t["datetime"] if t else None
    body_el = None
    for sel in BODY_SELECTORS:
        el = soup.select_one(sel)
        if el and len(el.get_text(" ", strip=True)) >= 200:
            body_el = el
            break
    if body_el is None:
        # Densest block of paragraphs.
        best, best_len = None, 0
        for div in soup.find_all(["div", "section"]):
            n = sum(len(p.get_text(" ", strip=True)) for p in div.find_all("p", recursive=False))
            if n > best_len:
                best, best_len = div, n
        body_el = best or soup.body or soup
    if body_el.find("h1"):
        first = body_el.find("h1")
        if first.get_text(" ", strip=True) == title:
            first.decompose()
    tags = [a.get_text(strip=True) for a in soup.select("a[rel~=tag], a[rel~=category]")][:5]
    author = _meta(soup, "author", "article:author")
    if not author or author.startswith("http"):
        author = ld.get("author")
    return {
        "url": url, "title": title, "date": date,
        "summary": _meta(soup, "description", "og:description", "twitter:description") or ld.get("description"),
        "author": author,
        "cover_image": _meta(soup, "og:image", "twitter:image") or ld.get("image"),
        "tag": _meta(soup, "article:section") or ld.get("articleSection") or (tags[0] if tags else None),
        "categories": tags,
        "body_html": str(body_el),
    }


# --- Sources -------------------------------------------------------------------------------

class MigrationIn(BaseModel):
    # "link": any address - an article, a listing or blog home, a sitemap or a feed.
    source: Literal["link", "wordpress", "sitemap", "rss", "urls", "file"]
    url: Optional[str] = Field(default=None, max_length=1000)
    urls: List[str] = Field(default_factory=list, max_length=MAX_ITEMS)
    records: List[dict] = Field(default_factory=list, max_length=MAX_ITEMS)
    wp_types: List[str] = Field(default_factory=lambda: ["posts"], max_length=10)
    include: Optional[str] = Field(default=None, max_length=200)
    exclude: Optional[str] = Field(default=None, max_length=200)
    type_mode: Literal["auto", "fixed"] = "auto"
    fixed_type: str = "blog"
    status: Literal["draft", "published"] = "draft"
    mirror_media: bool = True
    attach_pdfs: bool = True
    gated: bool = False
    on_conflict: Literal["skip", "update"] = "skip"
    limit: int = Field(default=200, ge=1, le=MAX_ITEMS)


def _rss_date(value: Optional[str]) -> Optional[str]:
    """RSS dates are RFC 822 ("Tue, 02 Jun 2026 09:00:00 GMT"); Atom's are ISO already."""
    if not value:
        return None
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(value).isoformat()
    except (TypeError, ValueError):
        return value


TYPE_ALIASES = {re.sub(r"[^a-z]", "", k): v for k, v in {
    "blog": "blog", "blog post": "blog", "article": "blog", "post": "blog", "white paper": "whitepaper", "datasheet": "datasheet",
    "case study": "casestudy", "customer story": "casestudy", "ebook": "ebook", "webinar": "webinar", "podcast": "podcast",
    "leadership": "leadership", "event": "event", "solution brief": "brief", "brief": "brief", "collateral": "collateral",
    "brochure": "collateral", "press release": "news", "news": "news", "announcement": "news",
}.items()}


def normalise_type(value) -> Optional[str]:
    if not value:
        return None
    key = re.sub(r"[^a-z]", "", str(value).lower())
    return key if key in CONTENT_TYPES else TYPE_ALIASES.get(key)


def _match(params: MigrationIn, url: str) -> bool:
    path = urlparse(url).path or "/"
    if params.include and not _filter(params.include, path):
        return False
    if params.exclude and _filter(params.exclude, path):
        return False
    return True


def _filter(pattern: str, path: str) -> bool:
    parts = [p.strip() for p in pattern.split(",") if p.strip()]
    return any(p.lower() in path.lower() for p in parts)


async def wp_types(client: httpx.AsyncClient, root: str) -> list:
    try:
        data, _, _ = await fetch(client, urljoin(root.rstrip("/") + "/", "wp-json/wp/v2/types"))
        types = json.loads(data)
        skip = {"media", "blocks", "menu-items", "navigation", "templates", "template-parts", "global-styles", "font-families", "wp_pattern_category", "guest-author", "rm_content_editor"}
        return [{"rest_base": t.get("rest_base"), "name": t.get("name")} for t in types.values()
                if re.match(r"^[a-z0-9_-]+$", t.get("rest_base") or "") and t["rest_base"] not in skip]
    except (ValueError, httpx.HTTPError, AttributeError):
        return []


def wp_record(p: dict, rest_base: str) -> dict:
    """A WordPress REST API item (fetched with _embed) as a raw migration record."""
    emb = p.get("_embedded") or {}
    media = (emb.get("wp:featuredmedia") or [{}])[0] or {}
    terms = [t.get("name") for group in (emb.get("wp:term") or []) for t in (group or []) if isinstance(t, dict) and t.get("name")]
    author = ((emb.get("author") or [{}])[0] or {}).get("name")
    return {
        "url": p.get("link") or "", "slug": p.get("slug"), "title": unescape(_text((p.get("title") or {}).get("rendered", ""))),
        "date": (p.get("date_gmt") + "Z") if p.get("date_gmt") else p.get("date"),
        "summary": _text((p.get("excerpt") or {}).get("rendered", "")), "body_html": (p.get("content") or {}).get("rendered", ""),
        "cover_image": media.get("source_url"), "author": author, "tag": terms[0] if terms else None, "categories": terms,
        "wp_type": rest_base,
    }


async def _json(client: httpx.AsyncClient, url: str):
    try:
        data, _, _ = await fetch(client, url)
        return json.loads(data)
    except (ValueError, httpx.HTTPError):
        return None


async def find_wp_root(client: httpx.AsyncClient, url: Optional[str], cache: dict, soup: Optional[BeautifulSoup] = None) -> Optional[str]:
    """The WordPress install serving `url` (e.g. https://site.com/blog/), or None.

    Uses the page's <link rel="https://api.w.org/"> when present, otherwise asks
    the nearest parent folders for /wp-json/ - sites often run a separate
    WordPress for the blog under /blog/."""
    if not url:
        return None
    if soup is not None:
        tag = soup.find("link", rel=lambda r: r and "https://api.w.org/" in (r if isinstance(r, list) else [r]))
        if tag and tag.get("href", "").rstrip("/").endswith("wp-json"):
            return tag["href"].rstrip("/")[: -len("wp-json")]
    u = urlparse(url)
    parts = [p for p in u.path.split("/") if p]
    candidates = [f"{u.scheme}://{u.netloc}/" + "".join(f"{p}/" for p in parts[:i]) for i in range(min(len(parts), 3), -1, -1)]
    for root in candidates:
        if root in cache:
            if cache[root]:
                return root
            continue
        info = await _json(client, root + "wp-json/")
        cache[root] = isinstance(info, dict) and "wp/v2" in (info.get("namespaces") or [])
        if cache[root]:
            return root
    return None


async def wp_items(client: httpx.AsyncClient, root: str, rest_base: str, params: "MigrationIn", log, query: str = "") -> AsyncIterator[dict]:
    """Every item of one post type from a WordPress REST API, newest first."""
    if not re.match(r"^[a-z0-9_-]+$", rest_base):
        return
    page = 1
    while True:
        api = urljoin(root, f"wp-json/wp/v2/{rest_base}?per_page=50&page={page}&_embed=1&orderby=date&order=desc{query}")
        rows = await _json(client, api)
        if not isinstance(rows, list):
            if page == 1:
                await log("error", api, f"WordPress API not reachable for '{rest_base}'")
            return
        if not rows:
            return
        for p in rows:
            if _match(params, p.get("link") or ""):
                yield wp_record(p, rest_base)
        page += 1


LISTING_SKIP = re.compile(r"/(category|tag|author|page|feed|wp-content|wp-json|comments)/|\.(jpe?g|png|gif|webp|svg|pdf|zip|xml)$|#", re.I)


def listing_links(soup: BeautifulSoup, base: str) -> list:
    """Links from a listing page to the articles below it (same site, deeper path)."""
    b = urlparse(base)
    prefix = b.path if b.path.endswith("/") else b.path.rsplit("/", 1)[0] + "/"
    out = []
    for a in soup.find_all("a", href=True):
        href = urljoin(base, a["href"]).split("#")[0]
        u = urlparse(href)
        if u.netloc != b.netloc or not u.path.startswith(prefix) or u.path.rstrip("/") == b.path.rstrip("/") or LISTING_SKIP.search(u.path + ("#" if "#" in a["href"] else "")):
            continue
        if href not in out:
            out.append(href)
    return out


async def link_records(client: httpx.AsyncClient, url: str, params: "MigrationIn", log, roots: dict) -> AsyncIterator[dict]:
    """Whatever a pasted address points at: a sitemap or feed, a whole WordPress
    blog or category, a single article (through its WordPress API record when
    there is one), or a listing page whose articles are followed."""
    data, ctype, final = await fetch(client, url)
    start = data[:4000].lstrip().lower()
    if b"<urlset" in start or b"<sitemapindex" in start:
        async for rec in discover(client, params.model_copy(update={"source": "sitemap", "url": final}), log):
            yield rec
        return
    if b"<rss" in start or b"<feed" in start:
        async for rec in discover(client, params.model_copy(update={"source": "rss", "url": final}), log):
            yield rec
        return
    if "html" not in ctype and not start.startswith((b"<!doctype", b"<html")):
        raise ValueError(f"Not a web page, sitemap or feed ({ctype or 'unknown type'})")
    html = data.decode("utf-8", "replace")
    soup = BeautifulSoup(html, "html.parser")
    root = await find_wp_root(client, final, roots, soup)
    path = urlparse(final).path
    if root:
        root_path = urlparse(root).path
        rel = path[len(root_path):].strip("/") if path.startswith(root_path) else None
        types = [t for t in (params.wp_types or ["posts"]) if t]
        if rel == "" or (rel and re.fullmatch(r"page/\d+", rel)):
            await log("info", final, f"WordPress site at {root}: importing its {', '.join(types)}")
            for rest_base in types:
                async for rec in wp_items(client, root, rest_base, params, log):
                    yield rec
            return
        m = re.match(r"^(category|tag)/(?:.*/)?([^/]+)$", rel or "")
        if m:
            tax = "categories" if m.group(1) == "category" else "tags"
            terms = await _json(client, urljoin(root, f"wp-json/wp/v2/{tax}?slug={m.group(2)}"))
            if isinstance(terms, list) and terms:
                await log("info", final, f"WordPress {m.group(1)} '{terms[0].get('name')}': importing its posts")
                async for rec in wp_items(client, root, "posts", params, log, query=f"&{tax}={terms[0]['id']}"):
                    yield rec
                return
        if rel:
            slug = rel.rsplit("/", 1)[-1]
            available = [t["rest_base"] for t in await wp_types(client, root)] or ["posts", "pages"]
            for rest_base in dict.fromkeys(["posts", "pages", *available]):
                rows = await _json(client, urljoin(root, f"wp-json/wp/v2/{rest_base}?slug={slug}&_embed=1"))
                if isinstance(rows, list) and rows:
                    yield wp_record(rows[0], rest_base)
                    return
    # Not WordPress (or not found there): read the page itself.
    page = extract_page(html, final)
    words = len(_text(page.get("body_html") or "").split())
    links = listing_links(soup, final)
    if words < 250 and len(links) >= 3:
        await log("info", final, f"Listing page: following {len(links)} links")
        pages, next_url = 1, final
        while True:
            for href in links:
                yield {"url": href, "fetch": True}
            nxt = soup.find("a", rel=lambda r: r and "next" in (r if isinstance(r, list) else [r])) or soup.find("link", rel="next")
            if not nxt or pages >= 20:
                return
            next_url = urljoin(next_url, nxt.get("href"))
            try:
                data, _, next_url = await fetch(client, next_url)
            except ValueError:
                return
            soup = BeautifulSoup(data.decode("utf-8", "replace"), "html.parser")
            links = listing_links(soup, final)
            pages += 1
    yield {**page, "url": final}


async def discover(client: httpx.AsyncClient, params: MigrationIn, log) -> AsyncIterator[dict]:
    """Yield raw records ({url, title?, body_html?, ...}) up to params.limit."""
    count = 0
    if params.source == "wordpress":
        root = await find_wp_root(client, params.url, {}) or (urljoin(params.url, "/"))
        if root.rstrip("/") != (params.url or "").rstrip("/"):
            await log("info", params.url, f"Using the WordPress site at {root}")
        for rest_base in params.wp_types or ["posts"]:
            async for rec in wp_items(client, root, rest_base, params, log):
                yield rec
                count += 1
                if count >= params.limit:
                    return
    elif params.source in ("link", "urls"):
        roots: dict = {}
        targets = [params.url] if params.source == "link" else [u.strip() for u in params.urls if u.strip()]
        seen: set = set()
        for target in targets:
            if params.source == "urls" and not _match(params, target):
                continue
            try:
                async for rec in link_records(client, target, params, log, roots):
                    key = rec.get("url") or rec.get("title")
                    if key in seen:
                        continue
                    seen.add(key)
                    yield rec
                    count += 1
                    if count >= params.limit:
                        return
            except (ValueError, httpx.HTTPError) as exc:
                # One unreachable address counts as one failed item, not a failed run.
                yield {"url": target, "error": str(exc) or exc.__class__.__name__}
                count += 1
    elif params.source == "rss":
        data, _, _ = await fetch(client, params.url)
        root = ElementTree.fromstring(data)
        ns = {"atom": "http://www.w3.org/2005/Atom", "content": "http://purl.org/rss/1.0/modules/content/", "media": "http://search.yahoo.com/mrss/", "dc": "http://purl.org/dc/elements/1.1/"}
        entries = root.findall(".//item") or root.findall(".//atom:entry", ns)
        for e in entries:
            def t(path):
                el = e.find(path, ns)
                return (el.text or "").strip() if el is not None and el.text else None
            link = t("link")
            if not link:
                el = e.find("atom:link", ns)
                link = el.get("href") if el is not None else None
            if not link or not _match(params, link):
                continue
            media = e.find("media:content", ns)
            enclosure = e.find("enclosure")
            cover = media.get("url") if media is not None else (enclosure.get("url") if enclosure is not None and (enclosure.get("type") or "").startswith("image") else None)
            cats = [c.text for c in e.findall("category") if c.text]
            body_html = t("content:encoded") or t("atom:content")
            yield {
                "url": link, "title": unescape(t("title") or ""), "date": _rss_date(t("pubDate") or t("atom:published") or t("atom:updated") or t("dc:date")),
                "summary": _text(t("description") or t("atom:summary") or ""), "body_html": body_html,
                "cover_image": cover, "author": t("dc:creator") or t("author"), "tag": cats[0] if cats else None, "categories": cats,
                # Feeds that only carry a teaser: read the full article from the page.
                "fetch": len(_text(body_html or "")) < 400,
            }
            count += 1
            if count >= params.limit:
                return
    elif params.source == "sitemap":
        seen, queue = set(), [params.url]
        while queue and count < params.limit and len(seen) < 60:
            sm = queue.pop(0)
            if sm in seen:
                continue
            seen.add(sm)
            try:
                data, _, _ = await fetch(client, sm, 20 * 1024 * 1024)
                root = ElementTree.fromstring(data)
            except Exception as exc:
                await log("error", sm, f"Sitemap not readable: {exc}")
                continue
            tag = root.tag.split("}")[-1]
            locs = [(el.findtext("{*}loc") or "").strip() for el in root.findall("{*}sitemap" if tag == "sitemapindex" else "{*}url")]
            if tag == "sitemapindex":
                queue += [loc for loc in locs if loc]
                continue
            for el in root.findall("{*}url"):
                loc = (el.findtext("{*}loc") or "").strip()
                if loc and _match(params, loc):
                    yield {"url": loc, "lastmod": (el.findtext("{*}lastmod") or "").strip() or None, "fetch": True}
                    count += 1
                    if count >= params.limit:
                        return
    elif params.source == "file":
        for r in params.records:
            rec = {str(k).strip().lower(): (v.strip() if isinstance(v, str) else v) for k, v in r.items() if k}
            if not (rec.get("title") or rec.get("url")):
                continue
            if rec.get("url") and not rec.get("title") and not rec.get("body") and not rec.get("body_html"):
                rec["fetch"] = True
            yield rec
            count += 1
            if count >= params.limit:
                return


# --- Turning a record into a content item ----------------------------------------------------

PDF_LINK = re.compile(r"\((https?://[^)\s]+\.pdf(?:\?[^)\s]*)?)\)", re.I)
IMG_MD = re.compile(r"!\[([^\]]*)\]\((https?://[^)\s]+)\)")


async def _mirror(client: httpx.AsyncClient, url: str, by: str, cache: dict) -> Optional[dict]:
    if url in cache:
        return cache[url]
    try:
        data, _, final = await fetch(client, url, MAX_FILE)
        f = await store_file(data, urlparse(final).path.rsplit("/", 1)[-1] or "file", by)
    except (ValueError, HTTPException, httpx.HTTPError) as exc:
        logger.info("could not copy %s: %s", url, getattr(exc, "detail", exc))
        f = None
    cache[url] = f
    return f


async def build_item(client: httpx.AsyncClient, rec: dict, params: MigrationIn, *, by: str, mirror: bool, media_cache: dict) -> dict:
    url = rec.get("url") or ""
    if rec.get("error"):
        raise ValueError(rec["error"])
    if rec.get("fetch"):
        data, ctype, final = await fetch(client, url)
        if "html" not in ctype and not data.lstrip()[:15].lower().startswith((b"<!doctype", b"<html")):
            raise ValueError(f"Not a web page ({ctype or 'unknown type'})")
        page = extract_page(data.decode("utf-8", "replace"), final)
        rec = {**page, **{k: v for k, v in rec.items() if v and k not in ("fetch", "body_html", "url")}}
        url = final
    body_html, video = clean_html(rec.get("body_html") or "", url or site_url())
    body = rec.get("body") or to_markdown(body_html)
    title = (rec.get("title") or "").strip()[:200]
    if len(title) < 3:
        raise ValueError("No title found")
    summary = (rec.get("summary") or "").strip() or _text(body_html)[:280]
    summary = re.sub(r"\s*(\[…\]|\[\.\.\.\]|Continue reading.*|Read more.*)$", "", summary)[:600]
    path = urlparse(url).path if url else ""
    kind = normalise_type(rec.get("type")) or (params.fixed_type if params.type_mode == "fixed" else detect_type(path, " ".join(rec.get("categories") or []), rec.get("tag") or "", rec.get("wp_type") or "", title))
    slug = slugify(rec.get("slug") or (path.rstrip("/").rsplit("/", 1)[-1] if path.strip("/") else "") or title)
    slug = re.sub(r"\.(html?|php|aspx?)$", "", slug)
    cover = rec.get("cover_image") or rec.get("image")
    file_id = None
    pdf = rec.get("file_url") or (PDF_LINK.search(body).group(1) if params.attach_pdfs and PDF_LINK.search(body) else None)
    if mirror:
        for alt, src in list(dict.fromkeys(IMG_MD.findall(body)))[:30]:
            f = await _mirror(client, src, by, media_cache)
            if f and f["kind"] == "image":
                body = body.replace(f"]({src})", f"](/api/files/{f['id']}/{f['name']})")
        if cover and cover.startswith("http"):
            f = await _mirror(client, cover, by, media_cache)
            cover = f"/api/files/{f['id']}/{f['name']}" if f and f["kind"] == "image" else cover
        if pdf:
            f = await _mirror(client, pdf, by, media_cache)
            file_id = f["id"] if f and f["kind"] != "image" else None
    if cover and cover.startswith("http://"):
        cover = None
    gated = params.gated if file_id else False
    if isinstance(rec.get("gated"), str):
        gated = rec["gated"].lower() in ("1", "true", "yes", "y") and bool(file_id)
    return {
        "title": title, "type": kind, "slug": slug, "summary": summary, "body": body[:100_000], "tag": (rec.get("tag") or None) and str(rec["tag"])[:60],
        "author": (rec.get("author") or None) and str(rec["author"])[:120], "cover_image": cover, "file_id": file_id, "gated": gated,
        "video_url": rec.get("video_url") or video, "source_url": url or None,
        # A sitemap's lastmod is when the page last changed: only a fallback for the publish date.
        "date": rec.get("date") or rec.get("lastmod"),
        "products": detect_products(title, summary, " ".join(rec.get("categories") or [])),
        "seo_description": summary[:300] or None,
    }


# --- Jobs ------------------------------------------------------------------------------------------

async def _log(job_id: str, level: str, url: Optional[str], msg: str) -> None:
    entry = {"at": now_iso(), "level": level, "url": (url or "")[:500], "msg": str(msg)[:400]}
    await database.db.migration_jobs.update_one({"id": job_id}, {"$push": {"log": {"$each": [entry], "$slice": -LOG_KEEP}}})


async def run_job(job_id: str, params: MigrationIn, by: str) -> None:
    counts = {"found": 0, "processed": 0, "created": 0, "updated": 0, "skipped": 0, "failed": 0}
    await database.db.migration_jobs.update_one({"id": job_id}, {"$set": {"status": "running", "started_at": now_iso()}})

    async def log(level, url, msg):
        await _log(job_id, level, url, msg)

    sem = asyncio.Semaphore(CONCURRENCY)
    media_cache: dict = {}

    async def one(client, rec):
        async with sem:
            try:
                item = await build_item(client, rec, params, by=by, mirror=params.mirror_media, media_cache=media_cache)
                date = item.pop("date", None)
                outcome, doc = await upsert_imported(item, origin="import", status=params.status, date=date, by=by, on_conflict=params.on_conflict)
                counts[outcome] += 1
                if outcome != "skipped":
                    await log("info", rec.get("url"), f"{outcome}: {doc['title']} ({doc['type']})")
            except Exception as exc:
                counts["failed"] += 1
                await log("error", rec.get("url"), getattr(exc, "detail", None) or str(exc) or exc.__class__.__name__)
            counts["processed"] += 1
            await database.db.migration_jobs.update_one({"id": job_id}, {"$set": {"counts": counts}})

    try:
        async with new_client() as client:
            pending = []
            async for rec in discover(client, params, log):
                job = await database.db.migration_jobs.find_one({"id": job_id}, {"_id": 0, "cancel_requested": 1})
                if job and job.get("cancel_requested"):
                    break
                counts["found"] += 1
                pending.append(asyncio.create_task(one(client, rec)))
                if len(pending) >= CONCURRENCY * 4:
                    await asyncio.gather(*pending)
                    pending = []
            await asyncio.gather(*pending)
        job = await database.db.migration_jobs.find_one({"id": job_id}, {"_id": 0, "cancel_requested": 1})
        status = "cancelled" if job and job.get("cancel_requested") else "done"
        await database.db.migration_jobs.update_one({"id": job_id}, {"$set": {"status": status, "finished_at": now_iso(), "counts": counts}})
        if counts["created"] or counts["updated"]:
            _changed(f"migrated {counts['created'] + counts['updated']} items")
    except Exception as exc:
        logger.exception("migration %s failed", job_id)
        await log("error", params.url, f"Migration stopped: {exc}")
        await database.db.migration_jobs.update_one({"id": job_id}, {"$set": {"status": "failed", "finished_at": now_iso(), "counts": counts}})
    finally:
        _running.pop(job_id, None)


async def resume_interrupted() -> None:
    """Jobs still marked running belonged to a previous process: mark them interrupted.
    Re-running the same migration skips what was already imported."""
    await database.db.migration_jobs.update_many({"status": {"$in": ["queued", "running"]}}, {"$set": {"status": "interrupted", "finished_at": now_iso()}})


def _validate_params(params: MigrationIn) -> None:
    if params.source in ("link", "wordpress", "sitemap", "rss") and not params.url:
        raise HTTPException(status_code=422, detail="Enter the address to import from.")
    if params.source == "urls" and not [u for u in params.urls if u.strip()]:
        raise HTTPException(status_code=422, detail="Add at least one page address.")
    if params.source == "file" and not params.records:
        raise HTTPException(status_code=422, detail="The file has no rows to import.")
    if params.type_mode == "fixed" and params.fixed_type not in CONTENT_TYPES:
        raise HTTPException(status_code=422, detail="Unknown content type.")


@router.post("/preview", dependencies=[can_run])
async def preview(params: MigrationIn, sample: int = Query(8, ge=1, le=20)):
    """Dry run: find items and parse the first few without saving anything."""
    _validate_params(params)
    logs: list = []

    async def log(level, url, msg):
        logs.append({"level": level, "url": url, "msg": msg})

    probe = params.model_copy(update={"limit": min(params.limit, 500)})
    records, items = [], []
    async with new_client() as client:
        try:
            if params.url:
                await check_url(params.url)
            async for rec in discover(client, probe, log):
                records.append(rec)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Could not read the source: {exc}")

        async def parse(rec):
            try:
                it = await asyncio.wait_for(build_item(client, rec, params, by="preview", mirror=False, media_cache={}), 30)
                exists = await database.db.content.find_one({"$or": [{"source_url": it["source_url"]}, {"slug": it["slug"]}]} if it.get("source_url") else {"slug": it["slug"]}, {"_id": 0, "id": 1, "title": 1})
                return {**{k: it[k] for k in ("title", "type", "slug", "summary", "date", "cover_image", "source_url", "author", "tag", "products")},
                        "words": len(re.findall(r"\w+", it["body"])), "has_file": bool(PDF_LINK.search(it["body"]) or rec.get("file_url")),
                        "exists": bool(exists), "body_start": it["body"][:600]}
            except Exception as exc:
                return {"source_url": rec.get("url"), "error": getattr(exc, "detail", None) or str(exc) or exc.__class__.__name__}

        failed = [r for r in records if r.get("error")]
        logs += [{"level": "error", "url": r.get("url"), "msg": r["error"]} for r in failed]
        records = [r for r in records if not r.get("error")]
        items = await asyncio.gather(*(parse(r) for r in records[:sample]))
        available = await wp_types(client, params.url) if params.source == "wordpress" else []
    kinds: dict = {}
    for r in records:
        k = normalise_type(r.get("type")) or (params.fixed_type if params.type_mode == "fixed" else detect_type(urlparse(r.get("url") or "").path, " ".join(r.get("categories") or []), r.get("tag") or "", r.get("wp_type") or "", r.get("title") or ""))
        kinds[k] = kinds.get(k, 0) + 1
    return {"found": len(records), "capped": len(records) >= probe.limit, "items": items, "types": kinds, "wp_types_available": available, "log": logs[:20]}


@router.post("", status_code=201, dependencies=[can_run])
async def start(params: MigrationIn, user: dict = Depends(get_current_admin)):
    _validate_params(params)
    if params.url:
        try:
            await check_url(params.url)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
    if await database.db.migration_jobs.find_one({"status": {"$in": ["queued", "running"]}}, {"_id": 1}):
        raise HTTPException(status_code=409, detail="Another migration is still running. Wait for it to finish or cancel it.")
    job = {
        "id": uuid.uuid4().hex, "status": "queued", "created_at": now_iso(), "created_by": user["email"],
        "params": params.model_dump(exclude={"records", "urls"}) | {"urls_count": len(params.urls), "records_count": len(params.records)},
        "counts": {"found": 0, "processed": 0, "created": 0, "updated": 0, "skipped": 0, "failed": 0}, "log": [],
    }
    await database.db.migration_jobs.insert_one(dict(job))
    job.pop("_id", None)
    _running[job["id"]] = asyncio.create_task(run_job(job["id"], params, user["email"]))
    return job


@router.get("")
async def list_jobs(limit: int = Query(20, le=100)):
    return await database.db.migration_jobs.find({}, {"_id": 0, "log": 0}).sort("created_at", -1).to_list(limit)


@router.get("/redirects")
async def redirects(format: Literal["csv", "nginx", "apache", "json"] = "csv"):
    """Old URL -> new URL for everything migrated, to set up 301 redirects on the old site."""
    docs = await database.db.content.find({"source_url": {"$nin": [None, ""]}, "status": {"$ne": "archived"}}, {"_id": 0, "source_url": 1, "slug": 1, "type": 1}).to_list(10000)
    base = site_url()
    rows = [(d["source_url"], f"{base}/{'newsroom' if d['type'] == 'news' else 'resources'}/{d['slug']}") for d in docs]
    if format == "json":
        return [{"from": a, "to": b} for a, b in rows]
    if format == "csv":
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["old_url", "new_url"])
        w.writerows(rows)
        body, name = out.getvalue(), "redirects.csv"
    elif format == "nginx":
        body = "# Solix site migration: add inside the old site's server { } block\n" + "".join(f"location = {urlparse(a).path or '/'} {{ return 301 {b}; }}\n" for a, b in rows)
        name = "redirects.nginx.conf"
    else:
        body = "# Solix site migration: add to the old site's .htaccess\n" + "".join(f"Redirect 301 {urlparse(a).path or '/'} {b}\n" for a, b in rows)
        name = "redirects.htaccess"
    return PlainTextResponse(body, headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/{job_id}")
async def get_job(job_id: str):
    job = await database.db.migration_jobs.find_one({"id": job_id}, {"_id": 0})
    if not job:
        raise HTTPException(status_code=404, detail="Migration not found")
    return job


@router.post("/{job_id}/cancel", dependencies=[can_run])
async def cancel(job_id: str):
    res = await database.db.migration_jobs.update_one({"id": job_id, "status": {"$in": ["queued", "running"]}}, {"$set": {"cancel_requested": True}})
    if not res.matched_count:
        raise HTTPException(status_code=409, detail="This migration is not running.")
    return await get_job(job_id)
