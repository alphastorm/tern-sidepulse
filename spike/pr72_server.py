"""SPIKE: SidePulse #72's program-status server alone, on a private socket path.

Runs only ``ProgramState`` + ``ProgramServer`` from the #72 worktree: no devices, relays, phone links or the
user's publisher id. Use the SidePulse dev venv:

    PYTHONPATH=<sidepulse checkout of pull/72/head>/src <sidepulse dev venv>/bin/python \
        spike/pr72_server.py /tmp/tsp72/program-status.sock

SIGTERM stops it (the socket is unlinked); start it again on the same path to simulate a service restart.
"""
from __future__ import annotations

import signal
import sys
import threading
from pathlib import Path

from sidepulse.program_status import ProgramState
from sidepulse.program_transport import ProgramServer

PUBLISHER = "00000000-0000-4000-8000-0000000000aa"


def main(path: str) -> int:
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    server = ProgramServer(ProgramState(publisher=PUBLISHER), path=Path(path))
    server.start()
    print(f"listening {path}", flush=True)
    stop.wait()
    server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
