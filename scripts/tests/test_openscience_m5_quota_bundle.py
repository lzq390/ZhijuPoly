"""A bad or misplaced M5 quota package must fail before the target quota or workspace changes."""

import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import openscience_m5_quota_bundle as bundle


class BundleGuardTests(unittest.TestCase):
    def test_wrong_pin_or_owner_cannot_reach_quota_restore(self):
        owner = {
            "environment": "dev-m1",
            "user_id": "test-user-a",
            "workspace": "a" * 32,
            "uid": 230001,
        }
        manifest = {"format": bundle.FORMAT, "quota": {"identity": owner}}
        with (
            TemporaryDirectory() as base,
            patch.object(bundle.quota, "restore") as restore,
        ):
            root = Path(base)
            package, target = root / "package", root / "target"
            package.mkdir(mode=0o700)
            target.mkdir()
            raw = json.dumps(manifest).encode()
            (package / "manifest.json").write_bytes(raw)
            (package / "manifest.json").chmod(0o600)
            pin = hashlib.sha256(raw).hexdigest()
            for identity, supplied_pin in [
                (owner, "0" * 64),
                ({**owner, "user_id": "test-user-b"}, pin),
            ]:
                with (
                    self.subTest(identity=identity, supplied_pin=supplied_pin),
                    self.assertRaises(ValueError),
                ):
                    bundle.restore(target, identity, package, supplied_pin)
            restore.assert_not_called()
            self.assertEqual(list(target.iterdir()), [])

    def test_source_rejects_nested_bind_mount_on_same_device(self):
        with TemporaryDirectory() as base:
            mount = Path(base)
            source = mount / "workspace"
            source.mkdir()
            inside = str(source / "project space").replace(" ", r"\040")
            records = f"9 1 8:1 /shared {inside} rw - ext4 /dev/vdb rw\n"
            with patch.object(Path, "read_text", return_value=records):
                with self.assertRaisesRegex(ValueError, "nested mount"):
                    bundle.check_source_mount(source, mount)
