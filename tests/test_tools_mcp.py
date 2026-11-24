"""Tool registry, policy layer and the MCP endpoint (tool_registry.py, markhub_tools.py, mcp_server.py).

What must hold, whatever a model does:
* routing is fast and picks the right tool (evals/tool_router_golden.json),
* read-only is the default; writes need write mode, the user's own access level, a dry run and its token,
* a confirmation token works once, for that user, that tool and exactly those arguments,
* a viewer can never change anything; visitors never see staff tools."""
import os

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "solix_test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("ADMIN_EMAIL", "admin@example.com")
os.environ.setdefault("ADMIN_PASSWORD", "AdminPass!1")
os.environ["OPS_LOOP"] = "0"

import asyncio
import json
import sys

import pytest
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient

import database
import server  # noqa: F401
import tool_registry as tr
import tool_router_eval

PATCHED = ("server", "intent", "leads_admin", "admin", "auth", "emailer", "accounts", "delivery", "chat", "chat_leads", "content", "events", "press", "site_settings", "seo", "knowledge")
_loop = asyncio.new_event_loop()


def run(coro):
    return _loop.run_until_complete(coro)


@pytest.fixture(scope="module")
def admin():
    if getattr(database.db, "client", None) is database.client:
        database.db = AsyncMongoMockClient()["solix_test"]
    for name in PATCHED:
        mod = sys.modules.get(name)
        if mod is not None and hasattr(mod, "db"):
            mod.db = database.db
    tr.clear_policy_cache()
    with TestClient(server.app) as c:
        token = c.post("/api/auth/login", json={"email": os.environ["ADMIN_EMAIL"], "password": os.environ["ADMIN_PASSWORD"]}).json()["access_token"]
        c.headers.update({"Authorization": f"Bearer {token}"})
        yield c


def _user(admin, email, role):
    r = admin.post("/api/admin/users", json={"email": email, "name": "Tool Tester", "role": role})
    assert r.status_code == 201, r.text
    c = TestClient(server.app)
    tok = c.post("/api/auth/login", json={"email": email, "password": r.json()["temporary_password"]}).json()["access_token"]
    c.headers.update({"Authorization": f"Bearer {tok}"})
    return c


def _mcp(client, token, method, params=None, mid=1, path="/api/mcp"):
    r = client.post(path, json={"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}}, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    return r.json()


def _call(client, token, name, args, **extra):
    res = _mcp(client, token, "tools/call", {"name": "call_tool", "arguments": {"name": name, "arguments": args, **extra}})["result"]
    return json.loads(res["content"][0]["text"]), res["isError"]


def _lead(email="pat.buyer@globex.example", stage="mql"):
    async def go():
        await database.db.leads.insert_one({"id": "lead-tools-1", "email": email, "name": "Pat Buyer", "stage": stage, "score": 40, "history": [], "created_at": "2026-11-01T00:00:00+00:00"})
    run(go())
    return "lead-tools-1"


# --- router -------------------------------------------------------------------------------------------------------

def test_router_meets_accuracy_and_latency_gates():
    out = tool_router_eval.evaluate()
    for s in ("mcp", "mcp_heldout", "sol"):
        assert out[s]["top3"] >= 0.95, out[s]["misses"]
        assert out[s]["p95_ms"] <= 15.0
    # Deferred loading: the top 3 schemas are a fraction of all of them.
    assert out["mcp"]["tokens_top3_avg"] < out["mcp"]["tokens_all_schemas"] / 4


@pytest.mark.parametrize("q,act", [("give lead 42 to Jordan", "write"), ("give me the radar summary", "read"), ("which leads are hot", "read"),
                                   ("delete all data about jane@acme.example", "delete"), ("approve enrollment 7", "write")])
def test_action_of_separates_reading_from_changing(q, act):
    assert tr.action_of(q) == act


def test_bad_arguments_never_reach_a_handler():
    schema = {"type": "object", "properties": {"stage": {"type": "string", "enum": ["mql", "sql"]}, "n": {"type": "integer", "maximum": 5}}, "required": ["stage"],
              "additionalProperties": False}
    assert tr.check_args(schema, {"stage": "won"}) and tr.check_args(schema, {"stage": "mql", "n": 9}) and tr.check_args(schema, {"stage": "mql", "x": 1})
    assert tr.check_args(schema, {"n": 1}) and not tr.check_args(schema, {"stage": "sql", "n": 2})


# --- policy through the admin console ---------------------------------------------------------------------------------

def test_write_needs_a_dry_run_and_the_token_works_once(admin):
    lead_id = _lead()
    blind = admin.post("/api/admin/tools/execute", json={"tool": "lead_update", "args": {"lead_id": lead_id, "stage": "sql"}})
    assert blind.status_code == 409 and blind.json()["detail"]["code"] == "needs_dry_run"
    dry = admin.post("/api/admin/tools/dry-run", json={"tool": "lead_update", "args": {"lead_id": lead_id, "stage": "sql"}}).json()
    assert dry["ok"] and dry["confirm_token"] and dry["preview"]["current"]["stage"] == "mql"
    assert run(database.db.leads.find_one({"id": lead_id}))["stage"] == "mql"  # a dry run changes nothing
    changed = admin.post("/api/admin/tools/execute", json={"tool": "lead_update", "args": {"lead_id": lead_id, "stage": "opportunity"}, "confirm_token": dry["confirm_token"]})
    assert changed.status_code == 409 and changed.json()["detail"]["code"] == "args_changed"
    ok = admin.post("/api/admin/tools/execute", json={"tool": "lead_update", "args": {"lead_id": lead_id, "stage": "sql"}, "confirm_token": dry["confirm_token"]})
    assert ok.status_code == 200 and ok.json()["ok"], ok.text
    assert run(database.db.leads.find_one({"id": lead_id}))["stage"] == "sql"
    again = admin.post("/api/admin/tools/execute", json={"tool": "lead_update", "args": {"lead_id": lead_id, "stage": "sql"}, "confirm_token": dry["confirm_token"]})
    assert again.json()["detail"]["code"] == "token_used"
    phases = [a["phase"] for a in admin.get("/api/admin/tools/audit", params={"tool": "lead_update"}).json()["items"]]
    assert {"dry_run", "execute", "rejected"} & set(phases) and "execute" in phases


def test_dry_run_catches_a_wrong_record_and_a_no_op(admin):
    missing = admin.post("/api/admin/tools/dry-run", json={"tool": "lead_update", "args": {"lead_id": "no-such-lead", "stage": "sql"}}).json()
    assert not missing["ok"] and "check the id" in missing["problems"][0] and "confirm_token" not in missing
    same = admin.post("/api/admin/tools/dry-run", json={"tool": "lead_update", "args": {"lead_id": "lead-tools-1", "stage": "sql"}}).json()
    assert not same["ok"] and "already at SQL" in same["problems"][0]
    bad = admin.post("/api/admin/tools/dry-run", json={"tool": "lead_update", "args": {"lead_id": "lead-tools-1", "stage": "maybe"}}).json()
    assert not bad["ok"] and "must be one of" in bad["problems"][0]


def test_viewer_can_read_but_never_write(admin):
    viewer = _user(admin, "viewer.tools@example.com", "viewer")
    tools = {t["name"]: t for t in viewer.get("/api/admin/tools").json()["tools"]}
    assert tools["leads_search"]["allowed_read_only"] and not tools["lead_update"]["allowed"]
    r = viewer.post("/api/admin/tools/dry-run", json={"tool": "lead_update", "args": {"lead_id": "lead-tools-1", "stage": "won"}})
    assert r.status_code == 403
    erase = viewer.post("/api/admin/tools/dry-run", json={"tool": "compliance_erase_person", "args": {"email": "x@y.example", "confirm": True}})
    assert erase.status_code == 403


def test_sales_cannot_erase_but_compliance_can_dry_run_it(admin):
    sales = _user(admin, "sales.tools@example.com", "sales")
    assert sales.post("/api/admin/tools/dry-run", json={"tool": "compliance_erase_person", "args": {"email": "x@y.example", "confirm": True}}).status_code == 403
    comp = _user(admin, "comp.tools@example.com", "compliance")
    need_confirm = comp.post("/api/admin/tools/dry-run", json={"tool": "compliance_erase_person", "args": {"email": "x@y.example"}}).json()
    assert not need_confirm["ok"] and "can't be undone" in need_confirm["problems"][0]


def test_admin_kill_switch_stops_every_write(admin):
    assert admin.put("/api/admin/tools/policy", json={"writes_enabled": False}).status_code == 200
    r = admin.post("/api/admin/tools/dry-run", json={"tool": "lead_update", "args": {"lead_id": "lead-tools-1", "stage": "won"}})
    assert r.status_code == 403 and "switched off" in r.json()["detail"]["message"]
    admin.put("/api/admin/tools/policy", json={"writes_enabled": True})


# --- MCP -------------------------------------------------------------------------------------------------------------

def test_mcp_read_token_defers_tools_and_cannot_write(admin):
    tok = admin.post("/api/admin/tools/mcp-tokens", json={"mode": "read", "label": "Claude Desktop"}).json()["token"]
    init = _mcp(admin, tok, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})["result"]
    assert init["serverInfo"]["name"] == "markhub" and "READ-ONLY" in init["instructions"]
    listed = [t["name"] for t in _mcp(admin, tok, "tools/list")["result"]["tools"]]
    assert listed == ["find_tools", "call_tool"]  # deferred: two schemas, not every tool
    found = json.loads(_mcp(admin, tok, "tools/call", {"name": "find_tools", "arguments": {"query": "move lead 42 to SQL"}})["result"]["content"][0]["text"])
    assert all(t["access"] == "read" for t in found["tools"])  # a read token is never offered a write tool
    out, err = _call(admin, tok, "leads_search", {"q": "pat.buyer"})
    assert not err and out["ok"]
    out, err = _call(admin, tok, "lead_update", {"lead_id": "lead-tools-1", "stage": "won"}, dry_run=True)
    assert err and "read-only" in out["error"]
    every = [t["name"] for t in _mcp(admin, tok, "tools/list", path="/api/mcp?tools=all")["result"]["tools"]]
    assert "leads_search" in every and "lead_update" not in every


def test_mcp_write_token_full_round_trip_and_revocation(admin):
    issued = admin.post("/api/admin/tools/mcp-tokens", json={"mode": "write", "label": "ops assistant"}).json()
    tok = issued["token"]
    found = json.loads(_mcp(admin, tok, "tools/call", {"name": "find_tools", "arguments": {"query": "move this lead to opportunity"}})["result"]["content"][0]["text"])
    assert found["tools"][0]["name"] == "lead_update" and found["routed_in_ms"] < 15
    out, err = _call(admin, tok, "lead_update", {"lead_id": "lead-tools-1", "stage": "opportunity"})
    assert err and out["code"] == "needs_dry_run"
    dry, err = _call(admin, tok, "lead_update", {"lead_id": "lead-tools-1", "stage": "opportunity"}, dry_run=True)
    assert not err and dry["confirm_token"]
    done, err = _call(admin, tok, "lead_update", {"lead_id": "lead-tools-1", "stage": "opportunity"}, confirm_token=dry["confirm_token"])
    assert not err and done["ok"]
    assert run(database.db.leads.find_one({"id": "lead-tools-1"}))["stage"] == "opportunity"
    # The header can narrow a write token to read for a session.
    r = admin.post("/api/mcp", json={"jsonrpc": "2.0", "id": 9, "method": "initialize", "params": {}}, headers={"Authorization": f"Bearer {tok}", "X-MarkHub-Mode": "read"})
    assert "READ-ONLY" in r.json()["result"]["instructions"]
    assert admin.delete(f"/api/admin/tools/mcp-tokens/{issued['jti']}").status_code == 204
    gone = admin.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers={"Authorization": f"Bearer {tok}"})
    assert gone.status_code == 401


def test_mcp_rejects_login_sessions_and_viewers_cannot_mint_write_tokens(admin):
    login_token = admin.headers["Authorization"][7:]
    r = admin.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers={"Authorization": f"Bearer {login_token}"})
    assert r.status_code == 401
    v = _user(admin, "viewer2.tools@example.com", "viewer")
    assert v.post("/api/admin/tools/mcp-tokens", json={"mode": "write"}).status_code == 403
    assert v.post("/api/admin/tools/mcp-tokens", json={"mode": "read"}).status_code == 201


def test_visitor_surface_never_sees_staff_tools():
    ctx = tr.Ctx(surface="sol")
    names = {t.name for t in run(tr.REGISTRY.visible(ctx))}
    assert names == {"search_site", "save_visitor_details", "create_demo_request", "request_expert_contact"}
    with pytest.raises(tr.PolicyError):
        run(tr.REGISTRY.execute("leads_search", {}, ctx))
