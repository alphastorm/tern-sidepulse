"""SPIKE (never merged): per-row delivery to SidePulse #72's program-status socket (inteliwear/sidepulse#72 @ 337c703).

One report per row: ``{"v":1,"type":"program_status","scope":…,"status":{…},"context":{…},"request_id":…}`` on its
own connection, half-closed; the daemon answers ``{"ok":true,"seq":…,"revision":…}``. Rows live only in the
daemon's memory and never expire unless they are ``done``, so this transport keeps a write-ahead ledger of every
scope it may have made visible and clears each one it no longer wants, including a crashed predecessor's.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import socket
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .clock import Clock
from .reducer import BLOCKED_ERROR, COMPLETED, IDLE_READY, MODE_PRIORITY, WAITING_FOR_INPUT, WORKING, Row

NAME = "program_status"
STATE_FOR_MODE = {
    WORKING: "working",
    WAITING_FOR_INPUT: "blocked",
    BLOCKED_ERROR: "error",
    COMPLETED: "done",
    IDLE_READY: "idle",
}
MAX_ROWS = 32  # SidePulse keeps 64 rows across every provider and evicts the oldest silently
MAX_SENDS_PER_TICK = 16
TICK_BUDGET_SECONDS = 0.3
SEND_TIMEOUT_SECONDS = 0.2
MAX_RETRY_SECONDS = 30.0
REFRESH_SECONDS = 300.0
MAX_STATUS_TITLE_BYTES = 192  # a longer status.title makes the daemon reject the whole report
MAX_LEDGER = 256
FALLBACK_NAME = "OMP in Tern"


def program_socket_for_dir(state_dir: Path) -> Path:
    """Upstream ``status_socket_path``: a /tmp fallback when the path exceeds 96 bytes."""
    path = state_dir / "program-status.sock"
    if len(str(path).encode()) > 96:
        digest = hashlib.sha256(str(path).encode()).hexdigest()[:16]
        return Path("/tmp") / f"sidepulse-status-{os.getuid()}" / f"{digest}.sock"
    return path


def scope_digest(salt: str, row: Row) -> str:
    return hashlib.sha256(f"{salt}\0{row.host}\0{row.pane}".encode("utf-8", "surrogatepass")).hexdigest()[:24]


def cut_bytes(text: str, limit: int) -> str:
    return text.encode("utf-8")[:limit].decode("utf-8", errors="ignore").rstrip()


def report_for(row: Row, digest: str) -> dict:
    status: dict = {"state": STATE_FOR_MODE[row.mode]}
    context: dict = {"provider": "omp", "origin": "Tern", "session_id": digest}
    if row.label:
        status["title"] = cut_bytes(row.label, MAX_STATUS_TITLE_BYTES)
        context["title"] = row.label
        context["title_source"] = "session"  # otherwise the daemon keeps the first title for ever
    else:
        context["display_name"] = FALLBACK_NAME
    return {"v": 1, "type": "program_status", "scope": "tern-" + digest, "status": status, "context": context}


def clear_for(scope: str) -> dict:
    return {"v": 1, "type": "program_status", "scope": scope, "status": {"state": "clear"}}


def exchange(target: Path, payload: bytes, timeout: float) -> dict:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(str(target))
        client.sendall(payload)
        client.shutdown(socket.SHUT_WR)
        data = bytearray()
        while chunk := client.recv(65536):
            data.extend(chunk)
            if len(data) > 1024 * 1024:
                raise OSError(errno.EMSGSIZE, "reply too large")
    finally:
        client.close()
    reply = json.loads(bytes(data) or b"null")
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
    scope: str
    body: dict
    priority: int
    request_id: str
    pending: bool = True
    clearing: bool = False
    delivered_at: float | None = None
    rejected: bool = False


class ScopeLedger:
    """Scopes that may be visible in SidePulse; written before the first report for a scope, pruned after its clear."""

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            scopes = data.get("scopes") if isinstance(data, dict) else None
        except (OSError, ValueError):
            scopes = None
        self.scopes: set[str] = {s for s in scopes or () if isinstance(s, str) and s.startswith("tern-") and len(s) <= 128}

    def save(self) -> None:
        temp = self.path.with_name(self.path.name + f".{os.getpid()}.tmp")
        temp.write_text(json.dumps({"scopes": sorted(self.scopes)}), encoding="utf-8")
        os.chmod(temp, 0o600)
        os.replace(temp, self.path)


class ProgramStatusTransport:
    """Implements the coordinator's transport boundary for SidePulse #72."""

    def __init__(
        self,
        *,
        session_id: str,
        salt: str,
        clock: Clock,
        candidates: tuple[Path, ...],
        ledger_path: Path,
        log: Callable[[str], None] | None = None,
        sender: Callable[[Path, bytes, float], dict] | None = None,
        max_rows: int = MAX_ROWS,
    ) -> None:
        self.session_id = session_id
        self.salt = salt
        self.clock = clock
        self.candidates = candidates
        self.ledger = ScopeLedger(ledger_path)
        self.log = log or (lambda _m: None)
        self.sender = sender or exchange
        self.max_rows = max_rows
        self.entries: dict[str, Entry] = {}
        self.failures = 0
        self.next_attempt_at = 0.0
        self.last_error: str | None = None
        self.target: Path | None = None
        self.target_signature: tuple[int, float] | None = None
        self.sends = 0
        self.rows_capped = 0
        # A predecessor's rows: clear every scope it may have left visible.
        for scope in sorted(self.ledger.scopes):
            self.entries[scope] = Entry(scope, clear_for(scope), -1, str(uuid.uuid4()), clearing=True)

    # ---- boundary -------------------------------------------------------------------------

    def publish(self, rows, *, force: bool = False) -> None:
        ordered = sorted(rows, key=lambda r: (MODE_PRIORITY[r.mode], r.host, r.pane))
        self.rows_capped = max(0, len(ordered) - self.max_rows)
        wanted: dict[str, tuple[dict, int]] = {}
        for row in ordered[: self.max_rows]:
            digest = scope_digest(self.salt, row)
            wanted["tern-" + digest] = (report_for(row, digest), MODE_PRIORITY[row.mode])
        for scope, (body, priority) in wanted.items():
            entry = self.entries.get(scope)
            if entry is None or entry.clearing or entry.body != body:
                self.entries[scope] = Entry(scope, body, priority, str(uuid.uuid4()))
            elif force:
                entry.pending = True
        for scope, entry in self.entries.items():
            if scope not in wanted and not entry.clearing:
                entry.body, entry.priority, entry.request_id = clear_for(scope), -1, str(uuid.uuid4())
                entry.clearing, entry.pending, entry.rejected = True, True, False

    def tick(self, immediate: bool = False) -> bool:
        now = self.clock.monotonic()
        self._detect_replacement(now)
        due = [e for e in self.entries.values() if e.pending and not e.rejected]
        if not due or (now < self.next_attempt_at and not immediate):
            return False
        new_scopes = {e.scope for e in due if not e.clearing} - self.ledger.scopes
        if new_scopes:
            self.ledger.scopes |= new_scopes
            if len(self.ledger.scopes) > MAX_LEDGER:
                self.log("program_status: ledger over its cap; some scopes may need a SidePulse restart to clear")
            try:
                self.ledger.save()
            except OSError as exc:
                self.last_error = f"ledger: {exc.strerror or type(exc).__name__}"
                return False  # never show a row the next coordinator could not clear
        deadline = now + TICK_BUDGET_SECONDS
        sent = 0
        for entry in sorted(due, key=lambda e: e.priority):
            if sent >= MAX_SENDS_PER_TICK or self.clock.monotonic() >= deadline:
                break
            error = self._send(entry)
            sent += 1
            if error is not None:
                self.failures += 1
                self.last_error = error
                self.next_attempt_at = self.clock.monotonic() + min(MAX_RETRY_SECONDS, float(2 ** min(self.failures, 5)))
                if self.failures == 1:
                    self.log(f"program_status delivery failing: {error}")
                break
        else:
            if self.failures:
                self.log(f"program_status recovered after {self.failures} failures")
            self.failures, self.last_error = 0, None
        return True

    def next_due(self) -> float | None:
        if any(e.pending and not e.rejected for e in self.entries.values()):
            return self.next_attempt_at
        live = [e.delivered_at for e in self.entries.values() if e.delivered_at is not None and not e.clearing]
        return min(live) + REFRESH_SECONDS if live else None

    def undelivered_neutral(self) -> list[str]:
        receiver = any(path.exists() for path in self.candidates)
        return [NAME] if receiver and (self.entries or self.ledger.scopes) else []

    def neutral_delivered(self) -> bool:
        return not self.entries and not self.ledger.scopes

    def delivery_summary(self) -> dict:
        shown = sum(1 for e in self.entries.values() if not e.clearing and not e.pending)
        pending = sum(1 for e in self.entries.values() if e.pending)
        return {NAME: {"rows": shown, "pending": pending, "error": self.last_error}}

    def to_dict(self) -> dict:
        return {
            "kind": NAME,
            "candidates": [str(p) for p in self.candidates],
            "target": str(self.target) if self.target else None,
            "rows": sum(1 for e in self.entries.values() if not e.clearing),
            "clearing": sum(1 for e in self.entries.values() if e.clearing),
            "pending": sum(1 for e in self.entries.values() if e.pending),
            "rejected": sum(1 for e in self.entries.values() if e.rejected),
            "ledger": len(self.ledger.scopes),
            "rows_capped": self.rows_capped,
            "failures": self.failures,
            "last_error": self.last_error,
            "sends": self.sends,
        }

    # ---- internals ------------------------------------------------------------------------

    def _detect_replacement(self, now: float) -> None:
        """A restarted daemon starts empty: resubmit every live row; refresh live rows every REFRESH_SECONDS."""
        if self.target is not None:
            current = signature(self.target)
            if current is not None and current != self.target_signature:
                self.target_signature = current
                for entry in self.entries.values():
                    if not entry.clearing:
                        entry.pending = True
                self.next_attempt_at = now
                self.log("program_status: receiver replaced, resubmitting every row")
        for entry in self.entries.values():
            if not entry.clearing and entry.delivered_at is not None and now - entry.delivered_at >= REFRESH_SECONDS:
                entry.pending = True

    def _send(self, entry: Entry) -> str | None:
        body = dict(entry.body, request_id=entry.request_id)
        payload = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
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
                if entry.clearing:
                    del self.entries[entry.scope]
                    self.ledger.scopes.discard(entry.scope)
                    try:
                        self.ledger.save()
                    except OSError:
                        pass
                return None
            error = str(reply.get("error") or "rejected")[:120]
            if "busy" in error.lower():
                return f"{target.name}: busy"
            entry.rejected = True  # this exact body will never be accepted; a changed row retries
            self.log(f"program_status rejected a report: {error}")
            return None
        return "; ".join(errors) if errors else "no receiver"
