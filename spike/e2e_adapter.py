"""SPIKE: the B-core coordinator + ProgramStatusTransport against SidePulse #72's server, end to end.

    SIDEPULSE_PR72_SRC=<sidepulse checkout of pull/72/head>/src PYTHONPATH="$SIDEPULSE_PR72_SRC:." <sidepulse dev venv>/bin/python spike/e2e_adapter.py

Real processes throughout: #72's ProgramServer (spike/pr72_server.py), the real helper (bridge.py register/snapshot,
which spawns the real coordinator) and SIGKILL / SIGTERM for the crash and restart cases. Prints one JSON report.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from sidepulse.program_presentation import DaemonMonitor
from sidepulse.status_bar import native_session_menu_title

from tests.helpers import run_bridge, short_tmpdir

ROOT = Path(__file__).resolve().parent.parent
PR72_SRC = os.environ["SIDEPULSE_PR72_SRC"]
GLYPH = "\U000f1517"
REPORT: dict = {}


def start_server(sock: Path) -> subprocess.Popen:
    env = dict(os.environ, PYTHONPATH=PR72_SRC)
    proc = subprocess.Popen([sys.executable, str(ROOT / "spike" / "pr72_server.py"), str(sock)], env=env, stdout=subprocess.PIPE, text=True)
    assert proc.stdout is not None and proc.stdout.readline().startswith("listening"), "pr72 server did not start"
    return proc


def stop_server(proc: subprocess.Popen) -> None:
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=5)


def rows(sock: Path) -> list[dict]:
    snapshot = DaemonMonitor(path=sock).snapshot()
    return [
        {"menu": native_session_menu_title(s), "mode": s.mode.value, "origin": s.origin, "provider": s.provider, "session": (s.session_id or "")[:8]}
        for s in (*snapshot.statuses, *snapshot.stale_statuses)
    ]


def wait_rows(sock: Path, predicate, timeout: float = 8.0) -> tuple[list[dict], float]:
    start = time.monotonic()
    while True:
        current = rows(sock)
        if predicate(current) or time.monotonic() - start > timeout:
            return current, round(time.monotonic() - start, 2)
        time.sleep(0.1)


def coordinator_pid(env: dict) -> int:
    return int((Path(env["TERN_SIDEPULSE_RUNTIME_DIR"]) / "coordinator.lock").read_text().strip())


def observe(env: dict, observations: list, *, observer: dict | None = None) -> dict:
    code, reg, err = run_bridge(["register"], {"observer": observer or {"window_key": ""}}, env)
    assert reg.get("ok"), (code, reg, err)
    code, ack, err = run_bridge(
        ["snapshot"],
        {"observer": observer or {"window_key": ""}, "epoch": reg["epoch"], "generation": reg["generation"], "seq": 1, "coverage": "complete", "observations": observations},
        env,
    )
    assert ack.get("ok"), (code, ack, err)
    return {"epoch": reg["epoch"], "generation": reg["generation"], "seq": 1, "ack_rows": ack.get("rows")}


def snapshot(env: dict, session: dict, observations: list) -> dict:
    session["seq"] += 1
    code, ack, err = run_bridge(
        ["snapshot"],
        {"observer": {"window_key": ""}, "epoch": session["epoch"], "generation": session["generation"], "seq": session["seq"], "coverage": "complete", "observations": observations},
        env,
    )
    assert ack.get("ok"), (code, ack, err)
    return ack


def main() -> int:
    tmp = short_tmpdir("tsp72e-")
    sock = tmp / "program-status.sock"
    env = dict(os.environ)
    env.pop("TERN_WINDOW_KEY", None)
    env.update(
        TERN_PLUGIN_DATA=str(tmp / "data"),
        TERN_SIDEPULSE_RUNTIME_DIR=str(tmp / "rt"),
        TERN_SIDEPULSE_UI_SOCKETS=str(tmp / "absent-ui.sock"),
        TERN_SIDEPULSE_SERVICE_SOCKETS=str(tmp / "absent-service.sock"),
        TERN_SIDEPULSE_TRANSPORT="program_status",
        TERN_SIDEPULSE_PROGRAM_SOCKETS=str(sock),
        TERN_SIDEPULSE_LIMITS=json.dumps({"lease": 3, "exit_grace": 1, "final_grace": 10}),
    )
    server = start_server(sock)
    three = [
        {"pane": 949187772435, "state": "working", "host": "local", "label": f"{GLYPH} DOCS: Review docs"},
        {"pane": 949187772445, "state": "idle", "host": "local", "label": "api work"},
        {"pane": 4521198205534217, "state": "waiting_input", "host": "sf-studio.tailfb479a.ts.net", "label": "\uf412 V0211: Context and Task Briefs"},
    ]
    try:
        session = observe(env, three)
        REPORT["1_three_sessions"], REPORT["1_latency_s"] = wait_rows(sock, lambda r: len(r) == 3)

        three[0]["label"] = f"{GLYPH} DOCS: Docs reviewed"
        snapshot(env, session, three)
        REPORT["2_label_change"], _ = wait_rows(sock, lambda r: any("Docs reviewed" in row["menu"] for row in r))

        pid = coordinator_pid(env)
        os.kill(pid, signal.SIGKILL)
        time.sleep(1.5)
        REPORT["3_after_sigkill_rows"] = rows(sock)  # orphans: nobody will ever clear these but us
        ledger = json.loads((tmp / "rt" / "program-scopes.json").read_text())
        REPORT["3_ledger_scopes"] = len(ledger["scopes"])

        session = observe(env, [three[0]])  # a window snapshot respawns the coordinator; two panes are gone
        REPORT["4_after_respawn"], REPORT["4_latency_s"] = wait_rows(sock, lambda r: len(r) == 1)
        REPORT["4_new_pid_differs"] = coordinator_pid(env) != pid

        stop_server(server)
        server = start_server(sock)  # SidePulse service restart: every row is gone
        REPORT["5_rows_right_after_restart"] = len(rows(sock))
        REPORT["5_after_restart"], REPORT["5_recovery_s"] = wait_rows(sock, lambda r: len(r) == 1)

        _code, disabled, _ = run_bridge(["disable"], {}, env)
        REPORT["6_disable_summary"] = disabled.get("summary")
        REPORT["6_after_disable"], _ = wait_rows(sock, lambda r: len(r) == 0)
        run_bridge(["enable"], {}, env)

        session = observe(env, [three[1]])
        REPORT["7_after_enable"], _ = wait_rows(sock, lambda r: len(r) == 1)
        # No more snapshots: the 3 s lease expires, the row is cleared and the coordinator exits.
        REPORT["8_after_lease_expiry"], REPORT["8_clear_s"] = wait_rows(sock, lambda r: len(r) == 0, timeout=15)
        lock = tmp / "rt" / "coordinator.lock"
        deadline = time.monotonic() + 10
        alive = True
        while time.monotonic() < deadline and alive:
            try:
                os.kill(int(lock.read_text().strip()), 0)
                time.sleep(0.2)
            except (OSError, ValueError):
                alive = False
        REPORT["8_coordinator_exited"] = not alive
        REPORT["log_tail"] = (tmp / "rt" / "coordinator.log").read_text().splitlines()[-14:]
    finally:
        stop_server(server)
    print(json.dumps(REPORT, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
