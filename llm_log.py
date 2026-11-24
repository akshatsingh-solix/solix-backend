"""One record per model call (`llm_calls`): which agent, which prompt version, which provider and model, how long,
how many tokens (estimated where the provider doesn't say), and whether the output passed its checks.

Why: the EU AI Act Art. 50 evidence for EchoLocate (spec C-17) is that a person reviewed every AI-drafted
message, so each enrollment carries the model and prompt version that drafted it next to the reviewer who
approved it. And when quality moves, this table says whether a prompt or a model changed.

Prompt versions are content hashes of the system prompt, so editing a prompt changes its version automatically.
Personal data is not stored here: only ids that point at the lead or contact (erasure reaches them there).
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Optional

import database

logger = logging.getLogger("solix.llm_log")

# Known shutdown dates of model ids (provider deprecation pages). The watchdog warns 30 days ahead for any model in
# use; extend with LLM_MODEL_SHUTDOWNS='{"model-id": "YYYY-MM-DD"}' on the server.
SHUTDOWNS = {"gpt-4.1-nano": "2026-10-23", "gpt-4.1-nano-2025-04-14": "2026-10-23", "gpt-4.1-mini": "2027-01-14", "gpt-4.1-mini-2025-04-14": "2027-01-14",
             "gpt-4o-2024-08-06": "2027-01-14", "gpt-4o-2024-11-20": "2027-01-14", "gpt-4o": "2027-01-14"}


def version(name: str, system_prompt: str) -> str:
    return f"{name}@{hashlib.sha256(system_prompt.encode()).hexdigest()[:10]}"


async def record(agent: str, *, prompt_version: str, provider: Optional[str], model: Optional[str], ms: Optional[float] = None, ok: bool = True,
                 tokens_in: Optional[int] = None, tokens_out: Optional[int] = None, outcome: str = "", ref: Optional[dict] = None) -> None:
    try:
        await database.db.llm_calls.insert_one({"at": datetime.now(timezone.utc).isoformat(), "at_dt": datetime.now(timezone.utc), "agent": agent,
                                                "prompt_version": prompt_version, "provider": provider, "model": model, "ms": ms, "ok": ok,
                                                "tokens_in": tokens_in, "tokens_out": tokens_out, "outcome": outcome[:200], **(ref or {})})
    except Exception:  # noqa: BLE001 - logging never breaks the call it describes
        logger.exception("llm call log write failed")


def shutdowns() -> dict:
    import json
    import os
    extra = {}
    try:
        extra = json.loads(os.environ.get("LLM_MODEL_SHUTDOWNS") or "{}")
    except ValueError:
        logger.warning("LLM_MODEL_SHUTDOWNS is not valid JSON")
    return {**SHUTDOWNS, **extra}


async def models_in_use(days: int = 14) -> list:
    since = datetime.now(timezone.utc).timestamp() - days * 86400
    cutoff = datetime.fromtimestamp(since, timezone.utc).isoformat()
    return [m for m in await database.db.llm_calls.distinct("model", {"at": {"$gte": cutoff}}) if m]
