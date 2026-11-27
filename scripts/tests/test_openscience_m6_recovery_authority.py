"""Exercise the mount and credential translation used by the split recovery site."""

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from openscience_m6_recovery_authority import RecoveryAuthority


class RecoveryAuthorityArgsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.transport = Path(self.tmp.name)
        self.authority = RecoveryAuthority.__new__(RecoveryAuthority)
        self.authority.guest = "/run/nexpoly-m6-authority-example1"
        self.authority.image = "sha256:" + "a" * 64
        self.authority.transport = self.transport
        self.authority.sources = {self.transport / "pg": "pg"}
        self.args = ["docker", "run", "--pull=never", "--network", "none"]

    def test_remaps_known_mount_and_transfers_credentials_on_stdin(self):
        seen = []
        def capture(*argv, stdin=None):
            seen.append((argv, stdin.read() if stdin else None))
        self.authority.command = capture
        env = self.transport / "app.env"
        env.write_bytes(b"test-file-contents")
        guest = self.authority.guest
        args = [*self.args, "--mount", f"type=bind,src={self.transport}/pg,dst=/m2pg,readonly",
                "--env-file", str(env), self.authority.image]
        mapped = self.authority.docker_args(args)
        self.assertEqual(mapped, [*self.args, "--mount", f"type=bind,src={guest}/pg,dst=/m2pg,readonly",
                                  "--env-file", guest + "/app.env", self.authority.image])
        self.assertEqual(seen, [(("dd", "of=" + guest + "/app.env", "status=none"), env.read_bytes()),
                                (("chmod", "600", guest + "/app.env"), None)])

    def test_requires_known_image_network_and_private_files(self):
        self.authority.command = Mock()
        for args, error in [
            ([*self.args, "other-image"], ValueError),
            (["docker", "run", "--pull=never", "--network", "bridge", self.authority.image, "none"], ValueError),
            ([*self.args, "--env-file", "/other/app.env", self.authority.image], ValueError),
            ([*self.args, "--mount", "type=bind,src=/other,dst=/m2pg", self.authority.image], KeyError),
        ]:
            with self.subTest(args=args), self.assertRaises(error):
                self.authority.docker_args(args)
        self.authority.command.assert_not_called()
