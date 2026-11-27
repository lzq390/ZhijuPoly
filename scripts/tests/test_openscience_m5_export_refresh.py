"""Updating the retained M5 NFS service must preserve every registered volume."""
import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import openscience_m5_native_storage as native

spec = importlib.util.spec_from_file_location("m5_exports", native.OPS / "guest-nfs-exports.py")
exports = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exports)


class ExportRefreshTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.rows = {slot: {"mount": str(root / slot), "pseudo": "/m5-" + slot, "uid": 1000 + i, "filesystem_uuid": slot}
                     for i, slot in enumerate(exports.SLOTS)}
        self.old = b"previously reviewed driver"
        self.driver = root / "guest.py"
        self.driver.write_bytes(self.old)
        self.config = root / "ganesha.conf"
        self.legacy = exports.export_config(self.rows, legacy=True)
        self.config.write_text(self.legacy)
        self.start = self.enterContext(patch.object(exports, "start_exports"))
        self.service = self.enterContext(patch.object(exports.subprocess, "run"))
        self.enterContext(patch.object(exports, "ROOT", root))
        self.enterContext(patch.object(exports, "metadata", return_value=self.rows))
        self.legacy_drivers = [self.old, b"earlier driver using the same four export IDs"]
        self.enterContext(patch.object(exports, "LEGACY_DRIVERS", {
            hashlib.sha256(driver).hexdigest() for driver in self.legacy_drivers
        }))
        self.digest = exports.binding_digest(self.rows)

    def test_legacy_upgrade_and_interrupted_activation_retry(self):
        current = exports.export_config(self.rows)
        for driver in self.legacy_drivers:
            self.driver.write_bytes(driver)
            self.config.write_text(self.legacy)
            self.assertEqual(exports.refresh(self.digest), self.rows)
            self.assertEqual(self.config.read_text(), current)
            self.assertEqual(self.driver.read_bytes(), Path(exports.__file__).read_bytes())
        for line in ("MDCACHE { Dir_Chunk = 0; }", "Slot_Table_Size = 1;", "Attr_Expiration_Time = 0;"):
            self.assertIn(line, current)
            self.assertNotIn(line, self.legacy)
        # Simulate the driver having been published before the config, and both
        # files having been published before systemd got the restart.
        self.config.write_text(self.legacy)
        exports.refresh(self.digest)
        exports.refresh(self.digest)
        self.assertEqual(self.start.call_count, len(self.legacy_drivers) + 2)

    def test_foreign_owner_configuration_and_driver_rejected_before_stop(self):
        with self.assertRaisesRegex(ValueError, "bindings"):
            exports.refresh("foreign")
        self.config.write_text(self.legacy.replace("Root_Squash", "None"))
        with self.assertRaisesRegex(ValueError, "configuration"):
            exports.refresh(self.digest)
        self.config.write_text(self.legacy)
        self.driver.write_bytes(b"unknown")
        with self.assertRaisesRegex(ValueError, "driver"):
            exports.refresh(self.digest)
        self.service.assert_not_called()

    def test_failed_start_restores_previous_service(self):
        failure = subprocess.CalledProcessError(1, "start")
        self.start.side_effect = [failure, None]
        with self.assertRaises(subprocess.CalledProcessError):
            exports.refresh(self.digest)
        self.assertEqual(self.driver.read_bytes(), self.old)
        self.assertEqual(self.config.read_text(), self.legacy)
        self.assertEqual(self.start.call_count, 2)

    def test_relay_or_volume_holder_prevents_any_guest_change(self):
        run = "123456789abc"
        data = {"run_id": run, "workspaces": {s: {**r, "volume": f"nexpoly-m5-{run}-{s}"}
                                              for s, r in self.rows.items()}}
        with patch.object(native.manager, "local_engine"), patch.object(native.manager, "docker") as docker, \
                patch.object(native, "guest", return_value=json.dumps(self.rows)) as guest:
            with socket.socket() as relay:
                relay.bind(("127.0.0.1", 0))
                relay.listen(1)
                data["port"] = relay.getsockname()[1]
                with self.assertRaisesRegex(ValueError, "relay"):
                    native.refresh_exports(Path(self.temp.name), data)
            docker.assert_not_called()
            docker.return_value = "dev-container"
            with self.assertRaisesRegex(ValueError, "consumers"):
                native.refresh_exports(Path(self.temp.name), data)
            guest.assert_not_called()
            def alias_in_use(*args):
                if args[0] == "volume":
                    return "alternate-mount"
                return "dev-container" if args[-1] == "volume=alternate-mount" else ""

            docker.side_effect = alias_in_use
            with self.assertRaisesRegex(ValueError, "consumers"):
                native.refresh_exports(Path(self.temp.name), data)
            guest.assert_not_called()
            docker.side_effect = None
            docker.return_value = ""
            native.refresh_exports(Path(self.temp.name), data)
            guest.assert_called_once()

    def test_start_reconciles_retained_export_files_before_reusing_relay(self):
        data = {"workspaces": self.rows}
        expected = [
            hashlib.sha256(Path(exports.__file__).read_bytes()).hexdigest(),
            hashlib.sha256(exports.export_config(self.rows).encode()).hexdigest(),
        ]
        with patch.object(native, "control"), patch.object(native, "guest") as guest, \
                patch.object(native, "refresh_exports") as refresh:
            for hashes in (expected, [expected[0], "old"], ["old", expected[1]]):
                guest.return_value = "\n".join(f"{digest} file" for digest in hashes).encode()
                native.start(Path(self.temp.name), data)
                self.assertEqual(refresh.called, hashes != expected)
                refresh.reset_mock()
