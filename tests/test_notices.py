import unittest

from tests.helpers import activate, advance, combined_mode, idle, make_coordinator, register, rows, snapshot, waiting, working

from tern_sidepulse.limits import Limits


class NoticeTest(unittest.TestCase):
    def test_fixed_deadline_despite_heartbeats(self):
        c, clock, sender, _, _ = make_coordinator(Limits(done_notice=15, failed_notice=30))
        gen, epoch = activate(c, [idle(1, "1", "done")])
        self.assertEqual(combined_mode(c), "completed")
        seq = 1
        for _ in range(14):
            advance(c, clock, 1)
            seq += 1
            snapshot(c, gen, epoch, seq, [idle(1, "1", "done")])
            self.assertEqual(combined_mode(c), "completed")
        advance(c, clock, 1.01)
        self.assertEqual(combined_mode(c), "idle_ready")
        self.assertEqual(sender.states(), ["done", "idle"])

    def test_failed_notice_lasts_longer_than_done(self):
        c, clock, _, _, _ = make_coordinator(Limits(done_notice=15, failed_notice=30))
        gen, epoch = activate(c, [idle(1, "1", "failed")])
        self.assertEqual(combined_mode(c), "blocked_error")
        seq = 1
        for _ in range(5):  # the window keeps heartbeating every 5 s
            advance(c, clock, 5)
            seq += 1
            snapshot(c, gen, epoch, seq, [idle(1, "1", "failed")])
        advance(c, clock, 4)
        self.assertEqual(combined_mode(c), "blocked_error")
        advance(c, clock, 1.5)
        self.assertEqual(combined_mode(c), "idle_ready")

    def test_omit_then_include_does_not_renew(self):
        c, clock, _, _, _ = make_coordinator(Limits(done_notice=15))
        gen, epoch = activate(c, [idle(1, "1", "done")])
        advance(c, clock, 10)
        snapshot(c, gen, epoch, 2, [idle(1)])
        self.assertEqual(combined_mode(c), "idle_ready")
        advance(c, clock, 4)
        snapshot(c, gen, epoch, 3, [idle(1, "1", "done")])
        self.assertEqual(combined_mode(c), "completed", "still inside the original 15 s window")
        advance(c, clock, 1.5)
        self.assertEqual(combined_mode(c), "idle_ready", "the deadline was fixed when the notice was first accepted")

    def test_working_voids_the_notice_for_good(self):
        c, _, _, _, _ = make_coordinator()
        gen, epoch = activate(c, [idle(1, "1", "done")])
        snapshot(c, gen, epoch, 2, [{"pane": 1, "state": "working", "notice": {"id": "1", "kind": "done"}}])
        self.assertEqual(combined_mode(c), "working")
        snapshot(c, gen, epoch, 3, [idle(1, "1", "done")])
        self.assertEqual(combined_mode(c), "idle_ready", "a late heartbeat cannot turn a resumed pane green")

    def test_waiting_clears_a_failure_notice(self):
        c, _, _, _, _ = make_coordinator()
        gen, epoch = activate(c, [idle(1, "1", "failed")])
        snapshot(c, gen, epoch, 2, [waiting(1)])
        self.assertEqual(combined_mode(c), "waiting_for_input")
        snapshot(c, gen, epoch, 3, [idle(1, "1", "failed")])
        self.assertEqual(combined_mode(c), "idle_ready")

    def test_vanished_pane_drops_its_notice(self):
        c, _, _, _, _ = make_coordinator()
        gen, epoch = activate(c, [idle(1, "1", "done")])
        snapshot(c, gen, epoch, 2, [])
        self.assertEqual(combined_mode(c), "idle_ready")
        snapshot(c, gen, epoch, 3, [idle(1, "1", "done")])
        self.assertEqual(combined_mode(c), "idle_ready", "a notice retired with its pane never returns")

    def test_new_notice_id_gets_a_new_deadline(self):
        c, clock, _, _, _ = make_coordinator(Limits(done_notice=15))
        gen, epoch = activate(c, [idle(1, "1", "done")])
        advance(c, clock, 14)
        snapshot(c, gen, epoch, 2, [idle(1, "2", "done")])
        advance(c, clock, 10)
        self.assertEqual(combined_mode(c), "completed")

    def test_notices_are_not_replayed_into_a_new_generation(self):
        c, clock, _, _, _ = make_coordinator(Limits(done_notice=15))
        activate(c, [idle(1, "1", "done")])
        advance(c, clock, 5)
        reg = register(c)
        snapshot(c, reg["generation"], reg["epoch"], 1, [idle(1)])
        self.assertEqual(combined_mode(c), "idle_ready")
        # The new VM's counter restarts at "1": that is a fresh notice, shown fresh.
        snapshot(c, reg["generation"], reg["epoch"], 2, [idle(1, "1", "failed")])
        self.assertEqual(combined_mode(c), "blocked_error")

    def test_late_completion_never_greens_an_active_pane(self):
        c, _, _, _, _ = make_coordinator()
        activate(c, [{"pane": 1, "state": "working", "notice": {"id": "1", "kind": "done"}}])
        self.assertEqual(combined_mode(c), "working")

    def test_status_is_a_pure_read(self):
        c, clock, _, _, _ = make_coordinator(Limits(done_notice=15))
        activate(c, [idle(1, "1", "done")])
        clock.advance(16)
        before = c.counters["notices_expired"]
        status = c.status()
        self.assertEqual(status["observers"][0]["active_notices"], [])
        self.assertEqual(status["rows"], {"completed": 1})
        self.assertEqual(c.counters["notices_expired"], before)
        self.assertEqual(combined_mode(c), "completed", "status must not change what the reaper has not yet applied")
        c.tick()
        self.assertEqual(combined_mode(c), "idle_ready")

    def test_bookkeeping_stays_bounded(self):
        c, _, _, _, _ = make_coordinator()
        gen, epoch = activate(c, [idle(1, "1", "done")])
        for i in range(2, 5000):
            snapshot(c, gen, epoch, i, [idle(1, str(i), "done")])
        observer = c.observers["4242:"]
        self.assertLessEqual(len(observer.retired_notices), 1024)
        self.assertEqual(len(observer.notices), 1)

    def test_discontinuity_voids_notices(self):
        c, clock, _, _, _ = make_coordinator(Limits(done_notice=15))
        gen, epoch = activate(c, [idle(1, "1", "done")])
        clock.advance(1, wall_seconds=600)
        c.tick()
        snapshot(c, gen, epoch, 2, [idle(1, "1", "done")])
        self.assertEqual(combined_mode(c), "idle_ready")

    def test_notice_tracks_are_per_row_key(self):
        c, _, _, _, _ = make_coordinator()
        gen, epoch = activate(c, [idle(1, "1", host="host-a"), idle(1, host="host-b")])
        snapshot(c, gen, epoch, 2, [idle(1, "1", host="host-a"), working(1, host="host-b")])
        self.assertCountEqual(rows(c), [
            ("host-a", "1", "completed", None),
            ("host-b", "1", "working", None),
        ])
        snapshot(c, gen, epoch, 3, [idle(1, "1", host="host-a"), idle(1, "1", host="host-b")])
        self.assertCountEqual(rows(c), [
            ("host-a", "1", "completed", None),
            ("host-b", "1", "completed", None),
        ])

    def test_notice_sets_only_its_own_row(self):
        c, _, _, _, _ = make_coordinator()
        gen, epoch = activate(c, [idle(1, "1", "failed", host="host-a"), idle(1, host="host-b")])
        self.assertCountEqual(rows(c), [
            ("host-a", "1", "blocked_error", None),
            ("host-b", "1", "idle_ready", None),
        ])
        snapshot(c, gen, epoch, 2, [idle(1, host="host-b")])
        snapshot(c, gen, epoch, 3, [idle(1, "1", "failed", host="host-a"), idle(1, "1", host="host-b")])
        self.assertCountEqual(rows(c), [
            ("host-a", "1", "idle_ready", None),
            ("host-b", "1", "completed", None),
        ])


if __name__ == "__main__":
    unittest.main()
