"""Unit tests import only pure modules (never server.py, which connects to Hyperliquid at import).
The network is blocked and every credential variable is removed, so nothing can reach an exchange."""
import os
import socket
import sys
from pathlib import Path

for k in list(os.environ):
    if k.startswith(("HYPERLIQUID_", "OPENAI", "GEMINI", "GOOGLE_API", "TG_", "HLPRO_")) or k == "SCALP_LIVE":
        os.environ.pop(k, None)
os.environ["SCALP_LIVE"] = "false"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _blocked(*args, **kwargs):
    raise RuntimeError("Network access is blocked in tests")


socket.socket.connect = _blocked
socket.create_connection = _blocked
