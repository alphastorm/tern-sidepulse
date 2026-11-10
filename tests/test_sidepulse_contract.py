"""The transport against SidePulse's real program-status server (inteliwear/sidepulse#72 and later).

Runs when a SidePulse with program status is available: ``SIDEPULSE_SRC`` (a checkout's ``src``, run with
``SIDEPULSE_PYTHON`` or this interpreter) or the installed ``~/.local/share/sidepulse/venv``. Otherwise it
skips, unless ``TERN_SIDEPULSE_REQUIRE_CONTRACT=1`` (CI pins the SidePulse revision in ``ci.yml``).
"""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from typing import NoReturn

from tests.helpers import ROOT, FakeClock, short_tmpdir

from tern_sidepulse.reducer import Row
from tern_sidepulse.transport import SidePulseTransport, ScopeLedger, exchange, scope_for

FIXTURE = ROOT / "tests" / "fixtures" / "sidepulse_server.py"
SALT = "contract-install"


def sidepulse_command() -> list[str] | None:
    source = os.environ.get("SIDEPULSE_SRC")
    python = os.environ.get("SIDEPULSE_PYTHON")
    if source:
        return [python or sys.executable, "-I", str(FIXTURE), "{socket}", source]
    installed = Path.home() / ".local" / "share" / "sidepulse" / "venv" / "bin" / "python"
    if python or installed.exists():
        return [python or str(installed), "-I", str(FIXTURE), "{socket}"]
    return None


class SidePulseServer:
    def __init__(self, test: unittest.TestCase, path: Path) -> None:
        command = sidepulse_command()
        if command is None:
            self._unavailable(test, "no SidePulse interpreter (set SIDEPULSE_SRC or SIDEPULSE_PYTHON)")
        self.path = path
        self.proc = subprocess.Popen(
            [part.replace("{socket}", str(path)) for part in command],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        line = self.proc.stdout.readline() if self.proc.stdout else ""
        try:
            hello = json.loads(line)
        except ValueError:
            self.stop()
            test.fail(f"SidePulse fixture failed to start: {line!r} {self.proc.stderr.read() if self.proc.stderr else ''}")
        if "error" in hello:
            self.stop()
            self._unavailable(test, f"this SidePulse has no program status: {hello['error']}")

    @staticmethod
    def _unavailable(test: unittest.TestCase, message: str) -> NoReturn:
        if os.environ.get("TERN_SIDEPULSE_REQUIRE_CONTRACT") == "1":
            test.fail(message)
        test.skipTest(message)

    def stop(self) -> None:
        if self.proc.poll() is None:
            try:
                self.proc.communicate(input="", timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.communicate()

    def rows(self) -> dict[str, dict]:
        """The daemon's own presentation of our rows, keyed by the session id we sent."""
        snapshot = exchange(self.path, json.dumps({"v": 1, "type": "get_state"}).encode("utf-8"), 2.0)["snapshot"]
        statuses = snapshot["statuses"] + snapshot.get("stale_statuses", [])
        return {status["session_id"]: status for status in statuses if status.get("provider") == "omp"}


def key(pane: str, host: str = "local") -> str:
    return scope_for(SALT, Row(host, pane, "idle_ready", None))[len("tern-sidepulse-"):]


class SidePulseContractTest(unittest.TestCase):
    def setUp(self):
        self.tmp = short_tmpdir("tsp-ct-")
        self.clock = FakeClock()
        self.server = self.start()

    def tearDown(self):
        self.server.stop()

    def start(self) -> SidePulseServer:
        server = SidePulseServer(self, self.tmp / "program-status.sock")
        self.addCleanup(server.stop)
        return server

    def transport(self, **kw) -> SidePulseTransport:
        return SidePulseTransport(clock=self.clock, candidates=(self.tmp / "program-status.sock",), ledger_path=self.tmp / "program-scopes.json", salt=SALT, **kw)

    def test_rows_labels_renames_and_clears(self):
        t = self.transport()
        longest = "\U0001f600" * 96  # the coordinator's longest sanitized label, 384 UTF-8 bytes
        t.publish([
            Row("local", "1", "working", "DOCS: Review docs"),
            Row("remote", "2", "waiting_for_input", "api work"),
            Row("local", "3", "idle_ready", None),
            Row("local", "4", "completed", "Finished"),
            Row("local", "5", "blocked_error", longest),
        ])
        t.tick()
        self.assertEqual(t.to_dict()["rejected"], 0, "SidePulse accepts every report shape we send")
        rows = self.server.rows()
        self.assertEqual({k: r["mode"] for k, r in rows.items()}, {
            key("1"): "working",
            key("2", "remote"): "waiting_for_input",
            key("3"): "idle_ready",
            key("4"): "completed",
            key("5"): "blocked_error",
        })
        self.assertTrue(rows[key("1")]["display_name"].startswith("DOCS: Review docs"))
        self.assertTrue(rows[key("5")]["display_name"].startswith(longest[:80]), "accepted; SidePulse cuts display names to 96 characters")
        self.assertEqual(rows[key("3")]["display_name"], "OMP in Tern")
        self.assertEqual({r["origin"] for r in rows.values()}, {"Tern"})

        t.publish([Row("local", "1", "working", "Renamed tab")])
        t.tick()
        rows = self.server.rows()
        self.assertTrue(rows[key("1")]["display_name"].startswith("Renamed tab"), "title_source session lets a rename replace the title")
        self.assertEqual(set(rows), {key("1")}, "rows that left were cleared")

        t.publish([Row("local", "1", "working", None)])
        t.tick()
        self.assertEqual(self.server.rows()[key("1")]["display_name"], "OMP in Tern", "the empty title stops SidePulse from keeping the old label")

        t.publish(())
        t.tick()
        self.assertEqual(self.server.rows(), {})
        self.assertEqual(ScopeLedger(self.tmp / "program-scopes.json").scopes, set())

    def test_labels_off(self):
        t = self.transport(labels=False)
        t.publish([Row("local", "1", "working", "Private tab")])
        t.tick()
        self.assertEqual(self.server.rows()[key("1")]["display_name"], "OMP in Tern")

    def test_restarted_sidepulse_gets_active_rows_back(self):
        t = self.transport()
        t.publish([Row("local", "1", "working", "Busy"), Row("local", "2", "completed", "Done")])
        t.tick()
        self.server.stop()
        self.server = self.start()
        self.assertEqual(self.server.rows(), {}, "a restarted daemon starts empty")
        self.clock.advance(1)
        t.tick()
        rows = self.server.rows()
        self.assertEqual({k: r["mode"] for k, r in rows.items()}, {key("1"): "working"}, "done rows are not resent as a fresh completion")

    def test_crashed_predecessor_rows_are_overwritten_then_cleared(self):
        old = self.transport()
        old.publish([Row("local", "1", "working", "Still open"), Row("local", "2", "working", "Closed meanwhile")])
        old.tick()  # then the process dies without clearing
        new = self.transport(inherit_grace=15.0)
        new.publish([Row("local", "1", "waiting_for_input", "Still open")])
        new.tick()
        self.assertEqual({k: r["mode"] for k, r in self.server.rows().items()}, {key("1"): "waiting_for_input", key("2"): "working"})
        self.clock.advance(15.1)
        new.tick()
        self.assertEqual(set(self.server.rows()), {key("1")})
        self.assertEqual(ScopeLedger(self.tmp / "program-scopes.json").scopes, {"tern-sidepulse-" + key("1")})

    def test_withdraw_clears_every_scope(self):
        old = self.transport()
        old.publish([Row("local", "1", "working", None)])
        old.tick()
        new = self.transport(inherit_grace=15.0)
        new.publish([Row("local", "2", "idle_ready", None)])
        new.tick()
        new.withdraw()
        new.tick(immediate=True)
        self.assertEqual(self.server.rows(), {})
        self.assertEqual(ScopeLedger(self.tmp / "program-scopes.json").scopes, set())


if __name__ == "__main__":
    unittest.main()
