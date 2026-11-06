"""Run the real window observer in a headless, deterministic Luau runtime."""
import os
import shutil
import subprocess
import unittest

from tests.helpers import ROOT, short_tmpdir


class LuauHarnessTest(unittest.TestCase):
    def _run_drive(self, drive: str) -> None:
        configured = os.environ.get("LUAU")
        luau = shutil.which(configured or "luau")
        if luau is None:
            message = f"no Luau executable: {configured or 'luau on PATH'} (set LUAU)"
            if os.environ.get("TERN_SIDEPULSE_REQUIRE_LUAU") == "1":
                self.fail(message)
            self.skipTest(message)

        parts = (ROOT / "tests/luau/harness.luau", ROOT / "window.luau", ROOT / "tests/luau" / drive)
        chunks = []
        offsets = []
        next_line = 1
        for part in parts:
            name = part.relative_to(ROOT).as_posix()
            start = next_line + 1
            chunk = f"-- @@ part: {name} starts at line {start}\n" + part.read_text(encoding="utf-8")
            if not chunk.endswith("\n"):
                chunk += "\n"
            chunks.append(chunk)
            offsets.append(f"{name}: line {start}")
            next_line += chunk.count("\n")
        offset_table = "Part offsets:\n" + "\n".join(offsets)
        combined = short_tmpdir("tsp-luau-") / drive
        combined.write_text("".join(chunks), encoding="utf-8")
        try:
            result = subprocess.run([luau, str(combined)], capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired as exc:
            self.fail(f"{offset_table}\nLuau timed out after 60 s\nstdout:\n{exc.stdout}\nstderr:\n{exc.stderr}")
        except OSError as exc:
            self.fail(f"{offset_table}\nCould not run Luau: {exc}")
        details = f"{offset_table}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        self.assertEqual(result.returncode, 0, details)
        self.assertEqual(result.stdout.splitlines()[-1:], ["OK"], details)

    def test_backoff_and_interpreter_replacement(self):
        self._run_drive("drive_backoff.luau")

    def test_labels_and_host_keys(self):
        self._run_drive("drive_labels.luau")

    def test_payload_budget_and_encode_failure(self):
        self._run_drive("drive_budget.luau")


if __name__ == "__main__":
    unittest.main()
