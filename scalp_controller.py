"""
Scalp controller — runs the loop, applies risk gates, routes paper vs live.

Owns:
  * background loop ticking each symbol
  * kill switches (max daily loss, max concurrent, per-symbol cooldown)
  * paper-trade ledger (scalp_paper_log.jsonl)
  * live execution (only if SCALP_LIVE=true AND a manually-flipped runtime flag)
  * open-position tracking + bracket exits (stop / take-profit)

server.py only does: construct ScalpController(info, exchange), call start(),
expose /scalp/status and /scalp/toggle.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

from scalp_agent import decide as _agent_decide
from scalp_engine import Evaluation, Plan, ScalpEngine

FEED_MAX = 200
EQUITY_MAX = 720       # ~1h at 5s ticks
EQUITY_SAMPLE_S = 30   # one point every 30s
AI_EXIT_INTERVAL_S = 20  # how often the agent re-judges an open position

logger = logging.getLogger(__name__)

SCALP_ENABLED = os.getenv("SCALP_ENABLED", "true").lower() == "true"
SCALP_LIVE_ENV = os.getenv("SCALP_LIVE", "false").lower() == "true"
SCALP_SYMBOLS = [
    s.strip() for s in os.getenv("SCALP_SYMBOLS", "BTC,ETH,SOL,HYPE").split(",")
    if s.strip()
]
SCALP_CAPITAL_USD = float(os.getenv("SCALP_CAPITAL_USD", "200"))
SCALP_MAX_CONCURRENT = int(os.getenv("SCALP_MAX_CONCURRENT", "2"))
SCALP_MAX_DAILY_LOSS = float(os.getenv("SCALP_MAX_DAILY_LOSS", "20"))
SCALP_COOLDOWN_S = float(os.getenv("SCALP_COOLDOWN_S", "180"))
SCALP_TICK_S = float(os.getenv("SCALP_TICK_S", "5"))
SCALP_USE_AGENT = os.getenv("SCALP_USE_AGENT", "true").lower() == "true"
# Desired leverage cap. Per-asset HL max (verified 2026-06-06: BTC 40, ETH 25,
# SOL 20, HYPE 10) is fetched from info.meta() and clamps this at entry time.
# At default 5x the clamp is inactive on all four tracked symbols; bump the env
# if you want to ride closer to each asset's cap.
SCALP_LEVERAGE = int(os.getenv("SCALP_LEVERAGE", "5"))
# Fallback when a symbol is missing from info.meta() (shouldn't happen for perps
# in SCALP_SYMBOLS, but defend against universe drift).
FALLBACK_MAX_LEV = 3
# When true, leverage scales the TRADED NOTIONAL (and therefore PnL / win-loss $):
# notional = margin * leverage. margin = CAPITAL * size_pct is the capital committed.
# When false, leverage only sets exchange margin/liquidation and notional == margin
# (PnL unchanged). The daily-loss cap is scaled by the same multiple so trade
# frequency is preserved (see _risk_ok). Reversible without code edits.
SCALP_LEV_SCALES_SIZE = os.getenv("SCALP_LEV_SCALES_SIZE", "true").lower() == "true"
# Watchlist scanner: AI scouts forming setups on non-positioned symbols, one
# bounded call every SCALP_WATCHLIST_INTERVAL_S (RPM-cheap). Off by default — it
# adds API calls. Writes scalp_watchlist.json + surfaces in /scalp/status.
SCALP_WATCHLIST = os.getenv("SCALP_WATCHLIST", "false").lower() == "true"
SCALP_WATCHLIST_INTERVAL_S = float(os.getenv("SCALP_WATCHLIST_INTERVAL_S", "90"))

PAPER_LOG = Path(__file__).parent / "scalp_paper_log.jsonl"
STATE_FILE = Path(__file__).parent / "scalp_state.json"
POS_FILE = Path(__file__).parent / "scalp_open_pos.json"
WATCHLIST_FILE = Path(__file__).parent / "scalp_watchlist.json"
# Written by scalp_monitor.py. The controller reads it (cheap, mtime-cached) and
# honors it in the DE-RISK direction only: {"paused": bool, "leverage_cap": int,
# "day": "YYYY-MM-DD", "reason": str}. A governor with a stale "day" is ignored
# (auto-resets each day, matching the engine's daily PnL roll).
GOV_FILE = Path(__file__).parent / "scalp_governor.json"


@dataclass
class OpenPos:
    symbol: str
    side: str
    entry: float
    size_usd: float
    size_coin: float
    stop_px: float
    tp_px: float
    opened_at: float
    paper: bool
    oid: Optional[int] = None
    exit_after_ts: float = 0.0   # >0 => time-exit at this epoch (GIGAFADE)
    last_ai_exit_check: float = 0.0
    leverage: int = 1            # exchange leverage applied (1 = unlevered fallback)
    margin_usd: float = 0.0      # capital committed (notional / leverage)
    signal: str = ""             # signal that opened this: "gigafade" | "momentum"
    ai_gated: bool = False       # True if the LLM judged this entry (vs rules auto-approve)
    model: str = ""              # "provider/model" that judged the entry (when ai_gated)


@dataclass
class ControllerStats:
    realized_pnl_today: float = 0.0
    trades_today: int = 0
    last_reset_ymd: str = field(default_factory=lambda: time.strftime("%Y-%m-%d"))


class ScalpController:
    def __init__(self, info, exchange):
        self.info = info
        self.exchange = exchange
        self.engine = ScalpEngine(info, SCALP_SYMBOLS)
        self.open_pos: dict[str, OpenPos] = {}
        self.stats = ControllerStats()
        self.live_runtime = False  # operator must flip via /scalp/toggle
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self.feed: deque = deque(maxlen=FEED_MAX)
        self.equity: deque = deque(maxlen=EQUITY_MAX)
        self._last_equity_ts: float = 0.0
        self._last_watchlist_ts: float = 0.0
        self._watchlist: dict = {"items": [], "ts": 0.0}
        self._max_lev: dict[str, int] = self._load_max_leverage()
        self._applied_lev: dict[str, int] = {}
        self._gov: dict = {}
        self._gov_mt: Optional[float] = None
        self._load_state()
        self._load_positions()

    # --- governor (de-risk only; written by scalp_monitor.py) --------------
    def _read_governor(self) -> dict:
        """Cheap mtime-cached read of scalp_governor.json. A governor whose
        'day' is not today is ignored (auto-reset). Returns {} if absent/bad."""
        try:
            mt = GOV_FILE.stat().st_mtime
        except OSError:
            self._gov, self._gov_mt = {}, None
            return {}
        if mt != self._gov_mt:
            try:
                self._gov = json.loads(GOV_FILE.read_text())
            except (OSError, json.JSONDecodeError):
                self._gov = {}
            self._gov_mt = mt
        if self._gov.get("day") and self._gov["day"] != time.strftime("%Y-%m-%d"):
            return {}
        return self._gov

    def _governor_paused(self) -> bool:
        return bool(self._read_governor().get("paused"))

    def _governor_cap(self) -> Optional[int]:
        v = self._read_governor().get("leverage_cap")
        try:
            return int(v) if v else None
        except (TypeError, ValueError):
            return None

    # --- leverage ----------------------------------------------------------
    def _load_max_leverage(self) -> dict[str, int]:
        """Cache per-asset maxLeverage from HL universe at startup.
        Some assets cap lower than others (e.g. HYPE 3x vs BTC 40x)."""
        try:
            meta = self.info.meta()
            out = {a["name"]: int(a.get("maxLeverage", FALLBACK_MAX_LEV))
                   for a in meta.get("universe", [])}
            logger.info("scalp: maxLev loaded for %d assets; tracked=%s",
                        len(out), {s: out.get(s) for s in SCALP_SYMBOLS})
            return out
        except Exception as exc:
            logger.warning("scalp: meta() failed, using fallback maxLev=%d: %s",
                           FALLBACK_MAX_LEV, exc)
            return {}

    def _effective_leverage(self, symbol: str) -> int:
        cap = self._max_lev.get(symbol, FALLBACK_MAX_LEV)
        target = SCALP_LEVERAGE
        gov_cap = self._governor_cap()      # de-risk: monitor may lower, never raise
        if gov_cap is not None:
            target = min(target, gov_cap)
        return max(1, min(target, cap))

    def _effective_daily_loss_cap(self) -> float:
        """Scale the daily-loss cap by the leverage multiple when leverage scales
        size, so the cap halts after the same NUMBER of losing trades as the
        unleveraged engine (bigger $ swings, same trade frequency)."""
        mult = SCALP_LEVERAGE if SCALP_LEV_SCALES_SIZE else 1
        return abs(SCALP_MAX_DAILY_LOSS) * max(1, mult)

    # --- persistence -------------------------------------------------------
    def _load_state(self) -> None:
        if not STATE_FILE.exists():
            return
        try:
            data = json.loads(STATE_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            return
        if data.get("ymd") == self.stats.last_reset_ymd:
            self.stats.realized_pnl_today = float(data.get("pnl", 0.0))
            self.stats.trades_today = int(data.get("trades", 0))

    def _save_state(self) -> None:
        try:
            STATE_FILE.write_text(json.dumps({
                "ymd": self.stats.last_reset_ymd,
                "pnl": self.stats.realized_pnl_today,
                "trades": self.stats.trades_today,
            }))
        except OSError as exc:
            logger.warning("scalp: state save failed: %s", exc)

    def _load_positions(self) -> None:
        """Reload open positions so a restart doesn't orphan them (which would
        leave entries with no matching exit and corrupt PnL)."""
        if not POS_FILE.exists():
            return
        try:
            data = json.loads(POS_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            return
        for sym, p in data.items():
            try:
                self.open_pos[sym] = OpenPos(**p)
            except TypeError:
                continue
        if self.open_pos:
            logger.info("scalp: reloaded %d open position(s): %s",
                        len(self.open_pos), list(self.open_pos.keys()))

    def _save_positions(self) -> None:
        try:
            POS_FILE.write_text(json.dumps(
                {sym: asdict(p) for sym, p in self.open_pos.items()}))
        except OSError as exc:
            logger.warning("scalp: positions save failed: %s", exc)

    def _log_paper(self, kind: str, body: dict) -> None:
        line = json.dumps({"ts": time.time(), "kind": kind, **body})
        try:
            with PAPER_LOG.open("a") as f:
                f.write(line + "\n")
        except OSError as exc:
            logger.warning("scalp: paper-log write failed: %s", exc)

    # --- risk gates --------------------------------------------------------
    def _roll_day(self) -> None:
        ymd = time.strftime("%Y-%m-%d")
        if ymd != self.stats.last_reset_ymd:
            self.stats = ControllerStats(last_reset_ymd=ymd)
            self._save_state()

    def _risk_ok(self) -> tuple[bool, str]:
        self._roll_day()
        if self._governor_paused():
            return False, f"governor paused ({self._read_governor().get('reason', 'de-risk')})"
        cap = self._effective_daily_loss_cap()
        if self.stats.realized_pnl_today <= -cap:
            return False, f"daily-loss cap hit ({self.stats.realized_pnl_today:.2f} <= -{cap:.0f})"
        if len(self.open_pos) >= SCALP_MAX_CONCURRENT:
            return False, f"max-concurrent ({len(self.open_pos)})"
        return True, "ok"

    def _is_live(self) -> bool:
        return SCALP_LIVE_ENV and self.live_runtime

    # --- entry / exit ------------------------------------------------------
    def _enter(self, plan: Plan) -> None:
        if plan.symbol in self.open_pos:
            return
        # margin = capital committed; notional = margin * leverage (drives PnL).
        margin_usd = SCALP_CAPITAL_USD * plan.size_pct
        if margin_usd < 5.0:
            return
        lev = self._effective_leverage(plan.symbol)
        size_usd = margin_usd * lev if SCALP_LEV_SCALES_SIZE else margin_usd
        size_coin = size_usd / plan.mid
        stop_px = plan.mid * (1 - plan.stop_bps / 1e4) if plan.side == "BUY" \
            else plan.mid * (1 + plan.stop_bps / 1e4)
        # tp_bps==0 => no fixed TP (time/AI-exit mode). Use 0.0 sentinel.
        if plan.tp_bps > 0:
            tp_px = plan.mid * (1 + plan.tp_bps / 1e4) if plan.side == "BUY" \
                else plan.mid * (1 - plan.tp_bps / 1e4)
        else:
            tp_px = 0.0
        exit_after = (time.time() + plan.hold_secs) if plan.hold_secs > 0 else 0.0
        paper = not self._is_live()
        oid = None
        if not paper:
            # Set leverage before opening. Only re-sign when it changes — leverage
            # persists on the account once set. is_cross=True keeps cross-margin
            # (this account also runs HERMES etc.); flip to False here for
            # isolated margin if scalp gets its own subaccount later.
            if self._applied_lev.get(plan.symbol) != lev:
                try:
                    self.exchange.update_leverage(lev, plan.symbol, is_cross=True)
                    self._applied_lev[plan.symbol] = lev
                    logger.info("scalp: update_leverage %s -> %dx (HL max=%d)",
                                plan.symbol, lev, self._max_lev.get(plan.symbol, -1))
                except Exception as exc:
                    logger.error("scalp: update_leverage failed for %s: %s",
                                 plan.symbol, exc)
                    return
            try:
                result = self.exchange.market_open(
                    plan.symbol, is_buy=(plan.side == "BUY"),
                    sz=round(size_coin, 6), px=None,
                )
                oid = (result.get("response", {}).get("data", {})
                       .get("statuses", [{}])[0].get("filled", {}).get("oid"))
                logger.info("scalp: LIVE entry %s %s sz=%.6f lev=%dx result=%s",
                            plan.symbol, plan.side, size_coin, lev, result)
            except Exception as exc:
                logger.error("scalp: live entry failed for %s: %s", plan.symbol, exc)
                return
        pos = OpenPos(plan.symbol, plan.side, plan.mid, size_usd, size_coin,
                      stop_px, tp_px, time.time(), paper, oid,
                      exit_after_ts=exit_after, leverage=lev, margin_usd=margin_usd,
                      signal=plan.signal, ai_gated=plan.ai_gated, model=plan.model)
        with self._lock:
            self.open_pos[plan.symbol] = pos
        self._save_positions()
        logger.info("scalp: entry [%s] %s %s @%.6f notional=$%.2f (margin=$%.2f x %dx, HL max=%dx)",
                    "PAPER" if paper else "LIVE", plan.symbol, plan.side,
                    plan.mid, size_usd, margin_usd, lev, self._max_lev.get(plan.symbol, -1))
        self._log_paper("entry", {
            "paper": paper, "symbol": plan.symbol, "side": plan.side,
            "signal": plan.signal, "ai_gated": plan.ai_gated, "model": plan.model,
            "ai_latency_ms": plan.ai_latency_ms,
            "entry": plan.mid, "size_usd": size_usd, "margin_usd": margin_usd,
            "stop": stop_px, "tp": tp_px,
            "leverage": lev, "max_leverage": self._max_lev.get(plan.symbol, -1),
            "hold_secs": plan.hold_secs, "reason": plan.reason, "features": plan.features,
        })

    def _exit(self, pos: OpenPos, exit_px: float, why: str) -> None:
        direction = 1 if pos.side == "BUY" else -1
        pnl_usd = (exit_px - pos.entry) * direction * pos.size_coin
        if not pos.paper:
            try:
                self.exchange.market_open(
                    pos.symbol, is_buy=(pos.side != "BUY"),
                    sz=round(pos.size_coin, 6), px=None,
                )
            except Exception as exc:
                logger.error("scalp: live exit failed for %s: %s", pos.symbol, exc)
        with self._lock:
            self.open_pos.pop(pos.symbol, None)
            self.stats.realized_pnl_today += pnl_usd
            self.stats.trades_today += 1
        self._save_state()
        self._save_positions()
        self.engine.set_cooldown(pos.symbol, SCALP_COOLDOWN_S)
        self._log_paper("exit", {
            "paper": pos.paper, "symbol": pos.symbol, "side": pos.side,
            "signal": pos.signal, "ai_gated": pos.ai_gated, "model": pos.model,
            "entry": pos.entry, "exit": exit_px, "pnl_usd": round(pnl_usd, 4),
            "size_usd": pos.size_usd, "margin_usd": pos.margin_usd,
            "leverage": pos.leverage,
            "held_s": round(time.time() - pos.opened_at, 1), "why": why,
        })
        logger.info("scalp: exit %s %s lev=%dx pnl=%.3f why=%s",
                    pos.symbol, pos.side, pos.leverage, pnl_usd, why)

    def _manage_open(self) -> None:
        if not self.open_pos:
            return
        try:
            mids = self.info.all_mids()
        except Exception as exc:
            logger.warning("scalp: all_mids failed: %s", exc)
            return
        now = time.time()
        for symbol, pos in list(self.open_pos.items()):
            px = float(mids.get(symbol, 0))
            if px <= 0:
                continue
            long = pos.side == "BUY"
            # 1) hard stop backstop (always)
            if (long and px <= pos.stop_px) or (not long and px >= pos.stop_px):
                self._exit(pos, px, "stop"); continue
            # 2) fixed TP (only if set; GIGAFADE uses tp_px==0 = none)
            if pos.tp_px > 0:
                if (long and px >= pos.tp_px) or (not long and px <= pos.tp_px):
                    self._exit(pos, px, "tp"); continue
            # 3) time-exit (GIGAFADE horizon)
            if pos.exit_after_ts > 0 and now >= pos.exit_after_ts:
                self._exit(pos, px, "time"); continue
            # 4) AI-judged early exit — ALL signals (2026-06-19). The hard stop (1)
            # and time-exit (3) above remain the floors; the LLM can only close
            # EARLY, never disable them. gigafade gets categorical labels; momentum
            # gets raw numbers (see _ai_wants_exit). Re-judged every AI_EXIT_INTERVAL_S.
            if (SCALP_USE_AGENT
                    and (now - pos.last_ai_exit_check) >= AI_EXIT_INTERVAL_S):
                pos.last_ai_exit_check = now
                if self._ai_wants_exit(pos, px):
                    self._exit(pos, px, "ai"); continue

    def _ai_wants_exit(self, pos: OpenPos, px: float) -> bool:
        """Ask the LLM if we should close early. Fail-open: any error => hold
        (time/stop backstops still protect us). gigafade is judged on categorical
        labels (time-managed fade); momentum on raw numbers + current book."""
        try:
            from scalp_agent import decide_exit
            direction = 1 if pos.side == "BUY" else -1
            upnl_bps = (px - pos.entry) / pos.entry * 1e4 * direction
            now = time.time()
            held_s = now - pos.opened_at
            if pos.signal == "momentum":
                # Raw view + current book imbalance (cheap single L2 fetch) so the
                # model can see if the book has flipped against the position.
                imb = 0.0
                try:
                    from scalp_engine import _ob_imbalance
                    book = self.engine._book(pos.symbol)
                    if book:
                        _, _, imb = _ob_imbalance(book)
                except Exception:
                    pass
                view = {
                    "symbol": pos.symbol, "signal": "momentum", "side": pos.side,
                    "upnl_bps": round(upnl_bps, 1), "held_s": round(held_s, 0),
                    "ob_imbalance_top5": round(imb, 3),
                }
            else:
                # Categorical labels, not raw bps (gigafade view, keeps prior behavior).
                if upnl_bps >= 40:    pnl_state = "up_big"
                elif upnl_bps >= 10:  pnl_state = "up_small"
                elif upnl_bps <= -40: pnl_state = "down_big"
                elif upnl_bps <= -10: pnl_state = "down_small"
                else:                 pnl_state = "flat"
                horizon = (pos.exit_after_ts - pos.opened_at) if pos.exit_after_ts > 0 else 0.0
                frac = (held_s / horizon) if horizon > 0 else 1.0
                time_held = "late" if frac >= 0.7 else ("mid" if frac >= 0.4 else "early")
                view = {
                    "symbol": pos.symbol, "signal": pos.signal, "side": pos.side,
                    "pnl_state": pnl_state, "time_held": time_held,
                }
            d = decide_exit(view)
            if d.get("exit"):
                logger.info("scalp: AI early-exit %s via %s (%s)",
                            pos.symbol, d.get("model", "?"), d.get("reason", ""))
                return True
        except Exception as exc:
            logger.debug("scalp: ai exit check failed (holding): %s", exc)
        return False

    # --- loop --------------------------------------------------------------
    def _record_eval(self, ev: Evaluation) -> None:
        self.feed.append({
            "ts": ev.ts,
            "symbol": ev.symbol,
            "stage": ev.stage,
            "mid": ev.mid,
            "gate_reason": ev.gate_reason,
            "snapshot": ev.snapshot,
            "decision_reason": ev.decision_reason,
            "side": ev.plan.side if ev.plan else "",
            "signal": ev.plan.signal if ev.plan else "",
            "size_pct": ev.plan.size_pct if ev.plan else 0.0,
            "size_usd": round(
                SCALP_CAPITAL_USD * (ev.plan.size_pct if ev.plan else 0.0)
                * (self._effective_leverage(ev.symbol) if SCALP_LEV_SCALES_SIZE else 1), 2),
            "stop_bps": ev.plan.stop_bps if ev.plan else 0.0,
            "tp_bps": ev.plan.tp_bps if ev.plan else 0.0,
            "leverage": self._effective_leverage(ev.symbol),
        })

    def _record_equity(self) -> None:
        now = time.time()
        if now - self._last_equity_ts < EQUITY_SAMPLE_S:
            return
        self._last_equity_ts = now
        # Realized + mark-to-market unrealized on open paper positions
        unreal = 0.0
        if self.open_pos:
            try:
                mids = self.info.all_mids()
            except Exception:
                mids = {}
            for pos in self.open_pos.values():
                px = float(mids.get(pos.symbol, pos.entry))
                direction = 1 if pos.side == "BUY" else -1
                unreal += (px - pos.entry) * direction * pos.size_coin
        self.equity.append({
            "ts": now,
            "realized": round(self.stats.realized_pnl_today, 4),
            "unrealized": round(unreal, 4),
            "total": round(self.stats.realized_pnl_today + unreal, 4),
        })

    def _maybe_watchlist(self) -> None:
        """Every SCALP_WATCHLIST_INTERVAL_S, ask the AI (one bounded call) to rank
        forming setups across symbols NOT currently in a position. Advisory only —
        it does not enter; it surfaces in /scalp/status + scalp_watchlist.json."""
        if not (SCALP_WATCHLIST and SCALP_USE_AGENT):
            return
        now = time.time()
        if now - self._last_watchlist_ts < SCALP_WATCHLIST_INTERVAL_S:
            return
        self._last_watchlist_ts = now
        snaps = []
        for sym in SCALP_SYMBOLS:
            if sym in self.open_pos:
                continue
            snap = self.engine.microstructure_snapshot(sym)
            if snap:
                snaps.append(snap)
        if not snaps:
            return
        try:
            from scalp_agent import rank_watchlist
            res = rank_watchlist(snaps)
        except Exception as exc:
            logger.debug("scalp: watchlist scan failed: %s", exc)
            return
        self._watchlist = {"items": res.get("items", []),
                           "model": res.get("model", ""), "ts": now}
        try:
            WATCHLIST_FILE.write_text(json.dumps(self._watchlist), encoding="utf-8")
        except OSError:
            pass
        if res.get("items"):
            top = res["items"][0]
            logger.info("scalp: watchlist top = %s %s score=%.2f (%s)",
                        top.get("symbol"), top.get("side"), top.get("score", 0),
                        res.get("model", "?"))

    def _tick(self) -> None:
        self._manage_open()
        self._record_equity()
        self._maybe_watchlist()
        ok, why = self._risk_ok()
        for sym in SCALP_SYMBOLS:
            if sym in self.open_pos:
                continue
            ev = self.engine.evaluate(sym, use_agent=SCALP_USE_AGENT)
            self._record_eval(ev)
            if not ok:
                continue
            # Re-check the concurrency cap each iteration. The `ok` snapshot is
            # taken once at tick start; without this, a single tick could open
            # several positions and breach max_concurrent (observed: 3 open at
            # cap=2). Daily-loss is unaffected within a tick (no PnL realized).
            if len(self.open_pos) >= SCALP_MAX_CONCURRENT:
                continue
            if ev.plan and ev.plan.enter:
                self._enter(ev.plan)

    def _loop(self) -> None:
        logger.info("scalp: loop started (symbols=%s, live_env=%s, agent=%s)",
                    SCALP_SYMBOLS, SCALP_LIVE_ENV, SCALP_USE_AGENT)
        while self.running:
            try:
                self._tick()
            except Exception:
                logger.exception("scalp: tick error")
            time.sleep(SCALP_TICK_S)

    def _warm_agent(self) -> None:
        """One probe call at startup to confirm a provider answers and (if Ollama
        is the only backstop) to load the local model into memory. Hosted APIs need
        no warm-up; the router's own timeout governs the call."""
        import scalp_agent as _sa
        try:
            ok, msg = _sa.health_check()
            logger.info("scalp: llm health: %s (%s)", ok, msg)
            _agent_decide({
                "symbol": "WARMUP", "signal": "gigafade", "side_hint": "SELL",
                "spread": "tight", "candle": "green",
                "candle_extremity": "marginal", "rsi_state": "not_extreme",
                "book_pressure_vs_fade": "neutral", "thesis": "warmup",
            })
            logger.info("scalp: agent warm-up done")
        except Exception:
            logger.exception("scalp: agent warm-up failed")

    def start(self) -> None:
        if not SCALP_ENABLED:
            logger.info("scalp: disabled via SCALP_ENABLED env")
            return
        if self.running:
            return
        self.running = True
        if SCALP_USE_AGENT:
            threading.Thread(target=self._warm_agent, name="scalp-warmup",
                             daemon=True).start()
        self._thread = threading.Thread(target=self._loop, name="scalp-loop",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.running = False

    # --- status / control --------------------------------------------------
    def status(self) -> dict:
        self._roll_day()
        return {
            "enabled": SCALP_ENABLED,
            "running": self.running,
            "live_env": SCALP_LIVE_ENV,
            "live_runtime": self.live_runtime,
            "is_live": self._is_live(),
            "use_agent": SCALP_USE_AGENT,
            "symbols": SCALP_SYMBOLS,
            "capital_usd": SCALP_CAPITAL_USD,
            "max_concurrent": SCALP_MAX_CONCURRENT,
            "max_daily_loss": SCALP_MAX_DAILY_LOSS,
            "leverage_target": SCALP_LEVERAGE,
            "leverage_scales_size": SCALP_LEV_SCALES_SIZE,
            "max_daily_loss_effective": self._effective_daily_loss_cap(),
            "leverage_effective": {s: self._effective_leverage(s) for s in SCALP_SYMBOLS},
            "leverage_hl_max": {s: self._max_lev.get(s, FALLBACK_MAX_LEV) for s in SCALP_SYMBOLS},
            "leverage_applied": dict(self._applied_lev),
            "governor": self._read_governor(),
            "open_positions": {k: asdict(v) for k, v in self.open_pos.items()},
            "trades_today": self.stats.trades_today,
            "realized_pnl_today": round(self.stats.realized_pnl_today, 3),
            "last_reset_ymd": self.stats.last_reset_ymd,
            "watchlist": self._watchlist,
            "config": self._config_snapshot(),
        }

    def _config_snapshot(self) -> dict:
        """Engine/agent/risk parameters for the system panel."""
        import scalp_engine as _se
        import llm_router as _lr
        lr = _lr.status()
        return {
            "llm_providers": lr["enabled"],
            "llm_provider_count": lr["count"],
            "model": ", ".join(p["name"] for p in lr["enabled"]) or "(none)",
            "agent_timeout_s": lr["timeout_s"],
            "momentum_ai": _se.SCALP_MOMENTUM_AI,
            "watchlist_on": SCALP_WATCHLIST,
            "tick_s": SCALP_TICK_S,
            "cooldown_s": SCALP_COOLDOWN_S,
            "capital_usd": SCALP_CAPITAL_USD,
            "max_concurrent": SCALP_MAX_CONCURRENT,
            "max_daily_loss": SCALP_MAX_DAILY_LOSS,
            "max_daily_loss_effective": self._effective_daily_loss_cap(),
            "leverage_target": SCALP_LEVERAGE,
            "leverage_scales_size": SCALP_LEV_SCALES_SIZE,
            "vol_ceiling": _se.MAX_REALIZED_VOL_PCT,
            "min_ret_z": _se.MIN_RET_Z,
            "fade_mode": _se.SCALP_FADE,
            "stop_widen": _se.STOP_WIDEN,
            "stop_atr_mult": _se.STOP_ATR_MULT,
            "signal": _se.SCALP_SIGNAL,
            "giga_tf": _se.GIGA_TF,
            "giga_pct": _se.GIGA_PCT,
            "giga_rsi": f"{_se.GIGA_RSI_OB:.0f}/{_se.GIGA_RSI_OS:.0f}",
            "giga_hold_candles": _se.GIGA_HOLD_CANDLES,
            "max_spread_bps": _se.MAX_SPREAD_BPS,
            "candle_interval": _se.CANDLE_INTERVAL,
        }

    def toggle_live(self, on: bool) -> dict:
        """Operator-controlled live flip. Still requires SCALP_LIVE=true in env."""
        self.live_runtime = bool(on)
        logger.warning("scalp: live_runtime flipped to %s (env_allows=%s)",
                       self.live_runtime, SCALP_LIVE_ENV)
        return {"live_runtime": self.live_runtime, "env_allows": SCALP_LIVE_ENV,
                "is_live": self._is_live()}

    def get_feed(self, limit: int = 100, stage: Optional[str] = None) -> list:
        items = list(self.feed)[-limit:][::-1]
        if stage:
            items = [i for i in items if i["stage"] == stage]
        return items

    def get_equity(self) -> list:
        return list(self.equity)

    def force_exit_all(self, reason: str = "operator") -> dict:
        try:
            mids = self.info.all_mids()
        except Exception:
            mids = {}
        closed = []
        for sym, pos in list(self.open_pos.items()):
            px = float(mids.get(sym, pos.entry))
            self._exit(pos, px, f"force:{reason}")
            closed.append(sym)
        return {"closed": closed}
