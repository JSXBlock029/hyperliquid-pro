"""
llm_router.py — multi-provider LLM rotation for the scalp engine.

WHY: the scalp agent used to call a single local 7B (qwen2.5 via Ollama). The 7B
can only reason over pre-computed CATEGORICAL labels — it cannot threshold raw
floats — so AI was limited to the gigafade sleeve. This router lets the agent
rotate across FREE hosted models (Groq / Gemini / OpenRouter / Cerebras), which
are 70B-class and CAN reason over raw market features, with the paid OpenAI key
as a fallback and the local Ollama as the final, always-available backstop.

DESIGN
- Every provider here speaks the OpenAI-compatible /chat/completions schema
  (Gemini via its openai compat endpoint), so a provider is just a row:
  {name, base_url, key_env, model, rpm}. One adapter, swappable fields.
- chat() walks the enabled providers in rotating order. On 429 (rate-limit) or
  timeout it advances to the next provider; on success it returns immediately,
  tagged with which provider/model answered (so decisions are a measurable
  per-model experiment downstream).
- Ollama is ALWAYS the final fallback so the hot loop never goes dark even with
  zero hosted keys configured.
- HARD timeout per call (default 6s). The scalp hot loop must never block; on
  total failure chat() returns None and the caller fails to its safe default
  (enter=False on entries, hold on exits).

CONFIG (all via env / .env, which server.py load_dotenv()s):
  SCALP_LLM_PROVIDERS   comma list + order, default "groq,cerebras,gemini,openrouter,openai,ollama"
  SCALP_LLM_TIMEOUT_S   per-call hard timeout, default 6
  GROQ_API_KEY / GEMINI_API_KEY (or GOOGLE_API_KEY) / OPENROUTER_API_KEY /
  CEREBRAS_API_KEY / OPENAI_API_KEY
  Per-provider model override: SCALP_<PROVIDER>_MODEL (e.g. SCALP_GROQ_MODEL)
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

TIMEOUT_S = float(os.getenv("SCALP_LLM_TIMEOUT_S", "6"))
# default rotation order: free + fast first, paid OpenAI before local backstop
_DEFAULT_ORDER = "groq,cerebras,gemini,openrouter,openai,ollama"


@dataclass(frozen=True)
class Provider:
    name: str
    base_url: str        # full /chat/completions URL ("" => native ollama path)
    key_env: str         # env var holding the API key ("" => no key needed)
    model: str
    rpm: int             # free-tier requests/min (advisory; for budgeting/log)
    native_ollama: bool = False
    fallback: bool = False  # tried only AFTER the free pool is exhausted (never
                            # round-robin primary): paid OpenAI + local Ollama


def _model(provider: str, default: str) -> str:
    return os.getenv(f"SCALP_{provider.upper()}_MODEL", default)


# Provider catalog. base_url/model reflect each vendor's free tier as of 2026-06.
# Models are env-overridable so a deprecated id can be swapped without code edits.
_CATALOG: dict[str, Provider] = {
    "groq": Provider(
        "groq", "https://api.groq.com/openai/v1/chat/completions",
        "GROQ_API_KEY", _model("groq", "llama-3.3-70b-versatile"), rpm=30),
    "cerebras": Provider(
        "cerebras", "https://api.cerebras.ai/v1/chat/completions",
        "CEREBRAS_API_KEY", _model("cerebras", "gpt-oss-120b"), rpm=30),
    "gemini": Provider(
        "gemini",
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "GEMINI_API_KEY", _model("gemini", "gemini-2.5-flash"), rpm=15),
    "openrouter": Provider(
        "openrouter", "https://openrouter.ai/api/v1/chat/completions",
        "OPENROUTER_API_KEY",
        _model("openrouter", "google/gemma-4-31b-it:free"), rpm=20),
    "openai": Provider(
        "openai", "https://api.openai.com/v1/chat/completions",
        "OPENAI_API_KEY", _model("openai", "gpt-4o-mini"), rpm=500,
        fallback=True),
    "ollama": Provider(
        "ollama", "", "",
        _model("ollama", os.getenv("SCALP_MODEL", "qwen2.5:7b-instruct")),
        rpm=9999, native_ollama=True, fallback=True),
}

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "127.0.0.1:11434")
if not OLLAMA_HOST.startswith("http"):
    OLLAMA_HOST = f"http://{OLLAMA_HOST}"


def _gemini_key() -> Optional[str]:
    return os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")


def _enabled_providers() -> list[Provider]:
    """Resolve the configured order to providers that actually have a key.
    Ollama (no key) is always eligible as the backstop."""
    order = [p.strip().lower()
             for p in os.getenv("SCALP_LLM_PROVIDERS", _DEFAULT_ORDER).split(",")
             if p.strip()]
    out: list[Provider] = []
    for name in order:
        prov = _CATALOG.get(name)
        if not prov:
            continue
        if prov.native_ollama:
            out.append(prov)
            continue
        key = _gemini_key() if name == "gemini" else os.getenv(prov.key_env)
        if key:
            out.append(prov)
    return out


# round-robin cursor so we don't hammer the first provider every call
_cursor = 0
_cursor_lock = threading.Lock()


def _next_start() -> int:
    global _cursor
    with _cursor_lock:
        i = _cursor
        _cursor += 1
        return i


@dataclass(frozen=True)
class LLMResult:
    text: str
    provider: str
    model: str
    latency_ms: float


def _call_openai_compat(prov: Provider, system: str, user: str,
                        temperature: float, max_tokens: int,
                        force_json: bool) -> Optional[str]:
    key = _gemini_key() if prov.name == "gemini" else os.getenv(prov.key_env)
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if prov.name == "openrouter":
        # OpenRouter asks for attribution headers; harmless elsewhere if omitted.
        headers["HTTP-Referer"] = "https://localhost/scalp"
        headers["X-Title"] = "hlpro2-scalp"
    payload: dict[str, Any] = {
        "model": prov.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if force_json:
        # Supported by OpenAI/Groq/Cerebras/Gemini-compat/most OpenRouter models.
        payload["response_format"] = {"type": "json_object"}
    resp = requests.post(prov.base_url, headers=headers, json=payload,
                         timeout=TIMEOUT_S)
    if resp.status_code in (429, 500, 502, 503, 529):
        # rate-limited / transient => signal rotation to next provider
        raise _Rotate(f"{prov.name} http {resp.status_code}")
    resp.raise_for_status()
    body = resp.json()
    msg = body["choices"][0]["message"]
    # Reasoning models (gpt-oss, gemini-2.5) sometimes omit 'content' or stash the
    # answer under 'reasoning' when they hit the token budget — fall back gracefully
    # so we don't KeyError; an empty result rotates to the next provider.
    return msg.get("content") or msg.get("reasoning") or ""


def _call_ollama(prov: Provider, system: str, user: str,
                 temperature: float, max_tokens: int,
                 force_json: bool) -> Optional[str]:
    payload = {
        "model": prov.model,
        "system": system,
        "prompt": user,
        "stream": False,
        "keep_alive": os.getenv("SCALP_KEEP_ALIVE", "30m"),
        "options": {"temperature": temperature, "num_predict": max_tokens},
    }
    if force_json:
        payload["format"] = "json"
    resp = requests.post(f"{OLLAMA_HOST}/api/generate", json=payload,
                         timeout=TIMEOUT_S)
    resp.raise_for_status()
    return resp.json().get("response", "")


class _Rotate(Exception):
    """Internal: provider was rate-limited/transient — advance to the next."""


def chat(system: str, user: str, *, temperature: float = 0.2,
         max_tokens: int = 160, force_json: bool = True) -> Optional[LLMResult]:
    """Send one prompt; rotate across providers on rate-limit/timeout.

    Returns LLMResult (text + which model answered + latency) on first success,
    or None if every provider failed. Never raises — the hot loop must not break.
    """
    providers = _enabled_providers()
    if not providers:
        return None
    # Round-robin only the FREE pool; OpenAI (paid) + Ollama (slow local) are tried
    # in fixed order ONLY after the free pool is exhausted — keeps cost down and
    # avoids paying a 6s Ollama timeout as a round-robin primary.
    pool = [p for p in providers if not p.fallback]
    tail = [p for p in providers if p.fallback]
    if pool:
        start = _next_start() % len(pool)
        order = pool[start:] + pool[:start] + tail
    else:
        order = tail
    for prov in order:
        t0 = time.time()
        try:
            if prov.native_ollama:
                text = _call_ollama(prov, system, user, temperature,
                                    max_tokens, force_json)
            else:
                text = _call_openai_compat(prov, system, user, temperature,
                                           max_tokens, force_json)
            ms = (time.time() - t0) * 1000
            if text:
                logger.info("llm_router: %s/%s ok (%.0fms)",
                            prov.name, prov.model, ms)
                return LLMResult(text, prov.name, prov.model, ms)
            raise _Rotate(f"{prov.name} empty body")
        except _Rotate as exc:
            logger.info("llm_router: rotate past %s (%s)", prov.name, exc)
            continue
        except requests.Timeout:
            logger.info("llm_router: %s timeout after %.0fms, rotating",
                        prov.name, (time.time() - t0) * 1000)
            continue
        except requests.RequestException as exc:
            logger.warning("llm_router: %s transport error (%s), rotating",
                           prov.name, exc.__class__.__name__)
            continue
        except (KeyError, ValueError, IndexError) as exc:
            logger.warning("llm_router: %s bad envelope (%s), rotating",
                           prov.name, exc)
            continue
    logger.warning("llm_router: all %d providers failed", len(order))
    return None


def status() -> dict:
    """Liveness summary for /scalp/status and health checks."""
    provs = _enabled_providers()
    return {
        "timeout_s": TIMEOUT_S,
        "enabled": [{"name": p.name, "model": p.model, "rpm": p.rpm}
                    for p in provs],
        "count": len(provs),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(status(), indent=2))
    r = chat("You are a JSON echo. Reply with a JSON object.",
             'Return {"ok": true, "who": "<your model family>"}.')
    if r:
        print(f"\n{r.provider}/{r.model} ({r.latency_ms:.0f}ms): {r.text}")
    else:
        print("\nNo provider answered (check keys / Ollama).")
