import json
import unittest

from tests.helpers import SENTINEL, make_coordinator, register, snapshot

from tern_sidepulse.doctor import run_doctor


class PrivacyTest(unittest.TestCase):
    def test_sentinel_in_any_client_field_never_leaves_the_coordinator(self):
        c, _, sender, logs, _ = make_coordinator()
        reg = register(c)
        observations = [
            {"pane": 1, "state": "working", "cwd": SENTINEL, "title": SENTINEL, "last_message": SENTINEL, "notice": {"id": "1", "kind": "done", "text": SENTINEL}},
        ]
        acks = [
            snapshot(c, reg["generation"], reg["epoch"], 1, observations, prompt=SENTINEL, diag={"omp": 1, "leak": SENTINEL}),
            snapshot(c, reg["generation"], reg["epoch"], 2, [{"pane": 2, "state": SENTINEL}]),
            snapshot(c, reg["generation"], reg["epoch"], 3, [{"pane": 3, "state": "idle", "notice": {"id": "9", "kind": SENTINEL}}]),
            c.handle({"schema": 1, "op": SENTINEL}),
            c.handle({"schema": 1, "op": "register", "observer": {"parent_pid": 4242, "window_key": SENTINEL * 3}}),
        ]
        status = c.status()
        everything = json.dumps(acks) + json.dumps(status) + "\n".join(logs)
        self.assertNotIn(SENTINEL, everything)
        self.assertEqual(sender.modes_for("events.sock"), ["idle_ready", "working"])

    def test_transport_payload_contains_only_status_metadata(self):
        c, _, sender, _, _ = make_coordinator()
        captured = []
        original = sender.__call__

        def spy(target, payload, timeout):
            captured.append(payload.decode("utf-8"))
            return original(target, payload, timeout)

        c.transport.sender = spy
        reg = register(c)
        snapshot(c, reg["generation"], reg["epoch"], 1, [{"pane": 1, "state": "working", "cwd": SENTINEL}])
        self.assertTrue(captured)
        for payload in captured:
            line = json.loads(payload)["line"]
            self.assertEqual(set(line), {"hook_event_name", "session_id", "logged_at", "sidepulse_status", "agent_origin", "agent_origin_kind", "bridge", "prompt"})
            self.assertNotIn(SENTINEL, payload)

    def test_doctor_report_filters_window_diag(self):
        from tests.helpers import short_tmpdir

        from tern_sidepulse.client import Settings
        from tern_sidepulse.coordinator import Limits

        tmp = short_tmpdir("tsp-doc-")
        settings = Settings(data_dir=tmp / "data", runtime_dir=tmp / "rt", ui_sockets=(tmp / "events.sock",), service_sockets=(tmp / "relay-events.sock",), limits=Limits(), bridge_path=tmp / "bridge.py")
        report = run_doctor(settings, {"omp": 2, "secret": SENTINEL, "pane_ids": [SENTINEL]}, window_key="")
        text = json.dumps(report)
        self.assertNotIn(SENTINEL, text)
        self.assertEqual(report["window_diag"], {"omp": 2})
        self.assertIn("socket delivery is not an LED check", report["summary"])


if __name__ == "__main__":
    unittest.main()
