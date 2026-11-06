"""Timing and size limits shared by the helper, the coordinator and the tests.

Kept free of imports so the per-snapshot helper never loads the coordinator just to parse them.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Limits:
    """Initial constants from the implementation packet; injectable for tests."""

    lease: float = 15.0
    reaper: float = 1.0
    done_notice: float = 15.0
    failed_notice: float = 30.0
    exit_grace: float = 5.0
    final_grace: float = 60.0
    discontinuity: float = 5.0
    max_observers: int = 64
    max_observations: int = 512
    max_tokens: int = 256
    max_request_bytes: int = 1024 * 1024
    read_deadline: float = 2.0

    def to_dict(self) -> dict:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    @classmethod
    def from_mapping(cls, data: object) -> "Limits":
        if not isinstance(data, dict):
            raise ValueError("limits must be a JSON object")
        values: dict = {}
        for name, value in data.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value < float("inf"):
                raise ValueError(f"limit {name} must be a positive finite number")
            values[name] = int(value) if name.startswith("max_") else float(value)
        return cls(**values)
