"""Coordinator process: singleton lock, private Unix socket, bounded reads, serialized handling."""
from __future__ import annotations

import errno
import fcntl
import json
import os
import selectors
import signal
import socket
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .coordinator import Coordinator, Limits, ProtocolError, decode_request
from .paths import LOCK_NAME, SOCKET_NAME, ensure_private_dir

RESPONSE_TIMEOUT = 0.5
MAX_CONNECTIONS = 32


class AlreadyRunning(Exception):
    pass


def acquire_lock(runtime: Path) -> int:
    """Hold the singleton lock for the process lifetime; never unlink a live lock file."""
    fd = os.open(runtime / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
            raise AlreadyRunning() from exc
        raise
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    return fd


def lock_holder_pid(runtime: Path) -> int | None:
    """The pid written by the live lock holder, or None when nobody holds the lock."""
    path = runtime / LOCK_NAME
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            text = os.read(fd, 32).decode("ascii", "replace").strip()
            return int(text) if text.isdigit() else -1
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    finally:
        os.close(fd)


def bind_socket(runtime: Path) -> socket.socket:
    """Bind under a temporary name, then rename over the canonical path (the lock proves any old file is stale)."""
    path = runtime / SOCKET_NAME
    temp = runtime / f"{SOCKET_NAME}.{os.getpid()}"
    try:
        temp.unlink()
    except FileNotFoundError:
        pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(temp))
    os.chmod(temp, 0o600)
    os.replace(temp, path)
    server.listen(16)
    server.setblocking(False)
    return server


@dataclass
class Connection:
    sock: socket.socket
    deadline: float
    buffer: bytearray = field(default_factory=bytearray)


class Server:
    def __init__(self, coordinator: Coordinator, runtime: Path, limits: Limits, log: Callable[[str], None], lock_fd: int) -> None:
        self.coordinator = coordinator
        self.runtime = runtime
        self.limits = limits
        self.log = log
        self.lock_fd = lock_fd
        self.server = bind_socket(runtime)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.server, selectors.EVENT_READ)
        self.connections: dict[socket.socket, Connection] = {}
        self.socket_path = runtime / SOCKET_NAME

    def install_signals(self) -> None:
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda *_: self.coordinator.request_stop("signal"))
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        signal.signal(signal.SIGPIPE, signal.SIG_IGN)

    def run(self, install_signals: bool = True) -> int:
        if install_signals:
            self.install_signals()
        self.log(f"coordinator listening on {self.socket_path} epoch {self.coordinator.epoch}")
        try:
            while True:
                now = self.coordinator.clock.monotonic()
                timeout = self.coordinator.wake_after(now)
                for conn in self.connections.values():
                    timeout = min(timeout, max(0.0, conn.deadline - now))
                for key, _mask in self.selector.select(timeout):
                    if key.fileobj is self.server:
                        self.accept()
                    else:
                        conn = self.connections.get(key.fileobj)  # type: ignore[arg-type]
                        if conn is not None:
                            self.read(conn)
                self.reap_connections()
                if self.coordinator.tick() == "exit":
                    break
        finally:
            self.close()
        return 0

    def close(self) -> None:
        self.coordinator.shutdown()
        for conn in list(self.connections.values()):
            self.drop(conn)
        self.selector.close()
        try:
            self.server.close()
        except OSError:
            pass
        try:
            if self.socket_path.is_socket():
                self.socket_path.unlink()
        except OSError:
            pass
        self.log(f"coordinator exiting ({self.coordinator.stop_reason or 'drained'})")
        os.close(self.lock_fd)

    def accept(self) -> None:
        try:
            sock, _ = self.server.accept()
        except OSError:
            return
        sock.setblocking(False)
        conn = Connection(sock, self.coordinator.clock.monotonic() + self.limits.read_deadline)
        if len(self.connections) >= MAX_CONNECTIONS:
            self.respond(conn, ProtocolError("busy", "too many concurrent clients").to_dict())
            return
        self.connections[sock] = conn
        self.selector.register(sock, selectors.EVENT_READ)

    def read(self, conn: Connection) -> None:
        try:
            chunk = conn.sock.recv(65536)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self.drop(conn)
            return
        if chunk:
            conn.buffer.extend(chunk)
            if len(conn.buffer) > self.limits.max_request_bytes:
                self.respond(conn, ProtocolError("oversized", "request exceeds the size limit", limit=self.limits.max_request_bytes).to_dict())
            return
        # EOF frames the request.
        if not conn.buffer:
            self.respond(conn, ProtocolError("empty_request", "no bytes before EOF").to_dict())
            return
        try:
            request = decode_request(bytes(conn.buffer))
        except ProtocolError as exc:
            self.respond(conn, exc.to_dict())
            return
        self.respond(conn, self.coordinator.handle(request))

    def respond(self, conn: Connection, response: dict) -> None:
        payload = json.dumps(response, separators=(",", ":")).encode("utf-8")
        try:
            conn.sock.setblocking(True)
            conn.sock.settimeout(RESPONSE_TIMEOUT)
            conn.sock.sendall(payload)
        except OSError:
            pass
        self.drop(conn)

    def reap_connections(self) -> None:
        now = self.coordinator.clock.monotonic()
        for conn in list(self.connections.values()):
            if now >= conn.deadline:
                self.log("dropped a client that did not finish its request in time")
                self.drop(conn)

    def drop(self, conn: Connection) -> None:
        self.connections.pop(conn.sock, None)
        try:
            self.selector.unregister(conn.sock)
        except (KeyError, ValueError):
            pass
        try:
            conn.sock.close()
        except OSError:
            pass


def run_coordinator(
    *,
    runtime: Path,
    data_dir: Path,
    limits: Limits,
    ui_sockets: tuple[Path, ...],
    service_sockets: tuple[Path, ...],
) -> int:
    """Lock first; everything that may write to disk or SidePulse happens under the lock."""
    from .clock import Clock
    from .storage import BoundedLog, is_disabled, load_identity, session_id_for, set_disabled
    from .transport import SidePulseTransport

    ensure_private_dir(runtime)
    try:
        lock_fd = acquire_lock(runtime)
    except AlreadyRunning:
        return 0
    early_stop: list[str] = []
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: early_stop.append("signal"))
    log = BoundedLog(runtime)
    try:
        identity = load_identity(data_dir, create=True)
        assert identity is not None
        clock = Clock()
        transport = SidePulseTransport.for_receivers(session_id_for(identity), clock, ui_sockets, service_sockets, log=log)
        coordinator = Coordinator(
            clock=clock,
            transport=transport,
            limits=limits,
            disabled=is_disabled(data_dir),
            persist_disabled=lambda flag: set_disabled(data_dir, flag),
            log=log,
        )
        server = Server(coordinator, runtime, limits, log, lock_fd=lock_fd)
        if early_stop:
            coordinator.request_stop("signal")
        return server.run()
    except Exception as exc:  # noqa: BLE001 - log the crash site, never a payload
        frame = traceback.extract_tb(exc.__traceback__)[-1] if exc.__traceback__ else None
        where = f"{os.path.basename(frame.filename)}:{frame.lineno}" if frame else "?"
        log(f"coordinator crashed: {type(exc).__name__} at {where}")
        raise
    finally:
        log.close()
