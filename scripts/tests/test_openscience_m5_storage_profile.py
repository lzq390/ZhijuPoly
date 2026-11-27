"""Exercise launch selection and interrupted setup without booting or editing retained media."""
import errno
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "scripts/ci"), str(ROOT / "scripts"), str(ROOT / "ops/openscience-per-user-dev")]
import prepare_openscience_m5_water_pool as migration
import start_openscience_m5_web_vm as vm
import water_pool


class StorageProfileTest(unittest.TestCase):
    def test_split_candidate_rejects_replaced_or_crossed_disks(self):
        with TemporaryDirectory() as tmp, patch.object(vm, "secondary", return_value=("primary-id", "backup-id")):
            root = Path(tmp)
            backup = root / "separate"
            backup.mkdir()
            layout = vm.disk_layout(root, backup)
            pins = {}
            for serial, (path, size) in layout.items():
                expected = root if serial in ("m5-a", "m5-b") else backup
                self.assertEqual(path.parent, expected)
                with path.open("xb") as stream:
                    stream.truncate(size)
                path.chmod(0o600)
                pins[serial] = path.stat().st_ino
            config = dict(backup_dir=str(backup), host_disks=dict(primary="primary-id", backup="backup-id"),
                          disk_inodes=pins)
            self.assertEqual(vm.checked_layout(root, config), layout)
            with self.assertRaisesRegex(ValueError, "disks changed"):
                vm.checked_layout(root, {**config, "host_disks": dict(primary="other", backup="backup-id")})
            disk, size = layout["m5-backup"]
            disk.rename(backup / "prior.raw")
            with disk.open("xb") as stream:
                stream.truncate(size)
            disk.chmod(0o600)
            with self.assertRaisesRegex(ValueError, "retained M5 disk"):
                vm.checked_layout(root, config)

    def test_only_explicit_fresh_layout_can_attach_raw(self):
        layout = {"filesystem_uuid": "pinned", "sources": {"a": "A", "b": "B"}}
        with TemporaryDirectory() as tmp, patch.object(vm, "launch_pinned") as start:
            root = Path(tmp)
            raw = [root / (serial + ".raw") for serial in migration.DATA_DISKS]
            for disk in raw:
                disk.write_text(disk.name)
            def digests():
                return [hashlib.sha256(p.read_bytes()).digest() for p in raw]
            before = digests()
            for profile in (None, migration.pool_profile(layout), migration.DEDICATED):
                (root / "vm.json").write_text(json.dumps({"storage": profile}))
                if profile == migration.DEDICATED:
                    vm.launch(root, 18777)
                    start.assert_called_once_with(root, 18777, False)
                    start.reset_mock()
                else:
                    with self.assertRaises(ValueError):
                        vm.launch(root, 18777)
                    start.assert_not_called()
            (root / "waterpool").mkdir()
            with self.assertRaisesRegex(ValueError, "incomplete"):
                vm.launch(root, 18777)
            (root / "vm.json").write_text(json.dumps({"storage": migration.pool_profile(layout)}))
            with patch.object(migration, "verify_layout", return_value={**layout, "filesystem_uuid": "other"}):
                with self.assertRaisesRegex(ValueError, "pin"):
                    vm.launch(root, 18777)
            start.assert_not_called()
            with patch.object(migration, "verify_layout", return_value=layout):
                vm.launch(root, 18777)
            start.assert_called_once_with(root, 18777, True)
            self.assertEqual(digests(), before)

    def test_interrupted_config_publication_is_retryable(self):
        cfg = {"filesystem_uuid": "original"}
        with TemporaryDirectory() as tmp, patch.object(water_pool, "CONFIG", Path(tmp) / "water.json"):
            with patch("os.replace", side_effect=OSError(errno.ENOSPC, "full")), self.assertRaises(OSError):
                water_pool.install_config(cfg)
            self.assertFalse(water_pool.CONFIG.exists())
            water_pool.install_config(cfg)
            water_pool.install_config(cfg)
            with self.assertRaisesRegex(ValueError, "changed"):
                water_pool.install_config({"filesystem_uuid": "foreign"})
            self.assertEqual(json.loads(water_pool.CONFIG.read_bytes()), cfg)
            self.assertEqual(water_pool.CONFIG.stat().st_mode & 0o777, 0o600)
