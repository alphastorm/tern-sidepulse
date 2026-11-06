import json
import socket
import subprocess
import threading
import time
import unittest
from pathlib import Path

from tests.helpers import FakeReceiver, RecordingSender, helper_argv, short_tmpdir, subprocess_env

from tern_sidepulse.clock import Clock
from tern_sidepulse.coordinator import Coordinator
from tern_sidepulse.limits import Limits
from tern_sidepulse.server import MAX_CONNECTIONS, Server, acquire_lock
from tern_sidepulse.transport import SidePulseTransport


class ServerFixture:
    """A real Server on a real clock in a thread; receivers are in-process recorders."""

    def __init__(self, limits: Limits) -> None:
        self.runtime = short_tmpdir("tsp-srv-")
        self.sender = RecordingSender(self.runtime)
        self.sender.up("events.sock")
        self.sender.up("relay-events.sock")
        self.logs: list[str] = []
        transport = SidePulseTransport.for_receivers(
            "tern-sidepulse:test", Clock(), (self.runtime / "events.sock",), (self.runtime / "relay-events.sock",), sender=self.sender, log=self.logs.append
        )
        self.coordinator = Coordinator(clock=Clock(), transport=transport, limits=limits, disabled=False, persist_disabled=lambda _f: None, log=self.logs.append)
        self.server = Server(self.coordinator, self.runtime, limits, self.logs.append, lock_fd=acquire_lock(self.runtime))
        self.thread = threading.Thread(target=self.server.run, kwargs={"install_signals": False}, daemon=True)
        self.thread.start()
        self.socket_path = self.runtime / "coordinator.sock"

    def raw(self, data: bytes, read: bool = True, hold: float = 0.0) -> bytes:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(5)
        client.connect(str(self.socket_path))
        try:
            client.sendall(data)
            if hold:
                time.sleep(hold)
            client.shutdown(socket.SHUT_WR)
        except OSError:
            pass  # the server may answer and close before a large request is fully written
        out = b""
        if read:
            while True:
                try:
                    chunk = client.recv(65536)
                except OSError:
                    break
                if not chunk:
                    break
                out += chunk
        client.close()
        return out

    def call(self, request: dict) -> dict:
        return json.loads(self.raw(json.dumps(request).encode()))

    def stop(self) -> None:
        self.coordinator.request_stop("test")
        self.thread.join(timeout=5)


class ProtocolTest(unittest.TestCase):
    def setUp(self):
        self.fx = ServerFixture(Limits(lease=1, reaper=0.1, exit_grace=60, final_grace=120, read_deadline=0.5, max_request_bytes=4096))

    def tearDown(self):
        self.fx.stop()

    def test_empty_malformed_oversized_and_schema_errors(self):
        self.assertEqual(json.loads(self.fx.raw(b""))["error"], "empty_request")
        self.assertEqual(json.loads(self.fx.raw(b"{not json"))["error"], "malformed_json")
        self.assertEqual(json.loads(self.fx.raw(b"[1,2]"))["error"], "bad_request")
        big = json.dumps({"schema": 1, "op": "status", "pad": "x" * 8192}).encode()
        self.assertEqual(json.loads(self.fx.raw(big))["error"], "oversized")
        self.assertEqual(self.fx.call({"schema": 1, "op": "frobnicate"})["error"], "unknown_op")
        self.assertTrue(self.fx.call({"schema": 1, "op": "status"})["ok"])

    def test_deep_nesting_and_non_finite_numbers_never_unwind_the_loop(self):
        deep = b"[" * 2000 + b"]" * 2000  # under max_request_bytes; a RecursionError on older interpreters
        answer = json.loads(self.fx.raw(deep))
        self.assertIn(answer["error"], ("malformed_json", "bad_request"))
        self.assertEqual(json.loads(self.fx.raw(b'{"schema": 1, "op": "status", "x": NaN}'))["error"], "malformed_json")
        self.assertTrue(self.fx.call({"schema": 1, "op": "status"})["ok"], "the coordinator is still serving")
        self.assertTrue(self.fx.thread.is_alive())

    def test_partial_sender_is_dropped_without_starving_the_reaper(self):
        reg = self.fx.call({"schema": 1, "op": "register", "observer": {"parent_pid": 1, "window_key": ""}})
        ack = self.fx.call({"schema": 1, "op": "snapshot", "observer": {"parent_pid": 1, "window_key": ""}, "epoch": reg["epoch"], "generation": reg["generation"], "seq": 1, "coverage": "complete", "observations": [{"pane": 1, "state": "working"}]})
        self.assertEqual(ack["desired"], "working")
        slow = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        slow.connect(str(self.fx.socket_path))
        slow.sendall(b'{"schema": 1, "op": "sta')  # never finishes
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and self.fx.sender.modes_for("events.sock")[-1] != "idle_ready":
            time.sleep(0.05)
        self.assertEqual(self.fx.sender.modes_for("events.sock")[-1], "idle_ready", "the 1 s lease expired and neutral went out while a partial client hung")
        self.assertTrue(any("did not finish" in line for line in self.fx.logs))
        slow.close()

    def test_connection_cap_answers_busy(self):
        held = []
        for _ in range(MAX_CONNECTIONS):
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(str(self.fx.socket_path))
            held.append(client)
        time.sleep(0.2)
        extra = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        extra.settimeout(2)
        extra.connect(str(self.fx.socket_path))
        out = b""
        try:
            while True:
                chunk = extra.recv(4096)
                if not chunk:
                    break
                out += chunk
        except socket.timeout:
            pass
        self.assertEqual(json.loads(out)["error"], "busy")
        for client in held:
            client.close()
        extra.close()


class SingletonTest(unittest.TestCase):
    def test_two_coordinators_only_one_survives_and_the_loser_sends_nothing(self):
        tmp = short_tmpdir("tsp-single-")
        ui = FakeReceiver(tmp / "events.sock")
        svc = FakeReceiver(tmp / "relay-events.sock")
        ui.start()
        svc.start()
        env = subprocess_env(tmp, ui.path, svc.path, {"lease": 2, "reaper": 0.2, "exit_grace": 1, "final_grace": 5})
        argv = [*helper_argv(env), "coordinator", "--runtime-dir", env["TERN_SIDEPULSE_RUNTIME_DIR"], "--data-dir", env["TERN_PLUGIN_DATA"], "--ui-socket", str(ui.path), "--service-socket", str(svc.path), "--limits", env["TERN_SIDEPULSE_LIMITS"]]
        procs = [subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for _ in range(2)]
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and all(p.poll() is None for p in procs):
                time.sleep(0.05)
            alive = [p for p in procs if p.poll() is None]
            self.assertEqual(len(alive), 1, "exactly one coordinator holds the lock")
            lock_pid = int((Path(env["TERN_SIDEPULSE_RUNTIME_DIR"]) / "coordinator.lock").read_text().strip())
            self.assertEqual(lock_pid, alive[0].pid)
            time.sleep(1.5)
            self.assertTrue(ui.wait_for("idle_ready", timeout=5))
            self.assertEqual(len(ui.events), 1, "the loser must not send its own startup neutral")
        finally:
            for p in procs:
                if p.poll() is None:
                    p.kill()
            ui.stop()
            svc.stop()


if __name__ == "__main__":
    unittest.main()
