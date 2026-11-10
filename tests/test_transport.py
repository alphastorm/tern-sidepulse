import json
import time
import unittest

from tests.helpers import SENTINEL, FakeClock, FakeProgramServer, RecordingSender, make_transport, scope, short_tmpdir

from tern_sidepulse.reducer import Row
from tern_sidepulse.transport import ACTIVE_REFRESH_SECONDS, MAX_ROWS, SEND_TIMEOUT_SECONDS, TICK_BUDGET_SECONDS, SidePulseTransport, ScopeLedger, exchange


def fresh(**kw) -> tuple[SidePulseTransport, RecordingSender, FakeClock, list[str]]:
    clock = FakeClock()
    sender = RecordingSender(short_tmpdir("tsp-tx-"))
    sender.clock = clock
    sender.up()
    logs: list[str] = []
    kw.setdefault("log", logs.append)
    return make_transport(sender, clock, **kw), sender, clock, logs


class ReportTest(unittest.TestCase):
    def test_states_and_context(self):
        t, sender, _, _ = fresh()
        modes = {"working": "working", "waiting_for_input": "blocked", "blocked_error": "error", "completed": "done", "idle_ready": "idle"}
        t.publish([Row("local", str(i), mode, f"Label {i}") for i, mode in enumerate(modes)])
        t.tick()
        self.assertEqual(sorted(sender.states()), sorted(modes.values()))
        for report in sender.reports:
            self.assertEqual(set(report), {"v", "type", "scope", "status", "context", "request_id"})
            self.assertEqual((report["v"], report["type"]), (1, "program_status"))
            self.assertEqual(set(report["status"]), {"state"}, "the daemon derives status.title from context.title")
            context = report["context"]
            self.assertEqual((context["provider"], context["origin"], context["title_source"]), ("omp", "Tern", "session"))
            self.assertTrue(context["title"].startswith("Label "))
            self.assertNotIn("display_name", context)
            self.assertEqual(report["scope"], "tern-sidepulse-" + context["session_id"])

    def test_unlabelled_rows_and_labels_off_send_an_empty_title(self):
        for labels, label in ((True, None), (False, "Private tab")):
            with self.subTest(labels=labels):
                t, sender, _, _ = fresh(labels=labels)
                t.publish([Row("local", "1", "working", label)])
                t.tick()
                context = sender.reports[-1]["context"]
                self.assertEqual(context["title"], "", "an empty session title stops SidePulse from keeping an older label")
                self.assertEqual(context["display_name"], "OMP in Tern")
                self.assertNotIn("Private", json.dumps(sender.reports))

    def test_scope_hides_host_and_pane_and_is_stable(self):
        t, sender, _, _ = fresh()
        t.publish([Row(SENTINEL, "949187772435", "working", None)])
        t.tick()
        payload = json.dumps(sender.reports)
        self.assertNotIn(SENTINEL, payload)
        self.assertNotIn("949187772435", payload)
        self.assertEqual(sender.reports[0]["scope"], scope("949187772435", SENTINEL))
        self.assertNotEqual(scope(1), scope(1, "remote"))


class PublishTest(unittest.TestCase):
    def test_publish_is_io_free_and_changes_send_once(self):
        t, sender, _, _ = fresh()
        t.publish([Row("local", "1", "working", "Work"), Row("remote", "2", "waiting_for_input", "Question")])
        self.assertEqual(sender.reports, [], "publishing records the rows without I/O")
        t.tick()
        self.assertEqual(sender.states(), ["blocked", "working"], "the most urgent row goes first")
        t.publish([Row("local", "1", "working", "Work"), Row("remote", "2", "waiting_for_input", "Question")])
        t.tick()
        self.assertEqual(len(sender.reports), 2, "an unchanged row is not resent")
        t.publish([Row("local", "1", "working", "Renamed"), Row("remote", "2", "waiting_for_input", "Question")])
        t.tick()
        self.assertEqual(len(sender.reports), 3)
        self.assertEqual(sender.shown()[scope(1)], ("working", "Renamed"))

    def test_force_resends_with_a_new_request_id(self):
        t, sender, _, _ = fresh()
        t.publish([Row("local", "1", "working", None)])
        t.tick()
        t.publish([Row("local", "1", "working", None)], force=True)
        t.tick()
        self.assertEqual(sender.states(), ["working", "working"])
        first, second = sender.reports
        self.assertNotEqual(first["request_id"], second["request_id"], "the daemon would answer a reused id from its receipt cache without applying it")

    def test_withdrawn_rows_are_cleared_and_leave_the_ledger(self):
        t, sender, _, _ = fresh()
        t.publish([Row("local", "1", "working", None), Row("local", "2", "idle_ready", None)])
        t.tick()
        self.assertEqual(t.ledger.scopes, {scope(1), scope(2)})
        t.publish([Row("local", "2", "idle_ready", None)])
        t.tick()
        self.assertEqual(set(sender.shown()), {scope(2)})
        self.assertEqual(t.ledger.scopes, {scope(2)})
        self.assertEqual(ScopeLedger(t.ledger.path).scopes, {scope(2)}, "the pruned ledger is on disk")
        t.publish(())
        t.tick()
        self.assertEqual(sender.shown(), {})
        self.assertEqual(t.undelivered_neutral(), [])

    def test_ledger_is_written_before_a_new_scope_is_shown(self):
        t, sender, _, _ = fresh()
        seen = []

        def spy(target, payload, timeout):
            seen.append(ScopeLedger(t.ledger.path).scopes)
            return sender(target, payload, timeout)

        t.sender = spy
        t.publish([Row("local", "1", "working", None)])
        t.tick()
        self.assertEqual(seen, [{scope(1)}])

    def test_unwritable_ledger_holds_new_rows_but_not_clears(self):
        t, sender, _, _ = fresh()
        t.publish([Row("local", "1", "working", None)])
        t.tick()
        t.ledger.path = sender.root / "missing-dir" / "program-scopes.json"
        t.publish([Row("local", "2", "working", None)])
        t.tick()
        self.assertEqual(sender.shown(), {}, "row 2, which the next coordinator could not find, is held back")
        self.assertEqual([(r["scope"], r["status"]["state"]) for r in sender.reports], [(scope(1), "working"), (scope(1), "clear")], "the row that left still gets its clear")
        self.assertIn("ledger", t.last_error or "")

    def test_rows_over_the_cap_drop_least_urgent_first(self):
        t, sender, _, _ = fresh()
        rows = [Row("local", f"{i:03}", "idle_ready", None) for i in range(MAX_ROWS)] + [Row("local", f"w{i}", "working", None) for i in range(8)]
        t.publish(rows)
        for _ in range(3):
            t.tick()
        self.assertEqual(len(sender.shown()), MAX_ROWS)
        self.assertEqual(sum(state == "working" for state, _ in sender.shown().values()), 8)
        self.assertEqual(t.to_dict()["rows_capped"], 8)


class DeliveryTest(unittest.TestCase):
    def test_backoff_grows_and_retries_keep_their_request_id(self):
        t, sender, clock, logs = fresh()
        sender.refuse.add("program-status.sock")
        t.publish([Row("local", "1", "working", None)])
        attempts, ids = [], set()
        original = sender.__call__

        def spy(target, payload, timeout):
            ids.add(json.loads(payload)["request_id"])
            return original(target, payload, timeout)

        t.sender = spy
        for second in range(70):
            before = t.failures
            t.publish([Row("local", "1", "working", None)])  # every reaper tick republishes the same rows
            t.tick()
            if t.failures != before:
                attempts.append(second)
            clock.advance(1)
        self.assertEqual(attempts[:6], [0, 2, 6, 14, 30, 60])
        self.assertEqual(len(ids), 1)
        self.assertEqual(sum("delivery failing" in line for line in logs), 1, "one line when delivery starts failing, never per retry")

    def test_change_during_backoff_waits_and_recovery_resets(self):
        t, sender, clock, logs = fresh()
        sender.refuse.add("program-status.sock")
        t.publish([Row("local", "1", "working", None)])
        t.tick()
        clock.advance(0.5)
        t.publish([Row("local", "1", "waiting_for_input", None)])
        self.assertFalse(t.tick(), "a change inside the backoff does not hammer the receiver")
        sender.refuse.clear()
        clock.advance(1.6)
        t.tick()
        self.assertEqual((t.failures, sender.states()), (0, ["blocked"]))
        self.assertTrue(any("recovered" in line for line in logs))

    def test_socket_appearing_during_backoff_is_tried_at_once(self):
        t, sender, clock, _ = fresh()
        sender.down()
        t.publish([Row("local", "1", "working", None)])
        t.tick()
        clock.advance(2.1)
        t.tick()
        self.assertEqual(t.failures, 2)
        sender.up()
        clock.advance(0.1)
        t.tick()
        self.assertEqual((t.failures, sender.states()), (0, ["working"]), "SidePulse starting after Tern is picked up before the 4 s backoff ends")

    def test_busy_backs_off_and_rejection_waits_for_a_change(self):
        t, sender, clock, logs = fresh()
        sender.answer = {"ok": False, "error": "Daemon is busy; retry"}
        t.publish([Row("local", "1", "working", f"{SENTINEL} tab")])
        t.tick()
        self.assertEqual((t.failures, t.last_error), (1, "program-status.sock: busy"))
        sender.answer = {"ok": False, "error": "title exceeds its limit or contains control characters"}
        clock.advance(2)
        t.tick()
        self.assertEqual(t.failures, 0, "a rejection is an answer, not an outage")
        self.assertEqual(t.to_dict()["rejected"], 1)
        clock.advance(60)
        self.assertFalse(t.tick(), "the same report is never resent")
        self.assertFalse(any(SENTINEL in line for line in logs))
        sender.answer = None
        t.publish([Row("local", "1", "working", "Shorter")])
        t.tick()
        self.assertEqual(sender.shown()[scope(1)], ("working", "Shorter"))

    def test_restarted_daemon_gets_its_rows_back_except_done(self):
        t, sender, clock, _ = fresh()
        t.publish([Row("local", "1", "working", "W"), Row("local", "2", "completed", "D"), Row("local", "3", "idle_ready", "I")])
        t.tick()
        sender.restart()
        clock.advance(1)
        t.tick()
        self.assertEqual({s: state for s, (state, _) in sender.shown().items()}, {scope(1): "working", scope(3): "idle"})
        self.assertEqual(sender.states().count("done"), 1, "a done row is never resent as a fresh completion")
        t.publish([Row("local", "1", "working", "W"), Row("local", "2", "idle_ready", "D"), Row("local", "3", "idle_ready", "I")])
        t.tick()
        self.assertEqual(sender.shown()[scope(2)], ("idle", "D"))

    def test_only_active_rows_are_refreshed(self):
        t, sender, clock, _ = fresh()
        t.publish([Row("local", "1", "working", None), Row("local", "2", "idle_ready", None)])
        t.tick()
        clock.advance(ACTIVE_REFRESH_SECONDS)
        self.assertLessEqual(t.next_due() or 0, clock.monotonic())
        t.tick()
        self.assertEqual(sender.states(), ["working", "idle", "working"])
        self.assertNotEqual(sender.reports[0]["request_id"], sender.reports[2]["request_id"])

    def test_hung_receiver_is_bounded_per_tick(self):
        t, sender, clock, _ = fresh()
        sender.cost = 5.0  # every attempt would block for 5 fake seconds
        t.publish([Row("local", str(i), "working", None) for i in range(10)])
        start = clock.monotonic()
        t.tick()
        self.assertLessEqual(clock.monotonic() - start, TICK_BUDGET_SECONDS + SEND_TIMEOUT_SECONDS + 0.01)

    def test_undelivered_neutral_needs_something_visible_and_a_receiver(self):
        t, sender, _, _ = fresh()
        self.assertEqual(t.undelivered_neutral(), [])
        t.publish([Row("local", "1", "working", None)])
        t.tick()
        self.assertEqual(t.undelivered_neutral(), ["program_status"])
        sender.down()
        self.assertEqual(t.undelivered_neutral(), [], "nothing can be visible when SidePulse is not listening")


class InheritedScopesTest(unittest.TestCase):
    def predecessor(self) -> tuple[SidePulseTransport, RecordingSender, FakeClock]:
        old, sender, clock, _ = fresh()
        old.publish([Row("local", "1", "working", "Still open"), Row("local", "2", "working", "Closed meanwhile")])
        old.tick()
        return old, sender, clock

    def test_still_open_rows_are_overwritten_and_the_rest_cleared_after_the_grace(self):
        _, sender, clock = self.predecessor()  # crashed: no withdraw
        logs: list[str] = []
        t = make_transport(sender, clock, inherit_grace=15.0, log=logs.append)
        self.assertEqual(t.inherited, {scope(1), scope(2)})
        self.assertTrue(any("2 scope(s) left by an earlier coordinator" in line for line in logs))
        self.assertEqual(t.undelivered_neutral(), ["program_status"], "a coordinator with inherited rows does not exit before clearing them")
        t.publish([Row("local", "1", "waiting_for_input", "Still open")])
        t.tick()
        self.assertNotIn("clear", sender.states(), "nothing is cleared inside the grace")
        self.assertEqual(sender.shown()[scope(1)], ("blocked", "Still open"))
        clock.advance(14.9)
        t.tick()
        self.assertIn(scope(2), sender.shown())
        clock.advance(0.2)
        t.tick()
        self.assertEqual(set(sender.shown()), {scope(1)})
        self.assertEqual(ScopeLedger(t.ledger.path).scopes, {scope(1)})
        self.assertEqual(sender.states().count("clear"), 1, "the overwritten row was never cleared and re-added")

    def test_withdraw_clears_inherited_scopes_at_once(self):
        _, sender, clock = self.predecessor()
        t = make_transport(sender, clock, inherit_grace=15.0)
        t.withdraw()
        t.tick(immediate=True)
        self.assertEqual(sender.shown(), {})
        self.assertEqual(t.ledger.scopes, set())

    def test_corrupt_or_foreign_ledger_entries_are_ignored(self):
        _, sender, clock = self.predecessor()
        path = sender.root / "program-scopes.json"
        path.write_text(json.dumps({"scopes": [scope(1), "claude-abc", 7, "tern-sidepulse-\u0000x"]}))
        self.assertEqual(make_transport(sender, clock).inherited, {scope(1)})
        path.write_text("{not json")
        self.assertEqual(make_transport(sender, clock).inherited, set())


class RealSocketTest(unittest.TestCase):
    def test_exchange_reads_the_acknowledgement_and_bounds_a_hung_receiver(self):
        tmp = short_tmpdir("tsp-rs-")
        receiver = FakeProgramServer(tmp / "program-status.sock")
        receiver.start()
        hung = FakeProgramServer(tmp / "hung.sock", hang=True)
        hung.start()
        try:
            reply = exchange(receiver.path, b'{"v":1,"type":"program_status","scope":"tern-sidepulse-x","status":{"state":"idle"}}', 1.0)
            self.assertEqual(reply, {"ok": True, "seq": 1, "revision": 1})
            started = time.monotonic()
            with self.assertRaises(OSError):
                exchange(hung.path, b"{}", 0.2)
            self.assertLess(time.monotonic() - started, 1.0, "a hung receiver costs one send timeout")
        finally:
            receiver.stop()
            hung.stop()

    def test_non_object_reply_is_an_error(self):
        tmp = short_tmpdir("tsp-rs-")
        receiver = FakeProgramServer(tmp / "program-status.sock", reply=["not", "an", "object"])  # type: ignore[arg-type]
        receiver.start()
        try:
            with self.assertRaises(OSError):
                exchange(receiver.path, b"{}", 1.0)
        finally:
            receiver.stop()


if __name__ == "__main__":
    unittest.main()
