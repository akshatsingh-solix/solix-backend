"""Sol's control panel: everything about how the concierge answers, set from
Admin > Conversations > Control and stored in MongoDB (settings key "sol").

API keys stay in the server's environment and are never sent to or accepted
from the browser. What the admin controls here:

* routing: which providers answer and in what order (e.g. local -> openai ->
  groq -> openrouter), which models each one uses, whether each counts as
  private (keeps prompts out of training) and how many answers it may serve
  at once. A provider whose key is added later starts switched off until the
  admin places it, so nothing answers that wasn't chosen.
* privacy_mode "private_only": only providers marked private ever see a chat.
* behaviour: temperature, answer length, first-token timeout,
  answer cache, knowledge-gap learning, the coding/abuse guard, per-visitor
  daily cap, and a short "playbook" of standing instructions (current offers,
  priorities) that Sol follows.
"""
from __future__ import annotations

import time
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field

import database
from database import now_iso
import llm

KEY = "sol"
CACHE_SECONDS = 15


class Route(BaseModel):
    provider: str = Field(max_length=40)
    enabled: bool = True
    models: List[str] = Field(default_factory=list, max_length=8)
    private: Optional[bool] = None          # None: the built-in default for this provider
    max_concurrency: Optional[int] = Field(default=None, ge=1, le=500)


class SolConfig(BaseModel):
    enabled: bool = True
    routing: List[Route] = Field(default_factory=list, max_length=20)
    privacy_mode: Literal["any", "private_only"] = "any"
    # Lower is steadier: the same question gets the same answer (consistency).
    temperature: float = Field(default=0.5, ge=0, le=1.2)
    max_output_tokens: int = Field(default=900, ge=150, le=2000)
    first_token_timeout: float = Field(default=12, ge=3, le=60)
    answer_cache: bool = True
    cache_hours: int = Field(default=24, ge=1, le=24 * 30)
    learning: bool = True
    block_code: bool = True
    daily_ip_limit: int = Field(default=150, ge=10, le=5000)
    playbook: str = Field(default="", max_length=2000)


# A local model on a CPU box serves a couple of answers at a time; hosted APIs far more.
DEFAULT_CONCURRENCY = {"local": 2, "custom": 4}

_cached: Optional[SolConfig] = None
_cached_at = 0.0


async def load(fresh: bool = False) -> SolConfig:
    global _cached, _cached_at
    if not fresh and _cached is not None and time.monotonic() - _cached_at < CACHE_SECONDS:
        return _cached
    try:
        doc = await database.db.settings.find_one({"key": KEY}, {"_id": 0}) or {}
    except Exception:
        doc = {}
    cfg = SolConfig(**{k: doc[k] for k in SolConfig.model_fields if k in doc})
    _cached, _cached_at = cfg, time.monotonic()
    return cfg


async def save(cfg: SolConfig, by: str) -> SolConfig:
    global _cached
    await database.db.settings.update_one({"key": KEY}, {"$set": {**cfg.model_dump(), "updated_at": now_iso(), "updated_by": by}}, upsert=True)
    _cached = None
    return await load(fresh=True)


def is_private(name: str, route: Optional[Route]) -> bool:
    if route is not None and route.private is not None:
        return route.private
    return name in llm.PRIVATE_BY_DEFAULT


def route(cfg: SolConfig, available: List[llm.Provider]) -> List[llm.Provider]:
    """The providers that may answer, in the admin's order, with the admin's models."""
    by_name = {p.name: p for p in available}
    if cfg.routing:
        # Placed providers in the admin's order; a newly keyed one stays off until placed.
        ordered = [(by_name[r.provider], r) for r in cfg.routing if r.provider in by_name and r.enabled]
    else:
        ordered = [(p, None) for p in available]
    out = []
    for p, r in ordered:
        if cfg.privacy_mode == "private_only" and not is_private(p.name, r):
            continue
        models = [m.strip() for m in (r.models if r else []) if m.strip()] or p.models
        out.append(llm.Provider(p.name, p.base_url, p.api_key, models, p.extra, p.headers))
    return out


def concurrency(cfg: SolConfig) -> Dict[str, int]:
    limits = dict(DEFAULT_CONCURRENCY)
    for r in cfg.routing:
        if r.max_concurrency:
            limits[r.provider] = r.max_concurrency
    return limits


def describe(cfg: SolConfig, available: List[llm.Provider]) -> List[dict]:
    """Every provider with a key, as the control screen shows it (no keys, ever)."""
    routes = {r.provider: r for r in cfg.routing}
    names = [r.provider for r in cfg.routing if r.provider in {p.name for p in available}]
    names += [p.name for p in available if p.name not in names]
    by_name = {p.name: p for p in available}
    limits = concurrency(cfg)
    rows = []
    for n in names:
        r, p = routes.get(n), by_name[n]
        rows.append({
            "provider": n,
            "enabled": (r.enabled if r else not cfg.routing),
            "models": (r.models if r and r.models else []),
            "default_models": p.models,
            "private": is_private(n, r),
            "max_concurrency": limits.get(n),
            "placed": r is not None,
        })
    return rows
