import unittest

from tests.helpers import activate, advance, idle, make_coordinator, register, snapshot, working

from tern_sidepulse.coordinator import Limits


class LeaseTest(unittest.TestCase):
    def test_unknown_coverage_never_renews_but_keeps_prior_observations(self):
        c, clock, _, _, _ = make_coordinator(Limits(lease=15))
        gen, epoch = activate(c, [working(1)])
        advance(c, clock, 10)
        ack = snapshot(c, gen, epoch, 2, [], coverage="unknown")
        self.assertFalse(ack["renewed"])
        self.assertEqual(c.desired, "working")
        advance(c, clock, 4)
        self.assertEqual(c.desired, "working")
        advance(c, clock, 1.5)
        self.assertEqual(c.desired, "idle_ready", "the original 15 s lease expired despite the heartbeat")

    def test_genuine_empty_snapshot_clears_now(self):
        c, _, _, _, _ = make_coordinator()
        gen, epoch = activate(c, [working(1)])
        snapshot(c, gen, epoch, 2, [])
        self.assertEqual(c.desired, "idle_ready")

    def test_vanished_window_expires_and_neutral_is_sent(self):
        c, clock, sender, _, _ = make_coordinator(Limits(lease=15, reaper=1))
        activate(c, [working(1)])
        advance(c, clock, 14)
        self.assertEqual(c.desired, "working")
        advance(c, clock, 1)
        self.assertEqual(c.desired, "idle_ready")
        self.assertEqual(sender.modes_for("events.sock")[-1], "idle_ready")
        self.assertEqual(c.counters["expired_observers"], 1)

    def test_exit_after_grace_once_neutral_delivered(self):
        c, clock, _, _, _ = make_coordinator(Limits(lease=2, reaper=1, exit_grace=5, final_grace=60))
        activate(c, [working(1)])
        self.assertIsNone(advance(c, clock, 2), "the lease ends at t=2 and the drain starts")
        self.assertIsNone(advance(c, clock, 4.9))
        self.assertEqual(advance(c, clock, 0.2), "exit")

    def test_fresh_registration_delays_the_drain(self):
        c, clock, _, _, _ = make_coordinator(Limits(lease=2, reaper=1, exit_grace=5))
        activate(c, [working(1)])
        advance(c, clock, 3)
        advance(c, clock, 4.5)
        register(c)
        self.assertIsNone(advance(c, clock, 1), "a window that just registered must get its activating snapshot in")

    def test_neutral_retried_until_delivered_when_receiver_present_but_refusing(self):
        c, clock, sender, _, _ = make_coordinator(Limits(lease=2, reaper=1, exit_grace=5, final_grace=40))
        activate(c, [working(1)])
        sender.refuse = {"events.sock", "relay-events.sock"}
        advance(c, clock, 3)
        self.assertEqual(c.desired, "idle_ready")
        # Both families still hold "working": the coordinator must not leave after the short grace.
        self.assertIsNone(advance(c, clock, 10))
        sender.refuse = set()
        advance(c, clock, 3)
        self.assertEqual(sender.modes_for("events.sock")[-1], "idle_ready")
        self.assertEqual(sender.modes_for("relay-events.sock")[-1], "idle_ready")
        self.assertEqual(advance(c, clock, 6), "exit")

    def test_final_grace_bounds_the_wait(self):
        c, clock, sender, logs, _ = make_coordinator(Limits(lease=2, reaper=1, exit_grace=5, final_grace=20))
        activate(c, [working(1)])
        sender.refuse = {"events.sock", "relay-events.sock"}
        advance(c, clock, 3)
        self.assertEqual(advance(c, clock, 25), "exit")
        self.assertTrue(any("final grace" in line for line in logs))

    def test_absent_receiver_does_not_block_exit(self):
        c, clock, _, _, _ = make_coordinator(Limits(lease=2, reaper=1, exit_grace=5, final_grace=60), receivers_up=False)
        activate(c, [working(1)])
        advance(c, clock, 3)
        self.assertEqual(advance(c, clock, 6), "exit", "nothing was ever delivered, so there is nothing to neutralize")

    def test_startup_neutral_retried_while_a_present_receiver_refuses(self):
        c, clock, sender, _, _ = make_coordinator(Limits(exit_grace=5, final_grace=20), receivers_up=False)
        sender.up("events.sock")
        sender.refuse = {"events.sock"}
        c.transport.set_desired("idle_ready", force=True)
        c.transport.tick()
        self.assertIsNone(advance(c, clock, 8), "a present receiver that never took the startup neutral keeps the coordinator alive")
        sender.refuse = set()
        sender.down("events.sock")
        sender.up("events.sock")  # a restarted receiver binds a new socket inode
        advance(c, clock, 1)
        self.assertEqual(sender.modes_for("events.sock")[-1], "idle_ready")


class ClockDiscontinuityTest(unittest.TestCase):
    def test_wall_jump_makes_output_idle_before_the_first_request_after_wake(self):
        c, clock, _, _, _ = make_coordinator(Limits(lease=15, done_notice=15))
        other = {"parent_pid": 4242, "window_key": "2"}
        gen_a, epoch = activate(c, [idle(1, "1", "done")])
        activate(c, [working(2)], observer=other)
        self.assertEqual(c.desired, "working")
        clock.advance(1, wall_seconds=7200)  # asleep for two hours; the monotonic clock paused
        # The first request after wake must not see pre-sleep state.
        ack = snapshot(c, gen_a, epoch, 2, [idle(1)])
        self.assertEqual(ack["desired"], "idle_ready")
        self.assertEqual(c.discontinuities, 1)
        self.assertTrue(c.observers["4242:2"].uncertain)
        # Its own fresh snapshot restores the other window's work.
        snapshot(c, c.observers["4242:2"].generation, epoch, 2, [working(2)], observer=other)
        self.assertEqual(c.desired, "working")

    def test_stalled_loop_also_counts(self):
        c, clock, _, _, _ = make_coordinator(Limits(lease=60, reaper=1, discontinuity=5))
        activate(c, [working(1)])
        clock.advance(30)
        c.tick()
        self.assertEqual(c.desired, "idle_ready")
        self.assertEqual(c.discontinuities, 1)


class InternalErrorTest(unittest.TestCase):
    def test_persist_failure_does_not_kill_the_coordinator(self):
        c, _, _, logs, _ = make_coordinator()

        def boom(_flag: bool) -> None:
            raise OSError(13, "denied")

        c.persist_disabled = boom
        response = c.handle({"schema": 1, "op": "disable"})
        self.assertTrue(response["disabled"])
        self.assertFalse(response["persisted"])
        self.assertTrue(c.disabled)
        self.assertTrue(any("latch" in line for line in logs))

    def test_unexpected_exception_is_contained(self):
        c, _, _, logs, _ = make_coordinator()
        c.op_status = lambda _req: 1 / 0  # type: ignore[assignment]
        response = c.handle({"schema": 1, "op": "status"})
        self.assertEqual(response["error"], "internal")
        self.assertEqual(c.counters["internal_errors"], 1)
        self.assertTrue(any("ZeroDivisionError" in line for line in logs))


if __name__ == "__main__":
    unittest.main()
