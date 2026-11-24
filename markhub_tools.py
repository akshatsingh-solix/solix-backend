"""MarkHub's staff tools, for AI assistants connected over MCP (mcp_server.py) and the admin's Tools console.

Every tool here is a thin, typed wrapper over an existing MarkHub API endpoint, called in-process *as the
signed-in user* (their own bearer token). So a tool can never do more than the person could do in the admin:
the endpoint's own role checks, access levels and validation still apply, on top of the registry's policy
(tool_registry.py: least privilege, read-only by default, dry run and confirmation before any change).

Writes and deletes have a dry run that looks the target up and says what would change, so a wrong record id,
a typo'd stage or a tool picked by mistake is caught before anything is touched.
"""
from __future__ import annotations

import re
from typing import Callable, Dict, List, Optional, Tuple

import httpx

import tool_registry as tr

BASE = "http://markhub.internal"


async def api(ctx: tr.Ctx, method: str, path: str, *, params: Optional[dict] = None, json: Optional[dict] = None) -> Tuple[int, object]:
    """Call MarkHub's own API in-process, as the caller."""
    import server
    headers = {"Authorization": ctx.authorization} if ctx.authorization else {}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url=BASE, timeout=60) as client:
        r = await client.request(method, path, params={k: v for k, v in (params or {}).items() if v not in (None, "")}, json=json, headers=headers)
    try:
        body = r.json()
    except ValueError:
        body = {"text": r.text[:2000]}
    return r.status_code, body


def _err(status: int, body) -> dict:
    detail = body.get("detail") if isinstance(body, dict) else body
    return {"ok": False, "status": status, "error": detail if isinstance(detail, (str, dict, list)) else str(detail)}


def _trim(obj, depth: int = 0):
    """Keep tool results small: models pay for every token, and long lists hide the answer."""
    if isinstance(obj, list):
        return [_trim(x, depth + 1) for x in obj[:25]]
    if isinstance(obj, dict):
        drop = {"history", "raw", "steps", "timeline", "text", "html", "body", "embedding"} if depth else set()
        return {k: _trim(v, depth + 1) for k, v in obj.items() if k not in drop and not k.startswith("_")}
    if isinstance(obj, str) and len(obj) > 600:
        return obj[:600] + "…"
    return obj


_PATH_ARG = re.compile(r"{(\w+)}")


def unwrap(obj):
    """Detail endpoints return {"lead": {...}, "timeline": ...} or {"account": {...}}: the record is the inner object."""
    if isinstance(obj, dict):
        for key in ("lead", "account", "company", "enrollment", "contact", "item"):
            if isinstance(obj.get(key), dict) and "id" in obj[key]:
                return obj[key]
    return obj


def api_tool(name: str, description: str, *, access: str, feature: str, method: str, path: str, props: Dict[str, dict],
             required: Tuple[str, ...] = (), examples: Tuple[str, ...] = (), body: Optional[Callable[[dict], dict]] = None,
             current: Optional[str] = None, check: Optional[Callable] = None, roles: Optional[Tuple[str, ...]] = None,
             min_level: Optional[str] = None, objects: Tuple[str, ...] = ()) -> None:
    """Register a tool that maps its arguments onto one endpoint. Path placeholders come from arguments; the
    rest go in the query string (GET) or the JSON body."""
    path_args = set(_PATH_ARG.findall(path))

    def split(args: dict) -> Tuple[str, dict]:
        url = _PATH_ARG.sub(lambda m: str(args.get(m.group(1), "")), path)
        rest = {k: v for k, v in args.items() if k not in path_args}
        return url, rest

    async def handler(args: dict, ctx: tr.Ctx) -> dict:
        url, rest = split(args)
        if method == "GET":
            status, out = await api(ctx, method, url, params=rest)
        else:
            status, out = await api(ctx, method, url, json=body(args) if body else rest)
        if status >= 400:
            return _err(status, out)
        return {"ok": True, "result": _trim(out)}

    async def validate(args: dict, ctx: tr.Ctx) -> dict:
        url, rest = split(args)
        preview: dict = {"will_call": f"{method} {url}", "with": body(args) if body else rest}
        problems: List[str] = []
        if current:
            cur_url = _PATH_ARG.sub(lambda m: str(args.get(m.group(1), "")), current)
            status, cur = await api(ctx, "GET", cur_url)
            if status == 404:
                problems.append(f"Nothing found at {cur_url}: check the id")
            elif status >= 400:
                problems.append(f"Couldn't read the current record ({status}): {_err(status, cur)['error']}")
            else:
                cur = unwrap(cur)
                preview["current"] = _trim(cur, 1)
                if check:
                    problems += await check(args, cur, ctx) or []
        elif check:
            problems += await check(args, None, ctx) or []
        return {"problems": problems, "preview": preview}

    params = {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}
    tr.REGISTRY.register(tr.Tool(name=name, description=description, parameters=params, access=access, feature=feature, handler=handler,
                                 validate=None if access == "read" else validate, surfaces=frozenset({"mcp", "admin"}), examples=examples,
                                 roles=frozenset(roles) if roles else None, min_level=min_level, objects=objects or OBJECTS.get(name) or OBJECTS.get(name.split("_")[0], ())))


# The things each family of tools acts on, as people name them. Used by the router's object term.
ACCOUNT = (r"\baccounts?\b", r"\bcompan(y|ies)\b", r"\bradar\b", r"\borg(anisation|anization)?s?\b")
LEAD = (r"\bleads?\b", r"\bmqls?\b", r"\bsqls?\b", r"marketing qualified", r"sales qualified", r"\bfunnel\b")
ENROLL = (r"\benrollments?\b", r"\bsequences?\b", r"\bdrafts?\b", r"approval", r"\boutreach\b")
REPLY = (r"\brepl(y|ies|ied)\b", r"\brespon(se|ses|ded)\b", r"\bmeeting\b", r"\banswered\b")
SUPPRESS = (r"suppress", r"opt(ed)?[ -]?out", r"unsubscrib", r"do not contact", r"\bdnc\b", r"block ?list")
GATE = (r"\bgate\b", r"allowed to", r"can we (e)?mail", r"blocked", r"compliant")
OBJECTS = {"radar": ACCOUNT, "leads": LEAD, "lead": LEAD, "content": (r"\bcontent\b", r"datasheet", r"white ?paper", r"case stud", r"\bblog", r"webinar", r"ebook"),
           "sol": (r"\bsol\b", r"\bchats?\b", r"visitor question", r"concierge"), "ops": (r"health", r"failing", r"broken", r"watchdog", r"alerts?", r"\bdown\b", r"system"),
           "reports": (r"pipeline", r"report", r"conversion", r"funnel", r"marketing (performance|numbers)")}
OBJECT_OF = {"outbound_kpis": (r"bounce", r"reply rate", r"open rate", r"variant", r"a/b", r"metrics?", r"kpis?", r"performance"),
             "outbound_contacts": (r"prospects?", r"contacts?\b"), "outbound_enrollments": ENROLL, "outbound_set_enrollment": ENROLL,
             "outbound_replies": REPLY, "outbound_log_reply": REPLY, "compliance_gate_check": GATE, "compliance_suppression_search": SUPPRESS,
             "compliance_suppress": SUPPRESS, "compliance_erase_person": (r"gdpr", r"erase", r"forget", r"everything about", r"all data"),
             "radar_research_queue": (r"research", r"brief", r"deep dive", r"dossier"), "radar_queue_research": (r"research", r"deep dive", r"investigate", r"brief"),
             "radar_summary": (r"overview", r"summary", r"radar (job|run|numbers)"),
             "lead_get": (r"lead \w*\d", r"timeline"), "radar_get_account": (r"account \w*\d", r"score"),
             "sol_teach_answer": (r"teach", r"approved answer", r"learned answer"), "sol_gaps": (r"gaps?", r"unanswered", r"can'?t answer"),
             "sol_insights": (r"insights?", r"\bchats?\b")}
OBJECTS.update(OBJECT_OF)

S = {"type": "string", "maxLength": 200}
ID = {"type": "string", "maxLength": 80, "description": "The record's id"}
PAGE = {"type": "integer", "minimum": 1, "maximum": 100, "description": "Results per page (default 20)"}
STAGES = ["lead", "mql", "sal", "sql", "opportunity", "won", "lost", "nurture", "disqualified"]


# --- Radar ---------------------------------------------------------------------------------------------------

api_tool("radar_search_accounts", "Search Radar accounts (companies with a competitive or buying signal) by name, owner, strength, status, country or competitor.",
         access="read", feature="radar", method="GET", path="/api/admin/radar/accounts",
         props={"q": S, "owner": S, "strength": {"type": "string", "enum": ["Strong", "Medium", "Weak"]}, "status": S, "country": S, "competitor": S, "page_size": PAGE},
         examples=("which strong accounts have no owner", "find the Acme account", "accounts in Germany with a Documentum signal", "show me my accounts",
                   "list companies hiring for Informatica", "top accounts by score"))
api_tool("radar_get_account", "One Radar account in full: signals with evidence, score breakdown, deep dive, contacts, owner and status.",
         access="read", feature="radar", method="GET", path="/api/admin/radar/accounts/{company_id}", props={"company_id": ID}, required=("company_id",),
         examples=("open the account", "why is this account scored so high", "what signals does this company have", "account details and contacts"))
api_tool("radar_summary", "Radar overview: account counts by strength and status, surges, unassigned accounts, last run.",
         access="read", feature="radar", method="GET", path="/api/admin/radar/summary", props={},
         examples=("radar overview", "how many accounts are surging", "how did the last radar run go", "summary of signals this week"))
api_tool("radar_research_queue", "Deep dives (account research briefs) waiting for review or already approved.",
         access="read", feature="radar", method="GET", path="/api/admin/radar/research", props={"status": {"type": "string", "enum": ["draft", "approved", "rejected"]}},
         examples=("which deep dives need review", "research briefs waiting for approval", "list approved dossiers"))
api_tool("radar_update_account", "Change a Radar account's owner, status or notes.", access="write", feature="radar", method="PATCH",
         path="/api/admin/radar/accounts/{company_id}", current="/api/admin/radar/accounts/{company_id}",
         props={"company_id": ID, "owner": S, "status": S, "notes": {"type": "string", "maxLength": 5000}, "reason": S}, required=("company_id",),
         examples=("assign this account to pat", "mark the account not a fit", "set the owner of Acme to me", "add a note to the account", "route account to the east rep"))
api_tool("radar_queue_research", "Queue a deep dive (cited research brief) for one account.", access="write", feature="radar", method="POST",
         path="/api/admin/radar/accounts/{company_id}/research", current="/api/admin/radar/accounts/{company_id}", props={"company_id": ID}, required=("company_id",),
         examples=("research this account", "run a deep dive on Globex", "investigate the company and draft a brief"))


# --- Leads -----------------------------------------------------------------------------------------------------

async def _stage_check(args: dict, cur, ctx) -> List[str]:
    if args.get("stage") and cur and cur.get("stage") == args["stage"]:
        return [f"The lead is already at {args['stage'].upper()}"]
    if not any(args.get(k) for k in ("stage", "owner", "notes")):
        return ["Nothing to change: give a stage, owner or notes"]
    return []

api_tool("leads_search", "Search leads (people in the funnel) by name or email, stage, owner, channel, country or minimum score.",
         access="read", feature="leads", method="GET", path="/api/admin/leads",
         props={"q": S, "stage": {"type": "string", "enum": STAGES}, "owner": S, "channel": S, "country": S, "min_score": {"type": "number", "minimum": 0}, "page_size": PAGE},
         examples=("show MQLs from this week", "find the lead jane@acme.com", "which leads does pat own", "hot leads over score 60", "leads that came from outbound"))
api_tool("lead_get", "One lead in full: score, stage, owner, timeline, linked account and outbound history.", access="read", feature="leads", method="GET",
         path="/api/admin/leads/{lead_id}", props={"lead_id": ID}, required=("lead_id",),
         examples=("open this lead", "what has this person done on the site", "lead timeline", "why is this lead an MQL"))
api_tool("lead_update", "Move a lead to another stage, change its owner or add notes.", access="write", feature="leads", method="PATCH",
         path="/api/admin/leads/{lead_id}", current="/api/admin/leads/{lead_id}", check=_stage_check,
         props={"lead_id": ID, "stage": {"type": "string", "enum": STAGES}, "owner": S, "notes": {"type": "string", "maxLength": 5000}, "reason": S}, required=("lead_id",),
         examples=("move this lead to SQL", "mark the lead as won", "assign the lead to sam", "disqualify this lead", "change the stage to opportunity"))
api_tool("reports_overview", "Pipeline and marketing report: leads by source and stage, MQL trend, outbound and Radar influence, account funnel.",
         access="read", feature="dashboard", method="GET", path="/api/admin/reports/overview", props={},
         examples=("how is pipeline looking", "marketing report", "how many MQLs this month", "what did outbound contribute to pipeline", "funnel conversion"))


# --- Outbound (EchoLocate) ----------------------------------------------------------------------------------------

async def _approve_check(args: dict, cur, ctx) -> List[str]:
    if not cur:
        return []
    action = args.get("action") or "approve"
    status = cur.get("status")
    if action == "approve" and status != "pending_approval":
        return [f"Only enrollments waiting for approval can be approved; this one is {status}"]
    if action in ("pause", "stop") and status not in ("pending_approval", "scheduled", "active"):
        return [f"This enrollment is {status}; there's nothing to {action}"]
    if action == "resume" and status != "paused":
        return [f"Only paused enrollments can be resumed; this one is {status}"]
    if action == "approve" and cur.get("contact_id"):
        code, gate = await api(ctx, "GET", f"/api/admin/compliance/gate/{cur['contact_id']}")
        if code < 400 and not gate.get("ok"):
            return [f"Fails the compliance gate: {'; '.join(gate.get('reasons') or [])}"]
    return []

api_tool("outbound_kpis", "EchoLocate outbound performance: sends, bounce, replies, positive replies, meetings, per-sequence and per-variant results, day-30 checklist.",
         access="read", feature="outbound", method="GET", path="/api/admin/outbound/kpis", props={"days": {"type": "integer", "minimum": 1, "maximum": 365}},
         examples=("how is outbound doing", "what's our bounce rate", "reply rate this month", "which variant is winning", "outbound metrics"))
api_tool("outbound_contacts", "Search outbound contacts (prospects) by name, email or account, with verification and gate status.", access="read", feature="outbound",
         method="GET", path="/api/admin/outbound/contacts", props={"q": S, "company_id": ID, "status": S, "page_size": PAGE},
         examples=("find the contact at Acme", "which prospects are unverified", "contacts for this account"))
api_tool("outbound_enrollments", "Outbound enrollments (a contact in a sequence): waiting for approval, scheduled, active, paused, replied or stopped.",
         access="read", feature="outbound", method="GET", path="/api/admin/outbound/enrollments",
         props={"status": {"type": "string", "enum": ["pending_approval", "scheduled", "active", "paused", "replied", "completed", "stopped", "needs_review"]}, "company_id": ID, "page_size": PAGE},
         examples=("what is waiting for approval", "list the approval queue", "which sequences are active", "paused enrollments", "drafts to review"))
api_tool("outbound_replies", "Classified replies to outbound email: interested, meeting, objection, referral, out of office, not interested, unsubscribe.",
         access="read", feature="outbound", method="GET", path="/api/admin/outbound/replies", props={"label": S, "page_size": PAGE},
         examples=("who replied", "show interested replies", "any meeting requests", "replies this week", "positive responses"))
api_tool("outbound_set_enrollment", "Approve, reject, pause, resume or stop one outbound enrollment. Approval re-runs the compliance gate.",
         access="write", feature="outbound", method="PATCH", path="/api/admin/outbound/enrollments/{enrollment_id}",
         current="/api/admin/outbound/enrollments/{enrollment_id}", check=_approve_check,
         props={"enrollment_id": ID, "action": {"type": "string", "enum": ["approve", "reject", "pause", "resume", "stop"]}, "reason": S}, required=("enrollment_id", "action"),
         examples=("approve this draft", "approve the email to jordan", "pause the sequence for this contact", "reject this enrollment", "stop emailing them", "resume the sequence"))
api_tool("outbound_log_reply", "Log a reply received outside the tools (e.g. a forwarded email) so it is classified and acted on.", access="write", feature="outbound",
         method="POST", path="/api/admin/outbound/replies",
         props={"contact_id": ID, "email": {"type": "string", "format": "email", "maxLength": 200}, "text": {"type": "string", "minLength": 1, "maxLength": 10000},
                "label": {"type": "string", "enum": ["interested", "meeting", "objection", "referral", "forward", "ooo", "not_interested", "unsubscribe", "neutral"]}},
         required=("text",), examples=("log this reply from jordan", "record that they said not now", "they replied by phone that they're interested"))


# --- Compliance -------------------------------------------------------------------------------------------------

async def _erase_check(args: dict, cur, ctx) -> List[str]:
    return [] if args.get("confirm") is True else ["Erasure deletes this person everywhere and can't be undone: set confirm to true"]

api_tool("compliance_gate_check", "Run the outbound compliance gate for one contact: suppression, country policy, LIA, verification, score, overlap. Says exactly what blocks sending.",
         access="read", feature="compliance", method="GET", path="/api/admin/compliance/gate/{contact_id}", props={"contact_id": ID}, required=("contact_id",),
         examples=("can we email this contact", "why is this contact blocked", "check the gate for jordan", "is this person allowed to be contacted"))
api_tool("compliance_suppression_search", "Search the suppression list (unsubscribes, bounces, do-not-contact, erasures).", access="read", feature="compliance",
         method="GET", path="/api/admin/compliance/suppression", props={"q": S, "reason": S},
         examples=("is this email suppressed", "who unsubscribed this week", "list do not contact entries"))
api_tool("compliance_suppress", "Add email addresses to the suppression list (do not contact). Takes effect for every channel immediately.",
         access="write", feature="compliance", method="POST", path="/api/admin/compliance/suppression",
         props={"emails": {"type": "array", "items": {"type": "string"}, "maxItems": 500}, "reason": {"type": "string", "enum": ["dnc", "unsubscribed", "not_interested", "complaint", "manual"]}},
         required=("emails",), examples=("add this address to do not contact", "suppress these emails", "they asked us to stop, block them"))
api_tool("compliance_erase_person", "GDPR Art. 17 erasure: delete one person from every MarkHub collection and queue deletion in connected tools; only a hash stays on the suppression list.",
         access="delete", feature="compliance", method="POST", path="/api/admin/compliance/erasure", check=_erase_check,
         props={"email": {"type": "string", "format": "email", "maxLength": 200}, "confirm": {"type": "boolean"}}, required=("email",),
         roles=("admin", "compliance"), min_level="edit",
         examples=("erase this person under gdpr", "delete all data about jane@acme.com", "right to be forgotten request"))


# --- Sol, content, operations -------------------------------------------------------------------------------------

api_tool("sol_gaps", "Questions visitors asked Sol that the site content doesn't answer well, by frequency.", access="read", feature="sol", method="GET",
         path="/api/admin/sol/gaps", props={"status": S},
         examples=("what can't sol answer", "knowledge gaps", "unanswered visitor questions", "what should we write content about"))
api_tool("sol_insights", "Sol conversation insights: volume, topics, lead capture, guard blocks, answer quality.", access="read", feature="sol", method="GET",
         path="/api/admin/sol/insights", props={},
         examples=("how is sol doing", "chat insights", "how many chats became leads", "what do visitors ask about"))
api_tool("sol_teach_answer", "Teach Sol an approved answer to a question; it is used from the next message on.", access="write", feature="sol", method="POST",
         path="/api/admin/sol/learned",
         props={"question": {"type": "string", "minLength": 3, "maxLength": 500}, "answer": {"type": "string", "minLength": 10, "maxLength": 4000}},
         required=("question", "answer"), examples=("teach sol this answer", "add a learned answer about pricing", "sol should answer this question like this"))
api_tool("content_search", "Search published and draft content (blogs, white papers, datasheets, case studies, webinars).", access="read", feature="content",
         method="GET", path="/api/admin/content", props={"q": S, "status": S, "type": S},
         examples=("find the SAP whitepaper", "which case studies are published", "drafts waiting to publish", "content about healthcare"))
api_tool("ops_health", "Watchdog health checks: Radar run, outbound sender, data sources, deliveries, bounce pause, model shutdown dates.",
         access="read", feature="settings", method="GET", path="/api/admin/ops/health", props={},
         examples=("is everything healthy", "what is broken", "watchdog status", "any alerts", "is the sender running"))
