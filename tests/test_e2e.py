import os
import time
import unittest
from pathlib import Path

from tests.helpers import FakeReceiver, kill_coordinator, run_bridge, short_tmpdir, subprocess_env

FAST = {"lease": 3, "reaper": 0.2, "exit_grace": 1, "final_grace": 5, "done_notice": 1}


class EndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tmp = short_tmpdir("tsp-e2e-")
        self.ui = FakeReceiver(self.tmp / "events.sock")
        self.svc = FakeReceiver(self.tmp / "relay-events.sock")
        self.ui.start()
        self.svc.start()
        self.env = subprocess_env(self.tmp, self.ui.path, self.svc.path, FAST)

    def tearDown(self):
        kill_coordinator(self.env)
        self.ui.stop()
        self.svc.stop()

    def send(self, reg: dict, seq: int, observations: list) -> dict:
        _, ack, err = run_bridge(["snapshot"], {"epoch": reg["epoch"], "generation": reg["generation"], "seq": seq, "coverage": "complete", "observations": observations}, self.env)
        self.assertTrue(isinstance(ack, dict) and ack.get("ok"), (ack, err))
        return ack

    def test_working_then_silence_neutralizes_within_bounds_and_exits(self):
        _, reg, _ = run_bridge(["register"], {}, self.env)
        self.send(reg, 1, [{"pane": 1, "state": "working"}])
        self.assertTrue(self.ui.wait_for("working"))
        self.assertTrue(self.svc.wait_for("working"))
        last = time.monotonic()
        self.assertTrue(self.ui.wait_for("idle_ready", timeout=FAST["lease"] + FAST["reaper"] + 1.0, minimum=2))
        self.assertLessEqual(self.ui.times[-1] - last, FAST["lease"] + FAST["reaper"] + 1.0)
        sock = Path(self.env["TERN_SIDEPULSE_RUNTIME_DIR"]) / "coordinator.sock"
        deadline = time.monotonic() + FAST["exit_grace"] + 3
        while time.monotonic() < deadline and sock.exists():
            time.sleep(0.05)
        self.assertFalse(sock.exists(), "the coordinator removed its socket on exit")
        _, out, _ = run_bridge(["status"], None, self.env)
        self.assertEqual(out["detail"], "not_running")

    def test_completion_then_next_turn_and_bounded_notice(self):
        _, reg, _ = run_bridge(["register"], {}, self.env)
        self.send(reg, 1, [{"pane": 1, "state": "working"}])
        self.send(reg, 2, [{"pane": 1, "state": "idle", "notice": {"id": "1", "kind": "done"}}])
        self.assertTrue(self.ui.wait_for("completed"))
        self.send(reg, 3, [{"pane": 1, "state": "working"}])
        self.assertTrue(self.ui.wait_for("working", minimum=2))
        self.send(reg, 4, [{"pane": 1, "state": "idle", "notice": {"id": "2", "kind": "done"}}])
        self.assertTrue(self.ui.wait_for("completed", minimum=2))
        self.assertTrue(self.ui.wait_for("idle_ready", timeout=FAST["done_notice"] + 1.5, minimum=2), "the notice expires on its own")

    def test_crash_recovery_rebuilds_from_the_current_snapshot(self):
        _, reg, _ = run_bridge(["register"], {}, self.env)
        self.send(reg, 1, [{"pane": 1, "state": "working"}])
        self.assertTrue(self.ui.wait_for("working"))
        kill_coordinator(self.env)
        time.sleep(0.3)
        _, out, _ = run_bridge(["snapshot"], {"epoch": reg["epoch"], "generation": reg["generation"], "seq": 2, "coverage": "complete", "observations": [{"pane": 1, "state": "working"}]}, self.env)
        self.assertEqual(out["error"], "epoch_mismatch")
        _, reg2, _ = run_bridge(["register"], {}, self.env)
        self.assertNotEqual(reg2["epoch"], reg["epoch"])
        self.send(reg2, 1, [{"pane": 1, "state": "working"}])
        modes = self.ui.modes()
        self.assertEqual(modes[-2:], ["idle_ready", "working"], "the restart neutralizes the identity first, then publishes the live state")

    def test_receiver_restart_gets_the_current_mode_only(self):
        _, reg, _ = run_bridge(["register"], {}, self.env)
        self.send(reg, 1, [{"pane": 1, "state": "waiting_input"}])
        self.assertTrue(self.ui.wait_for("waiting_for_input"))
        self.ui.restart()
        before = len(self.ui.events)
        self.send(reg, 2, [{"pane": 1, "state": "waiting_input"}])
        self.assertTrue(self.ui.wait_for("waiting_for_input", timeout=3, minimum=2))
        self.assertEqual(self.ui.modes()[before:], ["waiting_for_input"], "only the current mode, no replayed history")

    def test_other_family_down_then_back(self):
        self.svc.stop()
        _, reg, _ = run_bridge(["register"], {}, self.env)
        ack = self.send(reg, 1, [{"pane": 1, "state": "working"}])
        self.assertEqual(ack["delivery"]["ui"]["mode"], "working")
        self.assertIsNone(ack["delivery"]["service"]["mode"])
        self.svc = FakeReceiver(self.tmp / "relay-events.sock")
        self.svc.start()
        self.assertTrue(self.svc.wait_for("working", timeout=4), "the returning service gets the current mode without waiting out the full backoff")

    def test_max_size_labelled_snapshot_is_accepted(self):
        code, reg, err = run_bridge(["register"], {}, self.env)
        self.assertEqual(code, 0, err)
        self.assertTrue(reg["ok"], reg)
        observations = [{"pane": i, "state": "working", "host": "local", "label": "é" * 128} for i in range(512)]
        code, ack, err = run_bridge(["snapshot"], {
            "epoch": reg["epoch"],
            "generation": reg["generation"],
            "seq": 1,
            "coverage": "complete",
            "observations": observations,
        }, self.env, raw_utf8=True)
        self.assertEqual(code, 0, err)
        self.assertTrue(ack["ok"], ack)
        self.assertEqual(ack["rows"], {"working": 512})
        self.assertTrue(self.ui.wait_for("working"))
        self.assertTrue(self.svc.wait_for("working"))
        code, out, err = run_bridge(["status"], None, self.env)
        self.assertEqual(code, 0, err)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["status"]["rows_total"], 512)
        self.assertEqual(out["status"]["rows_labelled"], 512)

    @unittest.skipUnless(os.environ.get("TERN_SIDEPULSE_SLOW_TESTS"), "set TERN_SIDEPULSE_SLOW_TESTS=1 for the default-limits timing check")
    def test_default_limits_neutral_within_20s(self):
        env = subprocess_env(self.tmp, self.ui.path, self.svc.path, None)
        _, reg, _ = run_bridge(["register"], {}, env)
        _, ack, _ = run_bridge(["snapshot"], {"epoch": reg["epoch"], "generation": reg["generation"], "seq": 1, "coverage": "complete", "observations": [{"pane": 1, "state": "working"}]}, env)
        self.assertTrue(ack["ok"])
        last = time.monotonic()
        self.assertTrue(self.ui.wait_for("idle_ready", timeout=25, minimum=2))
        self.assertLessEqual(self.ui.times[-1] - last, 20.0)
        kill_coordinator(env)


if __name__ == "__main__":
    unittest.main()
