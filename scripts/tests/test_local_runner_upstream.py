"""Offline migration checks; never access real config, services, API or proxy."""
from __future__ import annotations

import contextlib
import importlib.util
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from unittest.mock import MagicMock

from ops.self_hosted_runner.common import Config, RunnerError

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "ops/self_hosted_runner"
SPEC = importlib.util.spec_from_file_location("nexpoly_switch_upstream", SOURCE / "switch-upstream.py")
migration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migration)


class UpstreamMigrationTests(unittest.TestCase):
    def old_config(self):
        return (SOURCE / "config.example.toml").read_bytes().replace(
            b"upstream_port = 17891", b"upstream_port = 27891")

    def test_migration_changes_only_explicit_network_integer_and_preserves_ids_comments(self):
        old = self.old_config().replace(b"app_id = 0", b"app_id = 5740902").replace(
            b"installation_id = 0", b"installation_id = 181516914").replace(
            b"upstream_port = 27891", b"upstream_port = 27891 # approved old parent").replace(b"\n", b"\r\n")
        new = migration.migrated_config(old)
        self.assertEqual(new, old.replace(b"upstream_port = 27891", b"upstream_port = 17891", 1))
        self.assertEqual(migration.migrated_config(new), new)

    def test_migration_rejects_unreviewed_duplicate_or_nonplain_config(self):
        for bad in (
            self.old_config().replace(b"upstream_port = 27891", b"upstream_port = 3128"),
            self.old_config().replace(b"upstream_port = 27891", b"upstream_port = 27_891"),
            self.old_config() + b"\n[network]\nupstream_port=27891\n",
            b"not TOML", b"\xff",
        ):
            with self.subTest(config=bad[-80:]), self.assertRaises(migration.host.HostError):
                migration.migrated_config(bad)

    def test_shared_and_host_contracts_require_new_parent(self):
        self.assertEqual(Config().upstream_port, 17891)
        Config().validate()
        with self.assertRaises(RunnerError):
            Config(upstream_port=27891).validate()
        config = migration.host.read_config(SOURCE / "config.example.toml")
        self.assertEqual(config["network"]["upstream_port"], 17891)

    def run_migration(self, *, install_failure=False, snapshot_failure=False, firewall=True, already_new=False):
        with TemporaryDirectory() as directory:
            directory = Path(directory)
            config = directory / "config.toml"
            old = self.old_config()
            if already_new:
                old = migration.migrated_config(old)
            config.write_bytes(old)
            config.chmod(0o640)
            seen = []
            def install(source, installed_path):
                seen.append((source, installed_path.read_bytes()))
                if install_failure:
                    raise migration.host.HostError("fixture install failed")
            with patch.object(migration.host, "CONFIG", config), \
                    patch.object(migration.host, "root_only"), \
                    patch.object(migration.host, "installation_lock", return_value=contextlib.nullcontext()), \
                    patch.object(migration, "protected_file", side_effect=lambda path: path.read_bytes()), \
                    patch.object(migration, "require_quiescent") as quiescent, \
                    patch.object(migration.host, "snapshot_package", side_effect=(migration.host.HostError("bad source") if snapshot_failure else None)), \
                    patch.object(migration.host, "_install", side_effect=install), \
                    patch.object(migration.host, "firewall_check", return_value=firewall), \
                    patch.object(migration, "proxy_health") as health:
                error = None
                try:
                    migration.switch_upstream(SOURCE)
                except migration.host.HostError as exc:
                    error = str(exc)
                backup = directory / migration.BACKUP_NAME
                return {"original": old, "config": config.read_bytes(), "mode": config.stat().st_mode & 0o777,
                        "backup": backup.read_bytes() if backup.exists() else None, "seen": seen,
                        "health_calls": health.call_count, "quiescent_calls": quiescent.call_count, "error": error}

    def test_switch_migrates_real_config_before_installer_and_keeps_original_backup(self):
        result = self.run_migration()
        self.assertIsNone(result["error"])
        self.assertEqual(result["backup"], result["original"])
        self.assertEqual(result["config"], migration.migrated_config(result["original"]))
        self.assertEqual(result["seen"][0][1], result["config"])
        self.assertEqual(result["mode"], 0o640)
        self.assertEqual(result["health_calls"], 1)
        self.assertEqual(result["quiescent_calls"], 2)

    def test_source_failure_precedes_config_change_and_install_failure_never_checks_health(self):
        invalid = self.run_migration(snapshot_failure=True)
        self.assertEqual(invalid["config"], invalid["original"])
        self.assertIsNone(invalid["backup"])
        self.assertEqual(invalid["seen"], [])
        failed = self.run_migration(install_failure=True)
        self.assertIn("fixture install failed", failed["error"])
        self.assertEqual(failed["backup"], failed["original"])
        self.assertEqual(failed["health_calls"], 0)
        self.assertEqual(failed["quiescent_calls"], 1)

    def test_firewall_mismatch_cannot_be_reported_as_success_or_probe_network(self):
        failed = self.run_migration(firewall=False)
        self.assertIn("firewall", failed["error"])
        self.assertEqual(failed["health_calls"], 0)

    def test_reapplying_new_config_rerenders_without_overwriting_old_backup(self):
        result = self.run_migration(already_new=True)
        self.assertEqual(result["config"], result["original"])
        self.assertIsNone(result["backup"])
        self.assertEqual(len(result["seen"]), 1)
        self.assertEqual(result["health_calls"], 1)

    def test_registered_idle_vm_is_rejected_immediately_without_services_or_api_mutation(self):
        with TemporaryDirectory() as directory:
            state = Path(directory)
            (state / "instances").mkdir()
            config = {"paths": {"state": str(state)}}
            for instance in ({"registered": True, "ever_busy": False}, {"job_id": 123}, None):
                with self.subTest(instance=instance):
                    def read(path):
                        return json.dumps({"reason": "administrator_drain"} if path.name == "drain.json"
                                          else {"instance": instance}).encode()
                    with patch.object(migration, "protected_file", side_effect=read), \
                            patch.object(migration.host, "run", side_effect=[
                                SimpleNamespace(stdout="inactive\n"), SimpleNamespace(stdout="")]) as run:
                        if instance is None:
                            migration.require_quiescent(config)
                        else:
                            with self.assertRaisesRegex(migration.host.HostError, "retire"):
                                migration.require_quiescent(config)
                        self.assertTrue(all(call.args[0][:2] in (["systemctl", "show"], ["systemctl", "list-units"])
                                            for call in run.call_args_list))

    def test_controller_or_vm_not_quiescent_fails_before_journal_access(self):
        for status, active in (("active", ""), ("activating", ""), ("inactive", "nexpoly-runner-vm@abc.service")):
            with self.subTest(status=status, active=active), \
                    patch.object(migration.host, "run", side_effect=[
                        SimpleNamespace(stdout=status), SimpleNamespace(stdout=active)]), \
                    patch.object(migration, "protected_file") as read:
                with self.assertRaises(migration.host.HostError):
                    migration.require_quiescent({"paths": {"state": "/unused"}})
                read.assert_not_called()

    @unittest.skipIf(os.geteuid() == 0, "requires unprivileged process")
    def test_actual_user_owned_config_is_rejected(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_bytes(self.old_config())
            with self.assertRaisesRegex(migration.host.HostError, "root-owned"):
                migration.protected_file(path)

    def test_unprivileged_call_fails_before_any_config_or_lock_access(self):
        with patch.object(migration.host.os, "geteuid", return_value=1000), \
                patch.object(migration, "protected_file") as read, \
                patch.object(migration.host, "installation_lock") as lock:
            with self.assertRaisesRegex(migration.host.HostError, "administrator"):
                migration.switch_upstream(SOURCE)
            read.assert_not_called()
            lock.assert_not_called()

    def test_health_uses_dedicated_proxy_despite_inherited_bypass_and_restores_environment(self):
        response = MagicMock()
        response.__enter__.return_value.status = 200
        opener = MagicMock()
        def open_request(request, *, timeout):
            self.assertEqual(request.full_url, "https://github.com/robots.txt")
            self.assertEqual(timeout, 15)
            self.assertNotIn("NO_PROXY", os.environ)
            self.assertNotIn("no_proxy", os.environ)
            return response
        opener.open.side_effect = open_request
        with patch.dict(os.environ, {"NO_PROXY": "*", "no_proxy": "github.com"}), \
                patch.object(migration.urllib.request, "build_opener", return_value=opener) as build:
            migration.proxy_health()
            self.assertEqual(os.environ["NO_PROXY"], "*")
            self.assertEqual(os.environ["no_proxy"], "github.com")
        self.assertEqual(build.call_args.args[0].proxies, {"https": "http://127.0.0.1:27892"})
        response.__enter__.return_value.read.assert_called_once_with(1024)
        response.__exit__.assert_called_once()

    def test_health_failure_is_sanitized_and_never_tries_another_proxy(self):
        opener = MagicMock()
        opener.open.side_effect = ConnectionResetError("sensitive.invalid registration-secret")
        with patch.dict(os.environ, {"NO_PROXY": "*"}), \
                patch.object(migration.urllib.request, "build_opener", return_value=opener) as build:
            with self.assertRaises(migration.host.HostError) as failure:
                migration.proxy_health()
            self.assertEqual(os.environ["NO_PROXY"], "*")
        self.assertNotIn("sensitive.invalid", str(failure.exception))
        self.assertNotIn("registration-secret", str(failure.exception))
        self.assertIn("ConnectionResetError", str(failure.exception))
        build.assert_called_once()
        opener.open.assert_called_once()


if __name__ == "__main__":
    unittest.main()
