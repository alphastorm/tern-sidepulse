import os
import shutil
import stat
import subprocess
import sys
import time
import unittest
from pathlib import Path

from tests.helpers import BRIDGE, FakeReceiver, kill_coordinator, run_bridge, short_tmpdir, subprocess_env

sys.path.insert(0, str(BRIDGE.parent))
import bridge  # noqa: E402

FAST = {"lease": 2, "reaper": 0.2, "exit_grace": 1, "final_grace": 5}


class VersionGuardTest(unittest.TestCase):
    def test_old_interpreter_gets_structured_json(self):
        problem = bridge.check_python((3, 9, 6))
        assert problem is not None
        self.assertEqual(problem["error"], "python_too_old")
        self.assertEqual(problem["version"], "3.9.6")
        self.assertIsNone(bridge.check_python((3, 10, 0)))


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = short_tmpdir("tsp-cli-")
        self.ui = FakeReceiver(self.tmp / "events.sock")
        self.svc = FakeReceiver(self.tmp / "relay-events.sock")
        self.ui.start()
        self.svc.start()
        self.env = subprocess_env(self.tmp, self.ui.path, self.svc.path, FAST)

    def tearDown(self):
        kill_coordinator(self.env)
        self.ui.stop()
        self.svc.stop()

    def test_usage_errors_are_json(self):
        code, out, _ = run_bridge(["frobnicate"], None, self.env)
        self.assertEqual(code, 2)
        self.assertEqual(out["error"], "usage")
        env = dict(self.env)
        env.pop("TERN_PLUGIN_DATA")
        code, out, _ = run_bridge(["register"], {}, env)
        self.assertEqual(code, 2)
        self.assertIn("data-dir", out["detail"])
        env = dict(self.env, TERN_SIDEPULSE_LIMITS='{"lease": -1}')
        code, out, _ = run_bridge(["status"], None, env)
        self.assertEqual(code, 2)

    def test_register_spawns_a_detached_coordinator_and_injects_identity(self):
        code, reg, err = run_bridge(["register"], {"observer": {"window_key": "w2"}}, self.env)
        self.assertEqual(code, 0, err)
        self.assertTrue(reg["ok"], reg)
        self.assertEqual(reg["observer_key"].split(":")[1], "w2")
        pid = reg["coordinator_pid"]
        self.assertNotEqual(os.getpgid(pid), os.getpgid(0), "the coordinator lives outside the caller's process group")
        self.assertTrue(self.ui.wait_for("idle_ready"), "startup neutral reaches the receiver")
        code, ack, _ = run_bridge(["snapshot"], {"observer": {"window_key": "w2"}, "epoch": reg["epoch"], "generation": reg["generation"], "seq": 1, "coverage": "complete", "observations": [{"pane": 7, "state": "waiting_input"}]}, self.env)
        self.assertTrue(ack["accepted"], ack)
        self.assertEqual(ack["desired"], "waiting_for_input")
        self.assertTrue(self.ui.wait_for("waiting_for_input"))
        self.assertTrue(self.svc.wait_for("waiting_for_input"))

    def test_snapshot_never_registers_on_its_own(self):
        code, reg, _ = run_bridge(["register"], {}, self.env)
        kill_coordinator(self.env)
        time.sleep(0.3)
        code, out, _ = run_bridge(["snapshot"], {"epoch": reg["epoch"], "generation": reg["generation"], "seq": 1, "coverage": "complete", "observations": []}, self.env)
        self.assertEqual(code, 0)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "epoch_mismatch", "a respawned coordinator has a new epoch; the window must register again")

    def test_latch_blocks_register_without_spawning(self):
        data = Path(self.env["TERN_PLUGIN_DATA"])
        data.mkdir(parents=True, exist_ok=True)
        (data / "disabled").write_text("x\n")
        code, out, _ = run_bridge(["register"], {}, self.env)
        self.assertEqual(out["error"], "disabled")
        self.assertFalse((Path(self.env["TERN_SIDEPULSE_RUNTIME_DIR"]) / "coordinator.sock").exists())
        code, out, _ = run_bridge(["enable"], None, self.env)
        self.assertTrue(out["ok"])
        self.assertEqual(out["coordinator"], "not_running")
        self.assertFalse((data / "disabled").exists())

    def test_disable_persists_latch_revokes_and_neutralizes(self):
        _, reg, _ = run_bridge(["register"], {}, self.env)
        run_bridge(["snapshot"], {"epoch": reg["epoch"], "generation": reg["generation"], "seq": 1, "coverage": "complete", "observations": [{"pane": 1, "state": "working"}]}, self.env)
        self.assertTrue(self.ui.wait_for("working"))
        code, out, _ = run_bridge(["disable"], None, self.env)
        self.assertTrue(out["disabled"])
        self.assertTrue(out["latch_persisted"])
        self.assertTrue((Path(self.env["TERN_PLUGIN_DATA"]) / "disabled").exists())
        self.assertTrue(self.ui.wait_for("idle_ready", minimum=2))
        _, late, _ = run_bridge(["snapshot"], {"epoch": reg["epoch"], "generation": reg["generation"], "seq": 2, "coverage": "complete", "observations": [{"pane": 1, "state": "working"}]}, self.env)
        self.assertEqual(late["error"], "disabled")

    def test_status_without_coordinator_and_doctor(self):
        code, out, _ = run_bridge(["status"], None, self.env)
        self.assertEqual(out["error"], "coordinator_unavailable")
        self.assertEqual(out["detail"], "not_running")
        code, doc, err = run_bridge(["doctor"], {"window_key": "", "diag": {"omp": 1}}, self.env)
        self.assertEqual(code, 0, err)
        self.assertTrue(doc["ok"])
        ids = {check["id"] for check in doc["checks"]}
        self.assertTrue({"python", "bridge", "data_dir", "runtime_dir", "coordinator"} <= ids)
        self.assertTrue(Path(doc["report"]).exists())

    def test_version_mismatch_stops_old_coordinator(self):
        from tern_sidepulse.cli import parse_args, resolve_settings
        from tern_sidepulse.client import request

        _, reg, _ = run_bridge(["register"], {}, self.env)
        pid = reg["coordinator_pid"]
        # Simulate a plugin update: a helper from newer code registers with its own version.
        settings = resolve_settings(parse_args(["register", "--runtime-dir", self.env["TERN_SIDEPULSE_RUNTIME_DIR"]]), BRIDGE)
        out = request(settings, {"schema": 1, "op": "register", "observer": {"parent_pid": 1, "window_key": ""}, "client_version": "9.9.9"}, spawn=False)
        self.assertEqual(out["error"], "version_mismatch")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                break
            time.sleep(0.05)
        else:
            self.fail("the old coordinator did not exit")

    def test_settings_precedence_flag_over_env(self):
        other = short_tmpdir("tsp-cli2-")
        code, out, _ = run_bridge(["status", "--runtime-dir", str(other)], None, self.env)
        self.assertEqual(out["detail"], "not_running")
        self.assertTrue(other.exists())

    def test_parser_grammar_matches_the_window_and_the_spawner(self):
        from tern_sidepulse.cli import UsageError, parse_args

        args = parse_args(["coordinator", "--runtime-dir", "/r", "--data-dir", "/d", "--ui-socket", "/a", "--ui-socket", "/b", "--limits", "{}"])
        self.assertEqual((args.command, args.runtime_dir, args.data_dir, args.ui_socket, args.service_socket, args.limits), ("coordinator", "/r", "/d", ["/a", "/b"], [], "{}"))
        for argv in ([], ["--data-dir", "/d"], ["status", "--data-dir"], ["status", "--help"], ["status", "extra"]):
            with self.assertRaises(UsageError, msg=argv):
                parse_args(argv)
        code, out, _ = run_bridge(["status", "--nope", "x"], None, self.env)
        self.assertEqual((code, out["error"]), (2, "usage"))

    def test_config_must_be_a_small_regular_file(self):
        from tern_sidepulse.storage import load_config

        data = Path(self.env["TERN_PLUGIN_DATA"])
        data.mkdir(parents=True, exist_ok=True)
        real = data / "real.json"
        real.write_text('{"ui_sockets": ["/elsewhere/events.sock"]}')
        (data / "config.json").symlink_to(real)
        self.assertEqual(load_config(data), {}, "a symlinked config is ignored, as the window half ignores it")
        (data / "config.json").unlink()
        os.mkfifo(data / "config.json")
        self.assertEqual(load_config(data), {}, "a FIFO never blocks the helper")
        (data / "config.json").unlink()
        (data / "config.json").write_text('{"ui_sockets": ["' + "/" + "x" * 70000 + '"]}')
        self.assertEqual(load_config(data), {}, "over 64 KiB is ignored")
        (data / "config.json").write_text('{"ui_sockets": ["/elsewhere/events.sock"]}')
        self.assertEqual(load_config(data), {"ui_sockets": ["/elsewhere/events.sock"]})

    def test_helper_caches_bytecode_under_the_data_dir(self):
        run_bridge(["status"], None, self.env)
        cache = Path(self.env["TERN_PLUGIN_DATA"]) / "pycache"
        self.assertTrue(any(cache.rglob("cli*.pyc")), "later helper runs skip compiling the package; the plugin dir stays untouched")

    def test_plain_shell_invocation_writes_no_bytecode_into_the_plugin_dir(self):
        # The README's `python3 bridge.py status …` has no -B and no pycache prefix; a __pycache__ under
        # the plugin directory would make Tern reload every window observer.
        copy = short_tmpdir("tsp-copy-")
        shutil.copy(BRIDGE, copy / "bridge.py")
        shutil.copytree(BRIDGE.parent / "tern_sidepulse", copy / "tern_sidepulse", ignore=shutil.ignore_patterns("__pycache__"))
        env = dict(self.env)
        env.pop("PYTHONDONTWRITEBYTECODE", None)
        proc = subprocess.run([sys.executable, str(copy / "bridge.py"), "status"], capture_output=True, env=env, timeout=15)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse((copy / "tern_sidepulse" / "__pycache__").exists())

    def test_identity_and_latch_are_user_private(self):
        data = Path(self.env["TERN_PLUGIN_DATA"])
        data.mkdir(parents=True, exist_ok=True)
        (data / "identity.json").write_text('{"install_id": "older-install", "plugin": "tern-sidepulse"}\n')
        os.chmod(data / "identity.json", 0o644)  # as an earlier build wrote it
        _, reg, _ = run_bridge(["register"], {}, self.env)
        self.assertTrue(reg["ok"], reg)
        run_bridge(["disable"], None, self.env)
        for name in ("identity.json", "disabled"):
            self.assertEqual(stat.S_IMODE(os.stat(data / name).st_mode), 0o600, name)
        self.assertIn("older-install", (data / "identity.json").read_text(), "tightening the mode keeps the identity")


if __name__ == "__main__":
    unittest.main()
