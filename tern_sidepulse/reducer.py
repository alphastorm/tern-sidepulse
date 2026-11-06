"""Tern observations → per-pane rows (mode + label)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

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

RowKey = tuple[str, str]
LABEL_MAX_CHARS = 96
_LABEL_DROPPED = re.compile("[\x00-\x1f\x7f-\x9f\ud800-\udfff\ue000-\uf8ff\U000f0000-\U0010ffff]")


@dataclass(frozen=True)
class Observation:
    pane: str
    state: str
    notice_id: str | None = None
    notice_kind: str | None = None
    host: str = "local"
    label: str | None = None

    @property
    def key(self) -> RowKey:
        return self.host, self.pane


@dataclass(frozen=True)
class Row:
    host: str
    pane: str
    mode: str
    label: str | None


def sanitize_label(raw: str) -> str | None:
    """Keep bounded display text without status glyphs, controls or lone surrogates."""
    text = _LABEL_DROPPED.sub("", " ".join(raw.split()))
    text = " ".join(text.split())[:LABEL_MAX_CHARS].rstrip()
    return text or None


def reduce_rows(contributions: Iterable[tuple[RowKey, str, int, str | None]]) -> tuple[Row, ...]:
    """Deduplicate row keys, keeping the highest mode and newest non-empty label."""
    reduced: dict[RowKey, tuple[str, int, str | None]] = {}
    for key, mode, order, label in contributions:
        best_mode, label_order, best_label = reduced.get(key, (mode, -1, None))
        if MODE_PRIORITY[mode] < MODE_PRIORITY[best_mode]:
            best_mode = mode
        if label is not None and order > label_order:
            label_order, best_label = order, label
        reduced[key] = best_mode, label_order, best_label
    return tuple(Row(host, pane, mode, label) for (host, pane), (mode, _, label) in sorted(reduced.items()))


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
