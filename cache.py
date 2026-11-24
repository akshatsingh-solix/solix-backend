"""Caching layer: a small in-process TTL cache plus HTTP caching helpers.

Public GET endpoints answer with Cache-Control and a strong ETag, so browsers,
the site's service worker and any CDN in front of the API can reuse responses
and revalidate with a cheap 304 instead of re-downloading. Expensive reads
(published content, leadership reports) are memoised for a short TTL so a
burst of traffic costs one database query, not hundreds.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any, Callable, Awaitable, Optional

from fastapi import Request
from fastapi.responses import JSONResponse, Response

_store: dict[str, tuple[float, Any]] = {}
_generation: dict[str, int] = {}
_inflight: dict[str, "asyncio.Future"] = {}
MAX_KEYS = 200


def bump(namespace: str) -> None:
    """Invalidate every cached entry in a namespace (e.g. after publishing).

    The old entries are dropped right away. They used to stay in memory until 500 keys had piled up, and the
    Radar bumps its account list on every account change, so each page load after a change kept another full copy
    of every account: on a 512 MB instance that ran the API out of memory."""
    _generation[namespace] = _generation.get(namespace, 0) + 1
    prefix = f"{namespace}:"
    for k in [k for k in _store if k.startswith(prefix)]:
        _store.pop(k, None)


def generation(namespace: str) -> int:
    return _generation.get(namespace, 0)


def _prune(now: float) -> None:
    for k in [k for k, (exp, _) in _store.items() if exp <= now]:
        _store.pop(k, None)
    if len(_store) >= MAX_KEYS:
        for k in sorted(_store, key=lambda k: _store[k][0])[: len(_store) - MAX_KEYS + MAX_KEYS // 5]:
            _store.pop(k, None)


async def memo(namespace: str, key: str, ttl: float, produce: Callable[[], Awaitable[Any]]) -> Any:
    full = f"{namespace}:{generation(namespace)}:{key}"
    hit = _store.get(full)
    now = time.monotonic()
    if hit and hit[0] > now:
        return hit[1]
    # Callers arriving while the value is being produced wait for that one result instead of each building
    # their own copy (ten admins' auto-refresh landing together was ten full account lists in memory at once).
    pending = _inflight.get(full)
    if pending is not None:
        return await asyncio.shield(pending)
    fut = asyncio.get_running_loop().create_future()
    _inflight[full] = fut
    try:
        value = await produce()
    except BaseException as e:
        fut.set_exception(e)
        fut.exception()  # mark retrieved: waiters get it, nobody logs "never retrieved"
        raise
    finally:
        _inflight.pop(full, None)
    fut.set_result(value)
    _prune(now)
    if full.split(":", 2)[1] == str(generation(namespace)):  # not invalidated while it was loading
        _store[full] = (now + ttl, value)
    return value


def size() -> dict:
    """Entries per namespace (Ops > Health)."""
    out: dict = {}
    for k in _store:
        ns = k.split(":", 1)[0]
        out[ns] = out.get(ns, 0) + 1
    return out


def clear() -> None:
    _store.clear()
    _inflight.clear()


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
