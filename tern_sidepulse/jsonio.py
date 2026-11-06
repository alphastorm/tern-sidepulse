"""Strict JSON decoding for everything a client hands us: the helper's stdin and the coordinator socket."""
from __future__ import annotations

import json


def _reject_constant(name: str) -> None:
    raise ValueError(f"{name} is not allowed")


def load_json(raw: bytes) -> object:
    """UTF-8 JSON with finite numbers only; deep nesting is an error, never a RecursionError."""
    try:
        return json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ValueError("not valid UTF-8 JSON") from exc
