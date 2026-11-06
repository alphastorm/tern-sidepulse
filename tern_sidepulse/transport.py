"""SidePulse wire contract and per-receiver-family delivery with bounded retry.

This module owns the reduction of per-pane rows to one mode and dies with the hook protocol.
Verified against the inspected SidePulse revision (``docs/compatibility.md``): UTF-8 JSON on an
``AF_UNIX``/``SOCK_STREAM`` socket, one object per connection, framed by closing the write
side. The envelope is ``{"provider": "omp", "line": {...}}``; the receiver reads to EOF and
never acknowledges. A successful send is transport success, not proof of an LED change.
"""
from __future__ import annotations

import json
import os
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from . import VERSION
from .clock import Clock
from .reducer import BLOCKED_ERROR, COMPLETED, IDLE_READY, MODE_PRIORITY, MODES, WAITING_FOR_INPUT, WORKING, Row

PROVIDER = "omp"
SEND_TIMEOUT_SECONDS = 0.2  # upstream's per-attempt deadline
FAMILY_BUDGET_SECONDS = 0.3  # all candidates of one family, per tick
MIN_RETRY_SPACING = 1.0  # a mode change during an outage may not hammer the receiver
MAX_RETRY_SECONDS = 30.0
ACTIVE_REFRESH_SECONDS = 300.0
MAX_EVENT_BYTES = 1024 * 1024

# Canonical hook event per mode. ``sidepulse_status`` carries the explicit mode as well; the
# collector honours it for every event except ``Interrupt`` (always idle) and ignores a
# ``Notification`` after ``completed``, so neutral uses ``Interrupt`` and nothing uses
# ``Notification`` or ``SessionEnd``.
EVENT_FOR_MODE = {
    WORKING: "UserPromptSubmit",
    WAITING_FOR_INPUT: "PermissionRequest",
    BLOCKED_ERROR: "StopFailure",
    COMPLETED: "Stop",
    IDLE_READY: "Interrupt",
}
ACTIVE_MODES = frozenset({WORKING, WAITING_FOR_INPUT, BLOCKED_ERROR})
TITLE = "OMP in Tern"


def combined_mode(rows: Iterable[Row]) -> str:
    """The hook protocol's one display mode, neutral when no rows contribute."""
    best = IDLE_READY
    for row in rows:
        if MODE_PRIORITY[row.mode] < MODE_PRIORITY[best]:
            best = row.mode
    return best


def iso_utc(wall: float) -> str:
    return datetime.fromtimestamp(wall, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def build_line(mode: str, session_id: str, wall: float) -> dict:
    """The ``line`` object for one mode. Carries status metadata only: no cwd, model, prompts or text."""
    if mode not in MODES:
        raise ValueError("unknown mode")
    line = {
        "hook_event_name": EVENT_FOR_MODE[mode],
        "session_id": session_id,
        "logged_at": iso_utc(wall),
        "sidepulse_status": mode,
        "agent_origin": "Tern",
        "agent_origin_kind": "tern",
        "bridge": f"tern-sidepulse/{VERSION}",
    }
    if mode == WORKING:
        line["prompt"] = TITLE  # a constant label the collector uses as the display title
    return line


def encode_event(line: dict) -> bytes:
    return json.dumps({"provider": PROVIDER, "line": line}, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def send_payload(target: Path, payload: bytes, timeout: float = SEND_TIMEOUT_SECONDS) -> None:
    """One attempt at one socket within ``timeout`` overall (connect and send share it); raises OSError on failure."""
    deadline = time.monotonic() + timeout
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(str(target))
        client.settimeout(max(0.01, deadline - time.monotonic()))
        client.sendall(payload)
        try:
            client.shutdown(socket.SHUT_WR)
        except OSError:
            pass
    finally:
        client.close()


def receiver_signature(path: Path) -> tuple[int, float] | None:
    try:
        info = os.stat(path)
    except OSError:
        return None
    return (info.st_ino, info.st_ctime)


@dataclass
class Family:
    """One receiver family (``ui`` = menu-bar app, ``service`` = headless service)."""

    name: str
    candidates: tuple[Path, ...]
    delivered_mode: str | None = None
    delivered_at: float | None = None  # monotonic
    delivered_wall: float | None = None
    delivered_target: str | None = None
    pending: bool = False
    failures: int = 0
    next_attempt_at: float = 0.0
    last_error: str | None = None
    last_attempt_at: float | None = None
    signature: tuple[int, float] | None = None
    probe_signature: tuple | None = None
    sends: int = 0

    def receiver_present(self) -> bool:
        return any(path.exists() for path in self.candidates)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "candidates": [str(path) for path in self.candidates],
            "delivered_mode": self.delivered_mode,
            "delivered_wall": iso_utc(self.delivered_wall) if self.delivered_wall is not None else None,
            "delivered_target": self.delivered_target,
            "pending": self.pending,
            "failures": self.failures,
            "last_error": self.last_error,
            "sends": self.sends,
            "receiver_present": self.receiver_present(),
        }


@dataclass
class SidePulseTransport:
    """Tracks delivery per family so success at one receiver cannot hide a stale state at the other."""

    session_id: str
    clock: Clock
    families: list[Family] = field(default_factory=list)
    desired: str = IDLE_READY
    send_timeout: float = SEND_TIMEOUT_SECONDS
    family_budget: float = FAMILY_BUDGET_SECONDS
    min_retry_spacing: float = MIN_RETRY_SPACING
    max_retry: float = MAX_RETRY_SECONDS
    active_refresh: float = ACTIVE_REFRESH_SECONDS
    sender: Callable[[Path, bytes, float], None] | None = None  # test hook: (target, payload, timeout)
    log: Callable[[str], None] | None = None

    @classmethod
    def for_receivers(
        cls, session_id: str, clock: Clock, ui: tuple[Path, ...], service: tuple[Path, ...], **kw
    ) -> "SidePulseTransport":
        families = [Family("ui", tuple(ui)), Family("service", tuple(service))]
        return cls(session_id=session_id, clock=clock, families=families, **kw)

    def _log(self, message: str) -> None:
        if self.log:
            self.log(message)

    def publish(self, rows: Iterable[Row], *, force: bool = False) -> None:
        mode = combined_mode(rows)
        if mode != self.desired:
            self._log(f"desired {self.desired} -> {mode}")
        self.set_desired(mode, force=force)

    def set_desired(self, mode: str, force: bool = False) -> bool:
        """Point every family at ``mode``. A change goes out promptly; a family already in backoff keeps its schedule."""
        changed = mode != self.desired
        self.desired = mode
        now = self.clock.monotonic()
        for family in self.families:
            if changed or force:
                family.pending = True
                if family.failures == 0:
                    family.next_attempt_at = now
                else:
                    family.next_attempt_at = max(now, (family.last_attempt_at or now) + self.min_retry_spacing)
            elif family.delivered_mode != mode and not family.pending:
                family.pending = True
                family.next_attempt_at = now
        return changed

    def tick(self, immediate: bool = False) -> bool:
        """Attempt pending deliveries whose retry time has come (every pending one when ``immediate``);
        returns True when anything was attempted."""
        now = self.clock.monotonic()
        attempted = False
        for family in self.families:
            self._detect_receiver_change(family, now)
            if not family.pending or (now < family.next_attempt_at and not immediate):
                continue
            attempted = True
            family.last_attempt_at = now
            error = self._attempt(family)
            if error is None:
                if family.failures:
                    self._log(f"{family.name} recovered after {family.failures} failures")
                family.pending = False
                family.failures = 0
                family.last_error = None
                family.delivered_mode = self.desired
                family.delivered_at = self.clock.monotonic()
                family.delivered_wall = self.clock.wall()
                family.sends += 1
            else:
                family.failures += 1
                family.last_error = error
                family.probe_signature = tuple(receiver_signature(path) for path in family.candidates)
                family.next_attempt_at = self.clock.monotonic() + min(self.max_retry, float(2 ** min(family.failures, 6)))
                if family.failures == 1:
                    self._log(f"{family.name} delivery failing: {error}")
        return attempted

    def _attempt(self, family: Family) -> str | None:
        payload = encode_event(build_line(self.desired, self.session_id, self.clock.wall()))
        if len(payload) > MAX_EVENT_BYTES:
            return "payload too large"
        deadline = self.clock.monotonic() + self.family_budget
        errors: list[str] = []
        for target in family.candidates:
            remaining = deadline - self.clock.monotonic()
            if remaining <= 0:
                errors.append("delivery budget exhausted")
                break
            timeout = min(self.send_timeout, remaining)
            try:
                if self.sender is not None:
                    self.sender(target, payload, timeout)
                else:
                    send_payload(target, payload, timeout)
            except OSError as exc:
                errors.append(f"{target.name}: {exc.strerror or type(exc).__name__}")
                continue
            family.delivered_target = str(target)
            family.signature = receiver_signature(target)
            self._log(f"sent {self.desired} to {family.name} via {target.name}")
            return None
        return "; ".join(errors) if errors else "no receiver"

    def _detect_receiver_change(self, family: Family, now: float) -> None:
        """A receiver that appears or is replaced gets the current mode without waiting out a backoff or refresh."""
        if family.pending:
            if family.failures > 0 and now < family.next_attempt_at:
                current = tuple(receiver_signature(path) for path in family.candidates)
                if current != family.probe_signature:
                    family.probe_signature = current
                    family.next_attempt_at = now
                    self._log(f"{family.name}: receiver changed during backoff, retrying now")
            return
        if family.delivered_mode not in ACTIVE_MODES or family.delivered_target is None:
            return
        signature = receiver_signature(Path(family.delivered_target))
        replaced = signature is not None and signature != family.signature
        aged = family.delivered_at is not None and now - family.delivered_at >= self.active_refresh
        if replaced or aged:
            family.pending = True
            family.next_attempt_at = now
            self._log(f"refresh {family.name}: {'receiver replaced' if replaced else 'periodic'}")

    def all_delivered(self, mode: str) -> bool:
        return all(family.delivered_mode == mode and not family.pending for family in self.families)

    def undelivered_neutral(self) -> list[str]:
        return [
            family.name
            for family in self.families
            if family.delivered_mode not in (None, IDLE_READY)
            or (family.delivered_mode is None and family.pending and family.receiver_present())
        ]

    def neutral_delivered(self) -> bool:
        return self.all_delivered(IDLE_READY)

    def delivery_summary(self) -> dict:
        return {
            family.name: {"mode": family.delivered_mode, "pending": family.pending, "error": family.last_error}
            for family in self.families
        }

    def next_due(self) -> float | None:
        due = [family.next_attempt_at for family in self.families if family.pending]
        return min(due) if due else None

    def to_dict(self) -> dict:
        return {"desired": self.desired, "families": [family.to_dict() for family in self.families]}
