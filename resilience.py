"""Keep MarkHub usable through restarts and server errors.

Two ASGI middlewares, mounted *inside* the CORS middleware so every response they produce carries CORS headers:

1. JSON errors. An unhandled exception used to become Starlette's plain 500 from its outermost layer, outside
   CORS, so the browser couldn't read it and the admin only ever said "Network Error". Now it is a JSON 500 with a
   reference id that is also in the server log.

2. Idempotency keys for writes. The admin sends an `Idempotency-Key` header with every POST/PUT/PATCH/DELETE and
   retries through a restart (a deploy, a replaced instance: 30-120 s on Render's free plan). A retry must never
   do the same thing twice, so the first response for a key is stored for a day and a retry with the same key
   gets that stored response instead of running again. A request still being processed answers 409 "in progress";
   a request that failed with a 5xx (or never finished) leaves no record, so its retry runs normally.
   Keys are scoped to the caller (a hash of their Authorization header), the method and the path.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("solix.resilience")

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
SKIP_PREFIXES = ("/api/chat/stream", "/api/mcp", "/api/outbound/webhooks")  # streams, and callers with their own retry/dedupe
MAX_STORED_BODY = 512 * 1024
KEY_TTL = timedelta(days=1)
IN_PROGRESS_WAIT_S = 20


def _json(status: int, payload: dict, extra_headers=()):
    body = json.dumps(payload).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()), *extra_headers]
    return [{"type": "http.response.start", "status": status, "headers": headers}, {"type": "http.response.body", "body": body}]


class JSONErrors:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started = False

        async def tracked(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)
        try:
            await self.app(scope, receive, tracked)
        except Exception:  # noqa: BLE001
            ref = uuid.uuid4().hex[:8]
            logger.exception("unhandled error %s on %s %s", ref, scope.get("method"), scope.get("path"))
            if started:
                raise  # the response is already on its way; nothing better can be sent
            for m in _json(500, {"detail": f"Something went wrong on the server (ref {ref}). It has been logged; please try again.", "ref": ref}):
                await send(m)


class Idempotency:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") not in WRITE_METHODS or scope.get("path", "").startswith(SKIP_PREFIXES):
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers") or []}
        raw = (headers.get("idempotency-key") or "").strip()
        if not raw or len(raw) > 200:
            return await self.app(scope, receive, send)
        try:
            import database
            db = database.db
            who = hashlib.sha256((headers.get("authorization") or "anonymous").encode()).hexdigest()[:16]
            key = hashlib.sha256(f"{who}|{scope['method']}|{scope['path']}|{raw}".encode()).hexdigest()
            prior = await self._claim(db, key)
        except Exception:  # noqa: BLE001 - the database being unreachable must not block the request itself
            logger.exception("idempotency lookup failed; handling the request without it")
            return await self.app(scope, receive, send)

        if prior is not None:
            if prior.get("state") == "done":
                hdrs = [(b"content-type", (prior.get("content_type") or "application/json").encode()), (b"idempotent-replay", b"true")]
                body = prior.get("body") or b""
                await send({"type": "http.response.start", "status": prior["status"], "headers": hdrs + [(b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
            for m in _json(409, {"detail": "This request is still being processed. It will finish on its own; refresh in a moment.", "code": "in_progress"}):
                await send(m)
            return

        status, ctype, chunks, size, ok = 500, "application/json", [], 0, True

        async def capture(message):
            nonlocal status, ctype, size, ok
            if message["type"] == "http.response.start":
                status = message["status"]
                for k, v in message.get("headers") or []:
                    if k.lower() == b"content-type":
                        ctype = v.decode()
            elif message["type"] == "http.response.body":
                size += len(message.get("body") or b"")
                if size <= MAX_STORED_BODY:
                    chunks.append(message.get("body") or b"")
                else:
                    ok = False
            await send(message)

        try:
            await self.app(scope, receive, capture)
        except BaseException:
            await self._release(db, key)
            raise
        if status >= 500 or not ok or "text/event-stream" in ctype:
            await self._release(db, key)  # failed or not storable: let a retry run again
        else:
            try:
                await db.idempotency.update_one({"key": key}, {"$set": {"state": "done", "status": status, "content_type": ctype, "body": b"".join(chunks),
                                                                        "done_at": datetime.now(timezone.utc)}})
            except Exception:  # noqa: BLE001
                logger.exception("could not store an idempotent response")

    async def _claim(self, db, key: str):
        """None when this request should run; the existing record when it shouldn't."""
        from pymongo import ReturnDocument
        now = datetime.now(timezone.utc)
        # Atomic "insert if missing": returns nothing when this call created the record (run the request), or the
        # record someone else created first. Doesn't depend on the unique index existing yet.
        existing = await db.idempotency.find_one_and_update(
            {"key": key}, {"$setOnInsert": {"key": key, "state": "running", "created_at": now, "expires_at": now + KEY_TTL}},
            upsert=True, return_document=ReturnDocument.BEFORE, projection={"_id": 0})
        if existing is None:
            return None
        # Someone sent this before: wait briefly if it's still running (the first attempt may be just about done).
        for _ in range(IN_PROGRESS_WAIT_S * 2):
            doc = await db.idempotency.find_one({"key": key}, {"_id": 0})
            if not doc:  # the first attempt failed and released the key: run this one
                return await self._claim(db, key)
            if doc.get("state") == "done":
                return doc
            created = doc.get("created_at")
            if created and created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if created and now - created > timedelta(minutes=5):
                await db.idempotency.delete_one({"key": key, "state": "running"})  # abandoned by a restart mid-request
                return await self._claim(db, key)
            await asyncio.sleep(0.5)
        return doc

    async def _release(self, db, key: str) -> None:
        try:
            await db.idempotency.delete_one({"key": key, "state": "running"})
        except Exception:  # noqa: BLE001
            logger.exception("could not release an idempotency key")
