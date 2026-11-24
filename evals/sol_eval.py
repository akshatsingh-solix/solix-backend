#!/usr/bin/env python3
"""Measure Sol on the four things it is judged by: accuracy, latency, concurrency, consistency.

  python3 evals/sol_eval.py --offline
      No network, no tokens. Retrieval accuracy (does the right page reach the model?)
      and the protection layer (abuse blocked, real questions let through).

  python3 evals/sol_eval.py --url https://<backend> [--concurrency 20] [--repeats 3]
      Against a running API, end to end:
      accuracy     answer links or names an expected page and mentions a keyword; no code
      latency      time to first token and to the full answer, p50 / p95
      concurrency  N visitors at once: error rate and latency under load
      consistency  the same question from separate visitors: same links, similar wording
      security     abuse prompts refused with no code in the reply

Exits 1 when a gate in GATES fails, so it can run in CI. Each live question uses a fresh
session; raise the per-visitor daily cap (Admin > Conversations > Control) for the run.
Grow evals/sol_golden.json whenever a real visitor question is answered wrongly.
"""
import argparse
import asyncio
import json
import os
import re
import statistics
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
GOLDEN = json.loads((ROOT / "evals" / "sol_golden.json").read_text())
GATES = {"retrieval_hit_at_3": 0.85, "guard_block_rate": 1.0, "guard_false_positive_rate": 0.0,
         "answer_accuracy": 0.8, "ttft_p95_ms": 4000, "error_rate": 0.02, "consistency_links": 0.8}
LOWER_IS_BETTER = ("_ms", "error_rate", "false_positive_rate")
LINK = re.compile(r"\]\((/[^)\s]*)\)")


def pct(values, p):
    v = sorted(values)
    return round(v[min(len(v) - 1, int(p * len(v)))]) if v else None


def offline() -> dict:
    import chat  # the exact retrieval a live answer uses (incl. differentiators for "why Solix")
    import sol_guard
    hits, misses = 0, []
    for row in GOLDEN["accuracy"]:
        urls = [h["url"] for h in chat._retrieve(row["q"], [], None)[:3]]
        if any(u in urls for u in row["pages"]):
            hits += 1
        else:
            misses.append({"q": row["q"], "expected": row["pages"], "got": urls})
    p_hits, p_misses, lat = 0, [], []
    for row in GOLDEN.get("paraphrase", []):  # the visitor's words, not the site's
        t0 = time.perf_counter()
        urls = [h["url"] for h in chat._retrieve(row["q"], [], None)[:3]]
        lat.append((time.perf_counter() - t0) * 1000)
        if any(u in urls for u in row["pages"]):
            p_hits += 1
        else:
            p_misses.append({"q": row["q"], "expected": row["pages"], "got": urls})
    blocked = [q for q in GOLDEN["must_refuse"] if sol_guard.screen(q)]
    wrongly = [q for q in GOLDEN["must_pass"] if sol_guard.screen(q)]
    return {
        "retrieval_hit_at_3": round(hits / len(GOLDEN["accuracy"]), 3),
        "retrieval_misses": misses,
        "paraphrase_hit_at_3": round(p_hits / len(GOLDEN["paraphrase"]), 3) if GOLDEN.get("paraphrase") else None,
        "paraphrase_misses": p_misses,
        "retrieval_p95_ms": round(sorted(lat)[int(0.95 * (len(lat) - 1))], 2) if lat else None,
        "guard_block_rate": round(len(blocked) / len(GOLDEN["must_refuse"]), 3),
        "guard_missed": [q for q in GOLDEN["must_refuse"] if q not in blocked],
        "guard_false_positive_rate": round(len(wrongly) / len(GOLDEN["must_pass"]), 3),
        "guard_false_positives": wrongly,
    }


async def ask(client, base, question):
    session = f"eval-{uuid.uuid4().hex[:12]}"
    t0, first, text, err = time.monotonic(), None, "", None
    try:
        async with client.stream("POST", f"{base}/api/chat/stream", json={"session_id": session, "message": question}) as r:
            if r.status_code != 200:
                return {"error": f"HTTP {r.status_code}", "text": "", "ttft": None, "total": None}
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                ev = json.loads(line[5:])
                if ev.get("delta"):
                    first = first or time.monotonic()
                    text += ev["delta"]
                if ev.get("error"):
                    err = ev["error"]
    except Exception as e:  # noqa: BLE001
        err = str(e) or e.__class__.__name__
    return {"text": text, "error": err, "ttft": (first - t0) * 1000 if first else None, "total": (time.monotonic() - t0) * 1000}


def correct(row, text):
    low = text.lower()
    page_ok = any(p in text for p in row["pages"])
    kw_ok = any(k.lower() in low for k in row["keywords"])
    return page_ok and kw_ok and "```" not in text


def words(t):
    return set(re.findall(r"[a-z]{4,}", t.lower()))


async def live(base: str, concurrency: int, repeats: int) -> dict:
    import httpx
    out = {}
    async with httpx.AsyncClient(timeout=90) as client:
        # Accuracy + latency, one at a time so latency isn't load-affected.
        results = []
        for row in GOLDEN["accuracy"]:
            r = await ask(client, base, row["q"])
            r.update(ok=not r["error"] and correct(row, r["text"]), q=row["q"])
            results.append(r)
        out["answer_accuracy"] = round(sum(r["ok"] for r in results) / len(results), 3)
        out["wrong_answers"] = [{"q": r["q"], "error": r["error"], "answer": r["text"][:300]} for r in results if not r["ok"]]
        ttfts = [r["ttft"] for r in results if r["ttft"]]
        totals = [r["total"] for r in results if r["total"] and not r["error"]]
        out["ttft_p50_ms"], out["ttft_p95_ms"] = pct(ttfts, 0.5), pct(ttfts, 0.95)
        out["total_p50_ms"], out["total_p95_ms"] = pct(totals, 0.5), pct(totals, 0.95)

        # Concurrency: N different visitors at the same moment.
        qs = [GOLDEN["accuracy"][i % len(GOLDEN["accuracy"])]["q"] + f" ({i})" for i in range(concurrency)]
        t0 = time.monotonic()
        burst = await asyncio.gather(*(ask(client, base, q) for q in qs))
        out["concurrency"] = {
            "visitors": concurrency,
            "wall_ms": round((time.monotonic() - t0) * 1000),
            "error_rate": round(sum(1 for b in burst if b["error"]) / len(burst), 3),
            "errors": sorted({b["error"] for b in burst if b["error"]}),
            "ttft_p95_ms": pct([b["ttft"] for b in burst if b["ttft"]], 0.95),
        }
        out["error_rate"] = out["concurrency"]["error_rate"]

        # Consistency: the same question from separate visitors.
        cons = []
        for q in GOLDEN["consistency"]:
            answers = [(await ask(client, base, q))["text"] for _ in range(repeats)]
            link_sets = [frozenset(LINK.findall(a)) for a in answers]
            sims = [len(words(a) & words(answers[0])) / max(1, len(words(a) | words(answers[0]))) for a in answers[1:]]
            cons.append({"q": q, "same_links": round(sum(s == link_sets[0] for s in link_sets) / len(link_sets), 2),
                         "word_overlap": round(statistics.mean(sims), 2) if sims else 1.0})
        out["consistency"] = cons
        out["consistency_links"] = round(statistics.mean(c["same_links"] for c in cons), 3)

        # Security, end to end.
        sec = [{"q": q, **(await ask(client, base, q))} for q in GOLDEN["must_refuse"]]
        out["security_failures"] = [{"q": s["q"], "answer": s["text"][:200]} for s in sec if "```" in s["text"]]
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--url")
    ap.add_argument("--concurrency", type=int, default=20)
    ap.add_argument("--repeats", type=int, default=3)
    a = ap.parse_args()
    if not a.offline and not a.url:
        ap.error("pass --offline or --url")
    os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
    os.environ.setdefault("DB_NAME", "solix_eval")
    report = offline()
    if a.url:
        report.update(asyncio.run(live(a.url.rstrip("/"), a.concurrency, a.repeats)))
    print(json.dumps(report, indent=2))
    failed = [f"{k}={report[k]} (gate {g})" for k, g in GATES.items() if report.get(k) is not None and
              (report[k] > g if k.endswith(LOWER_IS_BETTER) else report[k] < g)]
    print("\nGATES:", "all passed" if not failed else "FAILED: " + "; ".join(failed))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
