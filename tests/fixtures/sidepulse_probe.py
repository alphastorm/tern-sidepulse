"""Runs under SidePulse's own interpreter: feeds the bridge's lines through the real normalizer.

Prints one JSON object: the mode after each step of the table in docs/compatibility.md.
"""
import json
import sys

try:
    from sidepulse.collector import LiveAgentMonitor
    from sidepulse.providers import parse_log_line
except ImportError as exc:  # pragma: no cover - reported to the test, which skips
    print(json.dumps({"error": f"import: {exc}"}))
    sys.exit(0)

sys.path.insert(0, sys.argv[1])
from tern_sidepulse.transport import build_line  # noqa: E402

SESSION = "tern-sidepulse:fixture"
monitor = LiveAgentMonitor()
results = []
steps = [
    ("working", "working"),
    ("completed", "completed"),
    ("working", "working"),
    ("waiting_for_input", "waiting_for_input"),
    ("idle_ready", "idle_ready"),
    ("blocked_error", "blocked_error"),
    ("working", "working"),
    ("completed", "completed"),
    ("idle_ready", "idle_ready"),
]
wall = 1_800_000_000.0
for mode, expected in steps:
    wall += 1
    record = parse_log_line("omp", json.dumps(build_line(mode, SESSION, wall)))
    monitor.ingest_record(record)
    status = monitor.statuses_by_key.get(record.status_key)
    results.append({"sent": mode, "expected": expected, "got": status.mode.value if status else None, "key": record.status_key})

# Trap checks: the collector ignores Notification after completed and maps a bare SessionEnd to completed.
for event in ("Notification", "SessionEnd"):
    wall += 1
    monitor.ingest_record(parse_log_line("omp", json.dumps({"hook_event_name": "Stop", "session_id": SESSION, "logged_at": "2027-01-01T00:00:00Z"})))
    monitor.ingest_record(parse_log_line("omp", json.dumps({"hook_event_name": event, "session_id": SESSION, "logged_at": "2027-01-01T00:00:01Z"})))
    results.append({"trap": event, "got": monitor.statuses_by_key[f"omp:session:{SESSION}"].mode.value})

print(json.dumps({"results": results}))
