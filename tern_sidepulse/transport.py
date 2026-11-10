"""SidePulse program-status delivery: one row per OMP session, acknowledged by the daemon.

Verified against SidePulse ``ad4a521`` (inteliwear/sidepulse#72; ``docs/compatibility.md``): one UTF-8 JSON
``program_status`` report per ``AF_UNIX`` connection to ``program-status.sock``, framed by closing the write
side; the daemon answers ``{"ok": true, "seq": …, "revision": …}`` or ``{"ok": false, "error": …}``. Rows
live only in the daemon's memory and never expire unless they are ``done``, so this transport keeps a
write-ahead ledger of every scope it may have made visible and clears each one it no longer wants,
including a crashed predecessor's once a grace period has given every live window time to claim its rows.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import socket
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from .clock import Clock
from .reducer import BLOCKED_ERROR, COMPLETED, IDLE_READY, MODE_PRIORITY, WAITING_FOR_INPUT, WORKING, Row
from .storage import write_atomic

NAME = "program_status"
STATE_FOR_MODE = {
    WORKING: "working",
    WAITING_FOR_INPUT: "blocked",
    BLOCKED_ERROR: "error",
    COMPLETED: "done",
    IDLE_READY: "idle",
}
ACTIVE_MODES = frozenset({WORKING, WAITING_FOR_INPUT, BLOCKED_ERROR})
SCOPE_PREFIX = "tern-sidepulse-"
UNLABELLED = "OMP in Tern"
MAX_ROWS = 32  # SidePulse keeps 64 rows across every provider and silently evicts the least recently updated
TICK_BUDGET_SECONDS = 0.3
DRAIN_BUDGET_SECONDS = 2.0  # how long an exiting coordinator keeps clearing rows
SEND_TIMEOUT_SECONDS = 0.2
MAX_RETRY_SECONDS = 30.0
ACTIVE_REFRESH_SECONDS = 300.0  # restores an active row SidePulse evicted or lost without replacing its socket
MAX_REPLY_BYTES = 64 * 1024


def iso_utc(wall: float) -> str:
    return datetime.fromtimestamp(wall, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def scope_for(salt: str, row: Row) -> str:
    """Deterministic per install, host and pane, so a restarted coordinator overwrites its own rows; the salt keeps
    host keys out of everything SidePulse stores or forwards."""
    digest = hashlib.sha256(f"{salt}\0{row.host}\0{row.pane}".encode("utf-8", "surrogatepass")).hexdigest()[:24]
    return SCOPE_PREFIX + digest


def report_for(row: Row, scope: str, labels: bool) -> dict:
    """One row's report. ``title_source: "session"`` lets a renamed tab replace the title; an empty title stops
    the daemon from carrying an earlier label forward when a row loses its label or labels are turned off."""
    context = {"provider": "omp", "origin": "Tern", "session_id": scope[len(SCOPE_PREFIX):], "title_source": "session"}
    if labels and row.label:
        context["title"] = row.label
    else:
        context["title"] = ""
        context["display_name"] = UNLABELLED
    return {"v": 1, "type": NAME, "scope": scope, "status": {"state": STATE_FOR_MODE[row.mode]}, "context": context}


def clear_for(scope: str) -> dict:
    return {"v": 1, "type": NAME, "scope": scope, "status": {"state": "clear"}}


def exchange(target: Path, payload: bytes, timeout: float) -> dict:
    """One report on its own connection within ``timeout`` overall; returns the daemon's reply object.
    Raises OSError (transport) or ValueError (an unreadable reply)."""
    deadline = time.monotonic() + timeout
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(str(target))
        client.sendall(payload)
        client.shutdown(socket.SHUT_WR)
        data = bytearray()
        while True:
            client.settimeout(max(0.01, deadline - time.monotonic()))
            chunk = client.recv(65536)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > MAX_REPLY_BYTES:
                raise OSError(errno.EMSGSIZE, "reply too large")
    finally:
        client.close()
    reply = json.loads(data.decode("utf-8")) if data else None
    if not isinstance(reply, dict):
        raise OSError(errno.EBADMSG, "reply is not an object")
    return reply


def signature(path: Path) -> tuple[int, float] | None:
    try:
        info = os.stat(path)
    except OSError:
        return None
    return (info.st_ino, info.st_ctime)


@dataclass
class Entry:
    """One scope's delivery. ``request_id`` changes with every deliberate send and stays for retries, so the
    daemon applies a retried report at most once."""

    scope: str
    body: dict
    priority: int  # clears first (-1), then by mode priority
    active: bool
    request_id: str
    pending: bool = True
    clearing: bool = False
    rejected: bool = False
    delivered_at: float | None = None  # monotonic


class ScopeLedger:
    """Scopes this install may have made visible: written before a scope's first report, pruned after its clear."""

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        scopes = data.get("scopes") if isinstance(data, dict) else None
        self.scopes: set[str] = {
            scope
            for scope in (scopes if isinstance(scopes, list) else ())
            if isinstance(scope, str) and scope.startswith(SCOPE_PREFIX) and len(scope) <= 128 and scope.isprintable()
        }

    def save(self) -> None:
        write_atomic(self.path, json.dumps({"scopes": sorted(self.scopes)}) + "\n")


class SidePulseTransport:
    """Delivers the coordinator's rows to SidePulse's program-status socket, one scope per row."""

    def __init__(
        self,
        *,
        clock: Clock,
        candidates: Iterable[Path],
        ledger_path: Path,
        salt: str,
        labels: bool = True,
        inherit_grace: float = 15.0,
        max_rows: int = MAX_ROWS,
        sender: Callable[[Path, bytes, float], dict] | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.clock = clock
        self.candidates = tuple(candidates)
        self.salt = salt
        self.labels = labels
        self.max_rows = max_rows
        self.sender = sender or exchange
        self.log = log or (lambda _message: None)
        self.ledger = ScopeLedger(ledger_path)
        self.entries: dict[str, Entry] = {}
        # A predecessor's scopes wait one grace period: a session that is still open is overwritten in place by
        # the first snapshot that carries it instead of being cleared and re-added; the rest are cleared then.
        self.inherited = set(self.ledger.scopes)
        self.inherit_deadline = clock.monotonic() + inherit_grace
        self.failures = 0
        self.next_attempt_at = 0.0
        self.last_error: str | None = None
        self.target: Path | None = None
        self.target_signature: tuple[int, float] | None = None
        self.probe: tuple | None = None  # every candidate's signature at the last failure
        self.delivered_wall: float | None = None
        self.sends = 0
        self.rejections = 0
        self.rows_capped = 0
        if self.inherited:
            self.log(f"{NAME}: {len(self.inherited)} scope(s) left by an earlier coordinator")

    # ---- the coordinator's boundary ----------------------------------------------------------

    def publish(self, rows: Iterable[Row], *, force: bool = False) -> None:
        """Record the rows to show (at most ``max_rows``, most urgent first). Never does I/O; ``force`` resends
        rows whose report has not changed."""
        ordered = sorted(rows, key=lambda row: (MODE_PRIORITY[row.mode], row.host, row.pane))
        self.rows_capped = max(0, len(ordered) - self.max_rows)
        wanted = {}
        for row in ordered[: self.max_rows]:
            scope = scope_for(self.salt, row)
            wanted[scope] = (report_for(row, scope, self.labels), MODE_PRIORITY[row.mode], row.mode in ACTIVE_MODES)
        for scope, (body, priority, active) in wanted.items():
            self.inherited.discard(scope)
            entry = self.entries.get(scope)
            if entry is None or entry.clearing or entry.body != body:
                self.entries[scope] = Entry(scope, body, priority, active, new_request_id())
            elif force:
                entry.pending, entry.rejected, entry.request_id = True, False, new_request_id()
        for scope, entry in self.entries.items():
            if scope not in wanted and not entry.clearing:
                self._mark_clear(entry)
        if self.inherited and len(wanted) + len(self.inherited) > self.max_rows:
            self._clear_inherited()  # a predecessor's leftovers may not push this install past its row cap

    def withdraw(self) -> None:
        """Clear every scope this install may show, a predecessor's included: the coordinator is leaving."""
        self.publish(())
        self._clear_inherited()

    def drain(self, budget: float = DRAIN_BUDGET_SECONDS) -> None:
        """Exit path: tick, ignoring backoff, until nothing is pending, a send fails, or ``budget`` seconds pass."""
        deadline = self.clock.monotonic() + budget
        while self.clock.monotonic() < deadline and any(entry.pending and not entry.rejected for entry in self.entries.values()):
            failures = self.failures
            self.tick(immediate=True)
            if self.failures > failures:
                return

    def tick(self, immediate: bool = False) -> bool:
        """Send what is due within one bounded budget (ignoring backoff when ``immediate``); True if it tried."""
        now = self.clock.monotonic()
        if self.inherited and now >= self.inherit_deadline:
            self._clear_inherited()
        self._detect_receiver_change(now)
        for entry in self.entries.values():
            if entry.active and not entry.pending and entry.delivered_at is not None and now - entry.delivered_at >= ACTIVE_REFRESH_SECONDS:
                entry.pending, entry.request_id = True, new_request_id()
        due = [entry for entry in self.entries.values() if entry.pending and not entry.rejected]
        if not due or (now < self.next_attempt_at and not immediate):
            return False
        ledger_error = None
        new_scopes = {entry.scope for entry in due if not entry.clearing} - self.ledger.scopes
        if new_scopes:
            self.ledger.scopes |= new_scopes
            try:
                self.ledger.save()
            except OSError as exc:
                # Never show a row that the next coordinator could not find and clear.
                self.ledger.scopes -= new_scopes
                due = [entry for entry in due if entry.scope not in new_scopes]
                ledger_error = f"ledger: {exc.strerror or type(exc).__name__}"
        deadline = self.clock.monotonic() + TICK_BUDGET_SECONDS
        error = None
        cleared = False
        for entry in sorted(due, key=lambda item: item.priority):
            if self.clock.monotonic() >= deadline:
                break
            error = self._send(entry)
            cleared = cleared or (entry.clearing and not entry.pending)
            if error is not None:
                break
        if cleared:
            try:
                self.ledger.save()
            except OSError:
                pass  # the next coordinator clears an already cleared scope, which is harmless
        error = error or ledger_error
        if error is None:
            if self.failures:
                self.log(f"{NAME} recovered after {self.failures} failures")
            self.failures, self.last_error = 0, None
        else:
            self.failures += 1
            self.last_error = error
            self.probe = tuple(signature(path) for path in self.candidates)
            self.next_attempt_at = self.clock.monotonic() + min(MAX_RETRY_SECONDS, float(2 ** min(self.failures, 5)))
            if self.failures == 1:
                self.log(f"{NAME} delivery failing: {error}")
        return True

    def next_due(self) -> float | None:
        due = []
        if any(entry.pending and not entry.rejected for entry in self.entries.values()):
            due.append(self.next_attempt_at)
        if self.inherited:
            due.append(self.inherit_deadline)
        refresh = [
            entry.delivered_at + ACTIVE_REFRESH_SECONDS
            for entry in self.entries.values()
            if entry.active and not entry.pending and entry.delivered_at is not None
        ]
        if refresh:
            due.append(min(refresh))
        return min(due) if due else None

    def undelivered_neutral(self) -> list[str]:
        """Receivers that may still show a row from this install (none when SidePulse is not listening)."""
        if not (self.entries or self.ledger.scopes):
            return []
        return [NAME] if any(path.exists() for path in self.candidates) else []

    def delivery_summary(self) -> dict:
        return {NAME: {"rows": self._shown(), "pending": self._pending(), "error": self.last_error}}

    def to_dict(self) -> dict:
        return {
            "kind": NAME,
            "labels": self.labels,
            "candidates": [str(path) for path in self.candidates],
            "receiver_present": any(path.exists() for path in self.candidates),
            "target": str(self.target) if self.target else None,
            "rows": self._shown(),
            "pending": self._pending(),
            "clearing": sum(entry.clearing for entry in self.entries.values()),
            "rejected": sum(entry.rejected for entry in self.entries.values()),
            "inherited": len(self.inherited),
            "ledger": len(self.ledger.scopes),
            "rows_capped": self.rows_capped,
            "failures": self.failures,
            "last_error": self.last_error,
            "delivered_wall": iso_utc(self.delivered_wall) if self.delivered_wall is not None else None,
            "sends": self.sends,
            "rejections": self.rejections,
        }

    # ---- internals ----------------------------------------------------------------------------

    def _shown(self) -> int:
        return sum(not entry.clearing and not entry.pending for entry in self.entries.values())

    def _pending(self) -> int:
        return sum(entry.pending for entry in self.entries.values())

    def _mark_clear(self, entry: Entry) -> None:
        entry.body, entry.priority, entry.active, entry.request_id = clear_for(entry.scope), -1, False, new_request_id()
        entry.clearing, entry.pending, entry.rejected = True, True, False

    def _clear_inherited(self) -> None:
        for scope in sorted(self.inherited):
            if scope not in self.entries:
                self.entries[scope] = Entry(scope, clear_for(scope), -1, False, new_request_id(), clearing=True)
        self.inherited.clear()

    def _detect_receiver_change(self, now: float) -> None:
        """A socket that appears or changes during backoff is tried at once; a restarted daemon (a new socket at the
        target) starts empty, so every row is resubmitted, except ``done`` rows, which must not read as a fresh
        completion and go out again when they change."""
        if self.failures and now < self.next_attempt_at:
            current = tuple(signature(path) for path in self.candidates)
            if current != self.probe:
                self.probe = current
                self.next_attempt_at = now
                self.log(f"{NAME}: receiver changed during backoff, retrying now")
        if self.target is None:
            return
        current_target = signature(self.target)
        if current_target == self.target_signature:
            return
        if current_target is None:
            # The target is gone. A SidePulse now listening on another candidate started empty as well.
            if not any(signature(path) is not None for path in self.candidates if path != self.target):
                return
            self.target = None  # the next send picks whichever candidate answers
        else:
            self.target_signature = current_target
        for entry in self.entries.values():
            if not entry.clearing and entry.body["status"]["state"] != "done":
                if not entry.pending:
                    entry.request_id = new_request_id()
                entry.pending, entry.rejected = True, False
        self.next_attempt_at = now
        self.log(f"{NAME}: receiver replaced, resubmitting its rows")

    def _send(self, entry: Entry) -> str | None:
        """One report to the first candidate that answers. Returns an error that warrants backoff, else None."""
        payload = json.dumps(dict(entry.body, request_id=entry.request_id), separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        errors = []
        for target in self.candidates:
            try:
                reply = self.sender(target, payload, SEND_TIMEOUT_SECONDS)
            except (OSError, ValueError) as exc:
                errors.append(f"{target.name}: {getattr(exc, 'strerror', None) or type(exc).__name__}")
                continue
            self.sends += 1
            if self.target != target:
                self.target, self.target_signature = target, signature(target)
            if reply.get("ok") is True:
                entry.pending, entry.delivered_at = False, self.clock.monotonic()
                self.delivered_wall = self.clock.wall()
                if entry.clearing:
                    del self.entries[entry.scope]
                    self.ledger.scopes.discard(entry.scope)
                return None
            error = reply.get("error")
            if isinstance(error, str) and "busy" in error.lower():
                return f"{target.name}: busy"
            # This exact report will never be accepted; a changed row or a restarted daemon tries again. The
            # daemon's error text is never logged: it could quote the report, and the report carries the label.
            entry.rejected = True
            self.rejections += 1
            self.log(f"{NAME}: SidePulse rejected a {entry.body['status']['state']} report; it is resent when the row changes")
            return None
        return "; ".join(errors) if errors else "no receiver"


def new_request_id() -> str:
    return str(uuid.uuid4())
