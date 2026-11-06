"""Persistent bits: the stable bridge identity, user configuration, the disabled latch and a bounded log.

Only configuration and identity persist. Observations never touch disk, so a coordinator
restart cannot resurrect an old approval.
"""
from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path

from .paths import CONFIG_NAME, DISABLED_NAME, IDENTITY_NAME, LOG_NAME, PLUGIN_ID

MAX_LOG_BYTES = 1024 * 1024
MAX_CONFIG_BYTES = 65536  # the window half reads it with the same cap
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
    import uuid  # only the first coordinator start ever pays for this import

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
    """``config.json`` as the window half sees it: a regular, non-symlink file of at most 64 KiB, else empty."""
    try:
        fd = os.open(data_dir / CONFIG_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return {}
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return {}
        raw = os.read(fd, MAX_CONFIG_BYTES + 1)
    except OSError:
        return {}
    finally:
        os.close(fd)
    if len(raw) > MAX_CONFIG_BYTES:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


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
        handle = open(self.path, "a", encoding="utf-8", errors="backslashreplace")
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
        except (OSError, ValueError):  # ValueError: a rotation that failed left the handle closed
            pass

    def close(self) -> None:
        try:
            self._handle.close()
        except OSError:
            pass
