"""Monotonic and wall clocks, injectable for tests."""
from __future__ import annotations

import time


class Clock:
    """Reads both clocks; ``monotonic`` drives leases, ``wall`` stamps SidePulse events."""

    def monotonic(self) -> float:
        return time.monotonic()

    def wall(self) -> float:
        return time.time()


class FakeClock(Clock):
    def __init__(self, monotonic: float = 1000.0, wall: float = 1_800_000_000.0) -> None:
        self._monotonic = monotonic
        self._wall = wall

    def monotonic(self) -> float:
        return self._monotonic

    def wall(self) -> float:
        return self._wall

    def advance(self, seconds: float, wall_seconds: float | None = None) -> None:
        self._monotonic += seconds
        self._wall += seconds if wall_seconds is None else wall_seconds
