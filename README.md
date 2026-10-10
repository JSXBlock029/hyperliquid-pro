# Hyperliquid Pro (HLPRO2)

A self-hosted trading terminal for [Hyperliquid](https://hyperliquid.xyz) perpetuals: a Flask and Socket.IO server with a web UI for positions, orders and charts, a TradingView webhook receiver, a backtester, and an automated scalping stack that can use a local LLM to judge entries and exits.

> This is personal trading software, shared as source. It can place real orders with real money. Read the code before pointing it at a funded account, and start in paper mode. Nothing here is financial advice, and no performance is claimed.

## What is in the repo

| File | Role |
|---|---|
| `server.py` | Flask and Socket.IO app: REST API, web UI, TradingView `/webhook`, order, close and balance routes |
| `public/` | The web UI (`index.html` terminal, `scalp.html` scalper dashboard, charts) |
| `scalp_engine.py` | Polls 1m candles and the L2 book, computes features (EMA, ATR, z-score, book imbalance, spread) and applies a cheap deterministic gate |
| `scalp_agent.py` | Optional LLM judge for entries and exits that returns a structured plan |
| `scalp_controller.py` | Runs the loop, applies risk gates, routes each plan to paper or live execution |
| `scalp_monitor.py`, `scalp_stats.py`, `scalp_report_agent.py` | De-risk governor, edge statistics and a daily report |
| `scalp_backtest.py`, `backtester.py` | Backtesting for the scalp signals and for the terminal's strategies |
| `scanner_backend.py` | Token and market scanner behind `/scanner` |
| `llm_router.py` | Provider fallback for LLM calls (Ollama, Gemini, OpenAI) |
| `_hlpro2_keepalive.py`, `_scalp_launch.bat` | Windows helpers to launch and supervise the server |

## Quickstart (paper mode)

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                   # fill in your wallet addresses
python server.py
```

Open http://127.0.0.1:3000 for the terminal and http://127.0.0.1:3000/scalp for the scalper.

The scalper runs in **paper mode by default**. It places real orders only when `SCALP_LIVE=true` is set **and** live is switched on at runtime through the dashboard toggle. Paper fills are written to a local journal.

## Safety notes

- **Order routes are live.** `/api/order`, `/api/buy`, `/api/sell`, `/api/close-position`, `/api/close-all-positions` and the TradingView `/webhook` act on your account whenever API-wallet credentials are configured. The scalper's paper mode does not apply to them.
- **Network exposure.** The server binds to `127.0.0.1`. State-changing requests from any other address, or that pass through a proxy or tunnel (anything carrying forwarded-for headers), are rejected unless they carry `HLPRO_API_TOKEN` (header `X-API-Token` or `?token=`).
- **Use an API wallet.** Hyperliquid API (agent) wallets can trade but cannot withdraw. Prefer one over your main key.
- **Your address is public.** Anything traded from an address is visible on-chain. Keep your wallet addresses and keys in `.env`, which is git-ignored.

## Known limitations

- The server was written for one operator on Windows. Paths, process supervision and defaults assume that.
- `server.py` is large and holds many concerns in one file. It has not been split up.
- There is no automated test suite in this repository, and the project was not run as part of publishing it. Treat it as source to read and adapt rather than a packaged product.
- Strategy parameters (`GIGA_*`, `SCALP_*`) are tuned defaults, not recommendations.
