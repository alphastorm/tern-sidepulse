"""SidePulse #72 (337c703) behaviour probes for the tern-sidepulse per-session-row adapter.

Run with the SidePulse dev venv and the #72 worktree first on the path:
    PYTHONPATH=<sidepulse checkout of pull/72/head>/src <sidepulse dev venv>/bin/python spike/experiments.py
Never touches the installed SidePulse, its state directory or the user's publisher id.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sidepulse import program_status as ps
from sidepulse.program_presentation import ProgramMonitor
from sidepulse.program_transport import ProgramServer
from sidepulse.status_bar import native_session_menu_title, session_title_parts

PUBLISHER = "00000000-0000-4000-8000-0000000000aa"
OUT: dict = {}


def report(scope, state, label=None, *, source="session", session_id=None, cwd=None, kind=None):
    status = {"state": state}
    if label is not None and state != "clear":
        status["title"] = label
    if kind:
        status["kind"] = kind
    body = {"v": 1, "type": "program_status", "scope": scope, "status": status}
    if state != "clear":
        context = {"provider": "omp", "origin": "Tern"}
        if label is not None:
            context.update(title=label, title_source=source)
        if session_id:
            context["session_id"] = session_id
        if cwd:
            context["cwd"] = cwd
        body["context"] = context
    return body


def rows(state):
    return [
        {"scope_row": s.agent_id[:16], "name": s.display_name, "mode": s.mode.value, "menu": native_session_menu_title(s), "parts": session_title_parts(s)}
        for s in state.statuses()
    ]


def exchange(path: Path, body: dict) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(1.0)
        client.connect(str(path))
        client.sendall(json.dumps(body).encode())
        client.shutdown(socket.SHUT_WR)
        data = b""
        while chunk := client.recv(65536):
            data += chunk
    return json.loads(data)


def e1_baseline():
    st = ps.ProgramState(publisher=PUBLISHER)
    acks = [st.submit(report(f"tern-{i}", "working", f"Session {i}", session_id=f"{i:016x}")) for i in range(3)]
    before = len(st.statuses())
    acks.append(st.submit(report("tern-1", "clear")))
    OUT["E1"] = {"acks": acks, "rows_before": before, "rows_after_clear": len(st.statuses())}


def e2_orphans():
    """No lease: a reporter that stops reporting leaves working/blocked/error/idle rows; only done ages out."""
    st = ps.ProgramState(publisher=PUBLISHER)
    for scope, state in (("w", "working"), ("b", "blocked"), ("e", "error"), ("i", "idle"), ("d", "done")):
        st.submit(report(f"tern-{scope}", state, f"row {state}", session_id=f"{scope * 16}"))
    old = int((datetime.now(timezone.utc) - timedelta(days=2)).timestamp() * 1000)
    for key, update in st._sessions.items():  # simulate two days without a report
        st._sessions[key] = {**update, "updated_at": old}
    snap = ProgramMonitor(st).snapshot()
    OUT["E2"] = {
        "visible_after_2_days": sorted(s.mode.value for s in snap.statuses),
        "aggregate_mode": snap.aggregate.mode.value,
        "active_count": snap.aggregate.active_count,
    }


def e3_restart(tmp: Path):
    path = tmp / "program-status.sock"
    server = ProgramServer(ps.ProgramState(publisher=PUBLISHER), path=path)
    server.start()
    first = [exchange(path, report(f"tern-r{i}", "working", f"R{i}", session_id=f"{i:016x}")) for i in range(2)]
    inode_before = os.stat(path).st_ino
    state_before = exchange(path, {"v": 1, "type": "get_state"})["snapshot"]["statuses"]
    server.stop()
    server = ProgramServer(ps.ProgramState(publisher=PUBLISHER), path=path)
    server.start()
    inode_after = os.stat(path).st_ino
    state_after = exchange(path, {"v": 1, "type": "get_state"})["snapshot"]["statuses"]
    resend = exchange(path, report("tern-r0", "working", "R0", session_id=f"{0:016x}"))
    server.stop()
    OUT["E3"] = {
        "acks_before": first,
        "rows_before": len(state_before),
        "rows_after_restart": len(state_after),
        "inode_changed": inode_before != inode_after,
        "ack_after_restart": resend,
    }


def e4_title_source():
    st = ps.ProgramState(publisher=PUBLISHER)
    out = {}
    for source in ("prompt", "session"):
        scope = f"tern-t-{source}"
        st.submit(report(scope, "working", "First title", source=source, session_id="feedfacefeedface"))
        st.submit(report(scope, "working", "Renamed tab", source=source, session_id="feedfacefeedface"))
        row = next(s for s in st.statuses() if s.agent_id == st._session_identity(("local", PUBLISHER, scope)))
        out[source] = row.display_name
    OUT["E4"] = out


def e5_display():
    st = ps.ProgramState(publisher=PUBLISHER)
    variants = {
        "colon_title_session_id": report("tern-a", "working", "DOCS: Review docs", session_id="0123456789abcdef"),
        "plain_title_session_id": report("tern-b", "working", "api work", session_id="1123456789abcdef"),
        "colon_title_no_ids": report("tern-c", "working", "DOCS: Review docs"),
        "colon_title_cwd": report("tern-d", "working", "DOCS: Review docs", session_id="3123456789abcdef", cwd=str(Path(__file__).resolve().parent.parent)),
        "no_label": report("tern-e", "working", None, session_id="4123456789abcdef"),
    }
    for body in variants.values():
        st.submit(body)
    by_scope = {s.agent_id: s for s in st.statuses()}
    out = {}
    for name, body in variants.items():
        s = by_scope[st._session_identity(("local", PUBLISHER, body["scope"]))]
        out[name] = {"display_name": s.display_name, "menu": native_session_menu_title(s), "parts": session_title_parts(s), "session_id": s.session_id}
    OUT["E5"] = out


def e6_eviction():
    st = ps.ProgramState(publisher=PUBLISHER)
    st.submit(report("tern-ours", "working", "Ours", session_id="abcdefabcdefabcd"))
    for i in range(64):
        st.submit({"v": 1, "type": "program_status", "scope": f"other-{i}", "status": {"state": "idle"}})
    scopes = {s.agent_id for s in st.statuses()}
    OUT["E6"] = {"ours_present": st._session_identity(("local", PUBLISHER, "tern-ours")) in scopes, "rows": len(scopes)}


def e7_title_bytes():
    st = ps.ProgramState(publisher=PUBLISHER)
    label = "😀" * 49  # 196 bytes, 49 code points
    out = {}
    try:
        st.submit(report("tern-big", "working", label))
        out["status_title_196_bytes"] = "accepted"
    except ps.StatusError as exc:
        out["status_title_196_bytes"] = f"rejected: {exc}"
    try:
        body = report("tern-big2", "working", label)
        body["status"]["title"] = label.encode()[:192].decode(errors="ignore")
        st.submit(body)
        out["status_title_cut_192_context_title_196"] = "accepted"
    except ps.StatusError as exc:
        out["status_title_cut_192_context_title_196"] = f"rejected: {exc}"
    OUT["E7"] = out


def e9_forwarding():
    """What a relay or phone link would receive for one of our rows."""
    st = ps.ProgramState(publisher=PUBLISHER)
    st.start_delivery("relay-target")
    st.submit(report("tern-f", "working", "DOCS: Review docs", session_id="0123456789abcdef"))
    st.submit(report("tern-f", "idle", "DOCS: Review docs", session_id="0123456789abcdef"))
    events = st.pending_events("relay-target", local_only=True)
    OUT["E9"] = {
        "events_for_two_reports": len(events),
        "bridge_update": ps.bridge_update(events[-1][1]),
        "phone_update": ps.phone_update(events[-1][1]),
    }


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="tsp72-", dir="/tmp"))
    e1_baseline()
    e2_orphans()
    e3_restart(tmp)
    e4_title_source()
    e5_display()
    e6_eviction()
    e7_title_bytes()
    e9_forwarding()
    print(json.dumps(OUT, indent=2, default=str, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
