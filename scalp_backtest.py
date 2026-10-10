#!/usr/bin/env python3
"""
Scalp backtest harness — replays parameterized entry signals over historical
Hyperliquid candles (same venue we trade on) and simulates ATR/bps stop-TP
exits with realistic execution cost. Walk-forward (train/test) by default.

Why this exists: live paper at ~1 trade / 3 min is too slow to reach
significance and invites overfitting. Here we test hundreds of configs over
weeks of real candles in seconds, then paper-validate only the survivors.

LIMITATION: order-book imbalance / L2 features are live-only and NOT available
here. This harness covers candle-derivable signals. OB-dependent variants must
be tested on the live paper loop.

Usage:
    python scalp_backtest.py                 # default sweep
    python scalp_backtest.py --days 21 --tf 5m
"""
from __future__ import annotations

import argparse
import math
import time
from dataclasses import dataclass
from typing import Callable, Optional

from hyperliquid.info import Info
from hyperliquid.utils import constants

# ----- execution cost (Hyperliquid taker ~3.5bps/side + slippage) -----
# taker round-trip ~= 10bps; maker (post-only limit) ~= 2-3bps. Override via --cost.
FEE_BPS_PER_SIDE = 3.5
SLIP_BPS_PER_SIDE = 1.5
ROUND_TRIP_COST_BPS = 2 * (FEE_BPS_PER_SIDE + SLIP_BPS_PER_SIDE)  # ~10 bps

_info = None
def info() -> Info:
    global _info
    if _info is None:
        _info = Info(constants.MAINNET_API_URL, skip_ws=True)
    return _info


# ============================ data ============================
def fetch_candles(symbol: str, interval: str, days: int) -> list[dict]:
    """Paginate HL candles_snapshot backward to assemble ~`days` of history.
    HL caps ~5000 candles/request, so we walk back in chunks."""
    now = int(time.time() * 1000)
    want_start = now - days * 86400_000
    out: list[dict] = []
    end = now
    for _ in range(20):  # safety cap on pagination
        start = max(want_start, end - 4900 * _interval_ms(interval))
        chunk = info().candles_snapshot(symbol, interval, start, end)
        if not chunk:
            break
        out = chunk + out
        first_t = int(chunk[0]["t"])
        if first_t <= want_start or len(chunk) < 2:
            break
        end = first_t - 1
        if start <= want_start:
            break
    # dedupe by t, sort
    seen = {}
    for c in out:
        seen[int(c["t"])] = c
    rows = [seen[t] for t in sorted(seen)]
    return [{
        "t": int(c["t"]),
        "o": float(c["o"]), "h": float(c["h"]),
        "l": float(c["l"]), "c": float(c["c"]),
        "v": float(c.get("v", 0)),
    } for c in rows]


def _interval_ms(tf: str) -> int:
    return {"1m": 60_000, "3m": 180_000, "5m": 300_000,
            "15m": 900_000, "1h": 3_600_000}[tf]


# ====================== indicators ======================
def ema(vals: list[float], n: int) -> list[float]:
    if not vals:
        return []
    k = 2.0 / (n + 1.0)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rolling_std(vals: list[float], i: int, n: int) -> float:
    lo = max(0, i - n + 1)
    w = vals[lo:i + 1]
    if len(w) < 2:
        return 0.0
    m = sum(w) / len(w)
    return math.sqrt(sum((x - m) ** 2 for x in w) / (len(w) - 1))


def rolling_mean(vals: list[float], i: int, n: int) -> float:
    lo = max(0, i - n + 1)
    w = vals[lo:i + 1]
    return sum(w) / len(w) if w else 0.0


def rsi(closes: list[float], i: int, n: int = 14) -> float:
    lo = max(1, i - n + 1)
    gains = losses = 0.0
    for j in range(lo, i + 1):
        d = closes[j] - closes[j - 1]
        if d >= 0:
            gains += d
        else:
            losses -= d
    if losses == 0:
        return 100.0
    rs = (gains / n) / (losses / n)
    return 100 - 100 / (1 + rs)


def _rsi_series(c: list[float], n: int = 14) -> list[float]:
    """Wilder's RSI series (matches TradingView ta.rsi)."""
    out = [50.0] * len(c)
    if len(c) < 2:
        return out
    gain = loss = 0.0
    for i in range(1, len(c)):
        d = c[i] - c[i - 1]
        g = max(d, 0.0); l = max(-d, 0.0)
        if i <= n:
            gain += g; loss += l
            if i == n:
                ag, al = gain / n, loss / n
                out[i] = 100 - 100 / (1 + (ag / al if al else 1e9))
        else:
            ag = (ag * (n - 1) + g) / n
            al = (al * (n - 1) + l) / n
            out[i] = 100 - 100 / (1 + (ag / al if al else 1e9))
    return out


def _stoch_series(h, l, c, n: int = 14) -> list[float]:
    """Stochastic %K (matches ta.stoch)."""
    out = [50.0] * len(c)
    for i in range(len(c)):
        lo = max(0, i - n + 1)
        hh = max(h[lo:i + 1]); ll = min(l[lo:i + 1])
        out[i] = (c[i] - ll) / (hh - ll) * 100 if hh > ll else 50.0
    return out


def _macd_hist(c: list[float], fast=12, slow=26, sig=9) -> list[float]:
    ef = ema(c, fast); es = ema(c, slow)
    line = [ef[i] - es[i] for i in range(len(c))]
    sigl = ema(line, sig)
    return [line[i] - sigl[i] for i in range(len(c))]


def _percentile_nr(arr: list[float], pct: float) -> float:
    """Nearest-rank percentile (matches Pine array.percentile_nearest_rank)."""
    if not arr:
        return 0.0
    s = sorted(arr)
    k = max(1, math.ceil(pct / 100.0 * len(s)))
    return s[min(k - 1, len(s) - 1)]


# ====================== precompute features ======================
class Series:
    def __init__(self, candles: list[dict]):
        self.t = [c["t"] for c in candles]
        self.o = [c["o"] for c in candles]
        self.h = [c["h"] for c in candles]
        self.l = [c["l"] for c in candles]
        self.c = [c["c"] for c in candles]
        self.v = [c["v"] for c in candles]
        self.n = len(candles)
        self.ema9 = ema(self.c, 9)
        self.ema21 = ema(self.c, 21)
        self.rets = [0.0] + [
            (self.c[i] - self.c[i - 1]) / self.c[i - 1] if self.c[i - 1] else 0.0
            for i in range(1, self.n)
        ]
        # ATR(14) in price
        self.atr = [0.0] * self.n
        trs = [0.0]
        for i in range(1, self.n):
            tr = max(self.h[i] - self.l[i],
                     abs(self.h[i] - self.c[i - 1]),
                     abs(self.l[i] - self.c[i - 1]))
            trs.append(tr)
        for i in range(self.n):
            lo = max(0, i - 13)
            w = trs[lo:i + 1]
            self.atr[i] = sum(w) / len(w) if w else 0.0
        # ---- indicators for user (Pine) algos ----
        self.bodypct = [abs(self.c[i] - self.o[i]) / self.o[i] * 100 if self.o[i] else 0.0
                        for i in range(self.n)]
        self.rsi_arr = _rsi_series(self.c, 14)
        self.stoch = _stoch_series(self.h, self.l, self.c, 14)
        self.macdh = _macd_hist(self.c, 12, 26, 9)
        self.ema50 = ema(self.c, 50)
        # rolling 20 SMA + std for Bollinger
        self.sma20 = [rolling_mean(self.c, i, 20) for i in range(self.n)]
        self.std20 = [rolling_std(self.c, i, 20) for i in range(self.n)]

    def giga_thresh(self, i: int, pct: float, win: int = 500) -> float:
        lo = max(0, i - win + 1)
        return _percentile_nr(self.bodypct[lo:i + 1], pct)

    def ret_z(self, i: int, n: int = 30) -> float:
        s = rolling_std(self.rets, i, n)
        return self.rets[i] / s if s else 0.0

    def atr_bps(self, i: int) -> float:
        return (self.atr[i] / self.c[i]) * 1e4 if self.c[i] else 0.0


# ====================== entry signals ======================
# Each returns "BUY" | "SELL" | None for bar i (decision made at close of i,
# entered at open of i+1).
SignalFn = Callable[[Series, int, dict], Optional[str]]


def sig_momentum(s: Series, i: int, p: dict) -> Optional[str]:
    z = s.ret_z(i)
    if abs(z) < p["z"]:
        return None
    trend_up = s.ema9[i] > s.ema21[i]
    if z > 0 and trend_up:
        return "BUY"
    if z < 0 and not trend_up:
        return "SELL"
    return None


def sig_fade(s: Series, i: int, p: dict) -> Optional[str]:
    m = sig_momentum(s, i, p)
    return None if m is None else ("SELL" if m == "BUY" else "BUY")


def sig_zrevert(s: Series, i: int, p: dict) -> Optional[str]:
    """Mean-reversion: price z-score vs SMA(n). Fade extremes."""
    n = p.get("n", 20)
    m = rolling_mean(s.c, i, n)
    sd = rolling_std(s.c, i, n)
    if sd == 0:
        return None
    z = (s.c[i] - m) / sd
    if z <= -p["z"]:
        return "BUY"
    if z >= p["z"]:
        return "SELL"
    return None


def sig_rsi(s: Series, i: int, p: dict) -> Optional[str]:
    r = rsi(s.c, i, p.get("n", 14))
    if r <= p.get("lo", 30):
        return "BUY"
    if r >= p.get("hi", 70):
        return "SELL"
    return None


def sig_bbreak(s: Series, i: int, p: dict) -> Optional[str]:
    """Bollinger breakout (momentum): close beyond band -> go with it."""
    n = p.get("n", 20)
    m = rolling_mean(s.c, i, n)
    sd = rolling_std(s.c, i, n)
    if sd == 0:
        return None
    if s.c[i] > m + p["z"] * sd:
        return "BUY"
    if s.c[i] < m - p["z"] * sd:
        return "SELL"
    return None


def sig_emacross(s: Series, i: int, p: dict) -> Optional[str]:
    if i < 1:
        return None
    prev = s.ema9[i - 1] - s.ema21[i - 1]
    cur = s.ema9[i] - s.ema21[i]
    if prev <= 0 < cur:
        return "BUY"
    if prev >= 0 > cur:
        return "SELL"
    return None


def sig_gigafade(s: Series, i: int, p: dict) -> Optional[str]:
    """User algo: GIGAFADE. Fade a top-percentile body candle that coincides
    with an RSI extreme. Pine: 'FADE GIGA'."""
    if i < 50:
        return None
    bp = s.bodypct[i]
    thr = s.giga_thresh(i, p.get("pct", 80.0))
    if bp < thr or bp < p.get("min_body", 0.3):
        return None
    r = s.rsi_arr[i]
    if not (r >= p.get("rsi_ob", 68) or r <= p.get("rsi_os", 25)):
        return None
    green = s.c[i] > s.o[i]
    return "SELL" if green else "BUY"        # fade the giga candle


def sig_contrarian(s: Series, i: int, p: dict) -> Optional[str]:
    """User algo: Contrarian V8. Confluence score (RSI/Stoch/BB/MACD + candle
    patterns). invert=True => momentum mode (exhaustion = continuation)."""
    if i < 51:
        return None
    rsi_v, rsi_p = s.rsi_arr[i], s.rsi_arr[i - 1]
    stoch = s.stoch[i]
    mh, mhp = s.macdh[i], s.macdh[i - 1]
    bb_lo = s.sma20[i] - 2 * s.std20[i]
    bb_hi = s.sma20[i] + 2 * s.std20[i]
    o, c, h, l = s.o[i], s.c[i], s.h[i], s.l[i]
    body = abs(c - o)
    uw = h - max(o, c); lw = min(o, c) - l
    pgreen = s.c[i - 1] > s.o[i - 1]; pred = s.c[i - 1] < s.o[i - 1]
    pbody = abs(s.c[i - 1] - s.o[i - 1])
    hammer = lw > 2 * body and uw < 0.5 * body and body > 0
    sstar = uw > 2 * body and lw < 0.5 * body and body > 0
    bull_eng = pred and c > o and body > pbody and o <= s.c[i - 1]
    bear_eng = pgreen and c < o and body > pbody and o >= s.c[i - 1]
    rlo_e, rhi_e = p.get("rsi_lo_e", 20), p.get("rsi_hi_e", 80)
    rlo_m, rhi_m = p.get("rsi_lo_m", 30), p.get("rsi_hi_m", 70)
    so, sob = p.get("stoch_os", 25), p.get("stoch_ob", 75)
    macd_bull = mh > mhp; macd_bear = mh < mhp

    rc_bull = rsi_v < rlo_e or (rsi_v < rlo_m and rsi_v > rsi_p)
    osc_bull = stoch < so or c <= bb_lo
    bull_exh = rc_bull and osc_bull and macd_bull
    rc_bear = rsi_v > rhi_e or (rsi_v > rhi_m and rsi_v < rsi_p)
    osc_bear = stoch > sob or c >= bb_hi
    bear_exh = rc_bear and osc_bear and macd_bear

    bull = (3 if bull_exh else 0) + (2 if hammer else 0) + (2 if bull_eng else 0) \
        + (1 if (rsi_v < 40 and macd_bull) else 0) + (1 if stoch < so else 0) \
        + (1 if c <= bb_lo else 0)
    bear = (3 if bear_exh else 0) + (2 if sstar else 0) + (2 if bear_eng else 0) \
        + (1 if (rsi_v > 60 and macd_bear) else 0) + (1 if stoch > sob else 0) \
        + (1 if c >= bb_hi else 0)
    thr = p.get("score", 5)
    bsig = bull >= thr and not (bear >= thr and bear > bull)
    rsig = bear >= thr and not (bull >= thr and bull >= bear)
    if p.get("invert", True):
        bsig, rsig = rsig, bsig
    return "BUY" if bsig else ("SELL" if rsig else None)


SIGNALS: dict[str, SignalFn] = {
    "momentum": sig_momentum,
    "fade": sig_fade,
    "zrevert": sig_zrevert,
    "rsi": sig_rsi,
    "bbreak": sig_bbreak,
    "emacross": sig_emacross,
    "gigafade": sig_gigafade,
    "contrarian": sig_contrarian,
}


# ====================== simulation ======================
@dataclass
class Result:
    name: str
    symbol: str
    trades: int
    wr: float
    net_bps: float          # total, net of cost
    avg_bps: float          # per trade, net
    gross_avg_bps: float    # per trade, before cost
    pf: float               # profit factor
    expectancy_usd_per_100: float  # $ per $100 sized, per trade
    dir_wr: float = 0.0     # directional accuracy (gross move sign) — their metric


def simulate(s: Series, sig: SignalFn, p: dict, lo_i: int, hi_i: int) -> Result:
    """Replay over bars [lo_i, hi_i). Stop/TP in bps (ATR-scaled if atr_mult set)."""
    stop_bps = p.get("stop_bps", 25.0)
    tp_bps = p.get("tp_bps", 40.0)
    atr_mult_stop = p.get("atr_mult_stop")  # if set, stop = mult*atr_bps
    atr_mult_tp = p.get("atr_mult_tp")
    max_hold = p.get("max_hold", 36)
    cooldown = p.get("cooldown", 3)

    exit_mode = p.get("exit_mode", "bracket")  # "bracket" | "nbar"
    hold = p.get("hold", 1)

    pnls_bps: list[float] = []
    dir_wins = 0  # directional accuracy (sign of gross move), their native metric
    i = lo_i
    while i < hi_i - 1:
        side = sig(s, i, p)
        if side is None:
            i += 1
            continue
        if exit_mode == "nbar":
            entry = s.o[i + 1]
            if entry <= 0:
                i += 1
                continue
            long = side == "BUY"
            ex = min(i + 1 + hold, hi_i - 1)
            gross = ((s.c[ex] - entry) / entry * 1e4) * (1 if long else -1)
            if gross > 0:
                dir_wins += 1
            pnls_bps.append(gross - ROUND_TRIP_COST_BPS)
            i = ex + cooldown
            continue
        sb = atr_mult_stop * s.atr_bps(i) if atr_mult_stop else stop_bps
        tb = atr_mult_tp * s.atr_bps(i) if atr_mult_tp else tp_bps
        if sb <= 0 or tb <= 0:
            i += 1
            continue
        entry = s.o[i + 1]  # enter next bar open
        if entry <= 0:
            i += 1
            continue
        long = side == "BUY"
        stop_px = entry * (1 - sb / 1e4) if long else entry * (1 + sb / 1e4)
        tp_px = entry * (1 + tb / 1e4) if long else entry * (1 - tb / 1e4)
        exit_bps = None
        j = i + 1
        end_j = min(hi_i, i + 1 + max_hold)
        while j < end_j:
            hi, lo = s.h[j], s.l[j]
            if long:
                if lo <= stop_px:
                    exit_bps = -sb; break
                if hi >= tp_px:
                    exit_bps = tb; break
            else:
                if hi >= stop_px:
                    exit_bps = -sb; break
                if lo <= tp_px:
                    exit_bps = tb; break
            j += 1
        if exit_bps is None:  # time-stop at close
            cpx = s.c[min(j, hi_i - 1)]
            exit_bps = ((cpx - entry) / entry * 1e4) * (1 if long else -1)
        pnls_bps.append(exit_bps - ROUND_TRIP_COST_BPS)
        i = j + cooldown  # advance past the trade + cooldown

    n = len(pnls_bps)
    if n == 0:
        return Result(p.get("_name", "?"), s_symbol(s), 0, 0, 0, 0, 0, 0, 0, 0)
    wins = [x for x in pnls_bps if x > 0]
    losses = [x for x in pnls_bps if x <= 0]
    gross = [x + ROUND_TRIP_COST_BPS for x in pnls_bps]
    gross_wins = sum(1 for g in gross if g > 0)   # directional accuracy
    pf = (sum(wins) / -sum(losses)) if losses and sum(losses) != 0 else float("inf")
    return Result(
        name=p.get("_name", "?"), symbol=s_symbol(s), trades=n,
        wr=len(wins) / n, net_bps=sum(pnls_bps), avg_bps=sum(pnls_bps) / n,
        gross_avg_bps=sum(gross) / n, pf=pf,
        expectancy_usd_per_100=(sum(pnls_bps) / n) / 1e4 * 100,
        dir_wr=gross_wins / n,
    )


_SYM_TAG = {}
def s_symbol(s: Series) -> str:
    return _SYM_TAG.get(id(s), "?")


# ====================== sweep ======================
def default_grid() -> list[dict]:
    grid = []
    for z in (0.8, 1.2, 1.8):
        for stp, tp in ((20, 30), (25, 50), (15, 45)):
            grid.append({"_name": f"momentum z{z} {stp}/{tp}", "_sig": "momentum",
                         "z": z, "stop_bps": stp, "tp_bps": tp})
            grid.append({"_name": f"fade z{z} {stp}/{tp}", "_sig": "fade",
                         "z": z, "stop_bps": stp, "tp_bps": tp})
    for z in (1.5, 2.0, 2.5):
        for stp, tp in ((25, 40), (30, 60)):
            grid.append({"_name": f"zrevert z{z} n20 {stp}/{tp}", "_sig": "zrevert",
                         "z": z, "n": 20, "stop_bps": stp, "tp_bps": tp})
    for lo, hi in ((25, 75), (30, 70)):
        grid.append({"_name": f"rsi {lo}/{hi} 25/40", "_sig": "rsi",
                     "lo": lo, "hi": hi, "n": 14, "stop_bps": 25, "tp_bps": 40})
    for z in (1.5, 2.0):
        grid.append({"_name": f"bbreak z{z} 25/50", "_sig": "bbreak",
                     "z": z, "n": 20, "stop_bps": 25, "tp_bps": 50})
    grid.append({"_name": "emacross 25/45", "_sig": "emacross",
                 "stop_bps": 25, "tp_bps": 45})
    # ATR-scaled variants of the two reversion families
    for am_s, am_t in ((1.5, 2.5), (2.0, 3.0)):
        grid.append({"_name": f"zrevert z2.0 ATR{am_s}/{am_t}", "_sig": "zrevert",
                     "z": 2.0, "n": 20, "atr_mult_stop": am_s, "atr_mult_tp": am_t})
    return grid


def run(symbols: list[str], tf: str, days: int, train_frac: float = 0.65):
    print(f"\n{'='*78}\nSCALP BACKTEST | tf={tf} | {days}d | cost={ROUND_TRIP_COST_BPS:.0f}bps round-trip"
          f" | train={train_frac:.0%}\n{'='*78}")
    data = {}
    for sym in symbols:
        c = fetch_candles(sym, tf, days)
        if len(c) < 200:
            print(f"  {sym}: only {len(c)} candles — skipping")
            continue
        s = Series(c)
        _SYM_TAG[id(s)] = sym
        data[sym] = s
        span = (c[-1]["t"] - c[0]["t"]) / 86400_000
        print(f"  {sym}: {len(c)} candles, {span:.1f}d")

    grid = default_grid()
    # aggregate across symbols, train vs test
    agg: dict[str, dict] = {}
    for cfg in grid:
        sig = SIGNALS[cfg["_sig"]]
        tr_tr = tr_te = 0
        net_tr = net_te = 0.0
        win_tr = win_te = 0
        for sym, s in data.items():
            split = int(s.n * train_frac)
            rtr = simulate(s, sig, cfg, 25, split)
            rte = simulate(s, sig, cfg, split, s.n)
            tr_tr += rtr.trades; net_tr += rtr.net_bps; win_tr += round(rtr.wr * rtr.trades)
            tr_te += rte.trades; net_te += rte.net_bps; win_te += round(rte.wr * rte.trades)
        agg[cfg["_name"]] = {
            "tr_train": tr_tr, "avg_train": net_tr / tr_tr if tr_tr else 0,
            "wr_train": win_tr / tr_tr if tr_tr else 0,
            "tr_test": tr_te, "avg_test": net_te / tr_te if tr_te else 0,
            "wr_test": win_te / tr_te if tr_te else 0,
        }
    # rank by TEST avg bps (out-of-sample), require min trades
    rows = [(k, v) for k, v in agg.items() if v["tr_train"] >= 30 and v["tr_test"] >= 15]
    rows.sort(key=lambda kv: kv[1]["avg_test"], reverse=True)
    print(f"\n{'config':<26}{'TRAIN n/wr/avgbps':<26}{'TEST n/wr/avgbps':<26}edge?")
    print("-" * 90)
    for k, v in rows:
        edge = "OOS+ (real)" if (v["avg_test"] > 0 and v["avg_train"] > 0) else (
               "train-only" if v["avg_train"] > 0 else "no")
        print(f"{k:<26}"
              f"{v['tr_train']:>4} {v['wr_train']*100:>3.0f}% {v['avg_train']:>+6.1f}      "
              f"{v['tr_test']:>4} {v['wr_test']*100:>3.0f}% {v['avg_test']:>+6.1f}      {edge}")
    print("-" * 90)
    print("avgbps = net bps per trade AFTER cost. OOS+ = positive in BOTH train and held-out test.")
    pos = [k for k, v in rows if v["avg_test"] > 0 and v["avg_train"] > 0]
    if pos:
        print(f"\n{len(pos)} config(s) with out-of-sample edge: {pos[:5]}")
    else:
        print("\nNO config shows out-of-sample edge after cost. Signal likely has no candle-derivable edge at this tf.")


def user_grid() -> list[dict]:
    """User Pine algos: GIGAFADE + Contrarian V8. Tests their native directional
    thesis (nbar exits, hold 1-3) plus a futures bracket variant, with the key
    levers swept (percentile/RSI for giga; invert + score for contrarian)."""
    g = []
    for pct in (80.0, 85.0):
        for ob, os_ in ((68, 25), (70, 30)):
            for hold in (1, 2, 3):
                g.append({"_name": f"giga p{pct:.0f} {ob}/{os_} nbar{hold}",
                          "_sig": "gigafade", "pct": pct, "rsi_ob": ob,
                          "rsi_os": os_, "exit_mode": "nbar", "hold": hold,
                          "cooldown": 2})
            g.append({"_name": f"giga p{pct:.0f} {ob}/{os_} brk25/40",
                      "_sig": "gigafade", "pct": pct, "rsi_ob": ob, "rsi_os": os_,
                      "stop_bps": 25, "tp_bps": 40, "max_hold": 6, "cooldown": 2})
    for inv in (True, False):
        for score in (4, 5):
            tag = "mom" if inv else "contra"
            for hold in (1, 2, 3):
                g.append({"_name": f"v8-{tag} s{score} nbar{hold}",
                          "_sig": "contrarian", "invert": inv, "score": score,
                          "exit_mode": "nbar", "hold": hold, "cooldown": 2})
            g.append({"_name": f"v8-{tag} s{score} brk25/40",
                      "_sig": "contrarian", "invert": inv, "score": score,
                      "stop_bps": 25, "tp_bps": 40, "max_hold": 6, "cooldown": 2})
    return g


def run_user(symbols: list[str], tf: str, days: int, train_frac: float = 0.65):
    print(f"\n{'='*92}\nUSER ALGO BACKTEST (gigafade + contrarian-v8) | tf={tf} | {days}d"
          f" | cost={ROUND_TRIP_COST_BPS:.0f}bps | train={train_frac:.0%}\n{'='*92}")
    data = {}
    for sym in symbols:
        c = fetch_candles(sym, tf, days)
        if len(c) < 300:
            print(f"  {sym}: only {len(c)} candles — skipping"); continue
        s = Series(c); _SYM_TAG[id(s)] = sym; data[sym] = s
        print(f"  {sym}: {len(c)} candles, {(c[-1]['t']-c[0]['t'])/86400_000:.1f}d")

    agg = {}
    for cfg in user_grid():
        sig = SIGNALS[cfg["_sig"]]
        tr = {"n": 0, "net": 0.0, "dirw": 0.0}
        te = {"n": 0, "net": 0.0, "dirw": 0.0}
        for sym, s in data.items():
            split = int(s.n * train_frac)
            a = simulate(s, sig, cfg, 55, split)
            b = simulate(s, sig, cfg, split, s.n)
            tr["n"] += a.trades; tr["net"] += a.net_bps; tr["dirw"] += a.dir_wr * a.trades
            te["n"] += b.trades; te["net"] += b.net_bps; te["dirw"] += b.dir_wr * b.trades
        agg[cfg["_name"]] = {
            "ntr": tr["n"], "atr": tr["net"]/tr["n"] if tr["n"] else 0,
            "dtr": tr["dirw"]/tr["n"] if tr["n"] else 0,
            "nte": te["n"], "ate": te["net"]/te["n"] if te["n"] else 0,
            "dte": te["dirw"]/te["n"] if te["n"] else 0,
        }
    rows = [(k, v) for k, v in agg.items() if v["ntr"] >= 20 and v["nte"] >= 10]
    rows.sort(key=lambda kv: kv[1]["ate"], reverse=True)
    print(f"\n{'config':<24}{'TRAIN n/dir%/netbps':<26}{'TEST n/dir%/netbps':<26}verdict")
    print("-" * 92)
    for k, v in rows:
        ok = v["ate"] > 0 and v["atr"] > 0
        verdict = "OOS+ EDGE" if ok else ("train-only" if v["atr"] > 0 else "no")
        print(f"{k:<24}"
              f"{v['ntr']:>4} {v['dtr']*100:>3.0f}% {v['atr']:>+6.1f}        "
              f"{v['nte']:>4} {v['dte']*100:>3.0f}% {v['ate']:>+6.1f}        {verdict}")
    print("-" * 92)
    print("dir% = directional hit-rate (their metric). netbps = futures P&L/trade AFTER "
          f"{ROUND_TRIP_COST_BPS:.0f}bps cost.")
    pos = [k for k, v in rows if v["ate"] > 0 and v["atr"] > 0]
    print(f"\n{len(pos)} config(s) with out-of-sample futures edge: {pos[:6]}" if pos
          else "\nNO user algo shows out-of-sample futures edge after cost.")
    # also surface best directional accuracy regardless of futures PnL (Polymarket relevance)
    bydir = sorted(rows, key=lambda kv: kv[1]["dte"], reverse=True)[:3]
    print("Top directional accuracy (test):",
          ", ".join(f"{k} {v['dte']*100:.0f}%" for k, v in bydir))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="BTC,ETH,SOL,HYPE")
    ap.add_argument("--tf", default="5m")
    ap.add_argument("--days", type=int, default=20)
    ap.add_argument("--train", type=float, default=0.65)
    ap.add_argument("--mode", default="builtin", choices=["builtin", "user"])
    ap.add_argument("--cost", type=float, default=None,
                    help="round-trip cost bps override (taker~10, maker~3)")
    a = ap.parse_args()
    if a.cost is not None:
        ROUND_TRIP_COST_BPS = a.cost
    syms = [x.strip() for x in a.symbols.split(",")]
    if a.mode == "user":
        run_user(syms, a.tf, a.days, a.train)
    else:
        run(syms, a.tf, a.days, a.train)
