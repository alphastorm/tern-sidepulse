"""Status semantics: Tern observations → one SidePulse mode.

Applied per observer and then across observers. Adding the same observation twice never
changes the result: this is display-level duplicate tolerance, not cross-window identity.
"""
from __future__ import annotations

from dataclasses import dataclass

# SidePulse modes this bridge emits, highest priority first.
BLOCKED_ERROR = "blocked_error"
WAITING_FOR_INPUT = "waiting_for_input"
WORKING = "working"
COMPLETED = "completed"
IDLE_READY = "idle_ready"

MODE_PRIORITY = {BLOCKED_ERROR: 0, WAITING_FOR_INPUT: 1, WORKING: 2, COMPLETED: 3, IDLE_READY: 4}
MODES = tuple(MODE_PRIORITY)

# Tern agent states the observer forwards.
STATE_WORKING = "working"
STATE_WAITING = "waiting_input"
STATE_IDLE = "idle"
WITHDRAWN_STATES = frozenset({"exited", "closed", "unknown"})
KNOWN_STATES = frozenset({STATE_WORKING, STATE_WAITING, STATE_IDLE}) | WITHDRAWN_STATES

NOTICE_DONE = "done"
NOTICE_FAILED = "failed"
NOTICE_KINDS = frozenset({NOTICE_DONE, NOTICE_FAILED})


@dataclass(frozen=True)
class Observation:
    pane: str
    state: str
    notice_id: str | None = None
    notice_kind: str | None = None


def pane_mode(state: str, active_notice_kind: str | None) -> str | None:
    """The mode one pane contributes, or None when it is withdrawn.

    ``active_notice_kind`` is the outcome notice that is still within its deadline; the
    coordinator decides that, the reducer only applies it. Notices never outrank live work.
    """
    if state == STATE_WORKING:
        return WORKING
    if state == STATE_WAITING:
        return WAITING_FOR_INPUT
    if state == STATE_IDLE:
        if active_notice_kind == NOTICE_FAILED:
            return BLOCKED_ERROR
        if active_notice_kind == NOTICE_DONE:
            return COMPLETED
        return IDLE_READY
    return None


def notice_clears(state: str) -> bool:
    """Working or waiting states clear a pane's outcome notice at once."""
    return state in (STATE_WORKING, STATE_WAITING)


def aggregate(modes: list[str | None]) -> str:
    best = IDLE_READY
    for mode in modes:
        if mode is not None and MODE_PRIORITY[mode] < MODE_PRIORITY[best]:
            best = mode
    return best
