import os
import time
import unittest
from pathlib import Path

from tests.helpers import FakeProgramServer, kill_coordinator, run_bridge, short_tmpdir, subprocess_env

FAST = {"lease": 3, "reaper": 0.2, "exit_grace": 1, "final_grace": 5, "done_notice": 1}


class EndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tmp = short_tmpdir("tsp-e2e-")
        self.receiver = FakeProgramServer(self.tmp / "program-status.sock")
        self.receiver.start()
        self.env = subprocess_env(self.tmp, self.receiver.path, FAST)

    def tearDown(self):
        kill_coordinator(self.env)
        self.receiver.stop()

    def send(self, reg: dict, seq: int, observations: list) -> dict:
        code, ack, err = run_bridge(["snapshot"], {"epoch": reg["epoch"], "generation": reg["generation"], "seq": seq, "coverage": "complete", "observations": observations}, self.env)
        self.assertEqual(code, 0, err)
        self.assertTrue(isinstance(ack, dict) and ack.get("ok"), (ack, err))
        return ack

    def test_working_then_silence_clears_within_bounds_and_exits(self):
        _, reg, _ = run_bridge(["register"], {}, self.env)
        self.send(reg, 1, [{"pane": 1, "state": "working"}])
        self.assertTrue(self.receiver.wait_for(lambda rx: rx.states() == ["working"]))
        last = time.monotonic()
        self.assertTrue(self.receiver.wait_for(lambda rx: rx.states() == ["working", "clear"] and not rx.rows, timeout=FAST["lease"] + FAST["reaper"] + 1.0))
        self.assertLessEqual(self.receiver.times[-1] - last, FAST["lease"] + FAST["reaper"] + 1.0)
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
        self.assertTrue(self.receiver.wait_for(lambda rx: rx.states() == ["working", "done"]))
        self.send(reg, 3, [{"pane": 1, "state": "working"}])
        self.assertTrue(self.receiver.wait_for(lambda rx: rx.states() == ["working", "done", "working"]))
        self.send(reg, 4, [{"pane": 1, "state": "idle", "notice": {"id": "2", "kind": "done"}}])
        self.assertTrue(self.receiver.wait_for(lambda rx: rx.states() == ["working", "done", "working", "done"]))
        self.assertTrue(self.receiver.wait_for(lambda rx: rx.states() == ["working", "done", "working", "done", "idle"], timeout=FAST["done_notice"] + 1.5), "the notice expires on its own")
        self.assertEqual(list(self.receiver.shown().values()), [("idle", "OMP in Tern")])

    def test_crash_overwrites_live_scope_and_clears_closed_pane_after_the_lease(self):
        _, reg, _ = run_bridge(["register"], {}, self.env)
        live = {"pane": 1, "state": "working", "label": "Still open"}
        self.send(reg, 1, [live, {"pane": 2, "state": "waiting_input", "label": "Closed meanwhile"}])
        self.assertTrue(self.receiver.wait_for(lambda rx: len(rx.rows) == 2))
        scopes = {report["context"]["title"]: report["scope"] for report in self.receiver.reports}
        live_scope, closed_scope = scopes["Still open"], scopes["Closed meanwhile"]
        kill_coordinator(self.env)
        time.sleep(0.3)
        before = len(self.receiver.reports)
        restarted = time.monotonic()
        _, out, _ = run_bridge(["snapshot"], {"epoch": reg["epoch"], "generation": reg["generation"], "seq": 2, "coverage": "complete", "observations": [{"pane": 1, "state": "working"}]}, self.env)
        self.assertEqual(out["error"], "epoch_mismatch")
        _, reg2, _ = run_bridge(["register"], {}, self.env)
        self.assertNotEqual(reg2["epoch"], reg["epoch"])
        self.send(reg2, 1, [live])
        self.assertEqual(self.receiver.shown(), {
            live_scope: ("working", "Still open"), closed_scope: ("blocked", "Closed meanwhile"),
        }, "inherited scopes remain visible while live windows re-register")
        time.sleep(FAST["lease"] / 2)
        self.assertIn(closed_scope, self.receiver.rows, "closed inherited rows wait for the lease, not just registration")
        self.send(reg2, 2, [live])  # keep the live pane leased while the inherited closed pane ages out
        self.assertTrue(self.receiver.wait_for(lambda rx: closed_scope not in rx.rows, timeout=FAST["lease"] / 2 + FAST["reaper"] + 1))
        self.assertEqual(self.receiver.shown(), {live_scope: ("working", "Still open")})
        self.assertEqual([(report["scope"], report["status"]["state"]) for report in self.receiver.reports[before:]], [
            (live_scope, "working"), (closed_scope, "clear"),
        ], "the live scope is overwritten in place, never cleared and re-added")
        self.assertGreaterEqual(self.receiver.times[-1] - restarted, FAST["lease"])

    def test_receiver_restart_resubmits_current_rows_without_another_snapshot(self):
        _, reg, _ = run_bridge(["register"], {}, self.env)
        self.send(reg, 1, [
            {"pane": 1, "state": "waiting_input", "label": "Question"},
            {"pane": 2, "state": "working", "label": "Running"},
            {"pane": 3, "state": "idle", "label": "Ready"},
        ])
        self.assertTrue(self.receiver.wait_for(lambda rx: len(rx.rows) == 3))
        visible = self.receiver.shown()
        before = len(self.receiver.reports)
        restarted = time.monotonic()
        self.receiver.restart()
        self.assertTrue(self.receiver.wait_for(lambda rx: rx.shown() == visible, timeout=FAST["reaper"] + 1.0))
        self.assertEqual(self.receiver.states()[before:], ["blocked", "working", "idle"], "only current rows, no historical states")
        self.assertLessEqual(self.receiver.times[-1] - restarted, FAST["reaper"] + 1.0)

    def test_config_labels_false_suppresses_the_real_coordinator_title(self):
        data = Path(self.env["TERN_PLUGIN_DATA"])
        data.mkdir(parents=True, exist_ok=True)
        (data / "config.json").write_text('{"labels": false}\n')
        _, reg, _ = run_bridge(["register"], {}, self.env)
        self.send(reg, 1, [{"pane": 1, "state": "working", "label": "PRIVATE: Founder opt-out"}])
        self.assertTrue(self.receiver.wait_for(lambda rx: len(rx.rows) == 1))
        report = self.receiver.reports[0]
        self.assertEqual(report["context"]["title"], "")
        self.assertEqual(report["context"]["display_name"], "OMP in Tern")
        self.assertEqual(list(self.receiver.shown().values()), [("working", "OMP in Tern")])
        _, out, _ = run_bridge(["status"], None, self.env)
        self.assertFalse(out["status"]["transport"]["labels"], "the detached coordinator inherited the config setting")

    def test_disable_then_enable_applies_a_changed_labels_setting(self):
        _, reg, _ = run_bridge(["register"], {}, self.env)
        labelled = {"pane": 1, "state": "working", "label": "Private tab"}
        self.send(reg, 1, [labelled])
        self.assertTrue(self.receiver.wait_for(lambda rx: list(rx.shown().values()) == [("working", "Private tab")]))
        (Path(self.env["TERN_PLUGIN_DATA"]) / "config.json").write_text('{"labels": false}\n')
        code, out, err = run_bridge(["disable"], None, self.env)
        self.assertTrue(out.get("disabled"), (out, err))
        code, out, err = run_bridge(["enable"], None, self.env)  # right away, as the README says
        self.assertEqual(out.get("disabled"), False, (out, err))
        _, reg2, err = run_bridge(["register"], {}, self.env)
        self.assertTrue(reg2.get("ok"), (reg2, err))
        self.assertNotEqual(reg2["coordinator_pid"], reg["coordinator_pid"], "disable stopped the old coordinator")
        self.send(reg2, 1, [labelled])
        self.assertTrue(self.receiver.wait_for(lambda rx: list(rx.shown().values()) == [("working", "OMP in Tern")]))
        self.assertNotIn("Private tab", self.receiver.reports[-1]["context"]["title"])

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
        self.assertTrue(self.receiver.wait_for(lambda rx: len(rx.rows) == 32))
        self.assertEqual(set(self.receiver.shown().values()), {("working", "é" * 96)})
        code, out, err = run_bridge(["status"], None, self.env)
        self.assertEqual(code, 0, err)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["status"]["rows_total"], 512)
        self.assertEqual(out["status"]["rows_labelled"], 512)
        self.assertEqual(out["status"]["transport"]["rows"], 32)
        self.assertEqual(out["status"]["transport"]["rows_capped"], 480)

    @unittest.skipUnless(os.environ.get("TERN_SIDEPULSE_SLOW_TESTS"), "set TERN_SIDEPULSE_SLOW_TESTS=1 for the default-limits timing check")
    def test_default_limits_clear_within_20s(self):
        env = subprocess_env(self.tmp, self.receiver.path)
        env.pop("TERN_SIDEPULSE_LIMITS", None)
        _, reg, _ = run_bridge(["register"], {}, env)
        _, ack, _ = run_bridge(["snapshot"], {"epoch": reg["epoch"], "generation": reg["generation"], "seq": 1, "coverage": "complete", "observations": [{"pane": 1, "state": "working"}]}, env)
        self.assertTrue(ack["ok"])
        last = time.monotonic()
        self.assertTrue(self.receiver.wait_for(lambda rx: rx.states() == ["working", "clear"] and not rx.rows, timeout=25))
        self.assertLessEqual(self.receiver.times[-1] - last, 20.0)


if __name__ == "__main__":
    unittest.main()
