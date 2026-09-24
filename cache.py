"""Caching layer: a small in-process TTL cache plus HTTP caching helpers.

Public GET endpoints answer with Cache-Control and a strong ETag, so browsers,
the site's service worker and any CDN in front of the API can reuse responses
and revalidate with a cheap 304 instead of re-downloading. Expensive reads
(published content, leadership reports) are memoised for a short TTL so a
burst of traffic costs one database query, not hundreds.
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Callable, Awaitable, Optional

from fastapi import Request
from fastapi.responses import JSONResponse, Response

_store: dict[str, tuple[float, Any]] = {}
_generation: dict[str, int] = {}
MAX_KEYS = 500


def bump(namespace: str) -> None:
    """Invalidate every cached entry in a namespace (e.g. after publishing)."""
    _generation[namespace] = _generation.get(namespace, 0) + 1


def generation(namespace: str) -> int:
    return _generation.get(namespace, 0)


async def memo(namespace: str, key: str, ttl: float, produce: Callable[[], Awaitable[Any]]) -> Any:
    full = f"{namespace}:{generation(namespace)}:{key}"
    hit = _store.get(full)
    now = time.monotonic()
    if hit and hit[0] > now:
        return hit[1]
    value = await produce()
    if len(_store) >= MAX_KEYS:
        for k in sorted(_store, key=lambda k: _store[k][0])[: MAX_KEYS // 5]:
            _store.pop(k, None)
    _store[full] = (now + ttl, value)
    return value


def clear() -> None:
    _store.clear()


def cached_json(request: Request, payload: Any, *, max_age: int = 60, swr: int = 86400, private: bool = False) -> Response:
    """JSON response with an ETag; answers 304 when the client already has it."""
    body = json.dumps(payload, separators=(",", ":"), default=str).encode()
    etag = '"' + hashlib.sha1(body).hexdigest()[:20] + '"'
    scope = "private" if private else "public"
    headers = {"ETag": etag, "Cache-Control": f"{scope}, max-age={max_age}, stale-while-revalidate={swr}", "Vary": "Accept-Encoding"}
    inm = request.headers.get("if-none-match")
    if inm and etag in [t.strip() for t in inm.split(",")]:
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="application/json", headers=headers)


def no_store(payload: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status_code, headers={"Cache-Control": "no-store"})


def etag_matches(request: Request, etag: Optional[str]) -> bool:
    inm = request.headers.get("if-none-match")
    return bool(etag and inm and etag in [t.strip() for t in inm.split(",")])
