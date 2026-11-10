"""Runs SidePulse's own program-status server for the contract test, under a SidePulse interpreter.

    <python> -I sidepulse_server.py <socket path> [<sidepulse src dir>]

Only ``ProgramState`` and ``ProgramServer``: no devices, relays, phone links or the user's publisher id.
Prints one JSON line, ``{"listening": <path>}`` or ``{"error": …}`` when this SidePulse predates program
status, then serves until stdin closes.
"""
import json
import sys
from pathlib import Path

if len(sys.argv) > 2:
    sys.path.insert(0, sys.argv[2])
try:
    from sidepulse.program_status import ProgramState
    from sidepulse.program_transport import ProgramServer
except ImportError as exc:  # reported to the test, which skips
    print(json.dumps({"error": f"import: {exc}"}), flush=True)
    sys.exit(0)

server = ProgramServer(ProgramState(publisher="00000000-0000-4000-8000-0000000000aa"), path=Path(sys.argv[1]))
server.start()
print(json.dumps({"listening": sys.argv[1]}), flush=True)
sys.stdin.read()
server.stop()
