"""
Scalp decision agent — routes through llm_router (multi-provider rotation).

2026-06-19: backend swapped from a single local Ollama call to llm_router, which
rotates across FREE hosted 70B-class models (Groq/Gemini/OpenRouter/Cerebras) with
the paid OpenAI key as fallback and local Ollama as the final backstop. Because the
hosted models CAN reason over raw floats (the 7B could not), the agent now judges
BOTH signals: gigafade keeps its categorical-label view; momentum gets a raw-float
view (see MOMENTUM_SYSTEM_PROMPT). Each Decision is tagged with the provider/model
that produced it (decision.model) and its latency (decision.latency_ms) so the whole
thing is a measurable per-model experiment.

Per candidate signal, the engine hands the agent a compact JSON snapshot of the
setup. The agent returns a strict JSON decision:

    {
      "enter":     bool,
      "size_pct":  float,   # 0..1 of allotted scalp capital
      "stop_bps":  float,   # distance from entry, basis points
      "tp_bps":    float,   # distance from entry, basis points
      "reason":    str      # one short sentence
    }

Fail-closed: any timeout, parse error, or schema violation returns enter=False.
The hot loop never blocks on the model — timeout is hard-capped.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, replace
from typing import Any

import llm_router

logger = logging.getLogger(__name__)

# 2026-06-09: the model judges ONLY gigafade now (momentum is rules-only — the 7B
# adds no edge on coinflip momentum). Inputs are pre-computed CATEGORICAL LABELS, not
# raw floats: an offline A/B proved qwen2.5:7b cannot threshold bps/RSI numbers (it
# called a 0.16bps spread "too wide" and RSI 21 "not extreme"), but it classifies
# correctly over labels (entered clean extremes, refused the marginal). See
# _prompt_eval.py / scalp_engine.gigafade_agent_view.
SYSTEM_PROMPT = (
    "You are a futures scalping decision agent for Hyperliquid perps. You judge ONE "
    "gigafade setup at a time, described with pre-computed CATEGORICAL labels — do not "
    "do arithmetic. The strategy FADES an exhaustion candle (a top-percentile body at "
    "an RSI extreme); you take the contrarian side given by 'side_hint'. Spread, "
    "volatility and liquidity are ALREADY filtered by a deterministic gate, so 'spread' "
    "is always tight — NEVER refuse on spread. "
    "ENTER (enter=true) when the fade is clean: candle_extremity is 'well_above_normal' "
    "AND rsi_state is 'extreme_overbought' (for a SELL) or 'extreme_oversold' (for a "
    "BUY). REFUSE (enter=false) when candle_extremity is 'marginal' (the body is not "
    "genuinely large) or rsi_state is 'not_extreme'. NOTE: a fade NORMALLY trades INTO "
    "book pressure, so book_pressure_vs_fade is informational only — do NOT refuse "
    "solely because it is 'against'; let it tip only a genuinely borderline call. "
    "Respond with a single JSON object and nothing else. Schema: {\"enter\": bool, "
    "\"size_pct\": float in [0,1], \"stop_bps\": float in [5,120], "
    "\"tp_bps\": float in [0,200], \"reason\": short string}. tp_bps may be 0 "
    "(gigafade exit is time-managed). On a clean enter use size_pct 0.3-0.6 and "
    "stop_bps 40-80."
)

MOMENTUM_SYSTEM_PROMPT = (
    "You are a futures scalping decision agent for Hyperliquid perps judging ONE momentum "
    "scalp entry. Unlike the gigafade path, this snapshot gives you RAW NUMBERS — read them. "
    "Fields: side_hint (BUY/SELL — the side a deterministic pre-filter chose; you judge "
    "whether to TAKE it), mid, spread_bps (already tight — never refuse on spread), "
    "ema_spread_bps (EMA9-EMA21 trend in bps; positive=up-trend), ob_imbalance_top5 "
    "(-1..1, >0 bids dominate/upward pressure), realized_vol_30m, atr_bps (recent range), "
    "ret_z_1m (last-return z-score). HONEST CONTEXT: this momentum signal has shown NO "
    "durable edge in testing, so be SELECTIVE — only ENTER when the microstructure genuinely "
    "supports side_hint (e.g. for a BUY: ema_spread_bps>0 AND ob_imbalance favors bids AND "
    "ret_z_1m not already exhausted/overextended). REFUSE when the book pressure opposes the "
    "side, the trend is flat/against, or the move looks exhausted. Size DOWN in high atr_bps. "
    "Respond with a single JSON object and nothing else. Schema: {\"enter\": bool, "
    "\"size_pct\": float in [0,1], \"stop_bps\": float in [5,120], \"tp_bps\": float in "
    "[5,200] with tp_bps>=stop_bps (reward:risk>=1), \"reason\": short string}. On a clean "
    "enter use size_pct 0.2-0.5, stop_bps 10-30, tp_bps 15-60."
)

EXIT_SYSTEM_PROMPT = (
    "You manage an OPEN gigafade scalp on Hyperliquid (a faded exhaustion candle; the "
    "exit is normally time-based). The position is described with CATEGORICAL labels — "
    "do not do arithmetic. 'pnl_state' is one of up_big/up_small/flat/down_small/"
    "down_big; 'time_held' is early/mid/late. Decide whether to CLOSE EARLY. A time-exit "
    "and a hard stop already protect it, so only close early when the fade has clearly "
    "WORKED (pnl_state 'up_big' — lock the gain) or is clearly FAILING ('down_big') with "
    "time_held 'late'. When unsure, HOLD. Respond with a single JSON object: "
    "{\"exit\": bool, \"reason\": short string} and nothing else."
)

MOMENTUM_EXIT_SYSTEM_PROMPT = (
    "You manage an OPEN momentum scalp on Hyperliquid. A hard stop and a take-profit "
    "bracket already protect it, so you only decide whether to CLOSE EARLY (cut a loser "
    "before the stop, or bank a winner before it reverses). The position is described "
    "with raw numbers: 'upnl_bps' (current unrealized PnL in bps; negative=losing), "
    "'held_s' (seconds in trade), 'side', 'ema_spread_bps' and 'ob_imbalance_top5' (the "
    "CURRENT microstructure). CLOSE EARLY when the thesis has clearly broken — the trend/"
    "book has flipped AGAINST the position and upnl_bps is negative — or when a decent "
    "gain is fading (upnl_bps was good but momentum is rolling over). Otherwise HOLD and "
    "let the bracket work. When unsure, HOLD. Respond with a single JSON object: "
    "{\"exit\": bool, \"reason\": short string} and nothing else."
)


@dataclass(frozen=True)
class Decision:
    enter: bool
    size_pct: float
    stop_bps: float
    tp_bps: float
    reason: str
    model: str = ""          # "provider/model" that produced this decision
    latency_ms: float = 0.0  # round-trip latency of the LLM call

    @classmethod
    def refuse(cls, reason: str) -> "Decision":
        return cls(False, 0.0, 0.0, 0.0, reason)


_TRUE_STRINGS = {"true", "yes", "y", "1"}


def _as_bool(value: Any) -> bool:
    """Strict truthiness for model output. bool("false") is True in Python, so a model
    replying "enter": "false" used to open a trade. Only explicit true values count;
    anything else means no (no entry, no early exit)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in _TRUE_STRINGS
    return False


def _coerce(raw: Any, allow_zero_tp: bool = False) -> Decision:
    """Validate the model's JSON into a Decision.

    allow_zero_tp: True for TIME-managed signals (gigafade), where the system
    prompt explicitly permits tp_bps=0 (exit is handled by the time/AI backstop,
    and the engine forces tp_px=0 downstream regardless). Previously tp=0 was
    rejected for *every* signal, which silently refused every clean gigafade
    fade the model produced — the main reason gigafade barely fired with the
    agent on. For momentum (allow_zero_tp=False) a real TP with rr>=1 is still
    required so the position keeps its profit-taking guarantee.
    The stop ceiling matches the system prompt's [5,120] range (was 80, so
    valid wide-stop outputs were being thrown away).
    """
    if not isinstance(raw, dict):
        return Decision.refuse("agent: non-object response")
    try:
        enter = _as_bool(raw.get("enter", False))
        size = float(raw.get("size_pct", 0.0))
        stop = float(raw.get("stop_bps", 0.0))
        tp = float(raw.get("tp_bps", 0.0))
        reason = str(raw.get("reason", ""))[:140]
    except (TypeError, ValueError) as exc:
        return Decision.refuse(f"agent: bad types ({exc})")

    if enter:
        if not (0.0 < size <= 1.0):
            return Decision.refuse(f"agent: size_pct out of range ({size})")
        if not (5.0 <= stop <= 120.0):
            return Decision.refuse(f"agent: stop_bps out of range ({stop})")
        if allow_zero_tp:
            # time/AI-managed exit (gigafade): tp is advisory only — the engine
            # forces tp_px=0 downstream — so don't refuse on it at all (tp=0 and
            # tp<stop are both fine here). Refusing these silently killed fades.
            pass
        else:
            if not (5.0 <= tp <= 200.0):
                return Decision.refuse(f"agent: tp_bps out of range ({tp})")
            if tp < stop:
                return Decision.refuse(f"agent: tp<stop (rr<1)")
    return Decision(enter, size, stop, tp, reason)


_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(text: str) -> Any:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = _JSON_BLOCK.search(text)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None


def decide(snapshot: dict) -> Decision:
    """Ask the rotating model whether to enter this scalp.

    snapshot is a small dict the engine builds. gigafade snapshots use categorical
    labels (gigafade_agent_view); momentum snapshots use raw floats (build_snapshot).
    The system prompt is selected per signal. Fail-closed: any failure => refuse.
    """
    signal = snapshot.get("signal", "")
    system = MOMENTUM_SYSTEM_PROMPT if signal == "momentum" else SYSTEM_PROMPT
    user = json.dumps(snapshot, separators=(",", ":"))
    # 320 (not 160): reasoning models (gpt-oss/gemini-2.5) spend part of the budget
    # thinking before the JSON; too small a cap leaves an empty 'content'.
    res = llm_router.chat(system, user, temperature=0.2, max_tokens=320,
                          force_json=True)
    if res is None:
        return Decision.refuse("agent: no provider answered")
    parsed = _extract_json(res.text)
    # gigafade is time-managed -> tp_bps=0 is a valid (expected) model output.
    allow_zero_tp = signal == "gigafade"
    decision = _coerce(parsed, allow_zero_tp=allow_zero_tp) if parsed is not None \
        else Decision.refuse("agent: unparseable response")
    decision = replace(decision, model=f"{res.provider}/{res.model}",
                       latency_ms=round(res.latency_ms, 0))
    logger.info(
        "scalp_agent: %s sym=%s sig=%s enter=%s size=%.3f stop=%.1f tp=%.1f (%.0fms) reason=%s",
        decision.model, snapshot.get("symbol"), signal, decision.enter,
        decision.size_pct, decision.stop_bps, decision.tp_bps,
        decision.latency_ms, decision.reason,
    )
    return decision


def decide_exit(position: dict) -> dict:
    """Ask the rotating model whether to close an open position early.
    Fail-open: any error returns {"exit": False} so backstops manage the exit.
    Picks the momentum vs gigafade exit prompt from position['signal']."""
    system = (MOMENTUM_EXIT_SYSTEM_PROMPT
              if position.get("signal") == "momentum" else EXIT_SYSTEM_PROMPT)
    user = json.dumps(position, separators=(",", ":"))
    res = llm_router.chat(system, user, temperature=0.1, max_tokens=200,
                          force_json=True)
    if res is None:
        return {"exit": False, "reason": "hold (no provider answered)"}
    parsed = _extract_json(res.text)
    if isinstance(parsed, dict):
        return {"exit": _as_bool(parsed.get("exit", False)),
                "reason": str(parsed.get("reason", ""))[:120],
                "model": f"{res.provider}/{res.model}"}
    return {"exit": False, "reason": "hold (unparseable)"}


WATCHLIST_SYSTEM_PROMPT = (
    "You are a futures scalping scout for Hyperliquid perps. You are given the CURRENT "
    "microstructure of several symbols that are NOT yet in a position. Rank how well a "
    "short-term scalp setup is FORMING on each. Each item has: symbol, mid, spread_bps, "
    "ob_imbalance_top5 (-1..1, >0 bids dominate), ema_spread_bps (trend, +=up), "
    "realized_vol_30m, atr_bps, ret_z_1m. A good forming setup = a clear directional "
    "lean (trend + book agree) that is NOT yet exhausted, or a stretched move ripe to "
    "fade. Respond with ONE JSON object: {\"watchlist\": [{\"symbol\": str, \"score\": "
    "float 0..1, \"side\": \"BUY\"|\"SELL\"|\"none\", \"note\": short string}]}, one entry "
    "per input symbol, highest score = best setup forming. Nothing else."
)


def rank_watchlist(snapshots: list[dict]) -> dict:
    """Score forming setups across several non-positioned symbols in ONE call
    (deliberately a single request to stay RPM-cheap). Returns
    {"items": [...], "model": "provider/model"} or {"items": []} on failure."""
    if not snapshots:
        return {"items": []}
    user = json.dumps({"symbols": snapshots}, separators=(",", ":"))
    res = llm_router.chat(WATCHLIST_SYSTEM_PROMPT, user, temperature=0.2,
                          max_tokens=400, force_json=True)
    if res is None:
        return {"items": []}
    parsed = _extract_json(res.text)
    items = parsed.get("watchlist", []) if isinstance(parsed, dict) else []
    if not isinstance(items, list):
        items = []
    clean = []
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            clean.append({
                "symbol": str(it.get("symbol", "")),
                "score": max(0.0, min(1.0, float(it.get("score", 0.0)))),
                "side": str(it.get("side", "none")),
                "note": str(it.get("note", ""))[:120],
            })
        except (TypeError, ValueError):
            continue
    clean.sort(key=lambda x: x["score"], reverse=True)
    return {"items": clean, "model": f"{res.provider}/{res.model}"}


def health_check() -> tuple[bool, str]:
    """Quick liveness probe — returns (ok, message). At least one enabled
    provider (hosted key or local Ollama) must be configured."""
    st = llm_router.status()
    if st["count"] == 0:
        return False, "no LLM providers enabled (no keys + Ollama unreachable)"
    names = ", ".join(f"{p['name']}/{p['model']}" for p in st["enabled"])
    return True, f"ok — {st['count']} provider(s): {names}"
