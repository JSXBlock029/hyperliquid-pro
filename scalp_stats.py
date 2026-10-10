"""
Read scalp_paper_log.jsonl and aggregate closed trades into PnL stats.

The controller appends one line per event:
  {"ts":..., "kind":"entry"|"exit", ...}

Exit lines already carry realized pnl_usd, entry, exit, held_s, why, symbol,
side, paper. So "closed trades" = every exit line. We don't need to pair with
entries (exit is self-contained) but we expose entries count for context.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path

LOG = Path(__file__).parent / "scalp_paper_log.jsonl"


def _read_exits() -> list[dict]:
    if not LOG.exists():
        return []
    exits = []
    with LOG.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("kind") == "exit":
                exits.append(rec)
    return exits


def _bucket_stats(rows: list[dict]) -> dict:
    n = len(rows)
    if n == 0:
        return {"trades": 0, "pnl": 0.0, "wins": 0, "win_rate": 0.0,
                "avg_pnl": 0.0, "best": 0.0, "worst": 0.0}
    pnls = [float(r.get("pnl_usd", 0.0)) for r in rows]
    wins = sum(1 for p in pnls if p > 0)
    total = sum(pnls)
    return {
        "trades": n,
        "pnl": round(total, 4),
        "wins": wins,
        "win_rate": round(wins / n, 4),
        "avg_pnl": round(total / n, 4),
        "best": round(max(pnls), 4),
        "worst": round(min(pnls), 4),
    }


def get_trades(limit: int = 200) -> list[dict]:
    """Most-recent-first list of closed trades for the table."""
    exits = _read_exits()
    out = []
    for r in exits[-limit:][::-1]:
        out.append({
            "ts": r.get("ts"),
            "symbol": r.get("symbol"),
            "side": r.get("side"),
            "signal": r.get("signal"),
            "entry": r.get("entry"),
            "exit": r.get("exit"),
            "pnl_usd": round(float(r.get("pnl_usd", 0.0)), 4),
            "size_usd": r.get("size_usd"),       # traded notional (None on pre-leverage rows)
            "margin_usd": r.get("margin_usd"),   # capital committed
            "leverage": r.get("leverage"),       # exchange leverage used
            "held_s": r.get("held_s"),
            "why": r.get("why"),
            "paper": r.get("paper", True),
        })
    return out


def get_markers(symbol: str) -> dict:
    """Entry/exit events for one symbol, for chart overlays."""
    if not LOG.exists():
        return {"entries": [], "exits": []}
    entries, exits = [], []
    with LOG.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("symbol") != symbol:
                continue
            if rec.get("kind") == "entry":
                entries.append({
                    "ts": rec.get("ts"),
                    "px": rec.get("entry"),
                    "side": rec.get("side"),
                    "stop": rec.get("stop"),
                    "tp": rec.get("tp"),
                    "reason": rec.get("reason", ""),
                })
            elif rec.get("kind") == "exit":
                exits.append({
                    "ts": rec.get("ts"),
                    "px": rec.get("exit"),
                    "side": rec.get("side"),
                    "pnl_usd": round(float(rec.get("pnl_usd", 0.0)), 4),
                    "why": rec.get("why", ""),
                })
    return {"entries": entries, "exits": exits}


def get_stats() -> dict:
    """Overall + per-hour + per-day + per-symbol aggregates."""
    exits = _read_exits()

    by_hour: dict[str, list] = defaultdict(list)   # "YYYY-MM-DD HH:00"
    by_day: dict[str, list] = defaultdict(list)    # "YYYY-MM-DD"
    by_symbol: dict[str, list] = defaultdict(list)
    by_paper: dict[bool, list] = defaultdict(list)

    for r in exits:
        ts = float(r.get("ts", 0))
        lt = time.localtime(ts)
        by_hour[time.strftime("%Y-%m-%d %H:00", lt)].append(r)
        by_day[time.strftime("%Y-%m-%d", lt)].append(r)
        by_symbol[r.get("symbol", "?")].append(r)
        by_paper[bool(r.get("paper", True))].append(r)

    # last 24 hourly buckets (chronological)
    hourly = [
        {"bucket": k, **_bucket_stats(v)}
        for k, v in sorted(by_hour.items())
    ][-24:]
    daily = [
        {"bucket": k, **_bucket_stats(v)}
        for k, v in sorted(by_day.items())
    ][-30:]
    per_symbol = {
        sym: _bucket_stats(v) for sym, v in sorted(by_symbol.items())
    }

    return {
        "overall": _bucket_stats(exits),
        "paper": _bucket_stats(by_paper.get(True, [])),
        "live": _bucket_stats(by_paper.get(False, [])),
        "hourly": hourly,
        "daily": daily,
        "per_symbol": per_symbol,
    }
