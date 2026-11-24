"""MarkHub as an MCP server, plus the admin's Tools & MCP console API.

Endpoint: POST /api/mcp  (MCP "Streamable HTTP" transport, JSON responses; protocol 2025-06-18)
Auth:     Authorization: Bearer <MarkHub MCP token>  (Admin -> Tools & MCP -> Connect an assistant)

Why it stays stable in production
---------------------------------
* Deferred loading by default. `tools/list` returns two small meta tools, `find_tools` and `call_tool`,
  instead of every schema. The assistant asks `find_tools` for what it needs; the semantic router answers in
  well under a millisecond with the top matches and their full schemas. Context stays small however many
  tools MarkHub grows, and the assistant picks from 3 likely tools, not 30. Clients that can't do this can
  connect with `?tools=all` to list every tool they're allowed.
* Least privilege. A token is issued by a signed-in staff member and carries their identity and a mode.
  Read tokens (the default) can only see and call read tools. Write tokens still need the user's own access
  level on the feature, and every write or delete needs a dry run and its confirmation token.
* Calls run as the user through MarkHub's own API, so every existing validation and permission check applies.
* Per-token rate limit, a timeout per call, an admin kill switch, and every call in `tool_audit`.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

import database
import markhub_tools  # noqa: F401  (registers the staff tools)
import tool_registry as tr
from auth import JWT_ALGORITHM, create_access_token, get_current_admin

PROTOCOL = "2025-06-18"
SERVER_INFO = {"name": "markhub", "title": "MarkHub (Solix)", "version": "1.0.0"}
CALL_TIMEOUT = float(os.environ.get("MCP_CALL_TIMEOUT", "60"))
RATE = (int(os.environ.get("MCP_RATE_PER_MINUTE", "60")), 60)
_hits: Dict[str, deque] = defaultdict(deque)

public = APIRouter(prefix="/api", tags=["mcp"])
router = APIRouter(prefix="/api/admin/tools", tags=["tools"], dependencies=[Depends(get_current_admin)])


def _db():
    return database.db


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- MCP tokens --------------------------------------------------------------------------------------------------

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def issue_token(user: dict, mode: str, days: int, label: str) -> dict:
    jti = secrets.token_hex(12)
    exp = _now() + timedelta(days=days)
    token = jwt.encode({"sub": user["id"], "email": user["email"], "type": "mcp", "mode": mode, "jti": jti, "exp": exp}, os.environ["JWT_SECRET"], algorithm=JWT_ALGORITHM)
    await _db().mcp_tokens.insert_one({"jti": jti, "user_id": user["id"], "email": user["email"], "mode": mode, "label": label[:80], "hash": _token_hash(token),
                                       "created_at": _now().isoformat(), "expires_at": exp.isoformat(), "revoked": False, "last_used_at": None})
    return {"token": token, "jti": jti, "mode": mode, "expires_at": exp.isoformat()}


async def _mcp_user(request: Request) -> tuple:
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ""
    if not token:
        raise HTTPException(status_code=401, detail="Missing MCP token", headers={"WWW-Authenticate": "Bearer"})
    try:
        payload = jwt.decode(token, os.environ["JWT_SECRET"], algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired MCP token", headers={"WWW-Authenticate": "Bearer"})
    if payload.get("type") != "mcp":
        raise HTTPException(status_code=401, detail="Use an MCP token from Admin > Tools & MCP, not a login session")
    rec = await _db().mcp_tokens.find_one({"jti": payload.get("jti")}, {"_id": 0})
    if not rec or rec.get("revoked"):
        raise HTTPException(status_code=401, detail="This MCP token was revoked")
    user = await _db().users.find_one({"id": payload["sub"]}, {"_id": 0, "password_hash": 0})
    if not user or user.get("disabled"):
        raise HTTPException(status_code=401, detail="User not found")
    await _db().mcp_tokens.update_one({"jti": rec["jti"]}, {"$set": {"last_used_at": _now().isoformat()}})
    # The header can narrow a write token to read for one session, never widen a read token.
    mode = "read" if request.headers.get("X-MarkHub-Mode", "").lower() == "read" else payload.get("mode", "read")
    return user, mode, rec["jti"]


def _limited(key: str) -> bool:
    n, window = RATE
    q, now = _hits[key], time.monotonic()
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= n:
        return True
    q.append(now)
    return False


# --- meta tools (deferred loading) ---------------------------------------------------------------------------------

FIND = {"name": "find_tools", "title": "Find MarkHub tools",
        "description": "Describe what you want to do in MarkHub (e.g. 'approve outbound drafts', 'which accounts have no owner'). Returns the best matching tools with "
                       "their input schemas. Call this first, then call_tool.",
        "inputSchema": {"type": "object", "properties": {"query": {"type": "string", "maxLength": 500}, "limit": {"type": "integer", "minimum": 1, "maximum": 8}},
                        "required": ["query"], "additionalProperties": False},
        "annotations": {"readOnlyHint": True}}
CALL = {"name": "call_tool", "title": "Call a MarkHub tool",
        "description": "Run a tool found with find_tools. Read tools run at once. A WRITE or DELETE tool must first be called with dry_run=true: it returns a preview of "
                       "exactly what would change and a confirm_token; show the preview to the user, and only if they agree call again with the same arguments and the confirm_token.",
        "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}, "arguments": {"type": "object"}, "dry_run": {"type": "boolean"},
                                                         "confirm_token": {"type": "string"}}, "required": ["name"], "additionalProperties": False},
        "annotations": {"readOnlyHint": False}}


def _with_dry_run(schema: dict, tool: tr.Tool) -> dict:
    if tool.access == "read":
        return schema
    s = json.loads(json.dumps(schema))
    props = s["inputSchema"].setdefault("properties", {})
    props["dry_run"] = {"type": "boolean", "description": "Preview the change without making it (required before the real call)"}
    props["confirm_token"] = {"type": "string", "description": "Token from the dry run; required to make the change"}
    return s


def _text(obj: Any, error: bool = False) -> dict:
    out = {"content": [{"type": "text", "text": json.dumps(obj, default=str, ensure_ascii=False)[:60000]}], "isError": error}
    if isinstance(obj, dict) and not error:
        out["structuredContent"] = obj
    return out


async def run_tool(name: str, args: dict, ctx: tr.Ctx, *, dry_run: bool = False, confirm_token: Optional[str] = None) -> dict:
    tool = tr.REGISTRY.get(name)
    if not tool or ctx.surface not in tool.surfaces:
        return _text({"error": f"Unknown tool '{name}'. Use find_tools to see what you can call."}, True)
    try:
        if dry_run:
            return _text(await asyncio.wait_for(tr.REGISTRY.dry_run(name, args, ctx), CALL_TIMEOUT))
        res = await asyncio.wait_for(tr.REGISTRY.execute(name, args, ctx, confirm_token=confirm_token), CALL_TIMEOUT)
        return _text(res, isinstance(res, dict) and res.get("ok") is False)
    except tr.PolicyError as e:
        return _text({"error": str(e), "code": e.code}, True)
    except asyncio.TimeoutError:
        await tr.audit("timeout", ctx, name, args, ok=False)
        return _text({"error": f"{name} took longer than {int(CALL_TIMEOUT)}s and was stopped"}, True)


async def handle(msg: dict, ctx: tr.Ctx, list_all: bool) -> Optional[dict]:
    method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}

    def ok(result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def err(code, message):
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}

    if mid is None:  # a notification (e.g. notifications/initialized): no response
        return None
    if method == "initialize":
        return ok({"protocolVersion": PROTOCOL, "capabilities": {"tools": {"listChanged": False}}, "serverInfo": SERVER_INFO,
                   "instructions": ("MarkHub is Solix's marketing and sales hub (Radar accounts, leads, EchoLocate outbound, compliance, Sol). "
                                    "Use find_tools to discover tools for a task, then call_tool. This connection is "
                                    + ("WRITE-enabled: every change needs a dry run, the user's agreement, then the confirm_token." if ctx.mode == "write"
                                       else "READ-ONLY: you can look things up but not change anything."))})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        if list_all:
            return ok({"tools": [_with_dry_run(t.mcp_schema(), t) for t in await tr.REGISTRY.visible(ctx)]})
        return ok({"tools": [FIND, CALL]})
    if method == "tools/call":
        name, args = params.get("name"), dict(params.get("arguments") or {})
        if name == "find_tools":
            q = str(args.get("query") or "")[:500]
            picked, route = await tr.REGISTRY.select(q, ctx, k=int(args.get("limit") or 3), min_score=0.02)
            return ok(_text({"tools": [_with_dry_run(t.mcp_schema(), t) | {"access": t.access} for t in picked], "routed_in_ms": route.ms,
                             "mode": ctx.mode, "note": None if picked else "Nothing you can use matches; your role or a read-only token may limit what's available."}))
        if name == "call_tool":
            inner = dict(args.get("arguments") or {})
            return ok(await run_tool(str(args.get("name") or ""), inner, ctx, dry_run=bool(args.get("dry_run")), confirm_token=args.get("confirm_token")))
        dry, token = bool(args.pop("dry_run", False)), args.pop("confirm_token", None)
        return ok(await run_tool(str(name or ""), args, ctx, dry_run=dry, confirm_token=token))
    return err(-32601, f"Method not found: {method}")


@public.post("/mcp")
async def mcp_endpoint(request: Request):
    if not (await tr.policy_settings())["mcp_enabled"]:
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32000, "message": "MCP access is switched off by a MarkHub admin"}}, status_code=503)
    user, mode, jti = await _mcp_user(request)
    if _limited(jti):
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32000, "message": "Rate limit: too many calls, slow down"}}, status_code=429)
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}, status_code=400)
    ctx = tr.Ctx(surface="mcp", user=user, mode=mode, authorization=f"Bearer {create_access_token(user['id'], user['email'])}", extra={"jti": jti})
    list_all = request.query_params.get("tools") == "all"
    batch = body if isinstance(body, list) else [body]
    out = [r for r in [await handle(m, ctx, list_all) for m in batch if isinstance(m, dict)] if r is not None]
    if not out:
        return Response(status_code=202)
    return JSONResponse(out if isinstance(body, list) else out[0], headers={"MCP-Protocol-Version": PROTOCOL})


@public.get("/mcp")
async def mcp_get():
    # No server-initiated stream: this server only answers requests.
    return Response(status_code=405, headers={"Allow": "POST"})


# --- Admin: Tools & MCP console -------------------------------------------------------------------------------------

def _admin_ctx(user: dict, request: Request, mode: str = "read") -> tr.Ctx:
    return tr.Ctx(surface="admin", user=user, mode=mode, authorization=request.headers.get("Authorization"))


@router.get("")
async def list_tools(request: Request, user: dict = Depends(get_current_admin)):
    out = []
    for t in sorted(tr.REGISTRY.tools.values(), key=lambda t: (t.feature or "", t.access, t.name)):
        if "admin" not in t.surfaces and "mcp" not in t.surfaces:
            continue
        why_read = await tr.REGISTRY.check(t, _admin_ctx(user, request, "read"))
        why_write = await tr.REGISTRY.check(t, _admin_ctx(user, request, "write"))
        out.append({"name": t.name, "description": t.description, "access": t.access, "feature": t.feature, "parameters": t.parameters,
                    "allowed": why_write is None, "allowed_read_only": why_read is None, "why_not": why_write, "tokens": t.tokens(), "examples": list(t.examples[:3])})
    sol = [{"name": t.name, "access": t.access, "core": t.core, "tokens": t.tokens()} for t in tr.REGISTRY.on("sol")]
    return {"tools": out, "sol_tools": sol, "policy": await tr.policy_settings(), "role": user.get("role")}


@router.get("/route")
async def route(q: str, surface: str = "mcp", k: int = 3, user: dict = Depends(get_current_admin)):
    if surface not in ("mcp", "sol", "admin"):
        raise HTTPException(status_code=422, detail="surface must be mcp, sol or admin")
    r = tr.REGISTRY.route(q[:500], surface, k=max(1, min(k, 8)))
    all_tokens = sum(t.tokens() for t in tr.REGISTRY.on(surface))
    picked_tokens = sum(tr.REGISTRY.tools[n].tokens() for n, _ in r.tools)
    return {"tools": [{"name": n, "score": s, "access": tr.REGISTRY.tools[n].access} for n, s in r.tools], "ms": r.ms,
            "tokens_all_schemas": all_tokens, "tokens_routed": picked_tokens}


class CallIn(BaseModel):
    tool: str = Field(max_length=64)
    args: Dict[str, Any] = Field(default_factory=dict)
    confirm_token: Optional[str] = Field(default=None, max_length=1000)


@router.post("/dry-run")
async def dry_run(body: CallIn, request: Request, user: dict = Depends(get_current_admin)):
    try:
        return await tr.REGISTRY.dry_run(body.tool, body.args, _admin_ctx(user, request, "write"))
    except tr.PolicyError as e:
        raise HTTPException(status_code=403, detail={"message": str(e), "code": e.code})


@router.post("/execute")
async def execute(body: CallIn, request: Request, user: dict = Depends(get_current_admin)):
    try:
        return await tr.REGISTRY.execute(body.tool, body.args, _admin_ctx(user, request, "write"), confirm_token=body.confirm_token)
    except tr.PolicyError as e:
        raise HTTPException(status_code=403 if e.code == "denied" else 409, detail={"message": str(e), "code": e.code})


def _admin_only(user: dict) -> None:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admins only")


@router.get("/audit")
async def audit_log(tool: Optional[str] = None, phase: Optional[str] = None, limit: int = 100, user: dict = Depends(get_current_admin)):
    q: dict = {}
    if user.get("role") != "admin":
        q["user"] = user["email"]  # staff see their own trail; admins see everyone's
    if tool:
        q["tool"] = tool
    if phase:
        q["phase"] = phase
    items = await _db().tool_audit.find(q, {"_id": 0}).sort("at", -1).to_list(max(1, min(limit, 500)))
    return {"items": items}


class PolicyIn(BaseModel):
    writes_enabled: Optional[bool] = None
    mcp_enabled: Optional[bool] = None
    sol_deferred: Optional[bool] = None
    disabled: Optional[List[str]] = None


@router.put("/policy")
async def put_policy(body: PolicyIn, user: dict = Depends(get_current_admin)):
    _admin_only(user)
    vals = {k: v for k, v in body.model_dump().items() if v is not None}
    if "disabled" in vals:
        vals["disabled"] = [n for n in vals["disabled"] if n in tr.REGISTRY.tools]
    await _db().settings.update_one({"key": tr.SETTINGS_KEY}, {"$set": {**vals, "updated_by": user["email"], "updated_at": _now().isoformat()}}, upsert=True)
    tr.clear_policy_cache()
    return await tr.policy_settings()


class TokenIn(BaseModel):
    mode: str = Field(default="read", pattern="^(read|write)$")
    days: int = Field(default=30, ge=1, le=90)
    label: str = Field(default="AI assistant", max_length=80)


@router.post("/mcp-tokens", status_code=201)
async def create_token(body: TokenIn, user: dict = Depends(get_current_admin)):
    if body.mode == "write" and user.get("role") == "viewer":
        raise HTTPException(status_code=403, detail="Read-only users can only create read tokens")
    out = await issue_token(user, body.mode, body.days, body.label)
    base = (os.environ.get("PUBLIC_API_URL") or "").rstrip("/")
    out["endpoint"] = f"{base}/api/mcp" if base else "/api/mcp"
    out["note"] = "Shown once. Store it in your assistant's MCP settings as a Bearer token."
    return out


@router.get("/mcp-tokens")
async def list_tokens(user: dict = Depends(get_current_admin)):
    q = {} if user.get("role") == "admin" else {"user_id": user["id"]}
    return {"items": await _db().mcp_tokens.find(q, {"_id": 0, "hash": 0}).sort("created_at", -1).to_list(200)}


@router.delete("/mcp-tokens/{jti}", status_code=204)
async def revoke_token(jti: str, user: dict = Depends(get_current_admin)):
    rec = await _db().mcp_tokens.find_one({"jti": jti}, {"_id": 0})
    if not rec or (rec["user_id"] != user["id"] and user.get("role") != "admin"):
        raise HTTPException(status_code=404, detail="Token not found")
    await _db().mcp_tokens.update_one({"jti": jti}, {"$set": {"revoked": True, "revoked_by": user["email"], "revoked_at": _now().isoformat()}})
    return Response(status_code=204)


@router.get("/router-eval")
async def router_eval(user: dict = Depends(get_current_admin)):
    import tool_router_eval
    return tool_router_eval.evaluate()
