"""Nonprivileged fixtures only: never reads a host EnvironmentFile."""
from __future__ import annotations
from contextlib import contextmanager
from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import py_compile
import socket
import stat
import struct
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from gpu_resource import systemd_inventory as p
from ops.gpu_broker import server as native
from ops.gpu_broker import systemd_provider as root


def unit(**changes):
    value = native._SystemdUnitAuthority("fixture.service", "active", "running", "a" * 32,
        "0", "", "root", "", (), "", "")
    return replace(value, **changes)


def projection(**changes):
    value = p.UnitProjection("fixture.service", "active", "running", "a" * 32,
                           "0", "", "root", True, True)
    return replace(value, **changes)


class ConfigurationTests(unittest.TestCase):
    def capture(self, authority, *, manager=None, reader=None):
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, manager or "", ""))
        with patch.object(native, "_query_systemd_unit_authorities", return_value=(authority,)):
            return root.capture_configuration(p, native, time.monotonic() + 5, run=runner, read_file=reader)

    def test_no_secret_value_path_or_hash_in_projection(self):
        sentinel = "SYNTHETIC_NOT_A_REAL_PASSWORD"
        authority = unit(environment='CUDA_VISIBLE_DEVICES=1 UNRELATED_SENTINEL=' + sentinel,
                         environment_files=("/synthetic-only.env (ignore_errors=no)",))
        snapshot = self.capture(authority, reader=lambda *_: ({"UNRELATED_SENTINEL": sentinel}, ("private-file", sentinel, hashlib.sha256(sentinel.encode()).digest()), len(sentinel)))
        public = json.dumps([asdict(item) for item in snapshot.units])
        for forbidden in (sentinel, "synthetic-only.env", hashlib.sha256(sentinel.encode()).hexdigest(), "private-file"):
            self.assertNotIn(forbidden, public)
        self.assertTrue(snapshot.units[0].static_gpu1)
        self.assertIn(sentinel, snapshot.authorities[0].environment)

    def test_statically_relevant_none_and_other_gpu_preserved(self):
        for visibility in ("none", "3"):
            with self.subTest(visibility=visibility):
                snap = self.capture(unit(environment="CUDA_VISIBLE_DEVICES=" + visibility))
                self.assertFalse(snap.units[0].static_gpu1)
                self.assertTrue(snap.units[0].statically_relevant)
        snap = self.capture(unit(pass_environment="CUDA_VISIBLE_DEVICES"), manager="UNRELATED_SENTINEL=fixture\n")
        self.assertTrue(snap.units[0].statically_relevant)
        self.assertFalse(snap.units[0].static_gpu1)

    def test_unset_and_file_precedence_equal_legacy_semantics(self):
        authority = unit(environment="CUDA_VISIBLE_DEVICES=none", pass_environment="CUDA_VISIBLE_DEVICES",
                         environment_files=("/fixture.env (ignore_errors=no)",), unset_environment="CUDA_VISIBLE_DEVICES=1")
        snap = self.capture(authority, manager="CUDA_VISIBLE_DEVICES=1\n",
                            reader=lambda *_: ({"CUDA_VISIBLE_DEVICES": "1"}, ("fixture",), 23))
        self.assertFalse(snap.units[0].static_gpu1)
        self.assertTrue(snap.units[0].statically_relevant)  # PassEnvironment is authority even when unset.

    def test_full_authority_changes_even_with_same_projection(self):
        original = unit(environment="CUDA_VISIBLE_DEVICES=none UNRELATED_SENTINEL=first")
        first = self.capture(original)
        second = self.capture(replace(original, environment="CUDA_VISIBLE_DEVICES=none UNRELATED_SENTINEL=second"))
        self.assertEqual(first.units, second.units)
        self.assertNotEqual(first, second)
        a = self.capture(unit(pass_environment="CUDA_VISIBLE_DEVICES"), manager="CUDA_VISIBLE_DEVICES=none\nUNRELATED_SENTINEL=first\n")
        b = self.capture(unit(pass_environment="CUDA_VISIBLE_DEVICES"), manager="CUDA_VISIBLE_DEVICES=none\nUNRELATED_SENTINEL=second\n")
        self.assertEqual(a.units, b.units)
        self.assertNotEqual(a, b)
        a = self.capture(unit(environment_files=("/fixture.env (ignore_errors=yes)",)), reader=lambda *_: ({}, ("missing",), 0))
        b = self.capture(unit(environment_files=("/fixture.env (ignore_errors=yes)",)), reader=lambda *_: ({}, ("created",), 0))
        self.assertEqual(a.units, b.units)
        self.assertNotEqual(a, b)

    def test_transitional_files_fail_before_read(self):
        for state in ("activating", "reloading", "deactivating"):
            reader = Mock(side_effect=AssertionError("must not read transitional files"))
            with self.subTest(state=state), self.assertRaises(p.InventoryError):
                self.capture(unit(active_state=state, environment_files=("/fixture.env (ignore_errors=no)",)), reader=reader)
            reader.assert_not_called()

    def test_mixed_gpu1_declaration_and_invalid_token_fail(self):
        for value in ("all", "1,3", "unrecognized", "/caller/path"):
            with self.subTest(value=value), self.assertRaises((p.InventoryError, ValueError)):
                self.capture(unit(environment="CUDA_VISIBLE_DEVICES=" + value))

    def test_file_count_and_aggregate_bytes_limits(self):
        reader = Mock(return_value=({}, ("synthetic",), p.MAX_FILE_BYTES))
        with patch.object(p, "MAX_FILES", 1), self.assertRaises(p.InventoryError):
            self.capture(unit(environment_files=("/one (ignore_errors=no)", "/two (ignore_errors=no)")), reader=reader)
        self.assertEqual(reader.call_count, 1)
        with patch.object(p, "MAX_TOTAL_FILE_BYTES", 1), self.assertRaises(p.InventoryError):
            self.capture(unit(environment_files=("/one (ignore_errors=no)",)), reader=reader)
        reader = Mock(return_value=({}, ("synthetic",), 10))
        self.capture(unit(environment_files=("/one (ignore_errors=no)", "/two (ignore_errors=no)")), reader=reader)
        self.assertEqual(reader.call_args_list[0].args[2], p.MAX_TOTAL_FILE_BYTES)
        self.assertEqual(reader.call_args_list[1].args[2], p.MAX_TOTAL_FILE_BYTES - 10)

    def test_unit_limit_and_out_of_policy_identity(self):
        with patch.object(p, "MAX_UNITS", 0), self.assertRaises(p.InventoryError):
            self.capture(unit())
        with patch.dict(native.EXPECTED_GPU_UUIDS, {1: "different"}), self.assertRaises(p.InventoryError):
            self.capture(unit())


class FileBoundaryTests(unittest.TestCase):
    def test_nonroot_parent_rejected_before_read(self):
        fake = SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=1001)
        with patch.object(Path, "lstat", return_value=fake), patch.object(p.os, "open") as opened:
            with self.assertRaises(p.InventoryError):
                p.trusted_directory(Path("/synthetic/parent"))
        opened.assert_not_called()

    def test_writable_parent_and_parent_symlink_rejected(self):
        for mode in (stat.S_IFDIR | 0o777, stat.S_IFLNK | 0o777):
            with self.subTest(mode=mode), patch.object(Path, "lstat", return_value=SimpleNamespace(st_mode=mode, st_uid=0)):
                with self.assertRaises(p.InventoryError):
                    p.trusted_directory(Path("/synthetic/parent"))

    def test_fifo_symlink_hardlink_and_writable_file_rejected_before_open(self):
        for mode, links in ((stat.S_IFIFO | 0o600, 1), (stat.S_IFLNK | 0o777, 1),
                            (stat.S_IFREG | 0o600, 2), (stat.S_IFREG | 0o622, 1)):
            fake = SimpleNamespace(st_mode=mode, st_uid=0, st_nlink=links, st_size=1)
            with self.subTest(mode=mode, links=links), patch.object(p, "trusted_directory", return_value=()), \
                    patch.object(Path, "lstat", return_value=fake), patch.object(p.os, "open") as opened:
                with self.assertRaises(p.InventoryError):
                    p.trusted_bytes(Path("/synthetic/file"), 1024)
                opened.assert_not_called()

    def test_noncanonical_paths_rejected(self):
        for path in ("relative", "/a/../secret", "//secret"):
            with self.subTest(path=path), patch.object(p, "trusted_directory") as parents:
                with self.assertRaises(p.InventoryError):
                    root.file_configuration(Path(path), False, p)
                parents.assert_not_called()

    def test_fixture_content_parent_target_and_link_cas(self):
        # Explicit fixture-only Root ownership mocks; no real Root file read.
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            file = base / "fixture.env"
            file.write_text("CUDA_VISIBLE_DEVICES=none\nUNRELATED_SENTINEL=first\n")
            def parents(path):
                self.assertTrue(path.is_relative_to(base))
                return ((str(path), p.metadata_identity(path.lstat())),)
            def fixture_bytes(path, maximum):
                self.assertTrue(path.is_relative_to(base))
                raw = path.read_bytes()
                return raw, (p.metadata_identity(path.lstat()), parents(path.parent))
            with patch.object(p, "trusted_directory", side_effect=parents), patch.object(p, "trusted_bytes", side_effect=fixture_bytes):
                first = root.file_configuration(file, False, p)
                file.write_text("CUDA_VISIBLE_DEVICES=none\nUNRELATED_SENTINEL=second\n")
                second = root.file_configuration(file, False, p)
                self.assertEqual(first[0]["CUDA_VISIBLE_DEVICES"], second[0]["CUDA_VISIBLE_DEVICES"])
                self.assertNotEqual(first[1], second[1])
                missing = base / "missing.env"
                before = root.file_configuration(missing, True, p)
                missing.write_text("CUDA_VISIBLE_DEVICES=none\n")
                self.assertNotEqual(before[1], root.file_configuration(missing, True, p)[1])
                link = base / "compat.env"
                link.symlink_to(file.name)
                # UID1001's symlink is not compatibility authority.
                with self.assertRaises(p.InventoryError):
                    root.file_configuration(link, False, p)
                real_lstat = Path.lstat
                def synthetic_root_link(path):
                    result = real_lstat(path)
                    if path != link:
                        return result
                    keys = ("st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_size", "st_mtime_ns", "st_ctime_ns", "st_nlink")
                    return SimpleNamespace(**{key: 0 if key == "st_uid" else getattr(result, key) for key in keys})
                # Root ownership exists only in this fixture metadata mock.
                with patch.object(Path, "lstat", autospec=True, side_effect=synthetic_root_link):
                    original_link = root.file_configuration(link, False, p)
                    other = base / "other.env"
                    other.write_text(file.read_text())
                    link.unlink()
                    link.symlink_to(other.name)
                    changed_link = root.file_configuration(link, False, p)
                self.assertEqual(original_link[0], changed_link[0])
                self.assertNotEqual(original_link[1], changed_link[1])

    def test_open_nonblock_and_post_open_identity_before_pread(self):
        fake = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=0, st_nlink=1, st_size=1)
        with patch.object(p, "trusted_directory", return_value=()), patch.object(Path, "lstat", return_value=fake), \
                patch.object(p, "metadata_identity", side_effect=["original", "changed"]), \
                patch.object(p.os, "open", return_value=123) as opened, patch.object(p.os, "fstat", return_value=fake), \
                patch.object(p.os, "pread") as read, patch.object(p.os, "close"):
            with self.assertRaises(p.InventoryError):
                p.trusted_bytes(Path("/synthetic/file"), 100)
            self.assertTrue(opened.call_args.args[1] & os.O_NONBLOCK)
            read.assert_not_called()


class ProtocolTests(unittest.TestCase):
    def test_reject_unknown_paths_commands_gpu_scope_types_and_duplicate_keys(self):
        base = {"protocol": 1, "operation": "capture", "target_gpu": 1, "nonce": "a" * 32}
        for change in ({"path": "/root/secret"}, {"command": "id"}, {"target_gpu": 2},
                       {"target_gpu": True}, {"protocol": True}, {"nonce": "stale"}):
            with self.subTest(change=change), self.assertRaises(p.InventoryError):
                p.request(base | change, "capture", "a" * 32)
        for raw in (b'{"protocol":1,"protocol":1}', b'{"value":NaN}', b'[]', b'{'):
            with self.subTest(raw=raw), self.assertRaises(p.InventoryError):
                p.strict_json(raw, p.MAX_REQUEST)

    def test_projection_schema_no_secret_or_duplicate_units(self):
        valid = asdict(projection())
        for change in ({"Environment": "SYNTHETIC_SENTINEL"}, {"static_gpu1": 1},
                       {"statically_relevant": False}, {"unit": "-bad.service"}, {"main_pid": True}):
            with self.subTest(change=change), self.assertRaises(p.InventoryError):
                p.UnitProjection.parse(valid | change)

    def pair(self):
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        for item in (left, right):
            item.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
            self.addCleanup(item.close)
        return left, right

    def test_seqpacket_rejects_truncation_and_wrong_actual_sender(self):
        left, right = self.pair()
        left.send(b"x" * (p.MAX_REQUEST + 100))
        with self.assertRaises(p.InventoryError):
            p.receive(right, p.MAX_REQUEST, time.monotonic() + 1)
        left.send(b"{}")
        with self.assertRaises(p.InventoryError):
            p.receive(right, p.MAX_REQUEST, time.monotonic() + 1, sender=(os.getpid() + 1, os.geteuid(), os.getegid()))

    def test_received_rights_closed_on_rejection(self):
        left, right = self.pair()
        read_fd, write_fd = os.pipe()
        try:
            left.sendmsg([b"{}"], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, struct.pack("i", read_fd))])
            real_close = os.close
            closed = []
            def close(fd):
                closed.append(fd)
                real_close(fd)
            with patch.object(p.os, "close", side_effect=close), self.assertRaises(p.InventoryError):
                p.receive(right, p.MAX_REQUEST, time.monotonic() + 1)
            self.assertTrue(closed)
            for fd in closed:
                with self.assertRaises(OSError):
                    os.fstat(fd)
        finally:
            os.close(read_fd)
            os.close(write_fd)

    def session(self, *, snapshots=None, identity=None):
        client, server = self.pair()
        snapshot = root.ConfigurationSnapshot((), None, (), (projection(),))
        capture = Mock(side_effect=snapshots) if snapshots else Mock(return_value=snapshot)
        session = root.ConnectionSession(server, p, native, identity=identity or (lambda _: (11, "/synthetic")),
            capture=capture, verify=lambda: ("public_fixture",), boot_package=("public_fixture",),
            boot_runtime=({"pid": os.getpid(), "start_ticks": 11, "executable_device": 1, "executable_inode": 2}, ()),
            runtime=lambda: ({"pid": os.getpid(), "start_ticks": 11, "executable_device": 1, "executable_inode": 2}, ()))
        thread = threading.Thread(target=session.run, daemon=True)
        thread.start()
        self.addCleanup(lambda: thread.join(2))
        return client, session, capture, thread

    def round(self, client, value):
        end = time.monotonic() + 2
        p.send(client, value, p.MAX_REQUEST, end)
        return p.receive(client, p.MAX_RESPONSE, end, sender=(os.getpid(), os.geteuid(), os.getegid()))

    def test_single_connection_capture_validate_consumed_and_redacted(self):
        client, session, capture, thread = self.session()
        base = {"protocol": 1, "operation": "capture", "target_gpu": 1, "nonce": "b" * 32}
        first = self.round(client, base)
        self.assertTrue(first["ok"])
        second = self.round(client, base | {"operation": "validate", "capture_id": first["capture_id"]})
        self.assertTrue(second["ok"])
        thread.join(2)
        self.assertIsNone(session.snapshot)
        self.assertEqual(capture.call_count, 3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(client.recv(1), b"")

    def test_changed_full_private_snapshot_blocks_unchanged_projection(self):
        first = root.ConfigurationSnapshot(("private-first",), None, (), (projection(),))
        changed = replace(first, authorities=("private-changed",))
        client, session, _capture, thread = self.session(snapshots=[first, first, changed])
        base = {"protocol": 1, "operation": "capture", "target_gpu": 1, "nonce": "b" * 32}
        one = self.round(client, base)
        two = self.round(client, base | {"operation": "validate", "capture_id": one["capture_id"]})
        self.assertFalse(two["ok"])
        self.assertEqual(two["error"], "identity_changed")
        self.assertNotIn("private-", json.dumps(two))
        thread.join(2)
        self.assertIsNone(session.snapshot)

    def test_handle_nonce_wrong_target_and_connection_replays_fail(self):
        for change in ({"capture_id": "c" * 32}, {"nonce": "d" * 32}, {"target_gpu": 2}):
            with self.subTest(change=change):
                client, session, _capture, thread = self.session()
                base = {"protocol": 1, "operation": "capture", "target_gpu": 1, "nonce": "b" * 32}
                first = self.round(client, base)
                response = self.round(client, base | {"operation": "validate", "capture_id": first["capture_id"]} | change)
                self.assertFalse(response["ok"])
                thread.join(2)
                self.assertIsNone(session.snapshot)
        client, session, _capture, thread = self.session()
        response = self.round(client, {"protocol": 1, "operation": "validate", "target_gpu": 1,
                                      "nonce": "b" * 32, "capture_id": "c" * 32})
        self.assertFalse(response["ok"])
        thread.join(2)
        self.assertIsNone(session.snapshot)

    def test_caller_pid_start_cgroup_change_and_disconnect_destroy_state(self):
        calls = 0
        def identity(_):
            nonlocal calls
            calls += 1
            return (11, "/synthetic") if calls < 5 else (12, "/changed")
        client, session, _capture, thread = self.session(identity=identity)
        base = {"protocol": 1, "operation": "capture", "target_gpu": 1, "nonce": "b" * 32}
        one = self.round(client, base)
        self.assertTrue(one["ok"])
        two = self.round(client, base | {"operation": "validate", "capture_id": one["capture_id"]})
        self.assertFalse(two["ok"])
        thread.join(2)
        self.assertIsNone(session.snapshot)
        client, session, _capture, thread = self.session()
        self.assertTrue(self.round(client, base)["ok"])
        client.close()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertIsNone(session.snapshot)

    def test_wrong_uid_and_deadline_fail_without_privileged_reads(self):
        left, right = self.pair()
        capture = Mock(side_effect=AssertionError("must not capture"))
        with patch.object(p, "peer_credentials", return_value=(os.getpid(), 1002, 1002)):
            root.ConnectionSession(right, p, native, capture=capture, boot_package=(), boot_runtime=()).run()
        capture.assert_not_called()
        with self.assertRaises(p.InventoryError):
            p.remaining(time.monotonic() - 1)

    def test_running_source_a_cannot_accept_current_disk_source_b(self):
        connection = Mock()
        capture = Mock(side_effect=AssertionError("old running code must not capture using a new disk package"))
        with patch.object(p, "peer_credentials", return_value=(os.getpid(), 1001, 1001)), \
                patch.object(p, "send") as send, patch.object(p, "receive") as receive:
            session = root.ConnectionSession(connection, p, native, identity=lambda _: (11, "/synthetic"),
                capture=capture, verify=lambda: ("source-B",), boot_package=("source-A",),
                boot_runtime=("fixed-interpreter",), runtime=lambda: ("fixed-interpreter",))
            session.run()
        capture.assert_not_called()
        receive.assert_not_called()
        self.assertIsNone(session.snapshot)
        self.assertEqual(send.call_args.args[1]["error"], "identity_changed")
        connection.close.assert_called_once()


class NativeSeamTests(unittest.TestCase):
    def test_projection_validate_after_final_live_process_cas(self):
        events = []
        projected = projection(main_pid="101", control_group="/system.slice/fixture.service", user="devuser",
                               static_gpu1=False, statically_relevant=True)
        @contextmanager
        def factory(**_kwargs):
            events.append("capture")
            yield SimpleNamespace(units=(projected,), validate=lambda: events.append("validate"))
            events.append("close")
        def environment(_pid):
            events.append("environment")
            return {"CUDA_VISIBLE_DEVICES": "1"}
        def start_ticks(_pid):
            events.append("identity")
            return 11
        with patch.object(native, "_query_systemd_unit_authorities", return_value=()) as query, \
                patch.object(native, "_capture_systemd_environment_file", side_effect=AssertionError("no local privileged file read")):
            claims = native.query_systemd_gpu_claims(compute_processes={}, system_configuration_factory=factory,
                read_control_group_processes=lambda _group: frozenset({101}), read_process_environment=environment,
                read_process_cgroup=lambda _pid: projected.control_group,
                read_process_start_ticks=start_ticks, read_process_uids=lambda _pid: (1001,) * 4)
        self.assertEqual([call.args[0] for call in query.call_args_list], ["user", "user"])
        self.assertEqual(len(claims), 1)
        self.assertIn(p.TARGET_UUID, claims[0].gpu_uuids)
        self.assertFalse(claims[0].static_gpu_uuids)
        self.assertEqual(events.count("environment"), 1)
        self.assertGreater(events.count("identity"), 2)
        self.assertEqual(events[-3], "identity")
        self.assertEqual(events[-2:], ["validate", "close"])

    def test_invalid_root_validation_cannot_return_claim(self):
        @contextmanager
        def factory(**_kwargs):
            yield SimpleNamespace(units=(), validate=Mock(side_effect=native.BrokerError("gpu_claim_inventory_unavailable", "fixture changed")))
        with patch.object(native, "_query_systemd_unit_authorities", return_value=()), self.assertRaises(native.BrokerError):
            native.query_systemd_gpu_claims(compute_processes={}, system_configuration_factory=factory,
                                           read_control_group_processes=lambda _: frozenset())

    def test_ambient_provider_environment_cannot_activate_root_client(self):
        with patch.dict(os.environ, {"NEXPOLY_GPU_SYSTEMD_INVENTORY_MODE": "root-unix-v1"}), \
                patch.object(native, "_query_systemd_unit_authorities", return_value=()) as query, \
                patch.object(p, "configuration_client", side_effect=AssertionError("must not use ambient switch")):
            self.assertEqual(native.query_systemd_gpu_claims(compute_processes={}, read_control_group_processes=lambda _: frozenset()), ())
        self.assertEqual([call.args[0] for call in query.call_args_list], ["user", "system", "user", "system"])


class ServiceTrustTests(unittest.TestCase):
    def test_bootstrap_import_source_and_full_filesystem_cas(self):
        manifest = {"source_sha": "a" * 40, "source_tree": "b" * 40}
        initial = (manifest, ("initial-fs",))
        with patch.object(p, "verify_package", return_value=initial), patch.object(root, "_import_native", return_value="native-fixture") as imported:
            self.assertEqual(root._load_verified_native(p, *initial), ("native-fixture", initial))
            imported.assert_called_once()
        with patch.object(p, "verify_package", return_value=(manifest, ("replaced-fs",))), \
                patch.object(root, "_import_native") as imported, self.assertRaises(RuntimeError):
            root._load_verified_native(p, *initial)
        imported.assert_not_called()
        for after in ((manifest, ("replaced-fs",)), ({"source_sha": "c" * 40, "source_tree": "d" * 40}, initial[1])):
            with self.subTest(after=after), patch.object(p, "verify_package", side_effect=[initial, after]), \
                    patch.object(root, "_import_native", return_value="native-fixture"), self.assertRaises(RuntimeError):
                root._load_verified_native(p, *initial)

    def test_bootstrap_rejects_alternate_cache_prefix_before_filesystem_access(self):
        flags = SimpleNamespace(isolated=True, no_site=True, dont_write_bytecode=True)
        with patch.object(root.os, "geteuid", return_value=0), patch.object(root.os, "getegid", return_value=1001), \
                patch.object(root.sys, "flags", flags), patch.object(root.sys, "pycache_prefix", "/synthetic-cache"), \
                patch.object(root, "__file__", str(p.ENTRY)), patch.object(root.sys, "argv", [str(p.ENTRY), "serve"]), \
                patch.object(Path, "lstat") as read, self.assertRaises(RuntimeError):
            root._bootstrap()
        read.assert_not_called()

    def test_closed_source_layout_rejects_matching_stale_bytecode_and_extra_modules(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            hashes = {}
            for relative in p.PACKAGE_FILES:
                path = base / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture_value = 2\n")
                hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
            manifest = {"schema_version": 1, "source_sha": "a" * 40, "source_tree": "b" * 40, "files": hashes}
            (base / "manifest.json").write_text(json.dumps(manifest))
            def fixture_bytes(path, maximum):
                self.assertTrue(path.is_relative_to(base))
                raw = path.read_bytes()
                self.assertLessEqual(len(raw), maximum)
                return raw, ("synthetic_root_metadata", str(path))
            with patch.object(p, "INSTALL_ROOT", base), patch.object(p, "trusted_directory", return_value=()), \
                    patch.object(p, "trusted_bytes", side_effect=fixture_bytes):
                self.assertEqual(p.verify_package()[0], manifest)
                source = base / "ops/gpu_broker/server.py"
                before = source.stat()
                source.write_text("fixture_value = 1\n")
                os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
                py_compile.compile(str(source), doraise=True)
                source.write_text("fixture_value = 2\n")
                os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
                # Old cache matches source timestamp/size but must not load.
                with self.assertRaises(p.InventoryError):
                    p.verify_package()
                cache = source.parent / "__pycache__"
                for file in cache.iterdir():
                    file.unlink()
                cache.rmdir()
                self.assertEqual(p.verify_package()[0], manifest)
                for relative in ("json.py", "ops/gpu_broker/server.pyc", "ops/gpu_broker/server.so"):
                    extra = base / relative
                    extra.write_bytes(b"synthetic_non_whitelisted")
                    with self.subTest(relative=relative), self.assertRaises(p.InventoryError):
                        p.verify_package()
                    extra.unlink()

    def test_root_runtime_executable_and_interpreter_update_proof(self):
        authority = ((1, 2), ("public-interpreter",))
        with patch.object(p, "process_identity", return_value=(11, p.SERVICE_CGROUP)) as identity, \
                patch.object(p, "actual_argv", return_value=p.EXEC_ARGV) as argv, \
                patch.object(p, "interpreter_authority", return_value=authority) as interpreter, \
                patch.object(Path, "stat", return_value=SimpleNamespace(st_dev=1, st_ino=2)) as exe:
            proof = p.provider_runtime()
            self.assertEqual(proof[0], {"pid": os.getpid(), "start_ticks": 11, "executable_device": 1, "executable_inode": 2})
            exe.return_value = SimpleNamespace(st_dev=1, st_ino=3)
            with self.assertRaises(p.InventoryError):
                p.provider_runtime()
            exe.return_value = SimpleNamespace(st_dev=1, st_ino=2)
            interpreter.side_effect = [authority, ((1, 3), ("changed-interpreter",))]
            with self.assertRaises(p.InventoryError):
                p.provider_runtime()
            interpreter.side_effect = None
            argv.return_value = ("/usr/bin/python3", "unreviewed.py")
            with self.assertRaises(p.InventoryError):
                p.provider_runtime()
            argv.return_value = p.EXEC_ARGV
            identity.return_value = (11, "/different.service")
            with self.assertRaises(p.InventoryError):
                p.provider_runtime()

    def client(self):
        return p.SystemdConfigurationClient(expected_source_sha="a" * 40, expected_source_tree="b" * 40)

    def test_client_source_pin_rejected_before_socket_access(self):
        client = self.client()
        manifest = {"source_sha": "c" * 40, "source_tree": "d" * 40}
        with patch.object(p.os, "geteuid", return_value=1001), patch.object(p.os, "getegid", return_value=1001), \
                patch.object(p, "process_identity", return_value=(11, "/synthetic")), \
                patch.object(p, "verify_package", return_value=(manifest, ())), \
                patch.object(p, "trusted_directory", side_effect=AssertionError("no socket access after source mismatch")) as paths:
            with self.assertRaises(p.InventoryError):
                client.__enter__()
        self.assertIsNone(client.connection)
        paths.assert_not_called()

    def test_client_response_nonce_sender_pid_and_schema_bound(self):
        client = self.client()
        client.caller = (101, 1001, 1001)
        client.peer = (201, 0, 1001)
        client.connection = Mock()
        base = {"protocol": 1, "operation": "capture", "target_gpu": 1,
                "nonce": client.nonce, "caller_pid": 101, "ok": True}
        client.provider = ((22, p.SERVICE_CGROUP), "a" * 32, "fixed-exec", ((1, 2), ()))
        runtime = {"pid": 201, "start_ticks": 22, "executable_device": 1, "executable_inode": 2}
        with patch.object(p, "receive", return_value=base | {"provider": runtime}):
            self.assertTrue(client._response("capture")["ok"])
        for change in ({"pid": 202}, {"start_ticks": 23}, {"executable_device": True},
                       {"executable_inode": 3}, {"extra": 1}):
            with self.subTest(runtime_change=change), patch.object(p, "receive", return_value=base | {"provider": runtime | change}), self.assertRaises(p.InventoryError):
                client._response("capture")
        for change in ({"nonce": "d" * 32}, {"caller_pid": 102}, {"caller_pid": True},
                       {"target_gpu": 2}, {"target_gpu": True}, {"ok": 1}, {"operation": "validate"}):
            with self.subTest(change=change), patch.object(p, "receive", return_value=base | change), self.assertRaises(p.InventoryError):
                client._response("capture")
        value = base | {"ok": False, "error": "unavailable", "Environment": "SYNTHETIC_SENTINEL"}
        with patch.object(p, "receive", return_value=value), self.assertRaises(p.InventoryError):
            client._response("capture")

    def test_client_validate_consumes_on_success_and_failure(self):
        for fail in (False, True):
            client = self.client()
            client.connection = Mock()
            client.capture_id = "c" * 32
            response = {"protocol": 1, "operation": "validate", "target_gpu": 1,
                        "nonce": client.nonce, "caller_pid": os.getpid(), "ok": True, "capture_id": client.capture_id, "provider": {}}
            with patch.object(client, "_check"), patch.object(p, "send"), \
                    patch.object(client, "_response", side_effect=p.InventoryError() if fail else None, return_value=response):
                if fail:
                    with self.assertRaises(p.InventoryError):
                        client.validate()
                else:
                    client.validate()
                self.assertTrue(client.consumed)
                with self.assertRaises(p.InventoryError):
                    client.validate()

    def test_client_changed_service_or_package_rejected(self):
        client = self.client()
        client.caller = (os.getpid(), 1001, 1001)
        client.caller_identity = (11, "/synthetic")
        client.connection = Mock()
        client.peer = (201, 0, 1001)
        client.provider = ((22, p.SERVICE_CGROUP), "a" * 32, "fixed-exec", ((1, 2), ()))
        client.package = ({"public": "source"}, ())
        client.socket_identity, client.socket_parents = "socket", ()
        with patch.object(p, "process_identity", return_value=client.caller_identity), \
                patch.object(p, "peer_credentials", return_value=client.peer), \
                patch.object(p, "_provider_identity", return_value=client.provider) as provider, \
                patch.object(Path, "lstat", return_value=object()), patch.object(p, "metadata_identity", return_value="socket"), \
                patch.object(p, "trusted_directory", return_value=()), patch.object(p, "verify_package", return_value=client.package) as package:
            client._check()
            provider.return_value = (client.provider[0], "b" * 32, "fixed-exec", client.provider[3])
            with self.assertRaises(p.InventoryError):
                client._check()
            provider.return_value = client.provider
            package.return_value = ({"public": "changed"}, ())
            with self.assertRaises(p.InventoryError):
                client._check()

    def test_root_entry_rejects_uid1001_before_package_or_manager_read(self):
        with patch.object(root.os, "geteuid", return_value=1001), patch.object(Path, "lstat") as read:
            with self.assertRaises(RuntimeError):
                root._bootstrap()
            read.assert_not_called()

    def test_client_rejects_nonroot_peer_and_wrong_cgroup_before_systemctl(self):
        with patch.object(p.subprocess, "run") as run:
            for peer in ((101, 1001, 1001), (101, 0, 0)):
                with self.assertRaises(p.InventoryError):
                    p._provider_identity(peer, time.monotonic() + 1)
            run.assert_not_called()
        with patch.object(p, "process_identity", return_value=(11, "/other.service")), patch.object(p.subprocess, "run") as run:
            with self.assertRaises(p.InventoryError):
                p._provider_identity((101, 0, 1001), time.monotonic() + 1)
            run.assert_not_called()

    def test_client_pins_mainpid_invocation_exec_identity(self):
        props = {"Id": p.SERVICE, "MainPID": "101", "InvocationID": "a" * 32, "ActiveState": "active",
                 "SubState": "running", "User": "root", "Group": "1001", "ControlGroup": p.SERVICE_CGROUP,
                 "ExecStart": "{ path=/usr/bin/python3 ; argv[]=" + " ".join(p.EXEC_ARGV) + " ; ignore_errors=no ; pid=101 ; }"}
        with patch.object(p, "process_identity", return_value=(11, p.SERVICE_CGROUP)), patch.object(p.subprocess, "run") as run, \
                patch.object(p, "actual_argv", return_value=p.EXEC_ARGV) as argv, \
                patch.object(p, "interpreter_authority", return_value=((1, 2), ())) as interpreter:
            def completed(values):
                return subprocess.CompletedProcess([], 0, "\n".join(k + "=" + v for k, v in values.items()), "")
            run.return_value = completed(props)
            self.assertEqual(p._provider_identity((101, 0, 1001), time.monotonic() + 1)[0], (11, p.SERVICE_CGROUP))
            self.assertEqual(run.call_args.args[0][-2:], ["--", p.SERVICE])
            for change in ({"MainPID": "102"}, {"InvocationID": "bad"}, {"ExecStart": props["ExecStart"].replace("-I", "-E")}, {"User": "devuser"}):
                run.return_value = completed(props | change)
                with self.subTest(change=change), self.assertRaises(p.InventoryError):
                    p._provider_identity((101, 0, 1001), time.monotonic() + 1)
            run.return_value = completed(props)
            argv.return_value = ("/usr/bin/python3", "-I", "-S", "/different.py")
            with self.assertRaises(p.InventoryError):
                p._provider_identity((101, 0, 1001), time.monotonic() + 1)
            argv.return_value = p.EXEC_ARGV
            interpreter.side_effect = p.InventoryError("unsafe_file")
            with self.assertRaises(p.InventoryError):
                p._provider_identity((101, 0, 1001), time.monotonic() + 1)

    def test_fixed_systemctl_rejects_injection_paths_and_unit_switches_before_exec(self):
        runner = root.FixedSystemctl(p, time.monotonic() + 1)
        for argv in (["sh", "-c", "id"], ["systemctl", "--user", "show-environment"],
                     ["systemctl", "restart", "fixture.service"], ["systemctl", "show", "--root=/caller"]):
            with self.subTest(argv=argv), patch.object(root.subprocess, "Popen") as spawn, self.assertRaises(p.InventoryError):
                runner(argv)
            spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
