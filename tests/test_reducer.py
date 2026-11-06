import unittest

from tests.helpers import activate, combined_mode, idle, make_coordinator, rows, snapshot, waiting, working

from tern_sidepulse.reducer import pane_mode, reduce_rows


class ReducerTest(unittest.TestCase):
    def test_withdrawn_states_contribute_nothing(self):
        for state in ("exited", "closed", "unknown"):
            self.assertIsNone(pane_mode(state, "done"))
        self.assertEqual(reduce_rows([]), ())

    def test_notice_never_outranks_live_work(self):
        self.assertEqual(pane_mode("working", "failed"), "working")
        self.assertEqual(pane_mode("waiting_input", "done"), "waiting_for_input")
        self.assertEqual(pane_mode("idle", "done"), "completed")
        self.assertEqual(pane_mode("idle", "failed"), "blocked_error")
        self.assertEqual(pane_mode("idle", None), "idle_ready")


class ReductionThroughCoordinatorTest(unittest.TestCase):
    def test_one_waiting_plus_three_working_then_cleared(self):
        c, clock, sender, _, _ = make_coordinator()
        gen, epoch = activate(c, [working(1), working(2), working(3), waiting(4)])
        self.assertEqual(combined_mode(c), "waiting_for_input")
        self.assertCountEqual(rows(c), [
            ("local", "1", "working", None),
            ("local", "2", "working", None),
            ("local", "3", "working", None),
            ("local", "4", "waiting_for_input", None),
        ])
        snapshot(c, gen, epoch, 2, [working(1), working(2), working(3), working(4)])
        self.assertEqual(combined_mode(c), "working")
        self.assertEqual(len(rows(c)), 4)
        self.assertTrue(all(row[2] == "working" for row in rows(c)))
        self.assertEqual(sender.modes_for("events.sock"), ["idle_ready", "waiting_for_input", "working"])

    def test_duplicate_pane_ids_in_one_snapshot_reduce_once(self):
        c, _, _, _, _ = make_coordinator()
        activate(c, [working(1), {"pane": 1, "state": "waiting_input"}])
        # The first observation for a pane wins; the duplicate cannot escalate.
        self.assertEqual(combined_mode(c), "working")
        self.assertEqual(rows(c), [("local", "1", "working", None)])

    def test_startup_idle_is_not_success(self):
        c, _, sender, _, _ = make_coordinator()
        activate(c, [idle(1)])
        self.assertEqual(combined_mode(c), "idle_ready")
        self.assertEqual(rows(c), [("local", "1", "idle_ready", None)])
        self.assertNotIn("completed", sender.modes_for("events.sock"))

    def test_disjoint_windows_both_contribute(self):
        c, _, _, _, _ = make_coordinator()
        other = {"parent_pid": 4242, "window_key": "2"}
        gen_a, epoch = activate(c, [working(1)])
        gen_b, _ = activate(c, [idle(900, "1", "failed")], observer=other)
        self.assertEqual(combined_mode(c), "blocked_error")
        self.assertCountEqual(rows(c), [("local", "1", "working", None), ("local", "900", "blocked_error", None)])
        # Window A refreshing its own (working) view cannot erase window B's error.
        snapshot(c, gen_a, epoch, 2, [working(1)])
        self.assertEqual(combined_mode(c), "blocked_error")
        snapshot(c, gen_b, epoch, 2, [idle(900)], observer=other)
        self.assertEqual(combined_mode(c), "working")
        self.assertCountEqual(rows(c), [("local", "1", "working", None), ("local", "900", "idle_ready", None)])


if __name__ == "__main__":
    unittest.main()
