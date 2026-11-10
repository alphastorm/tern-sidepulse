import json
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.helpers import (
    SENTINEL,
    FakeProgramServer,
    activate,
    advance,
    kill_coordinator,
    make_coordinator,
    register,
    run_bridge,
    scope,
    short_tmpdir,
    snapshot,
    subprocess_env,
)

from tern_sidepulse.client import Settings
from tern_sidepulse.doctor import run_doctor
from tern_sidepulse.limits import Limits
from tern_sidepulse.paths import DOCTOR_NAME, LEDGER_NAME, LOG_NAME

SENTINEL_HOST = "SENTINEL-host-94b2-private"
SENTINEL_PANE = "SENTINEL-pane-a72d-private"
SENTINEL_EXTRA = "SENTINEL-field-f831-private"
RAW_LABEL = "\U000f1517 \t" + SENTINEL + "\n\uf412"


def doctor_settings(tmp: Path) -> Settings:
    return Settings(
        data_dir=tmp / "data",
        runtime_dir=tmp / "rt",
        program_sockets=(tmp / "program-status.sock",),
        limits=Limits(),
        bridge_path=tmp / "bridge.py",
    )


def private_observation(state, notice=None, *, host=SENTINEL_HOST):
    observation = {"pane": SENTINEL_PANE, "state": state, "host": host, "label": RAW_LABEL}
    if notice is not None:
        observation["notice"] = {"id": notice[0], "kind": notice[1]}
    return observation


class PrivacyAssertions(unittest.TestCase):
    def assert_private(self, *values, allow_local_panes=False):
        text = "\n".join(value if isinstance(value, str) else json.dumps(value) for value in values)
        secrets = (SENTINEL, SENTINEL_HOST, SENTINEL_EXTRA)
        if not allow_local_panes:
            secrets += (SENTINEL_PANE,)
        for secret in secrets:
            self.assertNotIn(secret, text)

    def assert_reports_private(self, reports, *, labels=True):
        self.assertTrue(reports, "exercise actual reports, not an empty receiver")
        for report in reports:
            with self.subTest(state=report["status"]["state"]):
                without_title = dict(report)
                if report["status"]["state"] == "clear":
                    self.assertNotIn("context", report)
                else:
                    context = dict(report["context"])
                    self.assertEqual(context.pop("title"), SENTINEL if labels else "")
                    if not labels:
                        self.assertEqual(context["display_name"], "OMP in Tern")
                    without_title["context"] = context
                self.assert_private(without_title)


class PrivacyTest(PrivacyAssertions):
    def test_labels_leave_only_in_titles_while_diagnostics_stay_private(self):
        c, _, sender, logs, _ = make_coordinator()
        reg = register(c)
        observations = [{
            "pane": SENTINEL_PANE,
            "state": "working",
            "host": SENTINEL_HOST,
            "label": RAW_LABEL,
            "cwd": SENTINEL_EXTRA,
            "title": SENTINEL_EXTRA,
            "model": SENTINEL_EXTRA,
            "last_message": SENTINEL_EXTRA,
            "notice": {"id": "1", "kind": "done", "text": SENTINEL_EXTRA},
        }]
        acks = [
            snapshot(c, reg["generation"], reg["epoch"], 1, observations, prompt=SENTINEL_EXTRA, diag={"omp": 1, "leak": SENTINEL_EXTRA}),
            snapshot(c, reg["generation"], reg["epoch"], 2, [{"pane": SENTINEL_PANE, "state": SENTINEL}]),
            snapshot(c, reg["generation"], reg["epoch"], 3, [{"pane": SENTINEL_PANE, "state": "idle", "notice": {"id": "9", "kind": SENTINEL}}]),
            c.handle({"schema": 1, "op": SENTINEL}),
            c.handle({"schema": 1, "op": "register", "observer": {"parent_pid": 4242, "window_key": SENTINEL * 3}}),
            c.handle({"schema": 1, "op": "status"}),
        ]
        self.assertTrue(acks[0]["accepted"])
        self.assertTrue(all(not ack["ok"] for ack in acks[1:5]))
        status = c.status()
        settings = doctor_settings(sender.root)
        with patch("tern_sidepulse.doctor.request", return_value={"ok": True, "status": status}):
            report = run_doctor(settings)
        self.assertIsNotNone(report["report"])
        self.assert_private(acks, status, logs, report, Path(report["report"]).read_text(encoding="utf-8"), c.transport.ledger.path.read_text())
        self.assertEqual(sender.states(), ["working"])
        self.assert_reports_private(sender.reports)

    def test_labels_false_keeps_label_in_memory_but_never_reports_it(self):
        c, _, sender, logs, _ = make_coordinator(labels=False)
        activate(c, [private_observation("working")])
        self.assertEqual(len(c.rows), 1)
        self.assertEqual(c.rows[0].label, SENTINEL)
        self.assertEqual(c.rows[0].host, SENTINEL_HOST)
        self.assertEqual(sender.shown(), {scope(SENTINEL_PANE, SENTINEL_HOST): ("working", "OMP in Tern")})
        self.assert_reports_private(sender.reports, labels=False)
        self.assert_private(sender.reports, c.status(), logs, c.transport.ledger.path.read_text())

    def test_program_reports_carry_labels_only_in_context_title(self):
        c, _, sender, _, _ = make_coordinator()
        gen, epoch = activate(c, [private_observation("working")])
        for seq, obs in enumerate((
            private_observation("waiting_input"),
            private_observation("idle", ("1", "failed")),
            private_observation("idle", ("2", "done")),
            private_observation("idle"),
        ), 2):
            self.assertTrue(snapshot(c, gen, epoch, seq, [obs])["ok"])
        self.assertTrue(snapshot(c, gen, epoch, 6, [])["ok"])
        self.assertEqual(sender.states(), ["working", "blocked", "error", "done", "idle", "clear"])
        self.assert_reports_private(sender.reports)

    def test_notice_diagnostics_keep_hosts_and_labels_private(self):
        c, _, sender, logs, _ = make_coordinator()
        activate(c, [private_observation("idle", ("1", "done"))])
        status = c.handle({"schema": 1, "op": "status"})
        self.assertEqual(status["status"]["rows"], {"completed": 1})
        self.assertEqual(status["status"]["observers"][0]["active_notices"], [{"pane": SENTINEL_PANE, "kind": "done"}])
        with patch("tern_sidepulse.doctor.request", return_value=status):
            doctor = run_doctor(doctor_settings(sender.root))
        report_path = Path(doctor["report"])
        self.assert_private(status, doctor, report_path.read_text(), allow_local_panes=True)
        self.assertEqual(report_path.stat().st_mode & 0o777, 0o600, "pane-level diagnostics stay user-private on this Mac")
        self.assert_private(logs, c.transport.ledger.path.read_text())
        self.assert_reports_private(sender.reports)

    def test_rejections_never_echo_labels_or_hosts(self):
        c, _, sender, logs, _ = make_coordinator()
        reg = register(c)
        responses = []
        for host in (SENTINEL_HOST + "\n", SENTINEL_HOST + "\ud800", SENTINEL_HOST * 10):
            ack = snapshot(c, reg["generation"], reg["epoch"], 1, [private_observation("working", host=host)])
            self.assertEqual(ack["error"], "bad_request")
            self.assertEqual(ack["detail"], "observation.host must be a short host key")
            responses.append(ack)
        for seq, label in enumerate((SENTINEL * 100, {"secret": SENTINEL}), 1):
            ack = snapshot(c, reg["generation"], reg["epoch"], seq, [{"pane": SENTINEL_PANE, "state": "working", "host": SENTINEL_HOST, "label": label}])
            self.assertTrue(ack["ok"], ack)
            self.assertIsNone(c.rows[0].label)
            responses.append(ack)
        self.assertEqual(c.counters["labels_rejected"], 2)
        self.assert_private(responses, sender.reports, c.status(), logs, c.transport.ledger.path.read_text())

    def test_receiver_rejection_never_logs_echoed_labels_or_hosts(self):
        c, _, sender, logs, _ = make_coordinator()
        sender.answer = {"ok": False, "error": "invalid " + SENTINEL + " " + SENTINEL_HOST + " " + SENTINEL_PANE}
        activate(c, [private_observation("working")])
        status = c.status()
        self.assertEqual(status["transport"]["rejected"], 1)
        self.assertEqual(sender.shown(), {}, "a rejected row was not accepted by SidePulse")
        with patch("tern_sidepulse.doctor.request", return_value={"ok": True, "status": status}):
            doctor = run_doctor(doctor_settings(sender.root))
        self.assertIn("receiver_rejections", {check["id"] for check in doctor["checks"]})
        self.assert_private(status, doctor, Path(doctor["report"]).read_text(), logs, c.transport.ledger.path.read_text())

    def test_lifecycle_logs_never_carry_labels_or_hosts(self):
        c, clock, _, logs, _ = make_coordinator(Limits(lease=2))
        observation = private_observation("idle", ("1", "done"))
        activate(c, [observation])
        activate(c, [observation])  # replacement
        advance(c, clock, 2)  # expiry
        gen, epoch = activate(c, [observation])
        clock.advance(1, wall_seconds=600)
        c.tick()
        responses = [snapshot(c, gen, epoch, 2, [private_observation("working")])]

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
        self.assert_private(responses, c.status(), logs, c.transport.ledger.path.read_text())

    def test_doctor_report_filters_window_diag(self):
        settings = doctor_settings(short_tmpdir("tsp-doc-"))
        report = run_doctor(settings, {"omp": 2, "label_tab": 1, "label_title": SENTINEL, "secret": SENTINEL, "pane_ids": [SENTINEL_PANE]}, window_key="")
        self.assert_private(report, Path(report["report"]).read_text())
        self.assertEqual(report["window_diag"], {"omp": 2, "label_tab": 1})
        self.assertIn("an accepted report is not an LED check", report["summary"])


class LabelPrivacyEndToEndTest(PrivacyAssertions):
    def setUp(self):
        self.tmp = short_tmpdir("tsp-private-e2e-")
        self.receiver = FakeProgramServer(self.tmp / "program-status.sock")
        self.receiver.start()
        self.env = subprocess_env(self.tmp, self.receiver.path, {"lease": 30, "reaper": 0.2, "exit_grace": 1, "final_grace": 5})
        self.outputs = []
        self.errors = []

    def tearDown(self):
        kill_coordinator(self.env)
        self.receiver.stop()

    def invoke(self, command, payload):
        code, out, err = run_bridge([command], payload, self.env, raw_utf8=True)
        self.outputs.append(out)
        self.errors.append(err)
        self.assertEqual(code, 0, err)
        self.assertTrue(out["ok"], out)
        return out

    def test_status_doctor_log_and_ledger_keep_labels_hosts_and_panes_private(self):
        reg = self.invoke("register", {})
        ack = self.invoke("snapshot", {
            "epoch": reg["epoch"],
            "generation": reg["generation"],
            "seq": 1,
            "coverage": "complete",
            "observations": [{
                "pane": SENTINEL_PANE,
                "state": "working",
                "host": SENTINEL_HOST,
                "label": RAW_LABEL,
                "cwd": SENTINEL_EXTRA,
                "title": SENTINEL_EXTRA,
                "last_message": SENTINEL_EXTRA,
                "notice": {"id": "1", "kind": "done", "text": SENTINEL_EXTRA},
            }],
            "prompt": SENTINEL_EXTRA,
            "diag": {"omp": 1, "secret": SENTINEL_EXTRA},
        })
        self.assertEqual(ack["rows"], {"working": 1})
        self.assertTrue(self.receiver.wait_for(lambda rx: rx.states() == ["working"]))
        self.assert_reports_private(self.receiver.reports)
        status = self.invoke("status", None)["status"]
        self.assertEqual(status["rows_total"], 1)
        self.assertEqual(status["rows_labelled"], 1)
        self.assertNotIn("session_id", status)
        doctor = self.invoke("doctor", {"diag": {"omp": 1, "label_tab": 1, "secret": SENTINEL_EXTRA}})
        self.assertEqual(doctor["status"]["rows_labelled"], 1)
        detail = next(check["detail"] for check in doctor["checks"] if check["id"] == "coordinator")
        self.assertIn("rows 1 (working 1), 1 labelled, 1 observer(s), disabled=False", detail)
        identity_detail = next(check["detail"] for check in doctor["checks"] if check["id"] == "identity")
        self.assertRegex(identity_detail, r"^install .{8}…$")
        runtime = Path(self.env["TERN_SIDEPULSE_RUNTIME_DIR"])
        self.assertEqual(Path(doctor["report"]), runtime / DOCTOR_NAME)
        surfaces = {
            "stdout": json.dumps(self.outputs),
            "stderr": "\n".join(self.errors),
            "doctor.json": (runtime / DOCTOR_NAME).read_text(encoding="utf-8"),
            "coordinator.log": (runtime / LOG_NAME).read_text(encoding="utf-8"),
            "program-scopes.json": (runtime / LEDGER_NAME).read_text(encoding="utf-8"),
        }
        for surface, text in surfaces.items():
            with self.subTest(surface=surface):
                self.assert_private(text)
        self.assertEqual(json.loads(surfaces["program-scopes.json"]), {"scopes": list(self.receiver.rows)})
        self.assertEqual(self.receiver.states(), ["working"])


if __name__ == "__main__":
    unittest.main()
