"""Provider-agnostic, free-tier-first LLM client for Sol.

Every provider here speaks the OpenAI chat-completions protocol, so one
streaming + tool-calling implementation covers all of them. Providers are
tried in order; a provider that fails before sending any text (rate limit,
outage, unsupported model, timeout) is skipped and the next one answers, so
visitors never see a free tier running out.

Configure with environment variables. Any provider whose key is set is used:

  GEMINI_API_KEY      Google AI Studio (free, no card)   SOL_GEMINI_MODELS
  GROQ_API_KEY        Groq (free, very fast)              SOL_GROQ_MODELS
  MISTRAL_API_KEY     Mistral "Experiment" plan (free)    SOL_MISTRAL_MODELS
  OPENROUTER_API_KEY  OpenRouter ":free" models           SOL_OPENROUTER_MODELS (falls back to CHAT_MODELS)
  OPENAI_API_KEY      OpenAI (paid)                       OPENAI_MODEL
  SOL_CUSTOM_BASE_URL + SOL_CUSTOM_API_KEY + SOL_CUSTOM_MODELS
                      any other OpenAI-compatible endpoint (vLLM, Ollama,
                      Together, Cerebras, a model you host on Render, ...)
  SOL_PROVIDER_ORDER  comma-separated order, default gemini,groq,mistral,openrouter,openai,custom
"""
import os
import json
import time
import logging
from dataclasses import dataclass, field
from typing import AsyncIterator, Dict, List, Optional

import httpx

logger = logging.getLogger("solix.llm")


@dataclass
class Provider:
    name: str
    base_url: str
    api_key: str
    models: List[str]
    extra: Dict = field(default_factory=dict)
    headers: Dict = field(default_factory=dict)


# OpenRouter's free models; CHAT_MODELS on the Render service overrides this.
DEFAULT_OPENROUTER_MODELS = "google/gemma-4-31b-it:free,thinkingmachines/inkling-small:free,nvidia/nemotron-3.5-lightning:free,openrouter/free"


def _models(var: str, default: str, fallback_var: Optional[str] = None) -> List[str]:
    raw = os.environ.get(var) or (os.environ.get(fallback_var) if fallback_var else None) or default
    return [m.strip() for m in raw.split(",") if m.strip()]


def configured_providers() -> List[Provider]:
    site = os.environ.get("SITE_URL", "https://www.solix.com")
    known = {}
    if os.environ.get("GEMINI_API_KEY"):
        known["gemini"] = Provider("gemini", "https://generativelanguage.googleapis.com/v1beta/openai", os.environ["GEMINI_API_KEY"],
                                   _models("SOL_GEMINI_MODELS", "gemini-2.5-flash,gemini-2.5-flash-lite"))
    if os.environ.get("GROQ_API_KEY"):
        known["groq"] = Provider("groq", "https://api.groq.com/openai/v1", os.environ["GROQ_API_KEY"],
                                 _models("SOL_GROQ_MODELS", "openai/gpt-oss-120b,openai/gpt-oss-20b"), {"reasoning_effort": "low"})
    if os.environ.get("MISTRAL_API_KEY"):
        known["mistral"] = Provider("mistral", "https://api.mistral.ai/v1", os.environ["MISTRAL_API_KEY"], _models("SOL_MISTRAL_MODELS", "mistral-small-latest"))
    if os.environ.get("OPENROUTER_API_KEY"):
        known["openrouter"] = Provider("openrouter", "https://openrouter.ai/api/v1", os.environ["OPENROUTER_API_KEY"],
                                       _models("SOL_OPENROUTER_MODELS", DEFAULT_OPENROUTER_MODELS, "CHAT_MODELS"),
                                       headers={"HTTP-Referer": site, "X-Title": "Solix Sol concierge"})
    if os.environ.get("OPENAI_API_KEY"):
        known["openai"] = Provider("openai", "https://api.openai.com/v1", os.environ["OPENAI_API_KEY"], _models("OPENAI_MODEL", "gpt-4o-mini"))
    if os.environ.get("SOL_CUSTOM_BASE_URL") and os.environ.get("SOL_CUSTOM_MODELS"):
        known["custom"] = Provider("custom", os.environ["SOL_CUSTOM_BASE_URL"].rstrip("/"), os.environ.get("SOL_CUSTOM_API_KEY", ""), _models("SOL_CUSTOM_MODELS", ""))
    order = [p.strip() for p in os.environ.get("SOL_PROVIDER_ORDER", "gemini,groq,mistral,openrouter,openai,custom").split(",")]
    ordered = [known[p] for p in order if p in known]
    return ordered + [p for n, p in known.items() if n not in order]


class ProviderError(Exception):
    pass


# (provider, model) -> monotonic time until which it is skipped after a rate limit or outage.
_cooldown: Dict[tuple, float] = {}
COOLDOWN_RATE_LIMIT = 60
COOLDOWN_ERROR = 20


def _cool(key: tuple, seconds: float) -> None:
    _cooldown[key] = time.monotonic() + seconds


def _available(key: tuple) -> bool:
    return _cooldown.get(key, 0) <= time.monotonic()


async def _stream_one(client: httpx.AsyncClient, p: Provider, model: str, messages: list, tools: Optional[list], max_tokens: int) -> AsyncIterator[dict]:
    body = {"model": model, "messages": messages, "stream": True, "max_tokens": max_tokens, "temperature": 0.3, **p.extra}
    if p.name == "gemini":
        # 2.5 models can skip thinking (faster first token); 3.x models can't, so keep it low.
        body["reasoning_effort"] = "none" if model.startswith("gemini-2.5") else "low"
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    headers = {"Content-Type": "application/json", **p.headers}
    if p.api_key:
        headers["Authorization"] = f"Bearer {p.api_key}"
    async with client.stream("POST", f"{p.base_url}/chat/completions", json=body, headers=headers) as r:
        if r.status_code != 200:
            detail = (await r.aread())[:300].decode("utf-8", "replace")
            raise ProviderError(f"HTTP {r.status_code}: {detail}")
        calls: Dict[int, dict] = {}
        async for line in r.aiter_lines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                data = json.loads(payload)
            except ValueError:
                continue
            if data.get("error"):
                raise ProviderError(str(data["error"])[:300])
            for choice in data.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    yield {"type": "text", "text": delta["content"]}
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index")
                    if idx is None:
                        # Some providers omit `index`: a new id starts a call, anything else continues the last one.
                        idx = len(calls) if (tc.get("id") or not calls) else max(calls)
                    slot = calls.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                    slot["id"] = tc.get("id") or slot["id"]
                    fn = tc.get("function") or {}
                    slot["name"] = fn.get("name") or slot["name"]
                    slot["arguments"] += fn.get("arguments") or ""
        if calls:
            out = []
            for i, c in sorted(calls.items()):
                try:
                    args = json.loads(c["arguments"] or "{}")
                except ValueError:
                    args = {}
                out.append({"id": c["id"] or f"call_{i}", "name": c["name"], "arguments": args if isinstance(args, dict) else {}})
            yield {"type": "tool_calls", "calls": out}


async def stream_completion(messages: list, tools: Optional[list] = None, max_tokens: int = 900, providers: Optional[List[Provider]] = None,
                            client: Optional[httpx.AsyncClient] = None) -> AsyncIterator[dict]:
    """Streams one assistant turn, failing over across providers and models.

    Yields {"type": "text", "text"}, {"type": "tool_calls", "calls"} and
    finally {"type": "meta", "provider", "model"}. Raises ProviderError when
    every provider fails before producing output.
    """
    providers = configured_providers() if providers is None else providers
    if not providers:
        raise ProviderError("No AI provider is configured")
    own = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(45, connect=8))
    errors = []
    try:
        candidates = [(p, m) for p in providers for m in p.models]
        # If everything is cooling down, try anyway rather than refuse.
        ready = [c for c in candidates if _available((c[0].name, c[1]))] or candidates
        for p, model in ready:
            started = False
            try:
                async for event in _stream_one(client, p, model, messages, tools, max_tokens):
                    started = True
                    yield event
                if not started:
                    # Free models sometimes spend the whole budget thinking and say nothing.
                    raise ProviderError("empty reply")
                yield {"type": "meta", "provider": p.name, "model": model}
                return
            except (ProviderError, httpx.HTTPError) as e:
                msg = str(e) or e.__class__.__name__
                if started:
                    # Text already reached the visitor; switching models mid-answer would garble it.
                    raise ProviderError(f"{p.name}/{model} failed mid-stream: {msg}")
                _cool((p.name, model), COOLDOWN_RATE_LIMIT if "429" in msg or "quota" in msg.lower() else COOLDOWN_ERROR)
                errors.append(f"{p.name}/{model}: {msg}")
                logger.warning("Sol provider %s/%s unavailable, trying next: %s", p.name, model, msg[:200])
        raise ProviderError("; ".join(errors) or "No provider answered")
    finally:
        if own:
            await client.aclose()
