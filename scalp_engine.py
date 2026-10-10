"""
Scalp engine — local poller over Hyperliquid candles + L2.

Loop:
  1. For each tracked symbol, poll 1m candles + L2 snapshot.
  2. Compute lightweight features (EMA9/21, ATR, return-zscore, OB imbalance,
     spread bps, realized vol).
  3. Apply a fast deterministic gate (cheap NOs filter most candles out).
  4. If gate passes, hand a compact snapshot to scalp_agent.decide().
  5. Engine returns a Plan to the caller (server.py wires execution + risk).

The engine itself does NOT place orders — that's the controller's job, so live
vs paper, kill switches, and bracket management stay in one place.
"""

from __future__ import annotations

import logging
import math
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

from scalp_agent import Decision, decide

logger = logging.getLogger(__name__)

# FADE MODE (2026-06-04): momentum entries were anti-predictive — 13 stops / 3 TPs
# (19% WR vs 33% breakeven) across 16+ trades. We now enter AGAINST the momentum
# signal (the gate still enforces signal coherence; we just trade the other side).
# Reversible: set SCALP_FADE=false to revert to momentum-following.
SCALP_FADE = os.getenv("SCALP_FADE", "true").lower() == "true"

# WIDEN STOPS (2026-06-04): fade fixed direction partially (WR 16%->~26%) but
# 76% stop-out persisted across 21 trades — getting wicked out before the
# reversion completes. Override the agent's stop to a floor of STOP_ATR_MULT x
# ATR, keeping the agent's target. Widens stop RELATIVE to target to cut
# premature wick-outs. Reversible: STOP_WIDEN=false.
STOP_WIDEN = os.getenv("STOP_WIDEN", "false").lower() == "true"  # reverted 22:45->23:02: widen FAILED (breakeven jumped to 52%, avg loss doubled)
STOP_ATR_MULT = float(os.getenv("STOP_ATR_MULT", "2.5"))

POLL_INTERVAL_S = 5.0
CANDLE_INTERVAL = "1m"
CANDLE_LOOKBACK_M = 60
MIN_CANDLES_FOR_SIGNAL = 30
MAX_SPREAD_BPS = 8.0
MAX_REALIZED_VOL_PCT = 0.025  # tuned 2026-06-04 from live feed (vol clustered 0.015-0.016)
MIN_RET_Z = 0.45             # tuned 2026-06-04 (observed max abs(ret_z)=0.79; 0.8 never fired)
STALE_CANDLE_S = 90

# SIGNAL SELECTOR (2026-06-05): backtest proved ret_z momentum/fade has NO edge.
# GIGAFADE (user Pine algo) showed real ~60% directional edge OOS — net-positive
# at maker cost with TIME-exit (not TP/SL). Wiring it as the live signal to
# forward-validate. Reversible: SCALP_SIGNAL=momentum.
SCALP_SIGNAL = os.getenv("SCALP_SIGNAL", "gigafade").lower()
# 2026-06-19: with hosted 70B models (llm_router) the LLM can read raw floats, so
# momentum is now AI-gated too (was rules-only since 2026-06-09 because the 7B
# couldn't threshold numbers). SCALP_MOMENTUM_AI=false reverts to rules-approve.
# On AI failure (no provider / timeout) momentum FAILS CLOSED (skips the entry) by
# default — safer for a net-negative signal — unless SCALP_MOMENTUM_AI_FALLBACK=rules.
SCALP_MOMENTUM_AI = os.getenv("SCALP_MOMENTUM_AI", "true").lower() == "true"
SCALP_MOMENTUM_AI_FALLBACK = os.getenv("SCALP_MOMENTUM_AI_FALLBACK", "skip").lower()
# --- GIGAFADE params (best OOS config: p80, RSI 70/30, hold 2) ---
GIGA_TF = os.getenv("GIGA_TF", "5m")
GIGA_PCT = float(os.getenv("GIGA_PCT", "80"))
GIGA_MIN_BODY = float(os.getenv("GIGA_MIN_BODY", "0.3"))   # % of price
GIGA_RSI_OB = float(os.getenv("GIGA_RSI_OB", "70"))
GIGA_RSI_OS = float(os.getenv("GIGA_RSI_OS", "30"))
GIGA_LOOKBACK = int(os.getenv("GIGA_LOOKBACK", "500"))
GIGA_HOLD_CANDLES = int(os.getenv("GIGA_HOLD_CANDLES", "2"))  # time-exit horizon
GIGA_STOP_BPS = float(os.getenv("GIGA_STOP_BPS", "60"))       # wide crash backstop
_TF_SECONDS = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "1h": 3600}


def _rsi_wilder(closes: list[float], n: int = 14) -> float:
    """Wilder RSI of the last value (matches TradingView ta.rsi)."""
    if len(closes) < n + 1:
        return 50.0
    gain = loss = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gain += max(d, 0.0); loss += max(-d, 0.0)
    ag, al = gain / n, loss / n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0.0)) / n
        al = (al * (n - 1) + max(-d, 0.0)) / n
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def _pctile_nr(arr: list[float], pct: float) -> float:
    if not arr:
        return 0.0
    s = sorted(arr)
    k = max(1, math.ceil(pct / 100.0 * len(s)))
    return s[min(k - 1, len(s) - 1)]


@dataclass
class SymbolState:
    symbol: str
    candles: deque = field(default_factory=lambda: deque(maxlen=CANDLE_LOOKBACK_M))
    last_signal_ts: float = 0.0
    cooldown_until: float = 0.0
    last_giga_candle_t: int = 0   # last signal-candle open-time we evaluated


@dataclass(frozen=True)
class Plan:
    """What the controller should do for this symbol on this tick."""
    symbol: str
    enter: bool
    side: str           # "BUY" or "SELL"
    size_pct: float     # of allotted scalp capital
    stop_bps: float
    tp_bps: float
    mid: float
    reason: str
    features: dict
    hold_secs: float = 0.0   # >0 => time-exit after this many seconds (GIGAFADE)
    signal: str = ""         # which signal produced this plan: "gigafade" | "momentum"
    ai_gated: bool = False   # True when the LLM judged this entry (vs rules auto-approve)
    model: str = ""          # "provider/model" that judged it (when ai_gated)
    ai_latency_ms: float = 0.0  # LLM round-trip latency for this decision


@dataclass(frozen=True)
class Evaluation:
    """Full record of one symbol-tick — used for the UI feed.

    Captures everything (gate fail, agent refusal, agent enter) so the user
    can see the bot's full thought process, not just trades it took.
    """
    ts: float
    symbol: str
    stage: str          # "no-data" | "gate-fail" | "agent-refuse" | "enter"
    mid: float
    gate_reason: str
    snapshot: dict      # what the agent saw (empty if no-data / gate-fail)
    decision_reason: str
    plan: Optional[Plan]


def _ema(values: list[float], n: int) -> float:
    if not values:
        return 0.0
    k = 2.0 / (n + 1.0)
    out = values[0]
    for v in values[1:]:
        out = v * k + out * (1.0 - k)
    return out


def _stdev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = sum(values) / len(values)
    var = sum((v - m) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(var)


def _features(closes: list[float], highs: list[float], lows: list[float]) -> dict:
    rets = [
        (closes[i] - closes[i - 1]) / closes[i - 1]
        for i in range(1, len(closes))
        if closes[i - 1] > 0
    ]
    last30 = rets[-30:] if len(rets) >= 30 else rets
    realized_vol = _stdev(last30) * math.sqrt(len(last30) or 1)
    ema9 = _ema(closes[-30:], 9)
    ema21 = _ema(closes[-30:], 21)
    last = closes[-1]
    trs = [
        max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        for i in range(1, len(closes))
    ]
    atr = (sum(trs[-14:]) / max(len(trs[-14:]), 1)) if trs else 0.0
    return {
        "last": last,
        "ema9": ema9,
        "ema21": ema21,
        "ema_spread_bps": ((ema9 - ema21) / last) * 10000 if last else 0.0,
        "realized_vol": realized_vol,
        "atr_bps": (atr / last) * 10000 if last else 0.0,
        "ret_z": (rets[-1] / (_stdev(last30) or 1e-9)) if rets else 0.0,
    }


def _ob_imbalance(book: dict) -> tuple[float, float, float]:
    """Return (mid, spread_bps, top5_imbalance) from an l2 snapshot."""
    levels = book.get("levels", [])
    if len(levels) < 2 or not levels[0] or not levels[1]:
        return 0.0, 0.0, 0.0
    bids, asks = levels[0], levels[1]
    bid_px = float(bids[0]["px"])
    ask_px = float(asks[0]["px"])
    mid = (bid_px + ask_px) / 2.0
    spread_bps = ((ask_px - bid_px) / mid) * 10000 if mid else 0.0
    bid_sz = sum(float(l["sz"]) for l in bids[:5])
    ask_sz = sum(float(l["sz"]) for l in asks[:5])
    total = bid_sz + ask_sz
    imb = (bid_sz - ask_sz) / total if total else 0.0
    return mid, spread_bps, imb


def _gate(features: dict, mid: float, spread_bps: float) -> tuple[bool, str, str]:
    """Cheap deterministic filter. Returns (pass, side, why)."""
    if mid <= 0:
        return False, "", "no mid"
    if spread_bps > MAX_SPREAD_BPS:
        return False, "", f"spread {spread_bps:.1f}bps > {MAX_SPREAD_BPS}"
    if features["realized_vol"] > MAX_REALIZED_VOL_PCT:
        return False, "", f"realized_vol {features['realized_vol']:.4f} too hot"
    if abs(features["ret_z"]) < MIN_RET_Z:
        return False, "", f"ret_z {features['ret_z']:.2f} too quiet"
    side = "BUY" if features["ema_spread_bps"] > 0 else "SELL"
    if (features["ret_z"] > 0) != (side == "BUY"):
        return False, "", "ret sign disagrees with EMA trend"
    return True, side, "passes pre-filter"


def build_snapshot(symbol: str, side: str, mid: float, spread_bps: float,
                   imbalance: float, features: dict) -> dict:
    """Compact JSON the LLM agent sees. Keep it under ~400 tokens."""
    return {
        "symbol": symbol,
        "side_hint": side,
        "mid": round(mid, 6),
        "spread_bps": round(spread_bps, 2),
        "ob_imbalance_top5": round(imbalance, 3),
        "ema_spread_bps": round(features["ema_spread_bps"], 2),
        "realized_vol_30m": round(features["realized_vol"], 5),
        "atr_bps": round(features["atr_bps"], 1),
        "ret_z_1m": round(features["ret_z"], 2),
    }


def _book_pressure(side: str, imbalance: float) -> str:
    """Categorical top-5 order-book pressure relative to the trade side.
    imbalance>0 => bids dominate (upward pressure). 'against' = pressure opposes
    our side; 'favors' = pressure helps it."""
    if side == "SELL":
        return "against" if imbalance > 0.30 else ("favors" if imbalance < -0.30 else "neutral")
    return "against" if imbalance < -0.30 else ("favors" if imbalance > 0.30 else "neutral")


def gigafade_agent_view(symbol: str, side: str, body_pct: float, thr: float,
                        rsi: float, green: bool, imbalance: float) -> dict:
    """CATEGORICAL-label snapshot for the LLM (gigafade only). A 7B model cannot
    threshold raw bps/RSI floats — proven 2026-06-09: it called RSI 21 'not extreme'
    and a 0.16bps spread 'too wide' — but it classifies reliably over pre-computed
    labels. Spread/vol/liquidity are already gate-filtered, so 'spread' is fixed
    'tight' and is explicitly not the model's concern."""
    # The deterministic gate ALREADY requires rsi past GIGA_RSI_OB/OS (70/30), so any
    # candidate reaching here is at an extreme by definition — 'extreme' = the gate
    # threshold, NOT a stricter invented cut. Replaying the 18 real gigafade trades
    # (2026-06-09) proved the proven ETH winners cluster at rsi 70.8/71.6/25.4: a
    # stricter "extreme=72/28" refused +$4.85 of ETH winners while keeping only +$3.26.
    # candle_extremity (body >= 1.3x p80 threshold) is the REAL filter — it kept every
    # non-HYPE winner and dropped the one marginal-body loser.
    if rsi >= GIGA_RSI_OB:        rstate = "extreme_overbought"
    elif rsi <= GIGA_RSI_OS:      rstate = "extreme_oversold"
    else:                         rstate = "not_extreme"   # shouldn't occur post-gate
    extremity = "well_above_normal" if (thr > 0 and body_pct >= 1.3 * thr) else "marginal"
    return {
        "symbol": symbol, "signal": "gigafade", "side_hint": side,
        "spread": "tight",
        "candle": "green" if green else "red",
        "candle_extremity": extremity,
        "rsi_state": rstate,
        "book_pressure_vs_fade": _book_pressure(side, imbalance),
        "thesis": f"fade {'overbought big-green->short' if green else 'oversold big-red->long'}",
    }


class ScalpEngine:
    def __init__(self, info, symbols: list[str]):
        """info is a hyperliquid.info.Info instance shared with server.py."""
        self.info = info
        self.symbols = list(symbols)
        self.state: dict[str, SymbolState] = {
            s: SymbolState(symbol=s) for s in symbols
        }

    def _refresh_candles(self, symbol: str) -> Optional[list[dict]]:
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - CANDLE_LOOKBACK_M * 60 * 1000
        try:
            candles = self.info.candles_snapshot(
                symbol, CANDLE_INTERVAL, start_ms, end_ms
            )
        except Exception as exc:  # SDK raises generic exceptions
            logger.warning("scalp_engine: candles_snapshot(%s) failed: %s", symbol, exc)
            return None
        if not candles:
            return None
        if (end_ms - int(candles[-1].get("t", 0))) > STALE_CANDLE_S * 1000:
            return None
        return candles

    def _book(self, symbol: str) -> Optional[dict]:
        try:
            return self.info.l2_snapshot(symbol)
        except Exception as exc:
            logger.warning("scalp_engine: l2_snapshot(%s) failed: %s", symbol, exc)
            return None

    def microstructure_snapshot(self, symbol: str) -> Optional[dict]:
        """Raw microstructure for a symbol regardless of any gate — used by the
        watchlist scanner to scout forming setups on symbols not in a position.
        Returns a build_snapshot-style dict (no side bias) or None on no data."""
        candles = self._refresh_candles(symbol)
        if not candles or len(candles) < MIN_CANDLES_FOR_SIGNAL:
            return None
        closes = [float(c["c"]) for c in candles]
        highs = [float(c["h"]) for c in candles]
        lows = [float(c["l"]) for c in candles]
        feats = _features(closes, highs, lows)
        book = self._book(symbol)
        if not book:
            return None
        mid, spread_bps, imb = _ob_imbalance(book)
        if mid <= 0:
            return None
        # side_hint omitted on purpose — the scanner decides direction itself.
        snap = build_snapshot(symbol, "", mid, spread_bps, imb, feats)
        snap.pop("side_hint", None)
        return snap

    def _signal_candles(self, symbol: str, tf: str, lookback: int) -> Optional[list[dict]]:
        tf_ms = _TF_SECONDS.get(tf, 300) * 1000
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - (lookback + 5) * tf_ms
        try:
            cs = self.info.candles_snapshot(symbol, tf, start_ms, end_ms)
        except Exception as exc:
            logger.warning("scalp_engine: signal candles(%s,%s) failed: %s", symbol, tf, exc)
            return None
        return cs or None

    def evaluate(self, symbol: str, *, use_agent: bool = True) -> Evaluation:
        st = self.state[symbol]
        now = time.time()
        if now < st.cooldown_until:
            return Evaluation(now, symbol, "no-data", 0.0,
                              f"cooldown {st.cooldown_until - now:.0f}s",
                              {}, "", None)

        if SCALP_SIGNAL == "gigafade":
            return self._eval_gigafade(symbol, now, st, use_agent)

        if SCALP_SIGNAL == "all":
            # GIGAFADE has priority (validation target). Only fall through to
            # momentum when gigafade does not produce an enter plan.
            ev_g = self._eval_gigafade(symbol, now, st, use_agent)
            if ev_g.plan and ev_g.plan.enter:
                return ev_g

        candles = self._refresh_candles(symbol)
        if not candles or len(candles) < MIN_CANDLES_FOR_SIGNAL:
            return Evaluation(now, symbol, "no-data", 0.0,
                              "insufficient candles", {}, "", None)
        closes = [float(c["c"]) for c in candles]
        highs = [float(c["h"]) for c in candles]
        lows = [float(c["l"]) for c in candles]
        feats = _features(closes, highs, lows)

        book = self._book(symbol)
        if not book:
            return Evaluation(now, symbol, "no-data", 0.0,
                              "no L2 book", {}, "", None)
        mid, spread_bps, imb = _ob_imbalance(book)

        gate_ok, side, why = _gate(feats, mid, spread_bps)
        if not gate_ok:
            return Evaluation(now, symbol, "gate-fail", mid, why, {}, "", None)

        if SCALP_FADE:
            side = "SELL" if side == "BUY" else "BUY"
            why = why + " | FADED"

        snap = build_snapshot(symbol, side, mid, spread_bps, imb, feats)
        # 2026-06-19: momentum is now AI-gated via llm_router (hosted 70B models can
        # read the raw-float snapshot the 7B couldn't). On AI failure we FAIL CLOSED
        # (skip) by default — momentum is a net-negative signal, so no-trade is the
        # safe default — unless SCALP_MOMENTUM_AI_FALLBACK=rules. SCALP_MOMENTUM_AI=
        # false fully reverts to the old rules auto-approve.
        ai_gated = use_agent and SCALP_MOMENTUM_AI
        if ai_gated:
            # build_snapshot omits 'signal' (it's reused gate-free by the watchlist),
            # so tag it here — decide() picks MOMENTUM_SYSTEM_PROMPT off this key.
            decision = decide({**snap, "signal": "momentum"})
            if not decision.enter and decision.model == "":
                # AI was unreachable (refuse with no model tag) — apply fallback.
                if SCALP_MOMENTUM_AI_FALLBACK == "rules":
                    decision = Decision(True, 0.5, 15.0, 30.0,
                                        "rules fallback (AI unreachable)")
                    ai_gated = False
                # else: leave as refuse => skip the entry (fail closed)
        else:
            decision = Decision(True, 0.5, 15.0, 30.0,
                                "rules-only (momentum AI disabled)")

        st.last_signal_ts = now
        stop_bps = decision.stop_bps
        if STOP_WIDEN and decision.enter:
            atr_bps = feats.get("atr_bps", 0.0)
            widened = STOP_ATR_MULT * atr_bps
            if widened > stop_bps:
                why = why + f" | STOP→{widened:.0f}bps(2.5xATR)"
                stop_bps = widened
        plan = Plan(
            symbol=symbol, enter=decision.enter, side=side,
            size_pct=decision.size_pct, stop_bps=stop_bps,
            tp_bps=decision.tp_bps, mid=mid, reason=decision.reason,
            features=snap, signal="momentum", ai_gated=ai_gated,
            model=decision.model, ai_latency_ms=decision.latency_ms,
        )
        stage = "enter" if decision.enter else "agent-refuse"
        return Evaluation(now, symbol, stage, mid, why, snap,
                          decision.reason, plan)

    def _eval_gigafade(self, symbol: str, now: float, st: "SymbolState",
                       use_agent: bool) -> Evaluation:
        """GIGAFADE signal: fade a top-percentile body candle at an RSI extreme.
        Fires once per newly-closed signal-tf candle. Time-exit downstream."""
        tf_s0 = _TF_SECONDS.get(GIGA_TF, 300)
        # cheap guard: skip the 500-candle fetch unless a new candle has closed
        last_closed_open_ms = (int(now) // tf_s0 * tf_s0 - tf_s0) * 1000
        if last_closed_open_ms <= st.last_giga_candle_t:
            return Evaluation(now, symbol, "no-data", 0.0,
                              "awaiting new candle", {}, "", None)
        cs = self._signal_candles(symbol, GIGA_TF, GIGA_LOOKBACK)
        if not cs or len(cs) < 60:
            return Evaluation(now, symbol, "no-data", 0.0,
                              "insufficient signal candles", {}, "", None)
        tf_s = _TF_SECONDS.get(GIGA_TF, 300)
        # last FULLY-CLOSED candle
        if (now * 1000 - int(cs[-1]["t"])) >= tf_s * 1000:
            idx = len(cs) - 1
        else:
            idx = len(cs) - 2
        if idx < 30:
            return Evaluation(now, symbol, "no-data", 0.0, "warming up", {}, "", None)
        ct = int(cs[idx]["t"])
        if ct <= st.last_giga_candle_t:
            return Evaluation(now, symbol, "no-data", 0.0,
                              "awaiting new candle", {}, "", None)

        o = float(cs[idx]["o"]); c = float(cs[idx]["c"])
        closes = [float(x["c"]) for x in cs[:idx + 1]]
        body_pct = abs(c - o) / o * 100 if o else 0.0
        bodies = [abs(float(x["c"]) - float(x["o"])) / float(x["o"]) * 100
                  for x in cs[max(0, idx - GIGA_LOOKBACK + 1):idx + 1] if float(x["o"])]
        thr = _pctile_nr(bodies, GIGA_PCT)
        rsi = _rsi_wilder(closes, 14)
        is_giga = body_pct >= thr and body_pct >= GIGA_MIN_BODY
        rsi_extreme = rsi >= GIGA_RSI_OB or rsi <= GIGA_RSI_OS

        # mark this candle processed regardless of outcome (fire once per candle)
        st.last_giga_candle_t = ct

        if not is_giga or not rsi_extreme:
            why = (f"no-giga body={body_pct:.2f}% thr={thr:.2f}% rsi={rsi:.0f}"
                   if not is_giga else f"rsi {rsi:.0f} not extreme")
            return Evaluation(now, symbol, "gate-fail", c, why, {}, "", None)

        green = c > o
        side = "SELL" if green else "BUY"   # fade the giga candle
        book = self._book(symbol)
        if not book:
            return Evaluation(now, symbol, "no-data", 0.0, "no L2 book", {}, "", None)
        mid, spread_bps, imb = _ob_imbalance(book)
        if spread_bps > MAX_SPREAD_BPS:
            return Evaluation(now, symbol, "gate-fail", mid,
                              f"spread {spread_bps:.1f}bps > {MAX_SPREAD_BPS}", {}, "", None)

        # Raw snapshot — kept for the UI feed / paper log (informative numbers).
        snap = {
            "symbol": symbol, "signal": "gigafade", "side_hint": side,
            "mid": round(mid, 6), "spread_bps": round(spread_bps, 2),
            "giga_body_pct": round(body_pct, 3), "giga_threshold_pct": round(thr, 3),
            "rsi14": round(rsi, 1), "candle": "green" if green else "red",
            "ob_imbalance_top5": round(imb, 3),
            "thesis": f"fade {'overbought big-green→short' if green else 'oversold big-red→long'}",
        }
        if use_agent:
            # The MODEL sees categorical labels, not the raw floats above (it can't
            # threshold them reliably). Raw snap still feeds the UI/log.
            agent_view = gigafade_agent_view(symbol, side, body_pct, thr, rsi, green, imb)
            decision: Decision = decide(agent_view)
        else:
            decision = Decision(True, 0.5, GIGA_STOP_BPS, 0.0, "rules-only auto-approve")

        why = (f"GIGAFADE {side} body={body_pct:.2f}%(thr{thr:.2f}) rsi={rsi:.0f}")
        plan = Plan(
            symbol=symbol, enter=decision.enter, side=side,
            size_pct=decision.size_pct,
            stop_bps=max(decision.stop_bps, GIGA_STOP_BPS),  # wide backstop
            tp_bps=0.0,                                       # time-exit, no fixed TP
            mid=mid, reason=decision.reason, features=snap,
            hold_secs=GIGA_HOLD_CANDLES * tf_s, signal="gigafade",
            ai_gated=use_agent, model=decision.model,
            ai_latency_ms=decision.latency_ms,
        )
        stage = "enter" if decision.enter else "agent-refuse"
        return Evaluation(now, symbol, stage, mid, why, snap, decision.reason, plan)

    def set_cooldown(self, symbol: str, seconds: float) -> None:
        self.state[symbol].cooldown_until = time.time() + seconds
