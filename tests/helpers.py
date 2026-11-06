"""Shared fixtures: fake SidePulse receivers, in-memory coordinators, and subprocess helpers."""
from __future__ import annotations

import atexit
import json
import os
import socket
import subprocess
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BRIDGE = ROOT / "bridge.py"
sys.path.insert(0, str(ROOT))

from tern_sidepulse.clock import Clock  # noqa: E402
from tern_sidepulse.coordinator import Coordinator  # noqa: E402
from tern_sidepulse.limits import Limits  # noqa: E402
from tern_sidepulse.paths import python_flags  # noqa: E402
from tern_sidepulse.transport import SidePulseTransport  # noqa: E402

SENTINEL = "SENTINEL-7f3a-never-leaks"


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


def short_tmpdir(prefix: str = "tsp-") -> Path:
    """A short path under /tmp so AF_UNIX names stay well under the macOS limit; removed when the run ends."""
    path = Path(tempfile.mkdtemp(prefix=prefix, dir="/tmp"))
    atexit.register(shutil.rmtree, path, ignore_errors=True)
    return path


class FakeReceiver(threading.Thread):
    """An AF_UNIX listener that reads each connection to EOF and records decoded payloads."""

    def __init__(self, path: Path, hang: bool = False) -> None:
        super().__init__(daemon=True)
        self.path = path
        self.events: list[dict] = []
        self.times: list[float] = []
        self._stop = threading.Event()
        self.hang = hang
        self.server: socket.socket | None = None
        self._bind()

    def _bind(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.path))
        server.listen(0 if self.hang else 8)
        server.settimeout(0.1)
        self.server = server

    def run(self) -> None:
        while not self._stop.is_set():
            server = self.server
            if server is None or self.hang:
                time.sleep(0.05)
                continue
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                continue
            with conn:
                conn.settimeout(2.0)
                chunks = []
                while True:
                    try:
                        chunk = conn.recv(65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    chunks.append(chunk)
            try:
                self.events.append(json.loads(b"".join(chunks).decode("utf-8")))
                self.times.append(time.monotonic())
            except ValueError:
                self.events.append({"bad": True})
                self.times.append(time.monotonic())

    def modes(self) -> list[str]:
        return [event["line"]["sidepulse_status"] for event in self.events if "line" in event]

    def stop(self) -> None:
        self._stop.set()
        server = self.server
        self.server = None
        if server is not None:
            server.close()
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def restart(self) -> None:
        """Rebind at the same path: a new inode, as a restarted SidePulse would produce."""
        server = self.server
        self.server = None
        if server is not None:
            server.close()
        time.sleep(0.02)
        self._bind()

    def wait_for(self, mode: str, timeout: float = 5.0, minimum: int = 1) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            modes = self.modes()
            if modes.count(mode) >= minimum and modes[-1] == mode:
                return True
            time.sleep(0.02)
        return False


class RecordingSender:
    """In-process transport sink. A target delivers only while its file exists (``up``/``down``)."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.sent: list[tuple[str, str]] = []
        self.clock: FakeClock | None = None
        self.cost = 0.0  # fake seconds each attempt costs (hung receiver)
        self.refuse: set[str] = set()  # present but refusing (a receiver restarting)

    def path(self, name: str) -> Path:
        return self.root / name

    def up(self, name: str) -> None:
        self.refuse.discard(name)
        if not self.path(name).exists():
            self.path(name).write_text("")

    def down(self, name: str) -> None:
        try:
            self.path(name).unlink()
        except FileNotFoundError:
            pass

    def __call__(self, target: Path, payload: bytes, timeout: float) -> None:
        if self.clock is not None and self.cost:
            self.clock.advance(min(self.cost, timeout))
        if not target.exists():
            raise FileNotFoundError(2, "No such file or directory")
        if target.name in self.refuse:
            raise ConnectionRefusedError(61, "Connection refused")
        line = json.loads(payload.decode("utf-8"))["line"]
        self.sent.append((target.name, line["sidepulse_status"]))

    def modes_for(self, name: str) -> list[str]:
        return [mode for target, mode in self.sent if target == name]


def make_coordinator(limits: Limits | None = None, disabled: bool = False, clock: FakeClock | None = None, receivers_up: bool = True):
    clock = clock or FakeClock()
    root = short_tmpdir("tsp-rx-")
    sender = RecordingSender(root)
    sender.clock = clock
    if receivers_up:
        sender.up("events.sock")
        sender.up("relay-events.sock")
    logs: list[str] = []
    transport = SidePulseTransport.for_receivers(
        "tern-sidepulse:test", clock, (root / "events.sock",), (root / "relay-events.sock",), sender=sender, log=logs.append
    )
    latch: list[bool] = []
    coordinator = Coordinator(
        clock=clock,
        transport=transport,
        limits=limits or Limits(),
        disabled=disabled,
        persist_disabled=latch.append,
        log=logs.append,
    )
    return coordinator, clock, sender, logs, latch


def advance(coordinator: Coordinator, clock: FakeClock, seconds: float, step: float = 1.0) -> str | None:
    """Advance the fake clock the way the serve loop experiences time: a reaper tick at least every ``step``."""
    result = None
    remaining = seconds
    while remaining > 0:
        chunk = min(step, remaining)
        clock.advance(chunk)
        remaining -= chunk
        result = coordinator.tick() or result
    return result


def combined_mode(coordinator: Coordinator) -> str:
    return coordinator.transport.desired


def rows(coordinator: Coordinator) -> list[tuple[str, str, str, str | None]]:
    return [(row.host, row.pane, row.mode, row.label) for row in coordinator.rows]


OBS = {"parent_pid": 4242, "window_key": ""}


def register(coordinator: Coordinator, observer: dict | None = None) -> dict:
    return coordinator.handle({"schema": 1, "op": "register", "observer": observer or OBS})


def snapshot(coordinator: Coordinator, generation: int, epoch: str, seq: int, observations: list, *, observer: dict | None = None, coverage: str = "complete", **extra) -> dict:
    request = {
        "schema": 1,
        "op": "snapshot",
        "observer": observer or OBS,
        "epoch": epoch,
        "generation": generation,
        "seq": seq,
        "coverage": coverage,
        "observations": observations,
    }
    request.update(extra)
    return coordinator.handle(request)


def activate(coordinator: Coordinator, observations: list, observer: dict | None = None) -> tuple[int, str]:
    reg = register(coordinator, observer)
    ack = snapshot(coordinator, reg["generation"], reg["epoch"], 1, observations, observer=observer)
    assert ack["ok"], ack
    return reg["generation"], reg["epoch"]


def working(pane: int, *, host: str | None = None, label: str | None = None) -> dict:
    obs = {"pane": pane, "state": "working"}
    if host is not None:
        obs["host"] = host
    if label is not None:
        obs["label"] = label
    return obs


def idle(pane: int, notice_id: str | None = None, kind: str = "done", *, host: str | None = None, label: str | None = None) -> dict:
    obs: dict = {"pane": pane, "state": "idle"}
    if notice_id is not None:
        obs["notice"] = {"id": notice_id, "kind": kind}
    if host is not None:
        obs["host"] = host
    if label is not None:
        obs["label"] = label
    return obs


def waiting(pane: int, *, host: str | None = None, label: str | None = None) -> dict:
    obs = {"pane": pane, "state": "waiting_input"}
    if host is not None:
        obs["host"] = host
    if label is not None:
        obs["label"] = label
    return obs


def helper_argv(env: dict) -> list[str]:
    """The interpreter invocation the window half uses, bytecode cached under the test data dir."""
    data = env.get("TERN_PLUGIN_DATA")
    return [sys.executable, *python_flags(Path(data) if data else None), str(BRIDGE)]


def run_bridge(args: list[str], stdin: dict | None, env: dict, timeout: float = 15.0, *, raw_utf8: bool = False) -> tuple[int, dict, str]:
    """Run bridge.py in a subprocess; returns (code, parsed stdout, stderr). Non-JSON output becomes an error dict."""
    proc = subprocess.run(
        [*helper_argv(env), *args],
        input=json.dumps(stdin or {}, ensure_ascii=not raw_utf8).encode("utf-8"),
        capture_output=True,
        env=env,
        timeout=timeout,
    )
    out = proc.stdout.decode("utf-8").strip()
    try:
        parsed = json.loads(out)
        if not isinstance(parsed, dict):
            parsed = {"ok": False, "error": "non_object", "raw": out}
    except ValueError:
        parsed = {"ok": False, "error": "non_json", "raw": out}
    return proc.returncode, parsed, proc.stderr.decode("utf-8")


def subprocess_env(tmp: Path, ui: Path, service: Path, limits: dict | None = None) -> dict:
    env = dict(os.environ)
    env.pop("TERN_WINDOW_KEY", None)
    env.update(
        {
            "TERN_PLUGIN_DATA": str(tmp / "data"),
            "TERN_SIDEPULSE_RUNTIME_DIR": str(tmp / "rt"),
            "TERN_SIDEPULSE_UI_SOCKETS": str(ui),
            "TERN_SIDEPULSE_SERVICE_SOCKETS": str(service),
        }
    )
    if limits:
        env["TERN_SIDEPULSE_LIMITS"] = json.dumps(limits)
    return env


def kill_coordinator(env: dict) -> None:
    runtime = Path(env["TERN_SIDEPULSE_RUNTIME_DIR"])
    lock = runtime / "coordinator.lock"
    try:
        pid = int(lock.read_text().strip() or "0")
    except (OSError, ValueError):
        return
    if pid > 1:
        try:
            os.kill(pid, 9)
        except OSError:
            pass
