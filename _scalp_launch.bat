@echo off
REM Launch the HLPRO2 Flask server with the scalp stack configured via environment variables.
REM Edit the values below, or set them in the environment before running.
REM Set SCALP_USE_AGENT=false for rules-only (no LLM judging of entries and exits).
set SCALP_USE_AGENT=true
set SCALP_SIGNAL=all
set SCALP_SYMBOLS=BTC,ETH,SOL
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
python server.py 1>server_out.log 2>server_err.log
