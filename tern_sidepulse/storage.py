"""Persistent bits: the stable bridge identity, user configuration, the disabled latch and a bounded log.

Only configuration and identity persist. Observations never touch disk, so a coordinator
restart cannot resurrect an old approval.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .paths import CONFIG_NAME, DISABLED_NAME, IDENTITY_NAME, LOG_NAME, PLUGIN_ID

MAX_LOG_BYTES = 1024 * 1024
SESSION_PREFIX = f"{PLUGIN_ID}:"


def _write_atomic(path: Path, text: str, mode: int = 0o600) -> None:
    temp = path.with_name(path.name + f".{os.getpid()}.tmp")
    with open(temp, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(temp, mode)
    os.replace(temp, path)


def load_identity(data_dir: Path, create: bool = True) -> dict | None:
    """The install identity (``identity.json``); created once, kept across reinstalls by Tern's data dir."""
    path = data_dir / IDENTITY_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("install_id"), str) and data["install_id"]:
            return data
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        if not create:
            return None
    if not create:
        return None
    data_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "install_id": str(uuid.uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "plugin": PLUGIN_ID,
    }
    _write_atomic(path, json.dumps(data, indent=2) + "\n", mode=0o644)
    return data


def session_id_for(identity: dict) -> str:
    return SESSION_PREFIX + identity["install_id"]


def load_config(data_dir: Path) -> dict:
    try:
        data = json.loads((data_dir / CONFIG_NAME).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, OSError, ValueError):
        return {}


def save_config(data_dir: Path, config: dict) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    _write_atomic(data_dir / CONFIG_NAME, json.dumps(config, indent=2, sort_keys=True) + "\n", mode=0o644)


def is_disabled(data_dir: Path) -> bool:
    return (data_dir / DISABLED_NAME).exists()


def set_disabled(data_dir: Path, disabled: bool) -> None:
    path = data_dir / DISABLED_NAME
    if disabled:
        data_dir.mkdir(parents=True, exist_ok=True)
        _write_atomic(path, datetime.now(timezone.utc).isoformat(timespec="seconds") + "\n", mode=0o644)
    else:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


class BoundedLog:
    """Append-only log in the runtime dir, rotated to ``.log.1`` whenever it exceeds ``MAX_LOG_BYTES``."""

    def __init__(self, runtime: Path, name: str = LOG_NAME, max_bytes: int = MAX_LOG_BYTES) -> None:
        self.path = runtime / name
        self.max_bytes = max_bytes
        self._handle = self._open()

    def _open(self):
        handle = open(self.path, "a", encoding="utf-8")
        os.chmod(self.path, 0o600)
        self._size = handle.tell()
        return handle

    def __call__(self, message: str) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        line = f"{stamp} [{os.getpid()}] {message}\n"
        try:
            if self._size > self.max_bytes:
                self._handle.close()
                os.replace(self.path, self.path.with_suffix(".log.1"))
                self._handle = self._open()
            self._handle.write(line)
            self._handle.flush()
            self._size += len(line.encode("utf-8"))
        except OSError:
            pass

    def close(self) -> None:
        try:
            self._handle.close()
        except OSError:
            pass
