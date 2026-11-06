import os
import shutil
import socket
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import short_tmpdir

from tern_sidepulse.paths import (
    MAX_SOCKET_PATH_BYTES,
    SOCKET_NAME,
    TEMP_SOCKET_SUFFIX_MAX,
    ensure_private_dir,
    runtime_dir,
    sidepulse_relay_socket_for_dir,
    sidepulse_service_sockets,
    sidepulse_ui_sockets,
)


class RuntimeDirTest(unittest.TestCase):
    LONG_BASE = Path("/Users/someone-with-a-very-long-account-name/Library/Application Support/Something/Deep/tern-sidepulse")

    def test_long_base_prefers_the_private_tmpdir(self):
        private = short_tmpdir("tsp-T-")
        with mock.patch.dict(os.environ, {"TMPDIR": str(private)}):
            chosen = runtime_dir(self.LONG_BASE)
        self.assertEqual(chosen, private / "tern-sidepulse", "the user's own temp dir is not guessable by other users")
        with mock.patch.dict(os.environ, {"TMPDIR": "relative/tmp"}):
            self.assertTrue(str(runtime_dir(self.LONG_BASE)).startswith("/tmp/tern-sidepulse-"), "a relative TMPDIR is ignored")
        with mock.patch.dict(os.environ, {"TMPDIR": "/" + "x" * 90}):
            self.assertTrue(str(runtime_dir(self.LONG_BASE)).startswith("/tmp/tern-sidepulse-"), "an over-long TMPDIR is ignored")

    def test_long_base_without_tmpdir_falls_back_to_tmp_and_the_temp_name_binds(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TMPDIR", None)
            chosen = runtime_dir(self.LONG_BASE)
        self.assertTrue(str(chosen).startswith("/tmp/tern-sidepulse-"))
        temp_name = str(chosen / f"{SOCKET_NAME}.{os.getpid()}")
        self.assertLessEqual(len(temp_name.encode()), MAX_SOCKET_PATH_BYTES)
        ensure_private_dir(chosen)
        self.addCleanup(shutil.rmtree, chosen, True)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(temp_name)
        finally:
            server.close()

    def test_short_base_is_kept(self):
        base = short_tmpdir("tsp-p-")
        self.assertEqual(runtime_dir(base), base)
        self.assertLessEqual(len(str(base / SOCKET_NAME).encode()) + TEMP_SOCKET_SUFFIX_MAX, MAX_SOCKET_PATH_BYTES)

    def test_xdg_does_not_change_the_default(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": "/somewhere/else"}):
            with_xdg = runtime_dir()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("XDG_STATE_HOME", None)
            without = runtime_dir()
        self.assertEqual(with_xdg, without)

    def test_private_dir_rejects_symlink_and_tightens_mode(self):
        tmp = short_tmpdir("tsp-pd-")
        target = tmp / "real"
        target.mkdir(mode=0o755)
        link = tmp / "link"
        link.symlink_to(target)
        with self.assertRaises(OSError):
            ensure_private_dir(link)
        ensure_private_dir(target)
        self.assertEqual(os.stat(target).st_mode & 0o777, 0o700)


class SidePulsePathsTest(unittest.TestCase):
    def test_relay_socket_long_path_fallback(self):
        short = Path("/tmp/x")
        self.assertEqual(sidepulse_relay_socket_for_dir(short), short / "relay-events.sock")
        long = Path("/Users/" + "a" * 80 + "/.local/state/sidepulse/agent-monitor")
        fallback = sidepulse_relay_socket_for_dir(long)
        self.assertTrue(str(fallback).startswith("/tmp/sidepulse-relay-"))
        self.assertTrue(str(fallback).endswith(".sock"))

    def test_candidates_follow_xdg_then_home(self):
        home = Path("/home/u")
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": "/xdg"}):
            ui = sidepulse_ui_sockets()
            self.assertEqual(ui[0], Path("/xdg/sidepulse/agent-monitor/events.sock"))
            self.assertEqual(ui[-1], Path.home() / ".local/state/sidepulse/agent-monitor/events.sock")
        self.assertEqual(sidepulse_service_sockets(home), (home / ".local/state/sidepulse/agent-monitor/relay-events.sock",))


if __name__ == "__main__":
    unittest.main()
