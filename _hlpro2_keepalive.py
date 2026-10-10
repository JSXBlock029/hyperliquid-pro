"""
_hlpro2_keepalive.py — one-shot watchdog for the HLPRO2 scalp stack.

Run periodically by Windows Task Scheduler (every ~10 min) AND it recovers the
stack within ~10 min of a reboot. Idempotent: if a process is already up it does
nothing, so double-runs never spawn duplicates.

Keeps alive:
  * server.py        (the Flask trading/scalp server)  -> via _scalp_launch.bat
  * scalp_monitor.py (de-risk governor + edge report)  -> --loop --interval 300
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
LOG = HERE / "hlpro2_keepalive.log"
PY = sys.executable
# DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP — child outlives this watchdog.
DETACHED = 0x00000008 | 0x00000200


def log(msg: str) -> None:
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def _running(needle: str) -> bool:
    """True if a python.exe with `needle` in its command line is running."""
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "Select-Object -ExpandProperty CommandLine"],
            text=True, stderr=subprocess.DEVNULL, timeout=30)
        return needle in (out or "")
    except Exception as exc:
        log(f"process check failed for {needle}: {exc}")
        return True  # fail-safe: assume up, don't risk spawning a duplicate


def launch_server() -> None:
    # MUST give the child a real console. server.py calls socketio.run() without
    # allow_unsafe_werkzeug=True, so if sys.stdin.isatty() is False (e.g. a
    # DETACHED_PROCESS launch) Werkzeug refuses to start and the server dies on
    # launch. `cmd /c start "" /MIN <bat>` spawns an independent minimized console
    # -> isatty() True -> server boots. (Documented quirk, project_hl_scalp memory.)
    subprocess.Popen(["cmd", "/c", "start", "", "/MIN", str(HERE / "_scalp_launch.bat")],
                     cwd=str(HERE), close_fds=True)


def launch_monitor() -> None:
    out = (HERE / "scalp_monitor_out.log").open("a", encoding="utf-8")
    err = (HERE / "scalp_monitor_err.log").open("a", encoding="utf-8")
    subprocess.Popen([PY, str(HERE / "scalp_monitor.py"), "--loop", "--interval", "300"],
                     cwd=str(HERE), stdout=out, stderr=err,
                     creationflags=DETACHED, close_fds=True)


def main() -> None:
    if not _running("server.py"):
        log("server.py DOWN -> launching"); launch_server()
    if not _running("scalp_monitor.py"):
        log("scalp_monitor.py DOWN -> launching"); launch_monitor()


if __name__ == "__main__":
    main()
