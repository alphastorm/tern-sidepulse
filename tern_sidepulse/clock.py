"""Monotonic and wall clocks, injectable for tests."""
from __future__ import annotations

import time


class Clock:
    """Reads both clocks; ``monotonic`` drives leases, ``wall`` stamps SidePulse events."""

    def monotonic(self) -> float:
        return time.monotonic()

    def wall(self) -> float:
        return time.time()
