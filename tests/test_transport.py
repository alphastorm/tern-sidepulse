import json
import time
import unittest

from tests.helpers import FakeClock, FakeReceiver, RecordingSender, short_tmpdir

from tern_sidepulse.transport import SidePulseTransport, build_line, encode_event


def transport_with(sender: RecordingSender, clock: FakeClock, **kw) -> SidePulseTransport:
    root = sender.root
    return SidePulseTransport.for_receivers("tern-sidepulse:test", clock, (root / "events.sock",), (root / "relay-events.sock",), sender=sender, **kw)


class WireFormatTest(unittest.TestCase):
    def test_envelope_and_events(self):
        for mode, event in (
            ("working", "UserPromptSubmit"),
            ("waiting_for_input", "PermissionRequest"),
            ("blocked_error", "StopFailure"),
            ("completed", "Stop"),
            ("idle_ready", "Interrupt"),
        ):
            payload = json.loads(encode_event(build_line(mode, "tern-sidepulse:abc", 1_800_000_000.5)))
            self.assertEqual(payload["provider"], "omp")
            line = payload["line"]
            self.assertEqual(line["hook_event_name"], event)
            self.assertEqual(line["sidepulse_status"], mode)
            self.assertEqual(line["session_id"], "tern-sidepulse:abc")
            self.assertEqual(line["logged_at"], "2027-01-15T08:00:00.500Z")
            self.assertNotIn("agent_id", line)
            self.assertNotIn("tool_name", line)
            self.assertNotIn("tool_input", line)
            self.assertNotIn("cwd", line)
            self.assertNotIn(event, ("Notification", "SessionEnd"))
            self.assertEqual("prompt" in line, mode == "working")

    def test_unknown_mode_rejected(self):
        with self.assertRaises(ValueError):
            build_line("tool_running", "x", 0)


class DeliveryTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.sender = RecordingSender(short_tmpdir("tsp-tx-"))
        self.sender.clock = self.clock
        self.sender.up("events.sock")
        self.sender.up("relay-events.sock")
        self.logs: list[str] = []
        self.t = transport_with(self.sender, self.clock, log=self.logs.append)

    def test_both_families_receive_each_change(self):
        self.t.set_desired("working")
        self.t.tick()
        self.t.set_desired("completed")
        self.t.tick()
        self.assertEqual(self.sender.modes_for("events.sock"), ["working", "completed"])
        self.assertEqual(self.sender.modes_for("relay-events.sock"), ["working", "completed"])

    def test_one_family_down_does_not_hide_the_other(self):
        self.sender.down("relay-events.sock")
        self.t.set_desired("working")
        self.t.tick()
        ui, svc = self.t.families
        self.assertEqual(ui.delivered_mode, "working")
        self.assertIsNone(svc.delivered_mode)
        self.assertTrue(svc.pending)
        self.assertIn("No such file", svc.last_error or "")
        self.assertFalse(self.t.all_delivered("working"))

    def test_backoff_grows_and_is_not_reset_by_repeated_set_desired(self):
        self.sender.down("events.sock")
        self.t.set_desired("working")
        attempts = []
        for second in range(0, 70):
            before = self.t.families[0].failures
            self.t.set_desired("working")  # every reaper tick re-asserts the same mode
            self.t.tick()
            if self.t.families[0].failures != before:
                attempts.append(second)
            self.clock.advance(1)
        self.assertEqual(attempts[:6], [0, 2, 6, 14, 30, 60])

    def test_change_during_outage_is_spaced_not_hammered(self):
        self.sender.down("events.sock")
        self.t.set_desired("working")
        self.t.tick()
        failures = self.t.families[0].failures
        self.clock.advance(0.2)
        self.t.set_desired("waiting_for_input")
        self.t.tick()
        self.assertEqual(self.t.families[0].failures, failures, "a change 0.2 s into an outage waits for the retry spacing")
        self.clock.advance(1.0)
        self.t.tick()
        self.assertEqual(self.t.families[0].failures, failures + 1)

    def test_recovery_resets_backoff(self):
        self.sender.down("events.sock")
        self.t.set_desired("working")
        self.t.tick()
        self.clock.advance(2.1)
        self.t.tick()
        self.assertEqual(self.t.families[0].failures, 2)
        self.sender.up("events.sock")
        self.clock.advance(0.1)
        self.t.tick()
        self.assertEqual(self.t.families[0].failures, 0, "a reappearing socket is retried before the 4 s backoff ends")
        self.assertEqual(self.sender.modes_for("events.sock"), ["working"])
        self.assertTrue(any("recovered" in line for line in self.logs))

    def test_receiver_restart_resends_active_mode_only(self):
        self.t.set_desired("working")
        self.t.tick()
        self.sender.down("events.sock")
        self.sender.up("events.sock")
        self.clock.advance(1)
        self.t.tick()
        self.assertEqual(self.sender.modes_for("events.sock"), ["working", "working"])
        self.t.set_desired("completed")
        self.t.tick()
        self.sender.down("events.sock")
        self.sender.up("events.sock")
        self.clock.advance(1)
        self.t.tick()
        self.assertEqual(self.sender.modes_for("events.sock")[-1], "completed")
        self.assertEqual(self.sender.modes_for("events.sock").count("completed"), 1, "completed is never resent as a fresh success")

    def test_idle_is_never_refreshed(self):
        self.t.set_desired("idle_ready")
        self.t.tick()
        for _ in range(10):
            self.clock.advance(self.t.active_refresh)
            self.t.tick()
        self.assertEqual(self.sender.modes_for("events.sock"), ["idle_ready"])

    def test_active_mode_is_refreshed_periodically(self):
        self.t.set_desired("working")
        self.t.tick()
        self.clock.advance(self.t.active_refresh + 1)
        self.t.tick()
        self.assertEqual(self.sender.modes_for("events.sock"), ["working", "working"])

    def test_hung_receiver_is_bounded_per_family(self):
        self.sender.cost = 5.0  # every attempt would block for 5 fake seconds
        self.t.set_desired("working")
        start = self.clock.monotonic()
        self.t.tick()
        self.assertLessEqual(self.clock.monotonic() - start, 2 * self.t.family_budget + 0.01)


class RealSocketTest(unittest.TestCase):
    def test_send_to_real_receiver_and_hung_receiver_times_out(self):
        tmp = short_tmpdir("tsp-rs-")
        receiver = FakeReceiver(tmp / "events.sock")
        receiver.start()
        hung = FakeReceiver(tmp / "relay-events.sock", hang=True)
        hung.start()
        try:
            clock = FakeClock()
            t = SidePulseTransport.for_receivers("tern-sidepulse:test", clock, (tmp / "events.sock",), (tmp / "relay-events.sock",))
            t.set_desired("working")
            started = time.monotonic()
            t.tick()
            elapsed = time.monotonic() - started
            self.assertTrue(receiver.wait_for("working"))
            self.assertEqual(receiver.events[0]["line"]["hook_event_name"], "UserPromptSubmit")
            self.assertLess(elapsed, 1.5, "a hung receiver must not block beyond its budget")
            self.assertTrue(t.families[0].delivered_mode == "working")
        finally:
            receiver.stop()
            hung.stop()


if __name__ == "__main__":
    unittest.main()
