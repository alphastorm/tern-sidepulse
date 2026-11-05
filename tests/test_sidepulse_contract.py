"""Optional: exercises the real SidePulse normalizer when a SidePulse interpreter is available."""
import json
import os
import subprocess
import unittest
from pathlib import Path

from tests.helpers import ROOT

FIXTURE = ROOT / "tests" / "fixtures" / "sidepulse_probe.py"


def sidepulse_python() -> str | None:
    configured = os.environ.get("SIDEPULSE_PYTHON")
    if configured:
        return configured
    candidate = Path.home() / ".local" / "share" / "sidepulse" / "venv" / "bin" / "python"
    return str(candidate) if candidate.exists() else None


class SidePulseContractTest(unittest.TestCase):
    def test_mapping_table_and_traps(self):
        python = sidepulse_python()
        if python is None:
            self.skipTest("no SidePulse interpreter (set SIDEPULSE_PYTHON)")
        proc = subprocess.run([python, "-I", str(FIXTURE), str(ROOT)], capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout.strip().splitlines()[-1])
        if "error" in data:
            self.skipTest(data["error"])
        for row in data["results"]:
            if "sent" in row:
                self.assertEqual(row["got"], row["expected"], row)
                self.assertEqual(row["key"], "omp:session:tern-sidepulse:fixture")
            elif row["trap"] == "Notification":
                self.assertEqual(row["got"], "completed", "Notification after completed is ignored by the collector")
            else:
                self.assertEqual(row["got"], "completed", "a bare SessionEnd maps to completed, not deletion")


if __name__ == "__main__":
    unittest.main()
