import hashlib
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
    sidepulse_program_socket_for_dir,
    sidepulse_program_sockets,
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
    def test_program_socket_short_path_and_96_byte_boundary(self):
        short = Path("/tmp/x")
        self.assertEqual(sidepulse_program_socket_for_dir(short), short / "program-status.sock")
        boundary = Path("/" + "a" * 75)
        self.assertEqual(len(str(boundary / "program-status.sock").encode("utf-8")), 96)
        self.assertEqual(sidepulse_program_socket_for_dir(boundary), boundary / "program-status.sock")

    def test_program_socket_long_path_fallback_matches_upstream_hash(self):
        directory = Path("/" + "a" * 76)
        path = directory / "program-status.sock"
        digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]
        with mock.patch("tern_sidepulse.paths.os.getuid", return_value=123):
            fallback = sidepulse_program_socket_for_dir(directory)
        self.assertEqual(fallback, Path("/tmp/sidepulse-status-123") / f"{digest}.sock")
        self.assertRegex(str(fallback), r"^/tmp/sidepulse-status-123/[0-9a-f]{16}\.sock$")

    def test_program_socket_counts_utf8_bytes_not_characters(self):
        directory = Path("/" + "é" * 38)
        path = directory / "program-status.sock"
        self.assertLess(len(str(path)), 96)
        digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]
        self.assertEqual(sidepulse_program_socket_for_dir(directory), Path(f"/tmp/sidepulse-status-{os.getuid()}/{digest}.sock"))

    def test_candidates_follow_xdg_then_home(self):
        home = Path("/home/u")
        fallback = home / ".local/state/sidepulse/agent-monitor/program-status.sock"
        with mock.patch("tern_sidepulse.paths.Path.home", return_value=home):
            with mock.patch.dict(os.environ, {"XDG_STATE_HOME": "/xdg"}):
                self.assertEqual(sidepulse_program_sockets(), (Path("/xdg/sidepulse/agent-monitor/program-status.sock"), fallback))
                self.assertEqual(sidepulse_program_sockets(home), (fallback,), "an explicit home bypasses XDG")
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(sidepulse_program_sockets(), (fallback,))

    def test_duplicate_xdg_home_candidate_is_not_repeated(self):
        home = Path("/home/u")
        with mock.patch("tern_sidepulse.paths.Path.home", return_value=home):
            with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(home / ".local/state")}):
                self.assertEqual(sidepulse_program_sockets(), (home / ".local/state/sidepulse/agent-monitor/program-status.sock",))


if __name__ == "__main__":
    unittest.main()
