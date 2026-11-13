"""Offline host regression tests; no root, network changes, VM or Docker access."""
import copy
import configparser
import fcntl
import fnmatch
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from ops.self_hosted_runner import controller, host


class LocalRunnerHostTests(unittest.TestCase):
    def setUp(self):
        self.config = host.read_config(host.PACKAGE / "config.example.toml")

    def test_install_refuses_non_root_before_any_mutation(self):
        with patch.object(host.os, "geteuid", return_value=1000), patch.object(host, "run") as run:
            with self.assertRaisesRegex(host.HostError, "administrator"):
                host.install(Path("/missing"), Path("/missing/config.toml"), with_packages=True)
            run.assert_not_called()

    def test_installer_refuses_active_template_lock_before_installation_changes(self):
        real_fstat = os.fstat
        def root_metadata(fd):
            info = real_fstat(fd)
            return SimpleNamespace(st_mode=info.st_mode, st_uid=0, st_nlink=info.st_nlink)
        with TemporaryDirectory() as directory:
            lock = Path(directory) / "controller.lock"
            with patch.object(controller, "LOCK", lock), patch.object(host, "CONTROLLER_LOCK", lock), \
                    patch.object(host, "root_only"), patch.object(host.os, "fstat", side_effect=root_metadata), \
                    patch.object(host, "_install") as install_body, patch.object(host, "run") as run:
                # This is the real lock used by serve/update-template, including
                # the download phase before any VM appears in systemctl.
                with controller.exclusive_lock():
                    with self.assertRaisesRegex(host.HostError, "template update.*holds the runner lock"):
                        host.install(Path("/missing"), Path("/missing/config.toml"), with_packages=True)
                install_body.assert_not_called()
                run.assert_not_called()

    def test_installer_holds_lock_through_body_and_releases_it_after_failure(self):
        real_fstat = os.fstat
        def root_metadata(fd):
            info = real_fstat(fd)
            return SimpleNamespace(st_mode=info.st_mode, st_uid=0, st_nlink=info.st_nlink)
        with TemporaryDirectory() as directory:
            lock = Path(directory) / "controller.lock"
            def install_body(*args, **kwargs):
                with lock.open("r+b") as contender:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                raise host.HostError("fixture installation failed")
            with patch.object(host, "CONTROLLER_LOCK", lock), patch.object(host, "root_only"), \
                    patch.object(host.os, "fstat", side_effect=root_metadata), \
                    patch.object(host, "_install", side_effect=install_body):
                with self.assertRaisesRegex(host.HostError, "fixture installation failed"):
                    host.install(Path("/missing"), Path("/missing/config.toml"))
                # A failed installation cannot strand the global lock.
                with lock.open("r+b") as contender:
                    fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_installer_rejects_symlink_lock_without_touching_target(self):
        real_fstat = os.fstat
        def root_metadata(fd):
            info = real_fstat(fd)
            return SimpleNamespace(st_mode=info.st_mode, st_uid=0, st_nlink=info.st_nlink)
        with TemporaryDirectory() as directory:
            target = Path(directory) / "unrelated"
            target.write_text("unchanged")
            lock = Path(directory) / "controller.lock"
            lock.symlink_to(target)
            with patch.object(host, "CONTROLLER_LOCK", lock), patch.object(host, "root_only"), \
                    patch.object(host.os, "fstat", side_effect=root_metadata), \
                    patch.object(host, "_install") as install_body:
                with self.assertRaises(OSError):
                    host.install(Path("/missing"), Path("/missing/config.toml"))
                install_body.assert_not_called()
            self.assertEqual(target.read_text(), "unchanged")

    def test_controller_stop_preserves_shared_lock_inode(self):
        config = configparser.ConfigParser(interpolation=None, strict=False)
        config.read(host.PACKAGE / "systemd/nexpoly-runner-controller.service")
        self.assertEqual(config["Service"]["RuntimeDirectory"], host.CONTROLLER_LOCK.parent.name)
        self.assertEqual(host.CONTROLLER_LOCK, controller.LOCK)
        self.assertEqual(config["Service"]["RuntimeDirectoryMode"], "0700")
        self.assertEqual(config["Service"]["RuntimeDirectoryPreserve"], "yes")

    def test_proxy_activation_uses_one_restart_and_checks_active_state(self):
        unit = "nexpoly-runner-proxy.service"
        expected = [
            ["systemctl", "enable", unit],
            ["systemctl", "restart", unit],
            ["systemctl", "is-active", "--quiet", unit],
        ]
        with patch.object(host, "run") as run, patch.object(host.socket, "create_connection") as connect:
            # Both initial installation and a retry apply the complete unit and
            # configuration once, without signaling a just-started daemon.
            host.activate_proxy()
            host.activate_proxy()
        self.assertEqual([call.args[0] for call in run.call_args_list], expected * 2)
        self.assertEqual(connect.call_count, 2)
        self.assertEqual(connect.call_args.args, (("127.0.0.1", 27892),))
        self.assertEqual(connect.call_args.kwargs, {"timeout": 0.5})

    def test_proxy_activation_does_not_hide_restart_failure(self):
        unit = "nexpoly-runner-proxy.service"
        with patch.object(host, "run", side_effect=[None, host.HostError("restart failed")]) as run, \
                patch.object(host.socket, "create_connection") as connect:
            with self.assertRaisesRegex(host.HostError, "restart failed"):
                host.activate_proxy()
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            ["systemctl", "enable", unit],
            ["systemctl", "restart", unit],
        ])
        connect.assert_not_called()

    def test_proxy_activation_rejects_daemon_that_does_not_remain_active(self):
        with patch.object(host, "run", side_effect=[None, None, host.HostError("inactive")]), \
                patch.object(host.socket, "create_connection") as connect:
            with self.assertRaisesRegex(host.HostError, "inactive"):
                host.activate_proxy()
        connect.assert_not_called()

    def test_proxy_activation_waits_for_delayed_local_bind(self):
        for transient in (ConnectionRefusedError, TimeoutError):
            with self.subTest(transient=transient.__name__), patch.object(host, "run") as run, \
                    patch.object(host.socket, "create_connection", side_effect=[
                        transient("not ready"), transient("not ready"), MagicMock()]) as connect, \
                    patch.object(host.time, "monotonic", return_value=0), \
                    patch.object(host.time, "sleep") as sleep:
                host.activate_proxy()
                self.assertEqual(connect.call_count, 3)
                self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.25, 0.25])
                checks = [call.args[0] for call in run.call_args_list if call.args[0][1] == "is-active"]
                self.assertEqual(len(checks), 3)

    def test_proxy_activation_listener_wait_has_a_30_second_deadline(self):
        elapsed = [0.0]
        def sleep(seconds):
            elapsed[0] += seconds
        with patch.object(host, "run"), \
                patch.object(host.socket, "create_connection", side_effect=ConnectionRefusedError("not ready")) as connect, \
                patch.object(host.time, "monotonic", side_effect=lambda: elapsed[0]), \
                patch.object(host.time, "sleep", side_effect=sleep):
            with self.assertRaisesRegex(host.HostError, "127.0.0.1:27892.*within 30 seconds"):
                host.activate_proxy()
        self.assertEqual(elapsed[0], 30)
        self.assertEqual(connect.call_count, 120)
        self.assertTrue(all(0 < call.kwargs["timeout"] <= 0.5 for call in connect.call_args_list))

    def test_proxy_activation_stops_waiting_when_service_exits(self):
        with patch.object(host, "run", side_effect=[None, None, None, host.HostError("inactive")]) as run, \
                patch.object(host.socket, "create_connection", side_effect=ConnectionRefusedError("not ready")) as connect, \
                patch.object(host.time, "sleep") as sleep:
            with self.assertRaisesRegex(host.HostError, "inactive"):
                host.activate_proxy()
        self.assertEqual(run.call_count, 4)
        self.assertEqual(connect.call_count, 1)
        sleep.assert_called_once_with(0.25)

    def test_proxy_activation_does_not_retry_or_expose_unexpected_socket_errors(self):
        with patch.object(host, "run"), patch.object(host.socket, "create_connection", side_effect=
                PermissionError("registration-secret sensitive.invalid")) as connect, \
                patch.object(host.time, "sleep") as sleep:
            with self.assertRaises(host.HostError) as failure:
                host.activate_proxy()
        message = str(failure.exception)
        self.assertIn("Cannot verify dedicated proxy listener 127.0.0.1:27892 readiness", message)
        self.assertNotIn("registration-secret", message)
        self.assertNotIn("sensitive.invalid", message)
        connect.assert_called_once()
        sleep.assert_not_called()

    def test_proxy_activation_closes_its_readiness_connection(self):
        connection, peer = host.socket.socketpair()
        try:
            with patch.object(host, "run"), patch.object(host.socket, "create_connection", return_value=connection):
                host.activate_proxy()
            self.assertEqual(connection.fileno(), -1)
        finally:
            connection.close()
            peer.close()

    def test_rejects_paths_network_or_resources_outside_reviewed_budget(self):
        mutations = [
            ('root = "/data/lzq/github-runner"', 'root = "/tmp/github-runner"'),
            ('state = "/data/lzq/github-runner/state"', 'state = "/data/lzq/github-runner/../state"'),
            ('upstream_host = "127.0.0.1"', 'upstream_host = "114.214.255.154"'),
            ('dns = "202.38.64.7"', 'dns = "127.0.0.53"'),
            ('proxy_port = 27892', 'proxy_port = 27891'),
            ('vcpus = 8', 'vcpus = 128'),
            ('memory_mib = 32768', 'memory_mib = 131072'),
            ('disk_gib = 160', 'disk_gib = 500'),
            ('host_min_free_gib = 100', 'host_min_free_gib = 1'),
        ]
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            for before, after in mutations:
                with self.subTest(mutation=after):
                    path.write_text((host.PACKAGE / "config.example.toml").read_text().replace(before, after))
                    with self.assertRaises(host.HostError):
                        host.read_config(path)

    def test_firewall_replacement_is_one_transaction_scoped_to_its_table(self):
        calls = []
        def run(args, **kwargs):
            calls.append((args, kwargs))
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        with patch.object(host, "root_only"), patch.object(host, "run", side_effect=run), \
                patch.object(host.pwd, "getpwnam", side_effect=lambda user: SimpleNamespace(pw_uid=200 if user == host.VM_USER else 201)):
            host.firewall_apply(self.config)
        check, apply = calls[-2:]
        self.assertIn("--check", check[0])
        self.assertEqual(check[1]["input_text"], apply[1]["input_text"])
        transaction = apply[1]["input_text"]
        self.assertTrue(transaction.startswith("delete table inet nexpoly_runner\ntable inet nexpoly_runner"))
        self.assertNotIn("flush", transaction)
        self.assertNotIn("docker", transaction.lower())
        self.assertIn("hook output", transaction)
        self.assertNotIn("hook forward", transaction)
        self.assertIn("meta skuid 200 jump vm_egress", transaction)
        self.assertIn("ip daddr 127.0.0.1 tcp dport 27892", transaction)
        self.assertIn("ip daddr 127.0.0.1 tcp dport 27891", transaction)
        self.assertIn("ip daddr 202.38.64.7 udp dport 53", transaction)
        vm_chain = transaction.split("chain vm_egress", 1)[1].split("chain proxy_egress", 1)[0]
        self.assertNotIn("dport 27891", vm_chain)
        self.assertNotIn("dport 443", vm_chain)
        self.assertTrue(vm_chain.rstrip().endswith("counter drop\n  }"))

    def test_proxy_assets_fail_closed_and_do_not_log_token_urls(self):
        assets = host.render_assets(self.config, 200, 201, ["127.0.0.1/32", "114.214.255.0/24"])
        proxy = assets["/etc/nexpoly-runner/squid.conf"]
        for directive in ("http_port 127.0.0.1:27892", "never_direct allow all",
                          "http_access deny !permitted_domains", "http_access deny CONNECT !ssl_ports",
                          "http_access deny !CONNECT !http_ports", "access_log none",
                          'dstdomain -n "/etc/nexpoly-runner/proxy-domains.txt"'):
            self.assertIn(directive, proxy)
        self.assertLess(proxy.index("http_access deny host_targets"), proxy.index("http_access allow loopback"))
        proxy_unit = assets["/etc/systemd/system/nexpoly-runner-proxy.service"]
        self.assertNotIn("ExecReload=", proxy_unit)
        self.assertIn("ExecStartPre=/usr/sbin/squid -k parse", proxy_unit)
        vm = assets["/etc/systemd/system/nexpoly-runner-vm@.service"]
        for directive in ("User=nexpoly-vm", "CPUQuota=800%", "MemoryMax=34G", "MemorySwapMax=0",
                          "KillMode=control-group", "AppArmorProfile=nexpoly-runner-vm", "LimitCORE=0",
                          "LogNamespace=nexpoly-runner", "OOMPolicy=kill"):
            self.assertIn(directive, vm)
        self.assertIn("SystemMaxUse=1G", assets["/etc/systemd/journald@nexpoly-runner.conf"])

    def test_qemu_has_kvm_only_bounded_disks_and_private_unix_channels(self):
        command = host.qemu_command(self.config, "00000000000040008000000000000001", check_files=False)
        text = " ".join(command)
        for fragment in ("q35,accel=kvm", "-smp 8", "-m 32768", "bps_rd=104857600,bps_wr=104857600",
                         "seed.iso,if=virtio,format=raw,readonly=on", "ipv6=off", "-sandbox on,",
                         "org.nexpoly.runner.bootstrap", "org.nexpoly.runner.events"):
            self.assertIn(fragment, text)
        for forbidden in ("hostfwd", "smb=", "virtiofs", "9p", "docker.sock", "accel=tcg"):
            self.assertNotIn(forbidden, text)

    def test_apparmor_allows_backing_image_locks_without_template_writes(self):
        assets = host.render_assets(self.config, 200, 201, ["127.0.0.1/32"])
        profile = assets["/etc/apparmor.d/nexpoly-runner-vm"]
        templates = self.config["paths"]["state"] + "/templates/"
        rules = []
        for line in profile.splitlines():
            fields = line.split("#", 1)[0].strip().rstrip(",").split()
            if any(field.startswith(templates) for field in fields):
                owner_only = fields[0] == "owner"
                if owner_only:
                    fields = fields[1:]
                self.assertEqual(len(fields), 2)
                pattern, permissions = fields
                self.assertLessEqual(set(permissions), {"r", "k"})
                rules.append((owner_only, pattern, set(permissions)))

        def nonowner_permissions(path):
            # The sealed base is root-owned; the QEMU account is not its owner.
            return set().union(*(permissions for owner, pattern, permissions in rules
                                 if not owner and fnmatch.fnmatchcase(path, pattern)))

        self.assertEqual(nonowner_permissions(templates + "base-" + "a" * 64 + ".qcow2"), {"r", "k"})
        self.assertEqual(nonowner_permissions(templates + "current.json"), {"r"})
        self.assertEqual(nonowner_permissions(templates + "unsealed.qcow2"), {"r"})

    @unittest.skipUnless(host.shutil.which("systemd-tmpfiles"), "systemd-tmpfiles is unavailable")
    def test_build_log_expiry_runs_without_a_future_build_and_never_creates_state(self):
        assets = host.render_assets(self.config, 200, 201, ["127.0.0.1/32"])
        rule = assets["/etc/tmpfiles.d/nexpoly-runner.conf"]
        reviewed_path = self.config["paths"]["state"] + "/logs/template-builds"
        self.assertEqual(rule, f"e {reviewed_path} - - - m:7d\n")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            logs = root / "build-logs"
            logs.mkdir(mode=0o700)
            old, recent = logs / "old.log", logs / "recent.log"
            old.write_text("expired")
            recent.write_text("retained")
            os.utime(old, (0, 0))
            configuration = root / "cleanup.conf"
            configuration.write_text(rule.replace(reviewed_path, str(logs)))
            result = host.subprocess.run(["systemd-tmpfiles", "--clean", str(configuration)],
                                         text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(old.exists())
            self.assertEqual(recent.read_text(), "retained")
            absent = root / "unmounted-state/logs/template-builds"
            configuration.write_text(rule.replace(reviewed_path, str(absent)))
            result = host.subprocess.run(["systemd-tmpfiles", "--create", "--clean", str(configuration)],
                                         text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(absent.exists())

    def test_qemu_rejects_instance_path_injection(self):
        for instance in ("../templates/current", "x,y", "00000000-0000-4000-8000-000000000001", "A" * 32):
            with self.subTest(instance=instance), self.assertRaisesRegex(host.HostError, "UUID"):
                host.qemu_command(self.config, instance, check_files=False)

    def fake_package(self, path):
        path.mkdir()
        for name in host.REQUIRED_FILES:
            file = path / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text("# fixture\n")
        (path / "guest").mkdir(exist_ok=True)
        (path / "guest/init.sh").write_text("#!/bin/sh\nexit 0\n")
        return path

    def test_snapshot_copies_complete_package_and_normalizes_permissions(self):
        with TemporaryDirectory() as directory:
            source = self.fake_package(Path(directory) / "staged")
            (source / "host.py").chmod(0o666)
            target = Path(directory) / "private"
            host.snapshot_package(source, target)
            self.assertEqual((target / "host.py").stat().st_mode & 0o777, 0o644)
            self.assertEqual((target / "guest/init.sh").stat().st_mode & 0o777, 0o755)
            self.assertEqual((source / "host.py").stat().st_mode & 0o777, 0o666)

    def test_snapshot_rejects_unsafe_or_incomplete_source(self):
        for bad_entry in ("symlink", "directory_symlink", "secret", "incomplete"):
            with self.subTest(entry=bad_entry), TemporaryDirectory() as directory:
                source = self.fake_package(Path(directory) / "staged")
                if bad_entry == "symlink":
                    (source / "outside").symlink_to("/etc/passwd")
                elif bad_entry == "directory_symlink":
                    (source / "outside").symlink_to("/etc", target_is_directory=True)
                elif bad_entry == "secret":
                    (source / "app.pem").write_text("test only, not a key")
                else:
                    (source / "controller.py").unlink()
                with self.assertRaises((host.HostError, OSError)):
                    host.snapshot_package(source, Path(directory) / "private")

    def test_snapshot_rejects_missing_runtime_dependency_before_install(self):
        # These are concrete runtime dependencies, independent of the installer
        # manifest: a nonempty guest/ directory alone cannot run render_seed.
        missing_files = (
            "github_api.py", "systemd/nexpoly-runner-storage.service",
            "systemd/nexpoly-runner-vm@.service", "systemd/nexpoly-runner-proxy.service",
            "systemd/nexpoly-runner-firewall.service", "systemd/nexpoly-runner-controller.service",
            "guest/bootstrap.py", "guest/build.py", "guest/smoke.py", "guest/job-event.py",
            "guest/job-started.sh", "guest/job-completed.sh",
            "guest/nexpoly-ci-bootstrap.service", "guest/software-lock.json",
        )
        for missing in missing_files:
            with self.subTest(missing=missing), TemporaryDirectory() as directory:
                source = self.fake_package(Path(directory) / "staged")
                (source / missing).unlink()
                with self.assertRaises(host.HostError) as error:
                    host.snapshot_package(source, Path(directory) / "private")
                self.assertEqual(str(error.exception), f"Incomplete staged package: {missing}")

    def test_storage_missing_image_fails_closed_without_mount_commands(self):
        with patch.object(host, "run") as run, patch.object(host.Path, "stat", side_effect=FileNotFoundError):
            self.assertFalse(host.storage_check(self.config))
            run.assert_not_called()

    def test_storage_rejects_mount_with_wrong_source(self):
        metadata = SimpleNamespace(st_mode=0o100600, st_size=host.STORAGE_BYTES,
                                   st_blocks=host.STORAGE_BYTES // 512, st_uid=0)
        mounted = {"filesystems": [{"target": self.config["paths"]["state"], "source": "/dev/nvme0n1",
                                   "fstype": "ext4", "options": "rw,nodev,nosuid,noexec"}]}
        with patch.object(host, "reject_symlinks"), patch.object(host.Path, "stat", return_value=metadata), \
                patch.object(host, "run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps(mounted))):
            self.assertFalse(host.storage_check(self.config))

    def test_storage_rejects_sparse_backing_file(self):
        metadata = SimpleNamespace(st_mode=0o100600, st_size=host.STORAGE_BYTES, st_blocks=4096, st_uid=0)
        with patch.object(host, "reject_symlinks"), patch.object(host.Path, "stat", return_value=metadata), patch.object(host, "run") as run:
            self.assertFalse(host.storage_check(self.config))
            run.assert_not_called()

    def backing_metadata(self, *, missing_bytes=0, **changes):
        values = {
            "st_mode": 0o100600, "st_size": host.STORAGE_BYTES,
            "st_blocks": (host.STORAGE_BYTES - missing_bytes) // 512,
            "st_uid": 0, "st_gid": 0, "st_nlink": 1,
            "st_dev": 1, "st_ino": 1234,
        }
        values.update(changes)
        return SimpleNamespace(**values)

    def test_storage_identity_can_be_checked_before_allocation_repair(self):
        metadata = self.backing_metadata(missing_bytes=3 * host.GIB)
        state = self.config["paths"]["state"]
        image = str(Path(self.config["paths"]["root"]) / "storage.ext4")
        mounted = {"filesystems": [{"target": state, "source": "/dev/loop6",
                                   "fstype": "ext4", "options": "rw,nodev,nosuid,noexec"}]}
        text_values = {
            "/sys/block/loop6/loop/backing_file": image + "\n",
            "/sys/block/loop6/loop/offset": "0\n",
            "/sys/block/loop6/loop/sizelimit": "0\n",
            "/sys/block/loop6/queue/discard_max_bytes": "4294966784\n",
        }
        def metadata_for(path, *args, **kwargs):
            return SimpleNamespace(st_dev=2 if str(path) == state else 1)
        def read_text(path, *args, **kwargs):
            return text_values[str(path)]
        with patch.object(host, "reject_symlinks"), \
                patch.object(host.Path, "stat", return_value=metadata), \
                patch.object(host.os, "stat", side_effect=metadata_for), \
                patch.object(host.Path, "read_text", read_text), \
                patch.object(host, "run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps(mounted))):
            self.assertFalse(host.storage_check(self.config))
            self.assertTrue(host.storage_check(self.config, require_allocation=False, require_guard=False))
            # Skipping allocation must never waive the loop/source identity check.
            text_values["/sys/block/loop6/loop/backing_file"] = "/data/some-other-image.ext4\n"
            self.assertFalse(host.storage_check(self.config, require_allocation=False, require_guard=False))

    def test_storage_rejects_fully_allocated_loop_with_discard_enabled(self):
        metadata = self.backing_metadata()
        state = self.config["paths"]["state"]
        image = str(Path(self.config["paths"]["root"]) / "storage.ext4")
        mounted = {"filesystems": [{"target": state, "source": "/dev/loop6",
                                   "fstype": "ext4", "options": "rw,nodev,nosuid,noexec"}]}
        text_values = {
            "/sys/block/loop6/loop/backing_file": image + "\n",
            "/sys/block/loop6/loop/offset": "0\n",
            "/sys/block/loop6/loop/sizelimit": "0\n",
            "/sys/block/loop6/queue/discard_max_bytes": "4294966784\n",
        }
        def read_text(path, *args, **kwargs):
            return text_values[str(path)]
        with patch.object(host, "reject_symlinks"), \
                patch.object(host.Path, "stat", return_value=metadata), \
                patch.object(host.os, "stat", side_effect=lambda path: SimpleNamespace(st_dev=2 if str(path) == state else 1)), \
                patch.object(host.Path, "read_text", read_text), \
                patch.object(host, "run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps(mounted))):
            self.assertFalse(host.storage_check(self.config))
            text_values["/sys/block/loop6/queue/discard_max_bytes"] = "0\n"
            self.assertTrue(host.storage_check(self.config))

    def test_reservation_fills_missing_blocks_without_truncation_or_formatting(self):
        image = Path(self.config["paths"]["root"]) / "storage.ext4"
        before = self.backing_metadata(missing_bytes=3 * host.GIB)
        after = self.backing_metadata()
        with patch.object(host, "root_only"), patch.object(host, "reject_symlinks"), \
                patch.object(host.os, "open", return_value=73) as opened, \
                patch.object(host.os, "close") as closed, \
                patch.object(host.os, "posix_fallocate") as allocate, \
                patch.object(host.os, "fstat", side_effect=lambda fd: after if allocate.called else before), \
                patch.object(host.os, "fsync") as fsync, \
                patch.object(host.shutil, "disk_usage", return_value=SimpleNamespace(free=140 * host.GIB)), \
                patch.object(host, "run") as run:
            host.reserve_storage(image, self.config)
        flags = opened.call_args.args[1]
        self.assertTrue(flags & os.O_NOFOLLOW)
        self.assertTrue(flags & os.O_RDWR)
        self.assertFalse(flags & (os.O_TRUNC | os.O_CREAT))
        allocate.assert_called_once_with(73, 0, host.STORAGE_BYTES)
        fsync.assert_called_with(73)
        closed.assert_called_once_with(73)
        run.assert_not_called()

    def test_reservation_preserves_host_free_space_floor(self):
        image = Path(self.config["paths"]["root"]) / "storage.ext4"
        metadata = self.backing_metadata(missing_bytes=4 * host.GIB)
        with patch.object(host, "root_only"), patch.object(host, "reject_symlinks"), \
                patch.object(host.os, "open", return_value=73), \
                patch.object(host.os, "close") as closed, \
                patch.object(host.os, "fstat", return_value=metadata), \
                patch.object(host.shutil, "disk_usage", return_value=SimpleNamespace(free=103 * host.GIB)), \
                patch.object(host.os, "posix_fallocate") as allocate:
            with self.assertRaises(host.HostError):
                host.reserve_storage(image, self.config)
        allocate.assert_not_called()
        closed.assert_called_once_with(73)

    def test_reservation_rejects_wrong_owner_mode_links_or_size_before_writes(self):
        image = Path(self.config["paths"]["root"]) / "storage.ext4"
        for changes in ({"st_uid": 1000}, {"st_mode": 0o100644}, {"st_nlink": 2},
                        {"st_size": host.STORAGE_BYTES - 4096}, {"st_mode": 0o060600}):
            with self.subTest(changes=changes), patch.object(host, "root_only"), patch.object(host, "reject_symlinks"), \
                    patch.object(host.os, "open", return_value=73), \
                    patch.object(host.os, "close"), \
                    patch.object(host.os, "fstat", return_value=self.backing_metadata(**changes)), \
                    patch.object(host.os, "posix_fallocate") as allocate:
                with self.assertRaises(host.HostError):
                    host.reserve_storage(image, self.config)
                allocate.assert_not_called()

    def test_storage_guard_refuses_foreign_mount_before_mutating_anything(self):
        with patch.object(host, "root_only"), patch.object(host, "storage_check", return_value=False), \
                patch.object(host.Path, "write_text") as write_text, \
                patch.object(host, "reserve_storage") as reserve:
            with self.assertRaises(host.HostError):
                host.protect_storage(self.config)
        write_text.assert_not_called()
        reserve.assert_not_called()

    def test_reservation_preserves_real_sparse_file_contents(self):
        # Exercise actual allocation on a tiny isolated file. Only its UID is
        # simulated so this regression test never requires administrator access.
        size = 2 * 1024 * 1024
        real_fstat = os.fstat
        config = {**self.config, "policy": {**self.config["policy"], "host_min_free_gib": 0}}
        def file_info(fd):
            info = real_fstat(fd)
            return SimpleNamespace(st_mode=info.st_mode, st_uid=0, st_size=info.st_size,
                                   st_nlink=info.st_nlink, st_blocks=info.st_blocks)
        with TemporaryDirectory() as directory:
            image = Path(directory) / "storage.ext4"
            with image.open("wb") as stream:
                stream.write(b"existing filesystem metadata\x00")
                stream.seek(size - 128)
                stream.write(bytes(range(128)))
            image.chmod(0o600)
            before = hashlib.sha256(image.read_bytes()).digest()
            self.assertLess(image.stat().st_blocks * 512, size)
            with patch.object(host, "root_only"), patch.object(host, "STORAGE_BYTES", size), \
                    patch.object(host.os, "fstat", side_effect=file_info):
                host.reserve_storage(image, config)
            self.assertEqual(hashlib.sha256(image.read_bytes()).digest(), before)
            self.assertEqual(image.stat().st_size, size)
            self.assertGreaterEqual(image.stat().st_blocks * 512, size)

    def test_storage_guard_changes_only_verified_loop_then_repairs_reservation(self):
        image = Path(self.config["paths"]["root"]) / "storage.ext4"
        guard = Path("/sys/block/loop6/queue/discard_max_bytes")
        reads = {Path("/sys/block/loop6/loop/backing_file"): str(image) + "\n", guard: "0\n"}
        with patch.object(host, "root_only"), patch.object(host, "storage_check", return_value=True) as check, \
                patch.object(host, "run", return_value=SimpleNamespace(stdout="/dev/loop6\n")), \
                patch.object(host.Path, "read_text", lambda path, *a, **kw: reads[path]), \
                patch.object(host.Path, "write_text", autospec=True) as write_text, \
                patch.object(host, "reserve_storage") as reserve:
            host.protect_storage(self.config)
        write_text.assert_called_once_with(guard, "0\n")
        reserve.assert_called_once_with(image, self.config)
        self.assertEqual(check.call_args_list[0].kwargs,
                         {"require_allocation": False, "require_guard": False})
        self.assertEqual(check.call_args_list[-1].kwargs, {})

    def test_installer_rejects_foreign_mount_before_chown_repair_or_format(self):
        with patch.object(host, "reject_symlinks"), patch.object(host.Path, "mkdir"), \
                patch.object(host.os.path, "ismount", return_value=True), \
                patch.object(host, "storage_check", return_value=False), \
                patch.object(host.os, "chown") as chown, \
                patch.object(host, "protect_storage") as protect, patch.object(host, "run") as run:
            with self.assertRaises(host.HostError):
                host.ensure_storage(self.config)
        chown.assert_not_called()
        protect.assert_not_called()
        run.assert_not_called()

    def test_firewall_verifier_rejects_foreign_chain(self):
        objects = [{"chain": {"name": name, "hook": "output", "policy": "accept"}}
                   for name in ("output", "vm_egress", "proxy_egress", "unexpected")]
        with patch.object(host, "run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps({"nftables": objects}))):
            self.assertFalse(host.firewall_check(self.config))

    def firewall_json(self):
        """Ubuntu nftables 1.0.9 ``--numeric --json`` output representation.

        The upstream 1.0.9 JSON tests distinguish comma-separated conntrack
        states (an array) from a braced set. ``--numeric`` also renders the
        nfproto symbol as 10 and conntrack established/related as 2 and 4.
        """
        scope = {"family": "inet", "table": host.NFT_TABLE}
        objects = [
            {"metainfo": {"version": "1.0.9", "release_name": "Old Doc Yak #3", "json_schema_version": 1}},
            {"table": {"family": "inet", "name": host.NFT_TABLE, "handle": 120}},
        ]
        for name in ("output", "vm_egress", "proxy_egress"):
            chain = {**scope, "name": name, "handle": 121 + len(objects)}
            if name == "output":
                chain.update({"type": "filter", "hook": "output", "prio": 0, "policy": "accept"})
            objects.append({"chain": chain})

        def match(left, right):
            return {"match": {"op": "==", "left": left, "right": right}}

        def destination(address, protocol, port):
            return [match({"payload": {"protocol": "ip", "field": "daddr"}}, address),
                    match({"payload": {"protocol": protocol, "field": "dport"}}, port),
                    {"counter": {"packets": 57, "bytes": 4321}}, {"accept": None}]

        def add(chain, expressions):
            objects.append({"rule": {**scope, "chain": chain, "expr": expressions,
                                      "handle": 130 + len(objects)}})

        add("output", [match({"meta": {"key": "skuid"}}, 995), {"jump": {"target": "vm_egress"}}])
        add("output", [match({"meta": {"key": "skuid"}}, 994), {"jump": {"target": "proxy_egress"}}])
        for chain in ("vm_egress", "proxy_egress"):
            add(chain, [match({"meta": {"key": "nfproto"}}, 10),
                        {"counter": {"packets": 0, "bytes": 0}}, {"drop": None}])
            if chain == "proxy_egress":
                add(chain, [{"match": {"op": "in", "left": {"ct": {"key": "state"}}, "right": [2, 4]}},
                            {"counter": {"packets": 10, "bytes": 4000}}, {"accept": None}])
            add(chain, destination("127.0.0.1", "tcp", 27892 if chain == "vm_egress" else 27891))
            add(chain, destination("202.38.64.7", "udp", 53))
            add(chain, destination("202.38.64.7", "tcp", 53))
            add(chain, [{"counter": {"packets": 1, "bytes": 52}}, {"drop": None}])
        return {"nftables": objects}

    def check_firewall_json(self, value):
        response = SimpleNamespace(returncode=0, stdout=json.dumps(value))
        with patch.object(host, "run", return_value=response), \
                patch.object(host.pwd, "getpwnam", side_effect=lambda user: SimpleNamespace(pw_uid=995 if user == host.VM_USER else 994)):
            return host.firewall_check(self.config)

    def test_firewall_accepts_ubuntu_numeric_output_with_list_conntrack_states(self):
        self.assertTrue(self.check_firewall_json(self.firewall_json()))

    def test_firewall_accepts_captured_nftables_1_0_9_deployment_rules(self):
        # Captured from the dedicated table only: approved addresses, ports,
        # service UIDs and counters; no credentials or unrelated host rules.
        fixture = Path(__file__).parent / "fixtures/nexpoly_runner_nft_1_0_9.json"
        self.assertTrue(self.check_firewall_json(json.loads(fixture.read_text())))

    def test_firewall_accepts_only_equivalent_symbolic_or_set_notation(self):
        for states in (["established", "related"], [4, 2],
                       {"set": ["related", "established"]}, {"set": [2, 4]}):
            with self.subTest(states=states):
                value = self.firewall_json()
                rules = [item["rule"] for item in value["nftables"] if "rule" in item]
                rules[2]["expr"][0]["match"]["right"] = "ipv6"
                rules[7]["expr"][0]["match"]["right"] = "ipv6"
                rules[8]["expr"][0]["match"]["right"] = states
                self.assertTrue(self.check_firewall_json(value))

    def test_firewall_rejects_unapproved_or_malformed_conntrack_states(self):
        for states in ([2, 4, 8], [2, 4, 64], [2, 2, 4], [2],
                       ["established", "related", "new"], ["established", "related", "untracked"],
                       {"set": [2, 4], "extra": True}, {"set": [2, 4, 8]},
                       6, "established", None, [True, 4], [[2], 4]):
            with self.subTest(states=states):
                value = self.firewall_json()
                rules = [item["rule"] for item in value["nftables"] if "rule" in item]
                rules[8]["expr"][0]["match"]["right"] = states
                self.assertFalse(self.check_firewall_json(value))

    def test_firewall_rejects_changed_packet_matches(self):
        original = self.firewall_json()
        rules = [item["rule"] for item in original["nftables"] if "rule" in item]
        mutations = (
            (0, 0, "right", 0),  # Matching root would exempt the VM account.
            (2, 0, "right", 2),  # IPv4 cannot replace the IPv6 deny rule.
            (3, 0, "right", "169.254.169.254"),
            (3, 1, "right", 27891),  # VM must not reach the original proxy.
            (4, 0, "right", "127.0.0.53"),
            (3, 1, "op", "!="),
            (8, 0, "right", [2, 4, 8]),  # "new" is not an established reply.
            (8, 0, "op", "=="),
        )
        for rule_index, expression_index, field, value in mutations:
            with self.subTest(rule=rule_index, field=field, value=value):
                changed = copy.deepcopy(original)
                changed_rules = [item["rule"] for item in changed["nftables"] if "rule" in item]
                changed_rules[rule_index]["expr"][expression_index]["match"][field] = value
                self.assertFalse(self.check_firewall_json(changed))
        self.assertEqual(len(rules), 13)

    def test_firewall_rejects_extra_or_missing_rule_statements(self):
        for mutation in ("early_accept", "missing_drop", "counter_with_verdict", "extra_match"):
            with self.subTest(mutation=mutation):
                value = self.firewall_json()
                rules = [item["rule"] for item in value["nftables"] if "rule" in item]
                if mutation == "early_accept":
                    rules[2]["expr"].insert(0, {"accept": None})
                elif mutation == "missing_drop":
                    rules[6]["expr"].pop()
                elif mutation == "counter_with_verdict":
                    rules[2]["expr"][1]["accept"] = None
                else:
                    rules[3]["expr"].insert(0, {"match": {"op": "==", "left": {"meta": {"key": "l4proto"}}, "right": 17}})
                self.assertFalse(self.check_firewall_json(value))

    def test_firewall_rejects_unapproved_table_or_chain_scope(self):
        for object_kind, field, replacement in (("table", "family", "ip"),
                                                ("table", "name", "foreign"),
                                                ("chain", "table", "foreign"),
                                                ("chain", "family", "ip"),
                                                ("rule", "table", "foreign"),
                                                ("rule", "family", "ip")):
            with self.subTest(kind=object_kind, field=field):
                value = self.firewall_json()
                next(item[object_kind] for item in value["nftables"] if object_kind in item)[field] = replacement
                self.assertFalse(self.check_firewall_json(value))

    def test_firewall_rejects_unapproved_objects_or_duplicate_chains(self):
        for extra in ({"chain": {"family": "inet", "table": host.NFT_TABLE, "name": "vm_egress"}},
                      {"flowtable": {"family": "inet", "table": host.NFT_TABLE, "name": "bypass"}},
                      {"set": {"family": "inet", "table": host.NFT_TABLE, "name": "bypass"}}):
            with self.subTest(extra=extra):
                value = self.firewall_json()
                value["nftables"].append(extra)
                self.assertFalse(self.check_firewall_json(value))

    def test_firewall_rejects_inactive_or_duplicate_table_records(self):
        for flags in (["dormant"], ["owner"], "dormant", None):
            with self.subTest(flags=flags):
                value = self.firewall_json()
                next(item["table"] for item in value["nftables"] if "table" in item)["flags"] = flags
                self.assertFalse(self.check_firewall_json(value))
        value = self.firewall_json()
        table = next(item for item in value["nftables"] if "table" in item)
        value["nftables"].append(copy.deepcopy(table))
        self.assertFalse(self.check_firewall_json(value))

    def test_firewall_rejects_unapproved_chain_hooking_or_flags(self):
        mutations = (("output", "hook", "input"), ("output", "type", "route"),
                     ("output", "prio", 1), ("output", "policy", "drop"),
                     ("output", "flags", ["binding"]),
                     ("vm_egress", "hook", "output"), ("vm_egress", "type", "filter"),
                     ("vm_egress", "prio", 0), ("vm_egress", "policy", "accept"),
                     ("vm_egress", "dev", "lo"), ("vm_egress", "devices", ["lo"]),
                     ("proxy_egress", "hook", "output"), ("proxy_egress", "flags", ["binding"]))
        for name, field, replacement in mutations:
            with self.subTest(chain=name, field=field):
                value = self.firewall_json()
                chain = next(item["chain"] for item in value["nftables"]
                             if "chain" in item and item["chain"]["name"] == name)
                chain[field] = replacement
                self.assertFalse(self.check_firewall_json(value))

    def test_firewall_rejects_malformed_json_records_without_raising(self):
        malformed = (None, [], {}, {"nftables": None}, {"nftables": {}},
                     {"nftables": [None]}, {"nftables": [{"chain": None}]},
                     {"nftables": [{"table": []}]})
        for value in malformed:
            with self.subTest(value=value):
                self.assertFalse(self.check_firewall_json(value))
        for mutation in ("chain_name", "rule_chain", "rule_expr", "expression", "counter"):
            with self.subTest(mutation=mutation):
                value = self.firewall_json()
                if mutation == "chain_name":
                    next(item["chain"] for item in value["nftables"] if "chain" in item)["name"] = []
                else:
                    rule = next(item["rule"] for item in value["nftables"] if "rule" in item)
                    if mutation == "rule_chain":
                        rule["chain"] = []
                    elif mutation == "rule_expr":
                        rule["expr"] = None
                    elif mutation == "expression":
                        rule["expr"] = [None]
                    else:
                        rule["expr"].insert(0, {"counter": {"packets": -1, "bytes": 0}})
                self.assertFalse(self.check_firewall_json(value))

    def test_domain_list_has_no_overlapping_parent_entries(self):
        domains = [line.strip() for line in (host.PACKAGE / "proxy/domains.txt").read_text().splitlines()
                   if line.strip() and not line.startswith("#")]
        self.assertEqual(len(domains), len(set(domains)))
        for domain in domains:
            self.assertNotIn(domain, {".", "*"})
            for parent in domains:
                if parent.startswith(".") and parent != domain:
                    self.assertFalse(domain.endswith(parent))


if __name__ == "__main__":
    unittest.main()
