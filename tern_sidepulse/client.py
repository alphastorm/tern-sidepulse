"""Short-lived client side: one request per connection, spawning a detached coordinator when none answers."""
from __future__ import annotations

import errno
import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .coordinator import Limits
from .paths import SOCKET_NAME

MAX_RESPONSE_BYTES = 1024 * 1024
MAX_SPAWNS = 2
SPAWN_SPACING = 0.3


@dataclass(frozen=True)
class Settings:
    data_dir: Path | None
    runtime_dir: Path
    ui_sockets: tuple[Path, ...]
    service_sockets: tuple[Path, ...]
    limits: Limits
    bridge_path: Path
    limits_json: str | None = None

    @property
    def socket_path(self) -> Path:
        return self.runtime_dir / SOCKET_NAME


class CoordinatorUnavailable(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def exchange(sock_path: Path, data: bytes, timeout: float) -> dict:
    """Connect, send, half-close, read to EOF. Raises OSError on transport failure."""
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(str(sock_path))
        client.sendall(data)
        client.shutdown(socket.SHUT_WR)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                raise OSError(errno.EMSGSIZE, "response too large")
            chunks.append(chunk)
    finally:
        client.close()
    raw = b"".join(chunks)
    if not raw:
        raise OSError(errno.ECONNRESET, "empty response")
    response = json.loads(raw.decode("utf-8"))
    if not isinstance(response, dict):
        raise OSError(errno.EBADMSG, "response is not an object")
    return response


def coordinator_argv(settings: Settings) -> list[str]:
    argv = [sys.executable, "-I", "-B", str(settings.bridge_path), "coordinator", "--runtime-dir", str(settings.runtime_dir)]
    if settings.data_dir is not None:
        argv += ["--data-dir", str(settings.data_dir)]
    for path in settings.ui_sockets:
        argv += ["--ui-socket", str(path)]
    for path in settings.service_sockets:
        argv += ["--service-socket", str(path)]
    if settings.limits_json:
        argv += ["--limits", settings.limits_json]
    return argv


def spawn_coordinator(settings: Settings) -> int:
    """Start the coordinator detached: new session, no inherited pipes, reparented to launchd."""
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    child = subprocess.Popen(
        coordinator_argv(settings),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
        cwd="/",
        env=env,
    )
    return child.pid


def request(settings: Settings, payload: dict, *, spawn: bool, total_timeout: float = 2.5) -> dict:
    """Send one request, spawning the coordinator when nothing listens. Raises CoordinatorUnavailable."""
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    deadline = time.monotonic() + total_timeout
    spawns = 0
    last_spawn = 0.0
    last_error = "unknown"
    attempt = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CoordinatorUnavailable(last_error)
        try:
            return exchange(settings.socket_path, data, min(1.0, max(0.1, remaining)))
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            last_error = errno.errorcode.get(exc.errno or 0, type(exc).__name__)
            if not spawn:
                raise CoordinatorUnavailable("not_running") from exc
            now = time.monotonic()
            if spawns < MAX_SPAWNS and now - last_spawn >= SPAWN_SPACING:
                spawn_coordinator(settings)
                spawns += 1
                last_spawn = now
        except (OSError, ValueError) as exc:
            # Sent but no usable answer: the same bytes are safe to repeat (duplicate acks, idempotent ops).
            last_error = errno.errorcode.get(getattr(exc, "errno", 0) or 0, type(exc).__name__)
        time.sleep(min(0.05 * (2**attempt), 0.4))
        attempt += 1
