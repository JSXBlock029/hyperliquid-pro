"""
scalp_report_agent.py — LOCAL-LLM daily analyst for the scalp engine.

Self-contained so it keeps working with no Claude access: it reads the artifacts
that scalp_monitor.py already produces (scalp_edge_report.json + the journal) and
the live /scalp/status, asks the LOCAL Ollama model (qwen2.5) for a plain-English
health + edge read with PROPOSE-ONLY tuning ideas, writes a dated markdown file,
and sends a short summary to Telegram.

It APPLIES NOTHING. Auto de-risk is the deterministic monitor's job; this agent is
advisory only — that separation is deliberate (LLM narrative must never move risk).

Run once (for the daily schtask):   python scalp_report_agent.py
Generate but don't send to TG:      python scalp_report_agent.py --no-send
Skip the LLM (mechanical summary):  python scalp_report_agent.py --no-llm
"""

import os
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
REPORT_JSON = HERE / "scalp_edge_report.json"
JOURNAL = HERE / "scalp_monitor_log.jsonl"

BASE_URL = "http://localhost:3000"
OLLAMA_HOST = "http://127.0.0.1:11434"
OLLAMA_MODEL = "qwen2.5:7b-instruct"
OLLAMA_TIMEOUT_S = 120

SYSTEM_PROMPT = (
    "You are a risk-aware quant analyst reviewing a PAPER crypto scalping bot. "
    "Backtests show this strategy has little-to-no proven directional edge, and it "
    "now trades at 5x leverage, so losses are amplified. Your job is to read the "
    "metrics and write a SHORT, honest report.\n"
    "HARD RULES:\n"
    "- The key metric is ret_bps (stake-normalized edge). Only trust it where "
    "n_priced is large enough (the report blanks it otherwise). Never invent numbers.\n"
    "- You PROPOSE ONLY. You never apply changes. Frame every suggestion as 'for "
    "human review'.\n"
    "- NEVER suggest raising leverage, size, or trade frequency unless a LABELED "
    "signal shows clearly POSITIVE ret_bps over a large sample. If edge is negative "
    "or unproven, say so plainly and suggest cutting/pausing, not scaling up.\n"
    "- A high win-rate does NOT mean profitable (small wins, big losses). Judge on "
    "ret_bps and avg $, not WR.\n"
    "Keep it under ~200 words: a one-line verdict, the per-signal/per-symbol read, "
    "and at most 3 concrete propose-only ideas."
)


def _get(url: str, timeout: int = 8):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def load_inputs() -> dict:
    report = {}
    if REPORT_JSON.exists():
        try:
            report = json.loads(REPORT_JSON.read_text())
        except (OSError, json.JSONDecodeError):
            report = {}
    status = _get(f"{BASE_URL}/scalp/status") or {}
    # last few journal lines for trend context
    jtail = []
    if JOURNAL.exists():
        try:
            lines = JOURNAL.read_text(encoding="utf-8").splitlines()[-6:]
            jtail = [json.loads(x) for x in lines if x.strip()]
        except (OSError, json.JSONDecodeError):
            jtail = []
    return {"report": report, "status": status, "journal_tail": jtail}


def mechanical_summary(inp: dict) -> str:
    """LLM-free fallback so the report still goes out if Ollama is down."""
    edge = (inp["report"].get("edge") or {})
    ov = edge.get("overall", {}) or {}
    st = inp["status"]
    gov = st.get("governor") or {}
    lines = [
        f"OVERALL: n={ov.get('n', 0)} (priced {ov.get('n_priced', 0)}), "
        f"WR {round((ov.get('wr') or 0)*100)}%, ret {ov.get('ret_bps')} bps, "
        f"avg ${ov.get('avg_pnl')}.",
        f"Today: pnl ${st.get('realized_pnl_today')}, {st.get('trades_today')} trades, "
        f"leverage {st.get('leverage_target')}x, governor "
        f"{'ACTIVE: '+str(gov) if gov else 'clear'}.",
    ]
    for k, v in (edge.get("by_signal") or {}).items():
        lines.append(f"  signal {k}: n={v.get('n')} priced={v.get('n_priced')} "
                     f"ret={v.get('ret_bps')}bps avg=${v.get('avg_pnl')}")
    for p in (inp["report"].get("proposals") or [])[:3]:
        lines.append(f"PROPOSAL: {p}")
    return "\n".join(lines)


def llm_analysis(inp: dict) -> str | None:
    edge = inp["report"].get("edge") or {}
    compact = {
        "overall": edge.get("overall"),
        "by_signal": edge.get("by_signal"),
        "by_symbol": edge.get("by_symbol"),
        "today": {
            "pnl": inp["status"].get("realized_pnl_today"),
            "trades": inp["status"].get("trades_today"),
            "leverage": inp["status"].get("leverage_target"),
            "governor": inp["status"].get("governor") or "clear",
        },
        "monitor_proposals": inp["report"].get("proposals"),
    }
    payload = {
        "model": OLLAMA_MODEL, "system": SYSTEM_PROMPT,
        "prompt": "Metrics JSON:\n" + json.dumps(compact, separators=(",", ":")),
        "stream": False, "keep_alive": "10m",
        "options": {"temperature": 0.3, "num_predict": 400},
    }
    try:
        req = urllib.request.Request(
            f"{OLLAMA_HOST}/api/generate",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT_S) as r:
            return (json.loads(r.read()).get("response") or "").strip() or None
    except Exception as exc:
        print(f"llm_analysis failed: {exc}")
        return None


def _tg_creds() -> tuple[str, str]:
    return os.getenv("TG_BOT_TOKEN", ""), os.getenv("TG_CHAT_ID", "")


def send_telegram(text: str) -> bool:
    token, chat = _tg_creds()
    if not token or not chat:
        print("telegram: missing token/chat")
        return False
    # Telegram hard-caps messages at 4096 chars.
    text = text[:3900]
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=data, method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            ok = json.loads(r.read()).get("ok")
            print(f"telegram: sent ok={ok}")
            return bool(ok)
    except Exception as exc:
        print(f"telegram send failed: {exc}")
        return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-send", action="store_true", help="don't send to Telegram")
    ap.add_argument("--no-llm", action="store_true", help="skip Ollama, mechanical summary only")
    args = ap.parse_args()

    inp = load_inputs()
    mech = mechanical_summary(inp)
    analysis = None if args.no_llm else llm_analysis(inp)
    body = analysis or mech
    source = "qwen2.5 (local)" if analysis else "mechanical fallback"

    day = time.strftime("%Y-%m-%d")
    out = HERE / f"scalp_analysis_{day}.md"
    md = (f"# Scalp analysis — {time.strftime('%Y-%m-%d %H:%M')} ({source})\n\n"
          f"_PROPOSE-ONLY. Auto de-risk is handled by scalp_monitor.py; this report "
          f"applies nothing._\n\n## Analysis\n\n{body}\n\n## Raw metrics\n\n```\n{mech}\n```\n")
    out.write_text(md, encoding="utf-8")
    print(f"wrote {out.name} ({source})")

    if not args.no_send:
        tg = (f"📊 Scalp daily ({source})\n\n{(analysis or mech)[:1500]}\n\n"
              f"(propose-only · full: {out.name})")
        send_telegram(tg)


if __name__ == "__main__":
    main()
