"""Measures the tool router on evals/tool_router_golden.json: top-1 and top-3 accuracy and latency per surface.

    python3 tool_router_eval.py          # prints the report; exit code 1 if a gate fails

Gates: top-3 accuracy >= 95% (the tool is among those loaded for the model) and p95 routing time <= 15 ms.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

GOLDEN = Path(__file__).parent / "evals" / "tool_router_golden.json"
GATES = {"top3": 0.95, "p95_ms": 15.0}


def evaluate() -> dict:
    os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
    os.environ.setdefault("DB_NAME", "solix")
    import chat  # noqa: F401  registers Sol's tools
    import markhub_tools  # noqa: F401  registers the staff tools
    import tool_registry as tr
    data = json.loads(GOLDEN.read_text())
    report = {}
    for surface in ("mcp", "mcp_heldout", "sol"):
        rows = data.get(surface) or []
        surf = "mcp" if surface.startswith("mcp") else surface
        top1 = top3 = 0
        times, misses = [], []
        tr.REGISTRY.route("warm up", surf)
        for q, want in rows:
            t0 = time.perf_counter()
            r = tr.REGISTRY.route(q, surf, k=3)
            times.append((time.perf_counter() - t0) * 1000)
            got = [n for n, _ in r.tools]
            top1 += bool(got) and got[0] == want
            top3 += want in got
            if not got or got[0] != want:
                misses.append({"q": q, "want": want, "got": got})
        times.sort()
        all_tokens = sum(t.tokens() for t in tr.REGISTRY.on(surf))
        report[surface] = {"cases": len(rows), "top1": round(top1 / len(rows), 3), "top3": round(top3 / len(rows), 3),
                           "p50_ms": round(times[len(times) // 2], 3), "p95_ms": round(times[int(0.95 * (len(times) - 1))], 3),
                           "tools": len(tr.REGISTRY.on(surf)), "tokens_all_schemas": all_tokens,
                           "tokens_top3_avg": round(sum(sum(tr.REGISTRY.tools[n].tokens() for n, _ in tr.REGISTRY.route(q, surf, 3).tools) for q, _ in rows) / len(rows)),
                           "misses": misses}
    report["gates_passed"] = all(report[s]["top3"] >= GATES["top3"] and report[s]["p95_ms"] <= GATES["p95_ms"] for s in ("mcp", "mcp_heldout", "sol"))
    return report


if __name__ == "__main__":
    out = evaluate()
    print(json.dumps(out, indent=2))
    sys.exit(0 if out["gates_passed"] else 1)
