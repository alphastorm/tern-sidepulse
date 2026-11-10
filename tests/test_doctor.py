import unittest
from pathlib import Path
from unittest.mock import patch

from tests.helpers import short_tmpdir

from tern_sidepulse.client import CoordinatorUnavailable, Settings
from tern_sidepulse.doctor import run_doctor
from tern_sidepulse.limits import Limits


def settings_for(tmp: Path) -> Settings:
    return Settings(
        data_dir=tmp / "data",
        runtime_dir=tmp / "rt",
        program_sockets=(tmp / "state" / "program-status.sock",),
        limits=Limits(),
        bridge_path=tmp / "bridge.py",
    )


def receiver(report: dict) -> list[dict]:
    return [check for check in report["checks"] if check["id"].startswith("receiver")]


class ReceiverCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = short_tmpdir("tsp-doctor-")
        (self.tmp / "state").mkdir()
        self.settings = settings_for(self.tmp)

    def doctor(self, transport: dict | None = None) -> dict:
        if transport is None:
            with patch("tern_sidepulse.doctor.request", side_effect=CoordinatorUnavailable("not_running")):
                return run_doctor(self.settings)
        status = {"observers": [], "rows": {}, "transport": transport}
        with patch("tern_sidepulse.doctor.request", return_value={"ok": True, "status": status}):
            return run_doctor(self.settings)

    def test_sidepulse_before_program_status_says_update(self):
        (self.tmp / "state" / "relay-events.sock").write_text("")
        [check] = receiver(self.doctor())
        self.assertEqual(check["level"], "error")
        self.assertIn("predates program status", check["detail"])
        self.assertIn("sidepulse update", check["remedy"])

    def test_no_sidepulse_says_start_it(self):
        [check] = receiver(self.doctor())
        self.assertEqual(check["level"], "warn")
        self.assertIn("start SidePulse", check["remedy"])

    def test_running_coordinator_reports_failures_rejections_and_labels_off(self):
        (self.tmp / "state" / "program-status.sock").write_text("")
        [check] = receiver(self.doctor({"labels": True, "rows": 1, "pending": 1, "last_error": "program-status.sock: busy"}))
        self.assertEqual((check["level"], check["detail"]), ("error", "delivery failing: program-status.sock: busy"))
        checks = receiver(self.doctor({"labels": False, "rows": 2, "pending": 0, "last_error": None, "rejected": 1, "delivered_wall": "2026-11-10T22:00:00.000Z"}))
        self.assertEqual([c["id"] for c in checks], ["receiver", "receiver_rejections"])
        self.assertEqual(checks[0]["level"], "ok")
        self.assertIn("2 row(s)", checks[0]["detail"])
        self.assertIn("labels off", checks[0]["detail"])
        self.assertEqual(checks[1]["level"], "warn")


if __name__ == "__main__":
    unittest.main()
