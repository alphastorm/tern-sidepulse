import unittest

from tests.helpers import OBS, activate, combined_mode, idle, make_coordinator, register, scope, snapshot, working

from tern_sidepulse import VERSION
from tern_sidepulse.coordinator import ProtocolError, decode_request
from tern_sidepulse.limits import Limits


class FencingTest(unittest.TestCase):
    def test_unissued_and_foreign_tokens_are_rejected(self):
        c, _, _, _, _ = make_coordinator()
        reg = register(c)
        self.assertEqual(snapshot(c, 99, reg["epoch"], 1, [])["error"], "unknown_token")
        other = {"parent_pid": 7, "window_key": ""}
        self.assertEqual(snapshot(c, reg["generation"], reg["epoch"], 1, [], observer=other)["error"], "unknown_token")

    def test_epoch_mismatch_requires_fresh_registration(self):
        c, _, _, _, _ = make_coordinator()
        reg = register(c)
        self.assertEqual(snapshot(c, reg["generation"], "stale-epoch", 1, [])["error"], "epoch_mismatch")

    def test_later_activation_wins_and_older_token_becomes_stale(self):
        c, _, _, _, _ = make_coordinator()
        first = register(c)
        second = register(c)
        self.assertTrue(snapshot(c, second["generation"], second["epoch"], 1, [working(1)])["activated"])
        late = snapshot(c, first["generation"], first["epoch"], 5, [idle(1)])
        self.assertEqual(late["error"], "stale_generation")
        again = snapshot(c, first["generation"], first["epoch"], 6, [idle(1)])
        self.assertEqual(again["error"], "expired_generation")
        self.assertEqual(combined_mode(c), "working")

    def test_unactivated_higher_token_cannot_block_live_generation(self):
        c, _, _, _, _ = make_coordinator()
        gen, epoch = activate(c, [working(1)])
        register(c)  # a dropped VM's registration: allocated, never activated
        ack = snapshot(c, gen, epoch, 2, [idle(1)])
        self.assertTrue(ack["accepted"])
        self.assertEqual(combined_mode(c), "idle_ready")

    def test_replacing_a_generation_rejects_the_old_one_even_with_higher_seq(self):
        c, _, _, _, _ = make_coordinator()
        gen, epoch = activate(c, [working(1)])
        reg = register(c)
        snapshot(c, reg["generation"], epoch, 1, [idle(1)])
        self.assertEqual(snapshot(c, gen, epoch, 50, [working(1)])["error"], "expired_generation")
        self.assertEqual(combined_mode(c), "idle_ready")

    def test_sequence_rules(self):
        c, clock, _, _, _ = make_coordinator()
        gen, epoch = activate(c, [working(1)])
        clock.advance(1)
        dup = snapshot(c, gen, epoch, 1, [idle(1)])
        self.assertTrue(dup["duplicate"])
        self.assertFalse(dup["renewed"])
        self.assertEqual(combined_mode(c), "working", "a duplicate seq carries no new content")
        self.assertEqual(snapshot(c, gen, epoch, 0, [idle(1)])["error"], "bad_request")
        snapshot(c, gen, epoch, 3, [idle(1)])
        self.assertEqual(snapshot(c, gen, epoch, 2, [working(1)])["error"], "seq_not_increasing")
        self.assertEqual(combined_mode(c), "idle_ready")

    def test_expired_lease_retires_the_generation(self):
        c, clock, _, _, _ = make_coordinator(Limits(lease=5))
        gen, epoch = activate(c, [working(1)])
        clock.advance(6)
        self.assertEqual(snapshot(c, gen, epoch, 2, [working(1)])["error"], "expired_generation")
        self.assertEqual(combined_mode(c), "idle_ready")

    def test_disable_revokes_everything_until_enable(self):
        c, _, sender, _, latch = make_coordinator()
        gen, epoch = activate(c, [working(1)])
        self.assertTrue(c.handle({"schema": 1, "op": "disable"})["disabled"])
        self.assertEqual(latch, [True])
        self.assertEqual(combined_mode(c), "idle_ready")
        self.assertEqual(c.rows, ())
        self.assertEqual(c.status()["rows"], {})
        self.assertEqual(sender.shown(), {})
        self.assertEqual(sender.states(), ["working", "clear"])
        self.assertEqual(snapshot(c, gen, epoch, 2, [working(1)])["error"], "disabled")
        self.assertEqual(register(c)["error"], "disabled")
        c.handle({"schema": 1, "op": "enable"})
        activate(c, [working(1)])
        self.assertEqual(combined_mode(c), "working")
        self.assertEqual(sender.states(), ["working", "clear", "working"])
        self.assertEqual(sender.shown(), {scope(1): ("working", "OMP in Tern")})

    def test_token_eviction_and_bounded_bookkeeping(self):
        c, _, _, _, _ = make_coordinator(Limits(max_tokens=8))
        first = register(c)
        for _ in range(20):
            register(c)
        self.assertEqual(len(c.tokens), 8)
        self.assertEqual(snapshot(c, first["generation"], first["epoch"], 1, [])["error"], "expired_generation")
        self.assertLessEqual(len(c.retire_reason), 256)
        for _ in range(3000):
            register(c)
        self.assertLessEqual(len(c.tokens), 8)
        self.assertLessEqual(len(c.retire_reason), 256)

    def test_observer_limit_applies_at_activation(self):
        c, _, _, _, _ = make_coordinator(Limits(max_observers=1))
        activate(c, [working(1)])
        other = {"parent_pid": 4242, "window_key": "2"}
        reg = register(c, other)
        self.assertEqual(reg["error"], "too_many_observers")

    def test_version_mismatch_stops_the_old_coordinator(self):
        c, _, _, _, _ = make_coordinator()
        response = c.handle({"schema": 1, "op": "register", "observer": OBS, "client_version": "9.9.9"})
        self.assertEqual(response["error"], "version_mismatch")
        self.assertEqual(response["version"], VERSION)
        self.assertEqual(c.tick(), "exit")

    def test_malformed_state_is_rejected_without_crashing(self):
        c, _, _, _, _ = make_coordinator()
        reg = register(c)
        bad = snapshot(c, reg["generation"], reg["epoch"], 1, [{"pane": 1, "state": {}}])
        self.assertEqual(bad["error"], "bad_request")
        self.assertEqual(c.handle({"schema": 2, "op": "status"})["error"], "unsupported_schema")
        self.assertEqual(c.handle({"schema": 1, "op": "nope"})["error"], "unknown_op")
        self.assertEqual(c.handle([])["error"], "bad_request")
        self.assertTrue(c.handle({"schema": 1, "op": "status"})["ok"])

    def test_window_key_must_be_printable(self):
        # A newline would forge coordinator.log lines; a lone surrogate crashed the reaper when the lease expired.
        c, _, _, _, _ = make_coordinator()
        for key in ("a\nb", "\ud800", "\x1b[31m", "x" * 65):
            self.assertEqual(register(c, {"parent_pid": 1, "window_key": key})["error"], "bad_request", repr(key))
        self.assertTrue(register(c, {"parent_pid": 1, "window_key": "héllo 2"})["ok"], "printable non-ASCII keys are fine")

    def test_non_finite_and_deeply_nested_requests_are_malformed_not_fatal(self):
        c, _, _, _, _ = make_coordinator()
        with self.assertRaises(ProtocolError) as caught:
            decode_request(b'{"schema": 1, "op": "status", "diag": {"omp": Infinity}}')
        self.assertEqual(caught.exception.error, "malformed_json")
        with self.assertRaises(ProtocolError):
            decode_request(b'{"schema": 1, "op": "snapshot", "limits": NaN}')
        deep = b"[" * 200_000 + b"]" * 200_000
        try:
            decoded = decode_request(deep)  # newer interpreters decode it; older ones must reject, never recurse out
        except ProtocolError as exc:
            self.assertEqual(exc.error, "malformed_json")
        else:
            self.assertEqual(c.handle(decoded)["error"], "bad_request")
        self.assertTrue(c.handle({"schema": 1, "op": "status"})["ok"])


if __name__ == "__main__":
    unittest.main()
