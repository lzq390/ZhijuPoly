"""Exercise release-file publication on an isolated temporary filesystem path."""

import os
from pathlib import Path
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

CI = Path(__file__).resolve().parents[1] / "ci"
sys.path.insert(0, str(CI))
from openscience_m6_cold_fixture import release_file


class ColdFixtureTests(unittest.TestCase):
    def test_partial_write_or_killed_writer_leaves_no_allocation_behind(self):
        code = """
import errno, os, resource, signal, sys
from pathlib import Path
from openscience_m6_cold_fixture import release_file
resource.setrlimit(resource.RLIMIT_FSIZE, (65536, 65536))
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
if sys.argv[2] == "kill":
    signal.signal(signal.SIGXFSZ, signal.SIG_DFL)
try:
    release_file("create", Path(sys.argv[1]), "interrupted", os.getuid())
except OSError as error:
    if error.errno != errno.EFBIG:
        raise
else:
    raise AssertionError("Expected an interrupted release file creation")
"""
        # RLIMIT_FSIZE allows part of the first MiB to be written. On the next
        # write the child either gets EFBIG or dies before it can run a finally.
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            sentinel = directory / "my-notes"
            sentinel.write_bytes(b"existing user data")
            for killed in (False, True):
                with self.subTest(killed=killed):
                    mode = "kill" if killed else "error"
                    child = subprocess.run([sys.executable, "-B", "-c", code, tmp, mode], cwd=tmp,
                                           env={**os.environ, "PYTHONPATH": str(CI)},
                                           capture_output=True)
                    self.assertEqual(child.returncode, -signal.SIGXFSZ if killed else 0, child.stderr)
                    self.assertEqual(list(directory.iterdir()), [sentinel])
                    self.assertEqual(sentinel.read_bytes(), b"existing user data")

    def test_published_file_can_be_verified_and_cleaned_after_a_lost_create_reply(self):
        with TemporaryDirectory() as tmp:
            folder, tag, uid = Path(tmp), "accepted", os.getuid()
            with patch("openscience_m6_cold_fixture.os.fsync", side_effect=[None, OSError("reply lost")]):
                with self.assertRaisesRegex(OSError, "reply lost"):
                    release_file("create", folder, tag, uid)
            verified = release_file("check", folder, tag, uid)
            self.assertEqual(verified["size"], 96 * 1024**2)
            self.assertGreaterEqual(verified["allocated_bytes"], verified["size"])
            path = folder / f"release-{tag}.txt"
            inode = path.stat().st_ino
            self.assertEqual(release_file("create", folder, tag, uid), {"name_in_use": True})
            self.assertEqual(path.stat().st_ino, inode)
            self.assertEqual(release_file("cleanup", folder, tag, uid), verified)
            self.assertEqual(release_file("cleanup", folder, tag, uid), {"absent": True})


if __name__ == "__main__":
    unittest.main()
