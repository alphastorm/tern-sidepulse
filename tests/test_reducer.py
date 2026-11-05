import unittest

from tests.helpers import activate, idle, make_coordinator, snapshot, waiting, working

from tern_sidepulse.reducer import aggregate, pane_mode


class ReducerTest(unittest.TestCase):
    def test_priority_order(self):
        self.assertEqual(aggregate(["working", "waiting_for_input", "working", "working"]), "waiting_for_input")
        self.assertEqual(aggregate(["completed", "working"]), "working")
        self.assertEqual(aggregate(["blocked_error", "waiting_for_input"]), "blocked_error")
        self.assertEqual(aggregate(["completed", "idle_ready"]), "completed")
        self.assertEqual(aggregate([]), "idle_ready")

    def test_withdrawn_states_contribute_nothing(self):
        for state in ("exited", "closed", "unknown"):
            self.assertIsNone(pane_mode(state, "done"))
        self.assertEqual(aggregate([None, None]), "idle_ready")

    def test_notice_never_outranks_live_work(self):
        self.assertEqual(pane_mode("working", "failed"), "working")
        self.assertEqual(pane_mode("waiting_input", "done"), "waiting_for_input")
        self.assertEqual(pane_mode("idle", "done"), "completed")
        self.assertEqual(pane_mode("idle", "failed"), "blocked_error")
        self.assertEqual(pane_mode("idle", None), "idle_ready")

    def test_duplicates_are_idempotent(self):
        self.assertEqual(aggregate(["working"]), aggregate(["working", "working", "working"]))


class ReductionThroughCoordinatorTest(unittest.TestCase):
    def test_one_waiting_plus_three_working_then_cleared(self):
        c, clock, sender, _, _ = make_coordinator()
        gen, epoch = activate(c, [working(1), working(2), working(3), waiting(4)])
        self.assertEqual(c.desired, "waiting_for_input")
        snapshot(c, gen, epoch, 2, [working(1), working(2), working(3), working(4)])
        self.assertEqual(c.desired, "working")
        self.assertEqual(sender.modes_for("events.sock"), ["idle_ready", "waiting_for_input", "working"])

    def test_duplicate_pane_ids_in_one_snapshot_reduce_once(self):
        c, _, _, _, _ = make_coordinator()
        activate(c, [working(1), {"pane": 1, "state": "waiting_input"}])
        # The first observation for a pane wins; the duplicate cannot escalate.
        self.assertEqual(c.desired, "working")

    def test_startup_idle_is_not_success(self):
        c, _, sender, _, _ = make_coordinator()
        activate(c, [idle(1)])
        self.assertEqual(c.desired, "idle_ready")
        self.assertNotIn("completed", sender.modes_for("events.sock"))

    def test_disjoint_windows_both_contribute(self):
        c, _, _, _, _ = make_coordinator()
        other = {"parent_pid": 4242, "window_key": "2"}
        gen_a, epoch = activate(c, [working(1)])
        gen_b, _ = activate(c, [idle(900, "1", "failed")], observer=other)
        self.assertEqual(c.desired, "blocked_error")
        # Window A refreshing its own (working) view cannot erase window B's error.
        snapshot(c, gen_a, epoch, 2, [working(1)])
        self.assertEqual(c.desired, "blocked_error")
        snapshot(c, gen_b, epoch, 2, [idle(900)], observer=other)
        self.assertEqual(c.desired, "working")


if __name__ == "__main__":
    unittest.main()
