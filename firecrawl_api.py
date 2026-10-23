"""Firecrawl (FIRECRAWL_API_KEY): web search and full-page reading for the Radar and the content research.

Costs on Firecrawl's plans: a search is 2 credits per 10 results (snippets only, no page reads), and reading a
page is 1 credit. The free plan has 1,000 credits a month and allows 10 searches and 10 page reads a minute,
two at a time, so callers keep result counts small and read only the few pages that matter. Running out of
credits is a 402; a rate limit is a 429, retried once after a short pause.
"""
from __future__ import annotations

import asyncio
import os
import re
from typing import List, Optional

import httpx

URL = "https://api.firecrawl.dev/v2"


def available() -> bool:
    return bool(os.environ.get("FIRECRAWL_API_KEY"))


async def _post(client: httpx.AsyncClient, path: str, body: dict, timeout: float) -> dict:
    for attempt in (0, 1):
        r = await client.post(f"{URL}{path}", json=body, timeout=timeout, headers={"Authorization": f"Bearer {os.environ['FIRECRAWL_API_KEY']}"})
        if r.status_code == 429 and attempt == 0:
            await asyncio.sleep(3)
            continue
        r.raise_for_status()
        return r.json()
    return {}


async def search(client: httpx.AsyncClient, query: str, limit: int = 5, tbs: Optional[str] = None) -> List[dict]:
    """Ranked pages with query-relevant highlights. `tbs` narrows by time, e.g. "qdr:y" for the past year."""
    body = {"query": query[:500], "limit": limit, "sources": ["web"], "ignoreInvalidURLs": True, **({"tbs": tbs} if tbs else {})}
    data = (await _post(client, "/search", body, timeout=60)).get("data") or {}
    web = data.get("web") if isinstance(data, dict) else data  # v1 answered with a plain list
    return [{"url": x["url"], "title": x.get("title") or (x.get("metadata") or {}).get("title") or x["url"], "date": "", "text": x.get("description") or ""}
            for x in web or [] if x.get("url")]


async def read(client: httpx.AsyncClient, url: str) -> dict:
    """A page's main content as plain text (1 credit)."""
    data = (await _post(client, "/scrape", {"url": url, "formats": ["markdown"], "onlyMainContent": True, "timeout": 30000}, timeout=60)).get("data") or {}
    meta = data.get("metadata") or {}
    return {"title": meta.get("title") or url, "text": plain(data.get("markdown") or ""), "date": (meta.get("publishedTime") or "")[:10]}


def plain(markdown: str) -> str:
    """Markdown to readable text: images dropped, links kept as their words."""
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", markdown)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"^[ \t]*(?:#+|>+|[*+\-](?=\s))[ \t]*", "", text, flags=re.M).replace("**", "")
    return re.sub(r"\n{3,}", "\n\n", text).strip()
