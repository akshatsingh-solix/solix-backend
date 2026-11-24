"""One registry for every tool an AI can call in MarkHub: Sol (visitors), the MCP endpoint (staff
assistants such as Claude Desktop) and the admin's own tool console.

Four guarantees, enforced here in code rather than left to a model's judgement:

1. Dynamic tool retrieval and deferred loading.
   Each surface sends a model only the tools a fast semantic router (semantic.py) ranks as relevant
   to the request, plus a small core set, instead of every schema on every call. Fewer schemas mean
   fewer tokens and fewer wrong picks. Routing is in-process and takes well under a millisecond.

2. Least-privilege scoping.
   A tool belongs to one MarkHub feature (permissions.py) and declares what it does: read, write or
   delete. A caller sees and can call a tool only when their access level on that feature allows it
   (read needs "view", write needs "edit", delete needs "full" unless the tool names the roles that
   may delete). Visitors only ever see visitor tools.

3. Read/write separation with a read-only default.
   Every staff session starts in read mode. Write and delete tools are neither listed nor callable
   until the caller opts in to write mode, and an admin can switch every write off from one switch.

4. Dry run before any change.
   A write or delete first runs a dry run: the arguments are checked against the tool's schema, the
   target is looked up, and the tool's own validator says exactly what would change and what would
   block it. Staff then get a single-use confirmation token tied to the user, the tool and the exact
   arguments (10 minutes). The real call is refused without it, so a model that picked the wrong
   tool, the wrong record or the wrong arguments changes nothing. Visitor tools (book a demo, save my
   details) run their dry-run validation automatically, because the visitor's own message is the
   confirmation.

Every route, dry run, call and refusal is written to `tool_audit` (arguments with personal values
masked).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, FrozenSet, List, Optional, Tuple

import semantic

logger = logging.getLogger("solix.tools")

ACCESS = ("read", "write", "delete")
LEVEL_FOR = {"read": "view", "write": "edit", "delete": "full"}
CONFIRM_TTL = timedelta(minutes=10)
SETTINGS_KEY = "tool_policy"
_policy_cache: Tuple[float, dict] = (0.0, {})

Handler = Callable[[dict, "Ctx"], Awaitable[dict]]


@dataclass
class Ctx:
    surface: str                      # "sol" | "mcp" | "admin"
    user: Optional[dict] = None       # signed-in staff user; None for a website visitor
    mode: str = "read"                # "read" (default) or "write"
    authorization: Optional[str] = None  # the caller's own bearer token, for tools that call MarkHub's API as them
    extra: dict = field(default_factory=dict)

    @property
    def who(self) -> str:
        return (self.user or {}).get("email") or f"visitor:{self.extra.get('session_id') or '?'}"


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    access: str
    handler: Handler
    surfaces: FrozenSet[str]
    feature: Optional[str] = None             # permissions.FEATURES key; None for visitor tools
    examples: Tuple[str, ...] = ()            # how people ask for it: the router's training text
    validate: Optional[Handler] = None        # dry run: {"ok", "preview", "problems"}
    roles: Optional[FrozenSet[str]] = None    # only these roles (admins always), on top of the level check
    min_level: Optional[str] = None           # override LEVEL_FOR[access]
    confirm: bool = True                      # staff writes need a confirmation token from a dry run
    core: bool = False                        # always loaded on its surfaces
    objects: Tuple[str, ...] = ()             # regexes for the things this tool acts on ("lead", "account"...)

    def __post_init__(self):
        assert self.access in ACCESS, self.access
        assert re.fullmatch(r"[a-z][a-z0-9_]{2,63}", self.name), self.name

    def openai_schema(self) -> dict:
        return {"type": "function", "function": {"name": self.name, "description": self.description, "parameters": self.parameters}}

    def mcp_schema(self) -> dict:
        return {"name": self.name, "description": self.description + ("" if self.access == "read" else f" ({self.access.upper()}: dry run first, then confirm.)"),
                "inputSchema": self.parameters,
                "annotations": {"readOnlyHint": self.access == "read", "destructiveHint": self.access == "delete", "idempotentHint": self.access == "read"}}

    def tokens(self) -> int:
        return len(json.dumps(self.openai_schema())) // 4 + 4


class PolicyError(Exception):
    def __init__(self, message: str, code: str = "denied"):
        super().__init__(message)
        self.code = code


# --- argument validation (a small JSON-schema subset: enough to stop malformed calls) -------------------------

_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "object": dict, "array": list}


def check_args(schema: dict, args: Any) -> List[str]:
    problems: List[str] = []
    if not isinstance(args, dict):
        return ["Arguments must be an object"]
    props = schema.get("properties") or {}
    for req in schema.get("required") or []:
        if args.get(req) in (None, ""):
            problems.append(f"'{req}' is required")
    if schema.get("additionalProperties") is False:
        for k in args:
            if k not in props:
                problems.append(f"Unknown argument '{k}'")
    for k, v in args.items():
        spec = props.get(k)
        if not spec or v is None:
            continue
        t = spec.get("type")
        if t and not isinstance(v, _TYPES.get(t, object)) or (t == "integer" and isinstance(v, bool)):
            problems.append(f"'{k}' must be {t}")
            continue
        if "enum" in spec and v not in spec["enum"]:
            problems.append(f"'{k}' must be one of {', '.join(map(str, spec['enum']))}")
        if isinstance(v, str):
            if spec.get("maxLength") and len(v) > spec["maxLength"]:
                problems.append(f"'{k}' is longer than {spec['maxLength']} characters")
            if spec.get("minLength") and len(v.strip()) < spec["minLength"]:
                problems.append(f"'{k}' is too short")
            if spec.get("format") == "email" and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", v.strip()):
                problems.append(f"'{k}' is not an email address")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            if "minimum" in spec and v < spec["minimum"]:
                problems.append(f"'{k}' must be at least {spec['minimum']}")
            if "maximum" in spec and v > spec["maximum"]:
                problems.append(f"'{k}' must be at most {spec['maximum']}")
    return problems


# --- audit -------------------------------------------------------------------------------------------------------

_MASK = re.compile(r"(email|phone|name|text|answer|notes|question|message)$", re.I)


def _masked(args: dict) -> dict:
    out = {}
    for k, v in (args or {}).items():
        if isinstance(v, str) and _MASK.search(k):
            out[k] = (hashlib.sha256(v.lower().encode()).hexdigest()[:10] + "…") if "email" in k.lower() else f"<{len(v)} chars>"
        else:
            out[k] = v if isinstance(v, (int, float, bool)) or v is None else str(v)[:120]
    return out


async def audit(phase: str, ctx: Ctx, tool: Optional[str], args: Optional[dict] = None, *, ok: bool = True, detail: str = "", ms: Optional[float] = None) -> None:
    try:
        import database
        await database.db.tool_audit.insert_one({
            "at": datetime.now(timezone.utc).isoformat(), "at_dt": datetime.now(timezone.utc), "phase": phase, "surface": ctx.surface, "user": ctx.who, "role": (ctx.user or {}).get("role") or "visitor",
            "mode": ctx.mode, "tool": tool, "args": _masked(args or {}), "ok": ok, "detail": detail[:300], "ms": ms})
    except Exception:  # noqa: BLE001 - auditing must never break the call it records
        logger.exception("tool audit write failed")


# --- confirmation tokens -------------------------------------------------------------------------------------

def _secret() -> bytes:
    return (os.environ.get("TOOL_CONFIRM_SECRET") or os.environ.get("JWT_SECRET") or "dev-only").encode()


def args_hash(args: dict) -> str:
    return hashlib.sha256(json.dumps(args or {}, sort_keys=True, default=str).encode()).hexdigest()[:32]


def make_token(tool: str, ctx: Ctx, args: dict) -> Tuple[str, str]:
    exp = int((datetime.now(timezone.utc) + CONFIRM_TTL).timestamp())
    body = {"t": tool, "u": (ctx.user or {}).get("id") or ctx.who, "a": args_hash(args), "e": exp, "n": secrets.token_hex(8)}
    raw = base64.urlsafe_b64encode(json.dumps(body, separators=(",", ":")).encode()).decode().rstrip("=")
    sig = hmac.new(_secret(), raw.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{raw}.{sig}", datetime.fromtimestamp(exp, timezone.utc).isoformat()


async def use_token(token: str, tool: str, ctx: Ctx, args: dict) -> None:
    try:
        raw, sig = token.rsplit(".", 1)
        if not hmac.compare_digest(sig, hmac.new(_secret(), raw.encode(), hashlib.sha256).hexdigest()[:32]):
            raise ValueError
        body = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except Exception:  # noqa: BLE001
        raise PolicyError("The confirmation token is not valid. Run the dry run again.", "bad_token")
    if body.get("t") != tool:
        raise PolicyError("That confirmation token is for a different tool.", "bad_token")
    if body.get("u") != ((ctx.user or {}).get("id") or ctx.who):
        raise PolicyError("That confirmation token belongs to someone else.", "bad_token")
    if body.get("a") != args_hash(args):
        raise PolicyError("The arguments changed since the dry run. Run the dry run again with these arguments.", "args_changed")
    if body.get("e", 0) < time.time():
        raise PolicyError("The confirmation expired. Run the dry run again.", "expired")
    import database
    try:
        await database.db.tool_confirmations.insert_one({"nonce": body["n"], "tool": tool, "user": ctx.who, "used_at": datetime.now(timezone.utc).isoformat(),
                                                          "expires_at": datetime.fromtimestamp(body["e"], timezone.utc) + timedelta(days=1)})
    except Exception as e:  # noqa: BLE001 - duplicate key: the token was already used
        if "duplicate" in str(e).lower() or "E11000" in str(e):
            raise PolicyError("That confirmation was already used. Each change needs its own dry run.", "token_used")
        raise


# --- policy settings (admin switches) ------------------------------------------------------------------------

async def policy_settings() -> dict:
    global _policy_cache
    if time.monotonic() - _policy_cache[0] < 15:
        return _policy_cache[1]
    try:
        import database
        doc = await database.db.settings.find_one({"key": SETTINGS_KEY}, {"_id": 0}) or {}
    except Exception:  # noqa: BLE001
        doc = {}
    val = {"writes_enabled": doc.get("writes_enabled", True), "disabled": list(doc.get("disabled") or []),
           "mcp_enabled": doc.get("mcp_enabled", True), "sol_deferred": doc.get("sol_deferred", True)}
    _policy_cache = (time.monotonic(), val)
    return val


def clear_policy_cache() -> None:
    global _policy_cache
    _policy_cache = (0.0, {})


# --- the registry ----------------------------------------------------------------------------------------------

@dataclass
class Route:
    tools: List[Tuple[str, float]]
    ms: float


class Registry:
    def __init__(self) -> None:
        self.tools: Dict[str, Tool] = {}
        self._index: Dict[str, semantic.VectorIndex] = {}

    def register(self, tool: Tool) -> Tool:
        self.tools[tool.name] = tool
        self._index.clear()
        return tool

    def get(self, name: str) -> Optional[Tool]:
        return self.tools.get(name)

    def on(self, surface: str) -> List[Tool]:
        return [t for t in self.tools.values() if surface in t.surfaces]

    # Router -----------------------------------------------------------------------------------------------
    def _router(self, surface: str) -> semantic.VectorIndex:
        idx = self._index.get(surface)
        if idx is None:
            idx = semantic.VectorIndex()
            for t in self.on(surface):
                label = t.name.replace("_", " ")
                idx.add(f"{label} {label} {t.description}", {"tool": t.name})
                for ex in t.examples:
                    idx.add(ex, {"tool": t.name})
            self._index[surface] = idx.build(max_df=0.6)
        return idx

    def route(self, query: str, surface: str, k: int = 3, names: Optional[set] = None) -> Route:
        """Top-k tools for a request.

        Score = semantic similarity to the tool's description and examples (semantic.py)
              + intent: does the request read, change or delete, and does that match the tool's access?
              + object: does the request name the thing the tool acts on (a lead, an account, a reply...)?
        The intent and object terms are what separate "show lead 42" from "give lead 42 to Jordan", which
        share almost every word."""
        start = time.perf_counter()
        pool = [t for t in self.on(surface) if names is None or t.name in names]
        sem: Dict[str, float] = {}
        for score, item in self._router(surface).search(query, k=60, where=(lambda it: it["tool"] in names) if names is not None else None):
            if score > sem.get(item["tool"], 0.0):
                sem[item["tool"]] = score
        act = action_of(query)
        best: Dict[str, float] = {}
        for t in pool:
            s = sem.get(t.name, 0.0)
            if act:
                s += 0.18 if t.access == act else (-0.12 if act == "read" or t.access == "read" else -0.05)
            if t.objects and any(re.search(o, query, re.I) for o in t.objects):
                s += 0.22
            if s > 0:
                best[t.name] = s
        ranked = sorted(best.items(), key=lambda kv: -kv[1])[:k]
        return Route(tools=[(n, round(s, 3)) for n, s in ranked], ms=round((time.perf_counter() - start) * 1000, 3))

    # Policy -----------------------------------------------------------------------------------------------
    async def check(self, tool: Tool, ctx: Ctx) -> Optional[str]:
        """Why this caller may not use this tool, or None."""
        pol = await policy_settings()
        if ctx.surface not in tool.surfaces:
            return f"{tool.name} isn't available here"
        if tool.name in pol["disabled"]:
            return f"{tool.name} is switched off by an admin"
        if ctx.user is None:
            return None if tool.feature is None else "Sign in to use this tool"
        if tool.feature is None:
            return "This tool is for website visitors only"
        if tool.access != "read":
            if ctx.mode != "write":
                return "This session is read-only. Reconnect in write mode to make changes"
            if not pol["writes_enabled"]:
                return "Changes through AI tools are switched off by an admin"
        role = ctx.user.get("role")
        if tool.roles and role != "admin" and role not in tool.roles:
            return f"Your role ({role}) can't use {tool.name}"
        import permissions
        need = tool.min_level or LEVEL_FOR[tool.access]
        if not await permissions.can(ctx.user, tool.feature, need):
            return f"Needs '{need}' access to {permissions.FEATURES[tool.feature]['label']}"
        return None

    async def visible(self, ctx: Ctx) -> List[Tool]:
        return [t for t in self.on(ctx.surface) if await self.check(t, ctx) is None]

    async def select(self, query: str, ctx: Ctx, k: int = 3, *, must: Tuple[str, ...] = (), min_score: float = 0.05) -> Tuple[List[Tool], Route]:
        """Deferred loading: the core tools, anything the caller insisted on, and the router's top k above a floor."""
        allowed = {t.name for t in await self.visible(ctx)}
        r = self.route(query, ctx.surface, k=k, names=allowed)
        names = [t.name for t in self.on(ctx.surface) if t.core and t.name in allowed]
        names += [n for n in must if n in allowed and n not in names]
        names += [n for n, s in r.tools if s >= min_score and n not in names]
        await audit("route", ctx, None, {"query": f"<{len(query)} chars>", "picked": ",".join(names)}, ms=r.ms)
        return [self.tools[n] for n in names], r

    # Dry run and execution -----------------------------------------------------------------------------------
    async def dry_run(self, name: str, args: dict, ctx: Ctx) -> dict:
        tool = self.get(name)
        if not tool:
            raise PolicyError(f"Unknown tool {name}", "unknown_tool")
        why = await self.check(tool, ctx)
        if why:
            await audit("denied", ctx, name, args, ok=False, detail=why)
            raise PolicyError(why)
        problems = check_args(tool.parameters, args)
        preview: Any = None
        if not problems and tool.validate:
            res = await tool.validate(args, ctx)
            problems += res.get("problems") or []
            preview = res.get("preview")
        out = {"tool": name, "access": tool.access, "ok": not problems, "problems": problems, "preview": preview}
        if not problems and tool.access != "read" and tool.confirm and ctx.user is not None:
            out["confirm_token"], out["expires_at"] = make_token(name, ctx, args)
        await audit("dry_run", ctx, name, args, ok=not problems, detail="; ".join(problems))
        return out

    async def execute(self, name: str, args: dict, ctx: Ctx, *, confirm_token: Optional[str] = None) -> dict:
        tool = self.get(name)
        if not tool:
            raise PolicyError(f"Unknown tool {name}", "unknown_tool")
        why = await self.check(tool, ctx)
        if why:
            await audit("denied", ctx, name, args, ok=False, detail=why)
            raise PolicyError(why)
        problems = check_args(tool.parameters, args)
        if problems:
            await audit("rejected", ctx, name, args, ok=False, detail="; ".join(problems))
            raise PolicyError("; ".join(problems), "bad_args")
        if tool.access != "read":
            if tool.confirm and ctx.user is not None:
                if not confirm_token:
                    raise PolicyError(f"{name} changes data: call it with dry_run first, check the preview, then call again with the confirm_token.", "needs_dry_run")
                await use_token(confirm_token, name, ctx, args)
            elif tool.validate:  # a visitor tool: the dry-run validation still has to pass
                res = await tool.validate(args, ctx)
                if res.get("problems"):
                    await audit("rejected", ctx, name, args, ok=False, detail="; ".join(res["problems"]))
                    return {"ok": False, "error": "; ".join(res["problems"])}
        start = time.perf_counter()
        try:
            result = await tool.handler(args, ctx)
        except PolicyError:
            raise
        except Exception as e:  # noqa: BLE001
            await audit("error", ctx, name, args, ok=False, detail=str(e), ms=round((time.perf_counter() - start) * 1000, 1))
            raise
        ok = not (isinstance(result, dict) and result.get("ok") is False)
        await audit("execute", ctx, name, args, ok=ok, ms=round((time.perf_counter() - start) * 1000, 1),
                    detail="" if ok else str((result or {}).get("error") or "")[:300])
        return result


# What a request wants to do. "Give me X" reads; "give X to Y" changes. Questions read.
_DELETE = re.compile(r"\b(erase|delete|purge|wipe|forget (me|them|him|her)|remove (everything|all (data|records))|right to be forgotten)\b", re.I)
_WRITE = re.compile(r"\b(make|set|assign|reassign|give (?!me\b|us\b)\w+.{0,40}\bto\b|change|move|bump|promote|close|mark|flag|halt|stop|pause|resume|approve|"
                    r"sign off|reject|record|log|add|put|teach|queue|kick off|start|suppress|block|update|disqualify|route|unsubscribe them|opt (them|him|her) out)\b", re.I)
_QUESTION = re.compile(r"^\s*(what|which|who|whom|how|is|are|was|were|did|do|does|has|have|any|can|could|should|why|when|where|show|list|find|look|pull up|give me|see|get)\b", re.I)


def action_of(query: str) -> Optional[str]:
    if _DELETE.search(query):
        return "delete"
    if _QUESTION.search(query) and not re.search(r"\b(and|then|please) (approve|assign|move|set|stop|pause)\b", query, re.I):
        return "read"
    if _WRITE.search(query):
        return "write"
    return None


REGISTRY = Registry()


def tool(**kw) -> Callable[[Handler], Handler]:
    """Decorator: @tool(name=..., description=..., parameters=..., access=..., surfaces=...)."""
    def wrap(fn: Handler) -> Handler:
        kw.setdefault("surfaces", frozenset({"mcp", "admin"}))
        kw["surfaces"] = frozenset(kw["surfaces"])
        if kw.get("roles"):
            kw["roles"] = frozenset(kw["roles"])
        kw["examples"] = tuple(kw.get("examples") or ())
        REGISTRY.register(Tool(handler=fn, **kw))
        return fn
    return wrap
