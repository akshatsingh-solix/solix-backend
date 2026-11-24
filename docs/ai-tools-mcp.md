# AI tools, the semantic router and MCP

One tool registry serves every AI surface in MarkHub: **Sol** (website visitors), **connected assistants** over
MCP (Claude, ChatGPT or any MCP client acting for a staff member), and the admin's own **AI tools** page.
Code: `semantic.py`, `tool_registry.py`, `markhub_tools.py`, `mcp_server.py`, `tool_router_eval.py`.

## 1. Why: accuracy, tokens and production stability

A model given every tool schema on every call pays for all of them in tokens and picks the wrong one more
often. MarkHub hands a model only the tools a request needs:

| | All staff tool schemas | After routing (top 3) |
|---|---|---|
| Tokens per call | about 3,400 | about 360 (**about 89% fewer**) |
| Routing time (p95, in-process) | — | about 0.2 ms |

## 2. The router (`semantic.py`, `tool_registry.Registry.route`)

No embedding API and no vector database: every text becomes a sparse vector of stemmed words, word pairs,
character 4-grams and domain concepts (a small thesaurus: "sunset" = "retire", "opted out" = "suppress"),
weighted by TF-IDF and compared by cosine similarity over postings. On top of meaning, two structured terms:

* **intent**: does the request read, change or delete? ("give me lead 42" reads; "give lead 42 to Jordan" changes);
* **object**: does it name what the tool acts on (a lead, an account, a reply, the suppression list)?

Measured on `evals/tool_router_golden.json` (`python3 tool_router_eval.py`, also the **Run accuracy eval** button):

| Set | Right tool in top 3 | Ranked first | p95 |
|---|---|---|---|
| Staff tools (tuning set, 40 requests) | 97.5% | 87.5% | 0.24 ms |
| Staff tools, **held-out wording** (26 requests, never used for tuning) | **96.2%** | 80.8% | 0.21 ms |
| Sol visitor tools (8 requests) | 100% | 100% | 0.06 ms |

The 95% gate is on top-3 accuracy, because the model then chooses among those three schemas. Top-1 is lower and
reported honestly; when the router's first pick is wrong, the dry run (section 4) catches a wrong change before
it happens. Add a golden row whenever a real request routes badly.

## 3. Deferred loading

* **Sol**: `search_site` is always loaded; the other visitor tools are loaded only when the turn calls for them
  (a demo request, contact details, "talk to a human"). Switch off in AI tools > Policy.
* **MCP**: `tools/list` returns two meta tools, `find_tools` and `call_tool`. The assistant describes what it
  wants, gets the top matches with their full schemas, then calls one. Clients that can't work this way connect
  with `?tools=all`.

## 4. The policy layer

| Rule | Where it is enforced |
|---|---|
| Least privilege: a tool belongs to one MarkHub area (Leads, Radar, Outbound...) and needs `view` to read, `edit` to write, `full` to delete (or the roles the tool names: erasure is admin and Compliance only) | `Registry.check`, plus the endpoint's own checks, because tools call MarkHub's API **as the user** |
| Read/write separation: staff sessions and MCP tokens start **read-only**; write tools aren't listed or callable until the user opts in (a write token) | `Registry.check` |
| Arguments are checked against the tool's schema (types, enums, lengths, emails, unknown keys) before anything runs | `check_args` |
| **Dry run before every write or delete**: the target is looked up, the tool's validator says exactly what would change and what blocks it (wrong id, no-op, fails the compliance gate, missing confirmation) | `Registry.dry_run` |
| The real call needs a **single-use confirmation token** bound to the user, the tool and the exact arguments, valid 10 minutes. Changed arguments, someone else's token or a reused one are refused | `make_token` / `use_token` |
| Visitor tools (book a demo, save details) run their dry-run validation automatically: the visitor's own message is the confirmation | `Registry.execute` |
| Admin switches: all AI writes off, MCP off, Sol deferred loading, any single tool off | `settings.tool_policy` |
| Every route, dry run, call, refusal and timeout is in `tool_audit`, with personal values masked | `audit` |

## 5. Connecting an assistant

Admin > **AI tools** > Connect an AI assistant: choose read-only (default) or read-and-write, copy the config
shown once:

```json
{"mcpServers": {"markhub": {"type": "http", "url": "https://<api>/api/mcp", "headers": {"Authorization": "Bearer <token>"}}}}
```

Tokens act as their owner with the owner's access, can be revoked at any time, and are rate limited
(`MCP_RATE_PER_MINUTE`, default 60) with a per-call timeout (`MCP_CALL_TIMEOUT`, default 60 s). A login session
token is refused at `/api/mcp`; only MCP tokens work there. Sending `X-MarkHub-Mode: read` narrows a write
token to read for that session.

## 6. Adding a tool

```python
api_tool("radar_summary", "Radar overview: ...", access="read", feature="radar", method="GET",
         path="/api/admin/radar/summary", props={}, examples=("radar overview", "how did the last run go"))
```

Give it 3-6 `examples` in the words people use, add rows to `evals/tool_router_golden.json`, and run
`python3 tool_router_eval.py`. A write tool gets `current=` (the GET path that shows the record) and, if
useful, a `check=` function that returns problems; that is its dry run.
