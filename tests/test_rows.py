import unittest

from tests.helpers import activate, advance, combined_mode, idle, make_coordinator, register, rows, scope, snapshot, waiting, working

from tern_sidepulse.limits import Limits
from tern_sidepulse.reducer import LABEL_MAX_CHARS, Observation, Row, reduce_rows, sanitize_label


OTHER = {"parent_pid": 4242, "window_key": "2"}


class RowReductionTest(unittest.TestCase):
    def test_same_key_keeps_highest_priority_mode(self):
        contributions = [
            (("local", "1"), "completed", 1, None),
            (("local", "1"), "working", 2, None),
            (("local", "1"), "waiting_for_input", 3, None),
            (("local", "1"), "idle_ready", 4, None),
        ]
        for items in (contributions, reversed(contributions)):
            self.assertEqual(reduce_rows(items), (Row("local", "1", "waiting_for_input", None),))

    def test_label_from_newest_contribution_with_label(self):
        contributions = [
            (("local", "1"), "idle_ready", 9, "Newest"),
            (("local", "1"), "working", 2, "Older"),
        ]
        self.assertEqual(reduce_rows(contributions), (Row("local", "1", "working", "Newest"),))

    def test_newest_without_label_keeps_older_label(self):
        self.assertEqual(reduce_rows([
            (("local", "1"), "working", 2, "Known"),
            (("local", "1"), "idle_ready", 9, None),
        ]), (Row("local", "1", "working", "Known"),))

    def test_same_pane_two_hosts_two_rows(self):
        self.assertCountEqual(reduce_rows([
            (("local", "1"), "working", 1, "Here"),
            (("remote", "1"), "completed", 2, "There"),
        ]), [Row("local", "1", "working", "Here"), Row("remote", "1", "completed", "There")])


class LabelSanitizerTest(unittest.TestCase):
    def test_omp_glyph_dropped(self):
        for glyph in ("\U000f1517", "\uf412", "\ue000", "\uf8ff", "\U000f0000", "\U000fffff", "\U00100000", "\U0010ffff"):
            with self.subTest(glyph=ascii(glyph)):
                self.assertEqual(sanitize_label(glyph + " DOCS: Review docs"), "DOCS: Review docs")

    def test_glyph_variants_give_same_label(self):
        labels = [sanitize_label(glyph + " Session title") for glyph in ("\U000f1517", "\U000f0837", "\uf412", "\uf407")]
        self.assertEqual(labels, ["Session title"] * 4)

    def test_whitespace_mapped_before_controls_dropped(self):
        self.assertEqual(sanitize_label("a\tb\x1b[31mc\x7f\x85d"), "a b[31mc d")
        self.assertEqual(sanitize_label(" a\x1c\x1d\x1e\x1f\r\n\t\v\f\x85\xa0\u2028\u2029\u3000b "), "a b")
        self.assertEqual(sanitize_label("a\x00\x01\x1b\x7f\x80\x9fb"), "ab")
        self.assertEqual(sanitize_label("a \uf412  b"), "a b")

    def test_lone_surrogates_dropped(self):
        label = sanitize_label("a\ud800 b\udfff")
        self.assertEqual(label, "a b")
        self.assertEqual(label.encode("utf-8"), b"a b")

    def test_cap_96_code_points_then_rstrip(self):
        self.assertEqual(LABEL_MAX_CHARS, 96)
        self.assertEqual(sanitize_label("é" * 128), "é" * 96)
        self.assertEqual(sanitize_label("x" * 95 + " tail"), "x" * 95)

    def test_empty_after_sanitizing_is_none(self):
        for raw in ("", " \t\r\n", "\U000f1517 \uf412\ud800\x00\x7f\x9f"):
            with self.subTest(raw=ascii(raw)):
                self.assertIsNone(sanitize_label(raw))

    def test_non_ascii_text_kept(self):
        self.assertEqual(sanitize_label("  Révision 日本語 e\u0301 🚀  "), "Révision 日本語 e\u0301 🚀")


class RowIdentityTest(unittest.TestCase):
    def test_missing_host_is_local(self):
        c, _, _, _, _ = make_coordinator()
        activate(c, [working(1), {"pane": 2, "state": "idle", "host": None}, working(3, host="local")])
        self.assertCountEqual(rows(c), [
            ("local", "1", "working", None),
            ("local", "2", "idle_ready", None),
            ("local", "3", "working", None),
        ])
        self.assertEqual(Observation("1", "working").key, ("local", "1"))

    def test_same_pane_on_two_hosts_is_two_rows(self):
        c, _, _, _, _ = make_coordinator()
        activate(c, [working(1, host="local"), waiting(1, host="sf-studio.tailfb479a.ts.net")])
        self.assertCountEqual(rows(c), [
            ("local", "1", "working", None),
            ("sf-studio.tailfb479a.ts.net", "1", "waiting_for_input", None),
        ])
        self.assertEqual(combined_mode(c), "waiting_for_input")

    def test_duplicate_key_in_snapshot_keeps_first(self):
        c, _, _, _, _ = make_coordinator()
        activate(c, [working(1, label="First"), {"pane": "1", "host": "local", "state": "waiting_input", "label": "Second"}])
        self.assertEqual(rows(c), [("local", "1", "working", "First")])

    def test_withdrawn_states_make_no_row(self):
        c, _, _, _, _ = make_coordinator()
        activate(c, [{"pane": i, "state": state, "label": "Withdrawn"} for i, state in enumerate(("exited", "closed", "unknown"))])
        self.assertEqual(c.rows, ())
        self.assertEqual(combined_mode(c), "idle_ready")


class CrossObserverTest(unittest.TestCase):
    def test_same_key_two_windows_one_row_higher_mode(self):
        c, _, sender, _, _ = make_coordinator()
        activate(c, [working(1, label="First")])
        activate(c, [waiting(1, label="Second")], observer=OTHER)
        self.assertEqual(rows(c), [("local", "1", "waiting_for_input", "Second")])
        self.assertEqual(sender.states(), ["working", "blocked"])
        self.assertEqual(sender.shown(), {scope(1): ("blocked", "Second")})

    def test_moving_session_never_doubles(self):
        c, _, sender, _, _ = make_coordinator()
        gen_a, epoch = activate(c, [working(1)])
        gen_b, _ = activate(c, [working(1)], observer=OTHER)
        self.assertEqual(rows(c), [("local", "1", "working", None)])
        snapshot(c, gen_a, epoch, 2, [])
        self.assertEqual(rows(c), [("local", "1", "working", None)])
        snapshot(c, gen_b, epoch, 2, [], observer=OTHER)
        self.assertEqual(c.rows, ())
        self.assertEqual(sender.states(), ["working", "clear"])
        self.assertEqual(sender.shown(), {})

    def test_uncertain_observer_contributes_no_rows(self):
        c, clock, _, _, _ = make_coordinator()
        activate(c, [working(1, label="Stale")])
        gen_b, epoch = activate(c, [idle(2, label="Other")], observer=OTHER)
        clock.advance(1, wall_seconds=600)
        c.tick()
        self.assertEqual(c.rows, ())
        self.assertEqual(combined_mode(c), "idle_ready")
        snapshot(c, gen_b, epoch, 2, [idle(1, label="Fresh")], observer=OTHER)
        self.assertEqual(rows(c), [("local", "1", "idle_ready", "Fresh")])

    def test_expired_observer_withdraws_only_its_rows(self):
        c, clock, _, _, _ = make_coordinator(Limits(lease=10))
        activate(c, [working(1, label="Older shared"), working(2, label="Expires")])
        advance(c, clock, 5)
        activate(c, [waiting(1, label="Survives"), idle(3)], observer=OTHER)
        advance(c, clock, 5)
        self.assertEqual(len(c.observers), 1)
        self.assertCountEqual(rows(c), [("local", "1", "waiting_for_input", "Survives"), ("local", "3", "idle_ready", None)])

    def test_notice_in_one_window_never_outranks_work_in_another(self):
        c, _, sender, _, _ = make_coordinator()
        gen_a, epoch = activate(c, [working(1)])
        activate(c, [idle(1, "1", "done")], observer=OTHER)
        self.assertEqual(rows(c), [("local", "1", "working", None)])
        snapshot(c, gen_a, epoch, 2, [])
        self.assertEqual(rows(c), [("local", "1", "completed", None)])
        self.assertEqual(sender.states(), ["working", "done"])
        self.assertEqual(sender.shown(), {scope(1): ("done", "OMP in Tern")})


class LabelPrecedenceTest(unittest.TestCase):
    def test_newest_complete_snapshot_wins(self):
        c, _, _, _, _ = make_coordinator()
        gen_a, epoch = activate(c, [working(1, label="First")])
        gen_b, _ = activate(c, [idle(1, label="Second")], observer=OTHER)
        self.assertEqual(rows(c), [("local", "1", "working", "Second")])
        snapshot(c, gen_a, epoch, 2, [working(1, label="Renamed")])
        self.assertEqual(rows(c), [("local", "1", "working", "Renamed")])
        snapshot(c, gen_b, epoch, 2, [idle(1)], observer=OTHER)
        self.assertEqual(rows(c), [("local", "1", "working", "Renamed")])
        snapshot(c, gen_b, epoch, 3, [idle(1, label="Newest")], observer=OTHER)
        self.assertEqual(rows(c), [("local", "1", "working", "Newest")])
        snapshot(c, gen_a, epoch, 3, [working(1)])
        self.assertEqual(rows(c), [("local", "1", "working", "Newest")])
        snapshot(c, gen_b, epoch, 4, [], observer=OTHER)
        self.assertEqual(rows(c), [("local", "1", "working", None)])

    def test_unknown_coverage_never_changes_labels(self):
        c, _, _, _, _ = make_coordinator()
        gen, epoch = activate(c, [working(1, label="Older complete")])
        activate(c, [idle(1, label="Newest complete")], observer=OTHER)
        ack = snapshot(c, gen, epoch, 2, [waiting(1, label="Unknown")], coverage="unknown")
        self.assertTrue(ack["ok"], ack)
        self.assertFalse(ack["renewed"])
        self.assertEqual(rows(c), [("local", "1", "working", "Newest complete")])

    def test_fenced_snapshot_never_changes_labels(self):
        c, _, _, _, _ = make_coordinator()
        stale = register(c)
        gen, epoch = activate(c, [working(1, label="Accepted")])
        snapshot(c, gen, epoch, 3, [working(1, label="Accepted")])
        activate(c, [idle(1, label="Other accepted")], observer=OTHER)
        for generation, request_epoch, seq, error in (
            (gen, "old-epoch", 4, "epoch_mismatch"),
            (gen + 100, epoch, 4, "unknown_token"),
            (stale["generation"], epoch, 4, "stale_generation"),
            (gen, epoch, 2, "seq_not_increasing"),
            (gen, epoch, 3, None),
        ):
            with self.subTest(error=error):
                ack = snapshot(c, generation, request_epoch, seq, [waiting(1, label="Fenced")])
                if error:
                    self.assertEqual(ack["error"], error)
                else:
                    self.assertTrue(ack["duplicate"])
                self.assertEqual(rows(c), [("local", "1", "working", "Other accepted")])
        activate(c, [working(1, label="Replacement")])
        ack = snapshot(c, gen, epoch, 99, [waiting(1, label="Retired")])
        self.assertEqual(ack["error"], "expired_generation")
        self.assertEqual(rows(c), [("local", "1", "working", "Replacement")])

    def test_label_change_alone_updates_exactly_one_report(self):
        c, _, sender, _, _ = make_coordinator()
        gen, epoch = activate(c, [working(1, label="\U000f1517 Original")])
        original = sender.reports[0]
        snapshot(c, gen, epoch, 2, [working(1, label="\uf412 Renamed")])
        self.assertEqual(rows(c), [("local", "1", "working", "Renamed")])
        self.assertEqual(len(sender.reports), 2)
        renamed = sender.reports[1]
        self.assertEqual(renamed["scope"], original["scope"])
        self.assertNotEqual(renamed["request_id"], original["request_id"])
        self.assertEqual(sender.shown(), {scope(1): ("working", "Renamed")})
        snapshot(c, gen, epoch, 3, [working(1, label="\uf412 Renamed")])
        self.assertEqual(len(sender.reports), 2, "an unchanged label does not resend")

    def test_withdrawn_observation_label_ignored(self):
        c, _, _, _, _ = make_coordinator()
        activate(c, [working(1, label="Live")])
        activate(c, [{"pane": 1, "state": "closed", "label": "Withdrawn"}], observer=OTHER)
        self.assertEqual(rows(c), [("local", "1", "working", "Live")])


class WireValidationTest(unittest.TestCase):
    def test_bad_host_rejected_fixed_message(self):
        c, _, _, _, _ = make_coordinator()
        reg = register(c)
        for host in ("", 5, True, [], {}, "a\nb", "\x00", "\x7f", "\x85", "\x9f", "\ud800", "\udfff", "x" * 129):
            with self.subTest(host=ascii(host)):
                ack = snapshot(c, reg["generation"], reg["epoch"], 1, [{"pane": 1, "state": "working", "host": host}])
                self.assertEqual(ack["error"], "bad_request")
                self.assertEqual(ack["detail"], "observation.host must be a short host key")
                self.assertEqual(c.rows, ())

    def test_host_at_limit_accepted(self):
        c, _, _, _, _ = make_coordinator()
        host = "é" * 128
        activate(c, [working(1, host=host)])
        self.assertEqual(rows(c), [(host, "1", "working", None)])

    def test_bad_label_dropped_not_rejected(self):
        c, _, _, _, _ = make_coordinator()
        reg = register(c)
        for seq, label in enumerate((7, True, [], {}, "x" * 1025), 1):
            with self.subTest(label=ascii(label)):
                ack = snapshot(c, reg["generation"], reg["epoch"], seq, [{"pane": 1, "state": "working", "label": label}])
                self.assertTrue(ack["ok"], ack)
                self.assertEqual(rows(c), [("local", "1", "working", None)])
                self.assertEqual(c.status()["counters"]["labels_rejected"], seq)

    def test_label_at_window_cap_accepted(self):
        c, _, _, _, _ = make_coordinator()
        gen, epoch = activate(c, [working(1, label="é" * 128)])
        self.assertEqual(rows(c), [("local", "1", "working", "é" * 96)])
        snapshot(c, gen, epoch, 2, [working(1, label="x" * 1024)])
        self.assertEqual(rows(c), [("local", "1", "working", "x" * 96)])
        snapshot(c, gen, epoch, 3, [{"pane": 1, "state": "working", "label": None}, working(2), working(3, label="\uf412 \t")])
        self.assertTrue(all(row.label is None for row in c.rows))
        self.assertEqual(c.counters["labels_rejected"], 0)

    def test_status_reports_counts_only(self):
        c, _, _, _, _ = make_coordinator()
        reg = register(c)
        ack = snapshot(c, reg["generation"], reg["epoch"], 1, [
            idle(1, "1", "failed", host="private-host", label="Error label"),
            waiting(2, label="Question label"),
            working(3),
            idle(4, "1", label="\uf412"),
            idle(5, label="Idle label"),
        ])
        expected = {"blocked_error": 1, "waiting_for_input": 1, "working": 1, "completed": 1, "idle_ready": 1}
        self.assertEqual(ack["rows"], expected)
        self.assertNotIn("desired", ack)
        status = c.status()
        self.assertEqual(status["rows"], expected)
        self.assertEqual(status["rows_total"], 5)
        self.assertEqual(status["rows_labelled"], 3)
        self.assertNotIn("desired", status)
        self.assertCountEqual(status["observers"][0]["active_notices"], [{"pane": "1", "kind": "failed"}, {"pane": "4", "kind": "done"}])

    def test_resync_and_disable_report_counts_only(self):
        c, _, sender, _, _ = make_coordinator()
        activate(c, [waiting(1, label="Question"), working(2, label="One"), working(3, label="Two")])
        resync = c.handle({"schema": 1, "op": "resync"})
        self.assertEqual(resync["rows"], {"waiting_for_input": 1, "working": 2})
        self.assertEqual(resync["summary"], "3 rows: waiting_for_input 1, working 2")
        self.assertNotIn("desired", resync)
        disabled = c.handle({"schema": 1, "op": "disable"})
        self.assertEqual(disabled["rows"], {})
        self.assertEqual(disabled["summary"], "no rows")
        self.assertNotIn("desired", disabled)
        self.assertEqual(resync["delivery"], {"program_status": {"rows": 3, "pending": 0, "error": None}})
        self.assertEqual(disabled["delivery"], {"program_status": {"rows": 0, "pending": 0, "error": None}})
        self.assertEqual(sender.states(), ["blocked", "working", "working"] * 2 + ["clear"] * 3)
        self.assertEqual(sender.shown(), {})


if __name__ == "__main__":
    unittest.main()
