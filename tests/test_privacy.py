import json
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.helpers import (
    SENTINEL,
    FakeReceiver,
    activate,
    advance,
    idle,
    kill_coordinator,
    make_coordinator,
    register,
    run_bridge,
    short_tmpdir,
    snapshot,
    subprocess_env,
    waiting,
    working,
)

from tern_sidepulse.client import Settings
from tern_sidepulse.doctor import run_doctor
from tern_sidepulse.limits import Limits
from tern_sidepulse.paths import DOCTOR_NAME, LOG_NAME

SENTINEL_HOST = "SENTINEL-host-94b2-private"
RAW_LABEL = "\U000f1517 \t" + SENTINEL + "\n\uf412"


def doctor_settings(tmp: Path) -> Settings:
    return Settings(
        data_dir=tmp / "data",
        runtime_dir=tmp / "rt",
        ui_sockets=(tmp / "events.sock",),
        service_sockets=(tmp / "relay-events.sock",),
        limits=Limits(),
        bridge_path=tmp / "bridge.py",
    )


class PrivacyTest(unittest.TestCase):
    def assert_private(self, *values):
        text = "\n".join(value if isinstance(value, str) else json.dumps(value) for value in values)
        self.assertNotIn(SENTINEL, text)
        self.assertNotIn(SENTINEL_HOST, text)

    def test_sentinels_in_labels_hosts_and_unknown_fields_never_leave_the_coordinator(self):
        c, _, sender, logs, _ = make_coordinator()
        reg = register(c)
        observations = [{
            "pane": 1,
            "state": "working",
            "host": SENTINEL_HOST,
            "label": RAW_LABEL,
            "cwd": SENTINEL,
            "title": SENTINEL,
            "model": SENTINEL,
            "last_message": SENTINEL,
            "notice": {"id": "1", "kind": "done", "text": SENTINEL},
        }]
        acks = [
            snapshot(c, reg["generation"], reg["epoch"], 1, observations, prompt=SENTINEL, diag={"omp": 1, "leak": SENTINEL}),
            snapshot(c, reg["generation"], reg["epoch"], 2, [{"pane": 2, "state": SENTINEL}]),
            snapshot(c, reg["generation"], reg["epoch"], 3, [{"pane": 3, "state": "idle", "notice": {"id": "9", "kind": SENTINEL}}]),
            c.handle({"schema": 1, "op": SENTINEL}),
            c.handle({"schema": 1, "op": "register", "observer": {"parent_pid": 4242, "window_key": SENTINEL * 3}}),
            c.handle({"schema": 1, "op": "status"}),
        ]
        status = c.status()
        settings = doctor_settings(short_tmpdir("tsp-doc-private-"))
        with patch("tern_sidepulse.doctor.request", return_value={"ok": True, "status": status}):
            report = run_doctor(settings)
        self.assertIsNotNone(report["report"])
        self.assert_private(acks, status, logs, report, Path(report["report"]).read_text(encoding="utf-8"))
        self.assertEqual(sender.modes_for("events.sock"), ["idle_ready", "working"])
        self.assertEqual(sender.modes_for("relay-events.sock"), ["idle_ready", "working"])

    def test_label_is_held_in_memory_only(self):
        c, _, _, _, _ = make_coordinator()
        activate(c, [working(1, host=SENTINEL_HOST, label=RAW_LABEL)])
        self.assertEqual(len(c.rows), 1)
        self.assertEqual(c.rows[0].label, SENTINEL)
        self.assertEqual(c.rows[0].host, SENTINEL_HOST)
        self.assert_private(c.status())

    def test_hook_event_payload_never_carries_labels(self):
        c, _, sender, _, _ = make_coordinator()
        captured = []
        original = sender.__call__

        def spy(target, payload, timeout):
            captured.append(payload.decode("utf-8"))
            return original(target, payload, timeout)

        c.transport.sender = spy
        gen, epoch = activate(c, [working(1, host=SENTINEL_HOST, label=RAW_LABEL)])
        for seq, obs in enumerate((
            waiting(1, host=SENTINEL_HOST, label=RAW_LABEL),
            idle(1, "1", "failed", host=SENTINEL_HOST, label=RAW_LABEL),
            idle(1, "2", host=SENTINEL_HOST, label=RAW_LABEL),
            idle(1, host=SENTINEL_HOST, label=RAW_LABEL),
        ), 2):
            self.assertTrue(snapshot(c, gen, epoch, seq, [obs])["ok"])
        self.assertTrue(captured)
        for payload in captured:
            event = json.loads(payload)
            self.assertEqual(set(event), {"provider", "line"})
            self.assertEqual(event["provider"], "omp")
            line = event["line"]
            expected = {"hook_event_name", "session_id", "logged_at", "sidepulse_status", "agent_origin", "agent_origin_kind", "bridge"}
            if line["sidepulse_status"] == "working":
                expected.add("prompt")
                self.assertEqual(line["prompt"], "OMP in Tern")
            self.assertEqual(set(line), expected)
            self.assert_private(payload)
        self.assertEqual(sender.modes_for("events.sock"), ["idle_ready", "working", "waiting_for_input", "blocked_error", "completed", "idle_ready"])

    def test_rejections_never_echo_labels_or_hosts(self):
        c, _, _, logs, _ = make_coordinator()
        reg = register(c)
        responses = []
        for host in (SENTINEL_HOST + "\n", SENTINEL_HOST + "\ud800", SENTINEL_HOST * 10):
            ack = snapshot(c, reg["generation"], reg["epoch"], 1, [working(1, host=host, label=RAW_LABEL)])
            self.assertEqual(ack["error"], "bad_request")
            self.assertEqual(ack["detail"], "observation.host must be a short host key")
            responses.append(ack)
        for seq, label in enumerate((SENTINEL * 100, {"secret": SENTINEL}), 1):
            ack = snapshot(c, reg["generation"], reg["epoch"], seq, [{"pane": 1, "state": "working", "host": SENTINEL_HOST, "label": label}])
            self.assertTrue(ack["ok"], ack)
            self.assertIsNone(c.rows[0].label)
            responses.append(ack)
        self.assertEqual(c.counters["labels_rejected"], 2)
        self.assert_private(responses, c.status(), logs)

    def test_lifecycle_logs_never_carry_labels_or_hosts(self):
        c, clock, _, logs, _ = make_coordinator(Limits(lease=2))
        observation = idle(1, "1", host=SENTINEL_HOST, label=RAW_LABEL)
        activate(c, [observation])
        activate(c, [observation])  # replacement
        advance(c, clock, 2)  # expiry
        gen, epoch = activate(c, [observation])
        clock.advance(1, wall_seconds=600)
        c.tick()
        responses = [snapshot(c, gen, epoch, 2, [working(1, host=SENTINEL_HOST, label=RAW_LABEL)])]

        def persist_failure(_flag):
            raise OSError(13, SENTINEL)

        c.persist_disabled = persist_failure
        responses.append(c.handle({"schema": 1, "op": "disable"}))

        def internal_failure(_request):
            raise RuntimeError(SENTINEL + SENTINEL_HOST)

        c.op_status = internal_failure
        responses.append(c.handle({"schema": 1, "op": "status"}))
        self.assertEqual(c.counters["expired_observers"], 1)
        self.assertEqual(c.discontinuities, 1)
        self.assertFalse(responses[1]["persisted"])
        self.assertEqual(responses[2]["error"], "internal")
        self.assertTrue(logs)
        self.assert_private(responses, c.status(), logs)

    def test_doctor_report_filters_window_diag(self):
        settings = doctor_settings(short_tmpdir("tsp-doc-"))
        report = run_doctor(settings, {"omp": 2, "label_tab": 1, "label_title": SENTINEL, "secret": SENTINEL, "pane_ids": [SENTINEL]}, window_key="")
        self.assert_private(report)
        self.assertEqual(report["window_diag"], {"omp": 2, "label_tab": 1})
        self.assertIn("socket delivery is not an LED check", report["summary"])


class LabelPrivacyEndToEndTest(unittest.TestCase):
    def test_status_doctor_log_and_receiver_never_carry_labels(self):
        tmp = short_tmpdir("tsp-private-e2e-")
        ui = FakeReceiver(tmp / "events.sock")
        svc = FakeReceiver(tmp / "relay-events.sock")
        ui.start()
        svc.start()
        env = subprocess_env(tmp, ui.path, svc.path, {"lease": 30, "reaper": 0.2, "exit_grace": 1, "final_grace": 5})
        outputs = []
        errors = []

        def invoke(command, payload):
            code, out, err = run_bridge([command], payload, env, raw_utf8=True)
            outputs.append(out)
            errors.append(err)
            self.assertEqual(code, 0, err)
            self.assertTrue(out["ok"], out)
            return out

        try:
            reg = invoke("register", {})
            ack = invoke("snapshot", {
                "epoch": reg["epoch"],
                "generation": reg["generation"],
                "seq": 1,
                "coverage": "complete",
                "observations": [{
                    "pane": 1,
                    "state": "working",
                    "host": SENTINEL_HOST,
                    "label": RAW_LABEL,
                    "cwd": SENTINEL,
                    "title": SENTINEL,
                    "last_message": SENTINEL,
                    "notice": {"id": "1", "kind": "done", "text": SENTINEL},
                }],
                "prompt": SENTINEL,
                "diag": {"omp": 1, "secret": SENTINEL},
            })
            self.assertEqual(ack["rows"], {"working": 1})
            self.assertTrue(ui.wait_for("working"))
            self.assertTrue(svc.wait_for("working"))
            status = invoke("status", None)["status"]
            self.assertEqual(status["rows_total"], 1)
            self.assertEqual(status["rows_labelled"], 1)
            doctor = invoke("doctor", {"diag": {"omp": 1, "label_tab": 1, "secret": SENTINEL}})
            self.assertEqual(doctor["status"]["rows_labelled"], 1)
            detail = next(check["detail"] for check in doctor["checks"] if check["id"] == "coordinator")
            self.assertIn("rows 1 (working 1), 1 labelled, 1 observer(s), disabled=False", detail)
            runtime = Path(env["TERN_SIDEPULSE_RUNTIME_DIR"])
            self.assertEqual(Path(doctor["report"]), runtime / DOCTOR_NAME)
            surfaces = {
                "stdout": json.dumps(outputs),
                "stderr": "\n".join(errors),
                "doctor.json": (runtime / DOCTOR_NAME).read_text(encoding="utf-8"),
                "coordinator.log": (runtime / LOG_NAME).read_text(encoding="utf-8"),
                "ui events": json.dumps(ui.events),
                "service events": json.dumps(svc.events),
            }
            for surface, text in surfaces.items():
                with self.subTest(surface=surface):
                    self.assertNotIn(SENTINEL, text)
                    self.assertNotIn(SENTINEL_HOST, text)
            self.assertEqual(ui.modes(), ["idle_ready", "working"])
            self.assertEqual(svc.modes(), ["idle_ready", "working"])
        finally:
            kill_coordinator(env)
            ui.stop()
            svc.stop()


if __name__ == "__main__":
    unittest.main()
