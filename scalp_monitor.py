"""
scalp_monitor.py — periodic health + de-risk governor + edge report for the
HLPRO2 scalp engine.

The scalp engine is a PAPER strategy with (per backtest) no proven directional
edge. This monitor therefore NEVER raises risk. It only:

  1. LIVENESS  — GET /scalp/status; is the engine running, is the paper log
                 fresh, has the daily-loss cap fired.
  2. EDGE      — read scalp_paper_log.jsonl; stake-normalized edge
                 (PnL / notional, in bps) per signal / symbol / hour over a
                 rolling window. Written to scalp_edge_report.{json,md}.
  3. GOVERN    — DE-RISK ONLY. On daily drawdown it lowers the leverage cap, then
                 pauses new entries, by writing scalp_governor.json (which the
                 controller honors). It never raises leverage and never un-pauses
                 within a day — the governor auto-resets daily. Within a day it is
                 monotonic toward *more* restrictive (no flapping).
  4. JOURNAL   — append every action + a one-line summary to scalp_monitor_log.jsonl.

Return-OPTIMIZATION (raising leverage, switching signal, changing gates) is
PROPOSE-ONLY: surfaced in the report's "proposals" section, never auto-applied.
That stays a human decision until a labeled, out-of-sample per-signal edge exists.

Run one cycle (for schtasks):   python scalp_monitor.py
Run continuously:               python scalp_monitor.py --loop --interval 300
Report only, touch nothing:     python scalp_monitor.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent
PAPER_LOG = HERE / "scalp_paper_log.jsonl"
GOV_FILE = HERE / "scalp_governor.json"
REPORT_JSON = HERE / "scalp_edge_report.json"
REPORT_MD = HERE / "scalp_edge_report.md"
JOURNAL = HERE / "scalp_monitor_log.jsonl"

BASE_URL = "http://localhost:3000"
WINDOW_TRADES = 300        # rolling window for the edge report
LIVENESS_STALE_S = 900     # no new closed trade in 15 min (while running) = stale

# De-risk guardrails, as fractions of the *effective* daily-loss cap (which the
# engine already scales by leverage). Drawdown is measured by realized_pnl_today.
DD_SOFT_FRAC = 0.50        # >= 50% of cap lost -> halve the leverage cap
DD_HARD_FRAC = 0.80        # >= 80% of cap lost -> pause new entries for the day
RB_MIN_N = 20              # min priced rows before a ret_bps figure is shown/trusted


# ----------------------------------------------------------------------------- io
def _today() -> str:
    return time.strftime("%Y-%m-%d")


def fetch_status() -> dict | None:
    try:
        with urllib.request.urlopen(f"{BASE_URL}/scalp/status", timeout=8) as r:
            return json.loads(r.read())
    except Exception:
        return None


def read_trades(window: int) -> list[dict]:
    """Most-recent `window` closed trades (exit records) from the paper log."""
    if not PAPER_LOG.exists():
        return []
    exits = []
    with PAPER_LOG.open("r", encoding="utf-8") as f:
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
    return exits[-window:]


# ------------------------------------------------------------------------- edge
def _seg(rows: list[dict]) -> dict:
    n = len(rows)
    if not n:
        return {"n": 0}
    pnl = sum(float(r.get("pnl_usd", 0) or 0) for r in rows)
    wins = sum(1 for r in rows if float(r.get("pnl_usd", 0) or 0) > 0)
    # stake-normalized edge: total PnL / total notional, in bps. Removes both
    # stake and leverage scaling -> the raw per-$-exposed edge. Only rows that
    # carry size_usd (post-leverage era) contribute to the bps figure.
    notion = sum(float(r.get("size_usd", 0) or 0) for r in rows if r.get("size_usd"))
    pnl_n = sum(float(r.get("pnl_usd", 0) or 0) for r in rows if r.get("size_usd"))
    ret_bps = (pnl_n / notion * 1e4) if notion else None
    why = defaultdict(int)
    for r in rows:
        why[r.get("why", "?")] += 1
    return {
        "n": n,
        "wr": round(wins / n, 3),
        "pnl": round(pnl, 2),
        "avg_pnl": round(pnl / n, 4),
        "ret_bps": round(ret_bps, 1) if ret_bps is not None else None,
        "n_priced": sum(1 for r in rows if r.get("size_usd")),
        "stops": why.get("stop", 0), "tps": why.get("tp", 0), "time": why.get("time", 0),
    }


def compute_edge(trades: list[dict]) -> dict:
    by_signal = defaultdict(list)
    by_symbol = defaultdict(list)
    by_sig_sym = defaultdict(list)
    by_hour = defaultdict(list)
    by_ai = defaultdict(list)
    by_model = defaultdict(list)
    for r in trades:
        sig = r.get("signal") or "UNLABELED"
        sym = r.get("symbol") or "?"
        by_signal[sig].append(r)
        by_symbol[sym].append(r)
        by_sig_sym[f"{sig}|{sym}"].append(r)
        hr = time.strftime("%H", time.localtime(float(r.get("ts", 0))))
        by_hour[hr].append(r)
        # AI-on vs rules-only: only rows that carry the ai_gated tag (post-wire);
        # older rows have no key and land in UNTAGGED so they don't pollute either.
        if "ai_gated" in r:
            by_ai["ai" if r.get("ai_gated") else "rules"].append(r)
        else:
            by_ai["UNTAGGED"].append(r)
        # Per-model split (2026-06-19, llm_router): which provider/model judged the
        # entry. Only AI-gated rows carry a non-empty 'model'; rules-only rows skip.
        m = r.get("model")
        if m:
            by_model[m].append(r)
    return {
        "window": len(trades),
        "overall": _seg(trades),
        "by_signal": {k: _seg(v) for k, v in by_signal.items()},
        "by_symbol": {k: _seg(v) for k, v in by_symbol.items()},
        "by_signal_symbol": {k: _seg(v) for k, v in by_sig_sym.items()},
        "by_hour": {k: _seg(v) for k, v in sorted(by_hour.items())},
        "by_ai_gated": {k: _seg(v) for k, v in by_ai.items()},
        "by_model": {k: _seg(v) for k, v in by_model.items()},
    }


def make_proposals(edge: dict) -> list[str]:
    """Return-optimization ideas — PROPOSE-ONLY, never auto-applied. Require a
    minimum sample so we don't react to 5-trade noise."""
    out = []
    MIN_N = 40
    sigs = edge.get("by_signal", {})
    # Require BOTH a labeled sample (n) and enough priced rows (n_priced) before
    # any ret_bps-based proposal — ret_bps is meaningless on too few priced rows.
    labeled = {k: v for k, v in sigs.items()
               if k != "UNLABELED" and v["n"] >= MIN_N and v.get("n_priced", 0) >= MIN_N}
    # If exactly one labeled signal is clearly positive and the other clearly
    # negative, suggest isolating the winner (this is the signal=all question).
    pos = {k: v for k, v in labeled.items() if (v.get("ret_bps") or 0) > 0}
    neg = {k: v for k, v in labeled.items() if (v.get("ret_bps") or -1) < 0}
    for k, v in pos.items():
        out.append(f"signal '{k}' POSITIVE ({v['ret_bps']}bps over n={v['n']}). "
                   f"If it holds out-of-sample, consider isolating it "
                   f"(SCALP_SIGNAL={k}) and/or raising leverage on it — HUMAN review.")
    for k, v in neg.items():
        out.append(f"signal '{k}' NEGATIVE ({v['ret_bps']}bps over n={v['n']}). "
                   f"Candidate to drop from SCALP_SIGNAL — HUMAN review.")
    if not labeled:
        out.append("No signal has >=40 labeled trades yet. Keep collecting; "
                   "edge is unmeasurable until the labeled sample grows.")
    # Per-symbol bleeders (propose pausing, do not auto-cut — that is noise-prone)
    for sym, v in edge.get("by_symbol", {}).items():
        if v["n"] >= MIN_N and v.get("n_priced", 0) >= MIN_N and (v.get("ret_bps") or 0) <= -50:
            out.append(f"symbol {sym} bleeding {v['ret_bps']}bps over n={v['n']} "
                       f"— consider dropping from SCALP_SYMBOLS — HUMAN review.")
    return out


# --------------------------------------------------------------------- governor
def decide_governor(status: dict) -> dict:
    """De-risk decision from daily drawdown. Monotonic within a day: merges with
    today's existing governor and only ever gets MORE restrictive."""
    cap = float(status.get("max_daily_loss_effective")
                or (status.get("config") or {}).get("max_daily_loss") or 0)
    pnl = float(status.get("realized_pnl_today", 0) or 0)
    lev_target = int(status.get("leverage_target", 1) or 1)

    desired = {"paused": False, "leverage_cap": None, "reason": ""}
    if cap > 0 and pnl <= -DD_HARD_FRAC * cap:
        desired = {"paused": True, "leverage_cap": max(1, lev_target // 2),
                   "reason": f"daily drawdown {pnl:.2f} <= -{DD_HARD_FRAC:.0%} cap (${cap:.0f})"}
    elif cap > 0 and pnl <= -DD_SOFT_FRAC * cap:
        desired = {"paused": False, "leverage_cap": max(1, lev_target // 2),
                   "reason": f"daily drawdown {pnl:.2f} <= -{DD_SOFT_FRAC:.0%} cap (${cap:.0f})"}

    # Merge monotonically with any existing same-day governor.
    cur = {}
    if GOV_FILE.exists():
        try:
            cur = json.loads(GOV_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            cur = {}
    if cur.get("day") == _today():
        if cur.get("paused"):
            desired["paused"] = True
        caps = [c for c in (cur.get("leverage_cap"), desired["leverage_cap"]) if c]
        if caps:
            desired["leverage_cap"] = min(caps)   # lower = more restrictive
            if not desired["reason"]:
                desired["reason"] = cur.get("reason", "")
    desired["day"] = _today()
    desired["updated_at"] = time.time()
    desired["updated_by"] = "scalp_monitor"
    return desired


def governor_changed(new: dict) -> bool:
    cur = {}
    if GOV_FILE.exists():
        try:
            cur = json.loads(GOV_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            cur = {}
    return (cur.get("paused"), cur.get("leverage_cap"), cur.get("day")) != \
           (new.get("paused"), new.get("leverage_cap"), new.get("day"))


# ----------------------------------------------------------------------- output
def write_report(edge: dict, status: dict, proposals: list[str], live: dict) -> None:
    REPORT_JSON.write_text(json.dumps({
        "generated_at": time.time(), "liveness": live,
        "edge": edge, "proposals": proposals,
    }, indent=2))

    def row(name, s):
        if not s.get("n"):
            return f"| {name} | 0 | 0 | — | — | — | — |"
        npr = s.get("n_priced", 0)
        # ret_bps only spans rows carrying size_usd (post-size-scaling era). Blank
        # it when that subset is too small to mean anything, so n and ret_bps are
        # never read as covering the same population.
        rb = ("—" if (s.get("ret_bps") is None or npr < RB_MIN_N)
              else f"{s['ret_bps']:+.1f}")
        return (f"| {name} | {s['n']} | {npr} | {s['wr']*100:.0f}% | {rb} | "
                f"${s['avg_pnl']:+.3f} | {s['stops']}/{s['tps']}/{s['time']} |")

    L = []
    L.append(f"# Scalp edge report — {time.strftime('%Y-%m-%d %H:%M:%S')}")
    L.append("")
    L.append(f"**Liveness:** {live.get('summary', '?')}")
    L.append("")
    L.append("Stake-normalized edge = total PnL / total notional, in bps "
             "(removes stake & leverage). `why` = stops/tps/time.")
    L.append("")
    hdr = "| segment | n | n_priced | WR | ret bps | avg $ | s/t/T |"
    sep = "|---|---|---|---|---|---|---|"
    L += [f"## Window ({edge['window']} trades) — n_priced = rows with size_usd "
          f"(only these feed ret bps; needs ≥{RB_MIN_N} to show)", "",
          hdr, sep, row("OVERALL", edge["overall"]), ""]
    L += ["### By signal", "", hdr, sep]
    L += [row(k, v) for k, v in edge["by_signal"].items()]
    L += ["", "### By symbol", "", hdr, sep]
    L += [row(k, v) for k, v in edge["by_symbol"].items()]
    L += ["", "### By signal × symbol", "", hdr, sep]
    L += [row(k, v) for k, v in edge["by_signal_symbol"].items()]
    if edge.get("by_ai_gated"):
        L += ["", "### By AI gating (ai = LLM judged the entry; rules = auto-approve)",
              "", hdr, sep]
        L += [row(k, v) for k, v in edge["by_ai_gated"].items()]
    if edge.get("by_model"):
        L += ["", "### By model (which provider/model judged AI-gated entries)",
              "", hdr, sep]
        L += [row(k, v) for k, v in sorted(edge["by_model"].items())]
    L += ["", "## Proposals (PROPOSE-ONLY — human review, never auto-applied)", ""]
    L += [f"- {p}" for p in proposals] or ["- (none)"]
    REPORT_MD.write_text("\n".join(L) + "\n", encoding="utf-8")


def journal(entry: dict) -> None:
    entry = {"ts": time.time(), **entry}
    with JOURNAL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


# ------------------------------------------------------------------------- main
def run_cycle(apply: bool = True) -> dict:
    status = fetch_status()
    now = time.time()
    trades = read_trades(WINDOW_TRADES)

    # liveness
    last_ts = max((float(t.get("ts", 0)) for t in trades), default=0)
    stale = (now - last_ts) > LIVENESS_STALE_S if last_ts else True
    running = bool(status and status.get("running"))
    live = {
        "server_up": status is not None,
        "engine_running": running,
        "log_stale": stale,
        "last_trade_age_s": round(now - last_ts) if last_ts else None,
        "realized_pnl_today": status.get("realized_pnl_today") if status else None,
        "trades_today": status.get("trades_today") if status else None,
    }
    flags = []
    if status is None:
        flags.append("SERVER DOWN")
    elif not running:
        flags.append("engine not running")
    elif stale:
        flags.append(f"log stale ({live['last_trade_age_s']}s)")
    live["summary"] = "OK" if not flags else " | ".join(flags)

    edge = compute_edge(trades)
    proposals = make_proposals(edge)
    write_report(edge, status or {}, proposals, live)

    action = {"governor_written": False}
    if status is not None:
        gov = decide_governor(status)
        action["governor_decision"] = {k: gov[k] for k in ("paused", "leverage_cap", "reason")}
        # Only ever write a RESTRICTIVE governor (de-risk). On healthy days no
        # file is created, so its presence always means "the monitor is cutting
        # risk right now". The file auto-expires daily (stale-day = ignored).
        restrictive = bool(gov.get("paused") or gov.get("leverage_cap"))
        if apply and restrictive and governor_changed(gov):
            GOV_FILE.write_text(json.dumps(gov, indent=2))
            action["governor_written"] = True

    journal({"liveness": live, "overall": edge["overall"],
             "by_signal": {k: {"n": v["n"], "ret_bps": v.get("ret_bps")}
                           for k, v in edge["by_signal"].items()},
             **action})
    return {"live": live, "edge": edge, "action": action, "proposals": proposals}


def main() -> None:
    global BASE_URL
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true", help="run forever")
    ap.add_argument("--interval", type=int, default=300, help="loop interval seconds")
    ap.add_argument("--base-url", default=BASE_URL)
    ap.add_argument("--dry-run", action="store_true", help="report only; never write governor")
    args = ap.parse_args()
    BASE_URL = args.base_url

    def once():
        res = run_cycle(apply=not args.dry_run)
        live, ov = res["live"], res["edge"]["overall"]
        rb = ov.get("ret_bps")
        print(f"[{time.strftime('%H:%M:%S')}] liveness={live['summary']} | "
              f"window n={ov.get('n', 0)} ret_bps={rb} | "
              f"governor={'WROTE ' if res['action'].get('governor_written') else ''}"
              f"{res['action'].get('governor_decision', {})}")

    if args.loop:
        while True:
            try:
                once()
            except Exception as exc:  # never let the monitor die on a bad cycle
                print(f"[{time.strftime('%H:%M:%S')}] cycle error: {exc}")
            time.sleep(max(30, args.interval))
    else:
        once()


if __name__ == "__main__":
    main()
