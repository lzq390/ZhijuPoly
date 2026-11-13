from __future__ import annotations

import base64
from contextlib import ExitStack
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import stat
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import urllib.error


ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "ops/self_hosted_runner"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


template = load("local_runner_template", PACKAGE / "template.py")
bootstrap = load("local_runner_guest_bootstrap", PACKAGE / "guest/bootstrap.py")
smoke = load("local_runner_guest_smoke", PACKAGE / "guest/smoke.py")


class RunnerTemplateTests(unittest.TestCase):
    def test_checked_in_lock_has_exact_official_artifact_pins(self):
        lock = template.read_lock(template.DEFAULT_LOCK)
        self.assertEqual(lock["buildx"]["version"], "0.31.1")
        self.assertEqual(lock["runner"]["version"], "2.338.2")
        self.assertIn("release-20261017/", lock["cloud_image"]["url"])
        self.assertEqual(len(lock["docker_apt"]["packages"]), 3)

    def test_lock_rejects_credentials_or_mutable_cloud_release(self):
        lock = template.read_lock(template.DEFAULT_LOCK)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lock.json"
            lock["cloud_image"]["url"] = lock["cloud_image"]["url"].replace("release-20261017/", "release/")
            path.write_text(json.dumps(lock))
            with self.assertRaisesRegex(template.TemplateError, "dated"):
                template.read_lock(path)
            lock["runner"]["url"] = "https://user:credential@github.com/file"
            path.write_text(json.dumps(lock))
            with self.assertRaisesRegex(template.TemplateError, "credential-free"):
                template.read_lock(path)

    def test_seed_generation_is_offline_and_contains_no_registration_payload(self):
        lock = template.read_lock(template.DEFAULT_LOCK)
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(template, "execute") as execute:
            user, meta = template.render_seed(lock, Path(directory), "abc123")
            data = json.loads(user.read_text().split("\n", 1)[1])
            self.assertEqual(json.loads(meta.read_text())["instance-id"], "abc123")
            self.assertFalse(data["ssh_pwauth"])
            self.assertEqual(data["runcmd"], [["python3", "/usr/local/lib/nexpoly-ci-runner/build.py"]])
            files = {item["path"]: item for item in data["write_files"]}
            self.assertIn("/etc/nexpoly-ci-runner/build-mode", files)
            self.assertNotIn("registration_token", json.dumps({k: v for k, v in data.items() if k != "write_files"}))
            self.assertNotIn("app.pem", user.read_text())
            network = json.loads((Path(directory) / "network-config").read_text())
            self.assertEqual(network["ethernets"]["ci"]["nameservers"]["addresses"], ["202.38.64.7"])
            self.assertFalse(network["ethernets"]["ci"]["dhcp4-overrides"]["use-dns"])
            payload = files["/usr/local/lib/nexpoly-ci-runner/bootstrap.py"]
            self.assertEqual(base64.b64decode(payload["content"]), (PACKAGE / "guest/bootstrap.py").read_bytes())
            execute.assert_not_called()

    def test_signature_requires_the_pinned_fingerprint(self):
        signed = "[GNUPG:] VALIDSIG " + "A" * 40 + " 2026-11-12 0 0 4 0 1 10 00 " + "B" * 40
        with mock.patch.object(template, "execute", return_value=subprocess.CompletedProcess([], 0, signed, "")):
            with self.assertRaisesRegex(template.TemplateError, "pinned signing key"):
                template.verify_signature(Path("sig"), Path("sums"), Path("keyring"), "C" * 40)
            template.verify_signature(Path("sig"), None, Path("keyring"), "B" * 40)

    def test_cloud_lock_must_match_signed_checksum_not_just_valid_signature(self):
        lock = template.read_lock(template.DEFAULT_LOCK)
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            (cache / "SHA256SUMS").write_text("0" * 64 + " *ubuntu-24.04-server-cloudimg-amd64.img\n")
            with mock.patch.object(template, "download"), mock.patch.object(template, "verify_signature"):
                with self.assertRaisesRegex(template.TemplateError, "differs"):
                    template.verify_cloud_metadata(lock, cache)

    def test_sealed_template_rejects_writable_symlink_backing_and_digest_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "image"
            source.write_bytes(b"fake qcow image")
            digest = template.sha256(source)
            image = source.rename(root / f"base-{digest}.qcow2")
            manifest = {"schema_version": 1, "profile": template.PROFILE, "virtual_size_bytes": template.VIRTUAL_SIZE, "sha256": digest, "image": image.name}
            with self.assertRaisesRegex(template.TemplateError, "read-only"):
                template.verify_template(image, manifest)
            image.chmod(0o444)
            info = {"format": "qcow2", "virtual-size": template.VIRTUAL_SIZE}
            with mock.patch.object(template, "check_sealed_file"), mock.patch.object(template, "execute", return_value=subprocess.CompletedProcess([], 0, json.dumps(info), "")):
                self.assertEqual(template.verify_template(image, manifest), manifest)
            info["backing-filename"] = "/unexpected/backing.qcow2"
            with mock.patch.object(template, "check_sealed_file"), mock.patch.object(template, "execute", return_value=subprocess.CompletedProcess([], 0, json.dumps(info), "")):
                with self.assertRaisesRegex(template.TemplateError, "standalone"):
                    template.verify_template(image, manifest)
            image.chmod(0o644)
            image.write_bytes(b"changed")
            image.chmod(0o444)
            with mock.patch.object(template, "check_sealed_file"), self.assertRaisesRegex(template.TemplateError, "checksum"):
                template.verify_template(image, manifest)
            image.unlink()
            image.symlink_to(root / "other")
            with self.assertRaisesRegex(template.TemplateError, "regular file"):
                template.verify_template(image, manifest)

    def test_template_requires_root_owner_and_bounded_allocated_blocks(self):
        image = mock.Mock()
        details = SimpleNamespace(st_mode=stat.S_IFREG | 0o444, st_uid=1000, st_blocks=1)
        image.lstat.return_value = details
        image.stat.return_value = details
        with self.assertRaisesRegex(template.TemplateError, "owned by root"):
            template.check_sealed_file(image)
        details.st_uid = 0
        details.st_blocks = template.MAX_TEMPLATE_BYTES // 512 + 1
        with self.assertRaisesRegex(template.TemplateError, "25 GiB"):
            template.check_sealed_file(image)
        details.st_blocks = 1
        template.check_sealed_file(image)

    def test_cgroup_children_prevent_sealing_after_main_pid_exits(self):
        result = subprocess.CompletedProcess([], 0, "ActiveState=inactive\nMainPID=0\nControlGroup=/system.slice/nexpoly-vm\n", "")
        with mock.patch.object(template, "execute", return_value=result), mock.patch.object(template.Path, "read_text", return_value="populated 1\nfrozen 0\n"):
            self.assertTrue(template._active("nexpoly-runner-vm@example.service"))
        with mock.patch.object(template, "execute", return_value=result), mock.patch.object(template.Path, "read_text", return_value="populated 0\nfrozen 0\n"):
            self.assertFalse(template._active("nexpoly-runner-vm@example.service"))
        with mock.patch.object(template, "execute", return_value=result), mock.patch.object(template.Path, "read_text", side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                template._active("nexpoly-runner-vm@example.service")

    def test_old_template_cleanup_preserves_current_previous_and_unknown_files(self):
        with tempfile.TemporaryDirectory() as directory:
            templates = Path(directory)
            current, previous, old = [f"base-{letter * 64}.qcow2" for letter in "abc"]
            for name in (current, previous, old, "unrelated.qcow2", "base-not-a-digest.qcow2"):
                (templates / name).touch()
            with mock.patch.object(template, "check_sealed_file") as check:
                template.prune_templates(templates, {current, previous})
            check.assert_called_once_with(templates / old)
            self.assertFalse((templates / old).exists())
            for name in (current, previous, "unrelated.qcow2", "base-not-a-digest.qcow2"):
                self.assertTrue((templates / name).exists())

    def test_runtime_seed_is_credential_free_and_does_not_reinstall_software(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            instance = state / "instances" / ("a" * 32)
            manifest = {"image": "base-" + "b" * 64 + ".qcow2"}
            with mock.patch.object(template, "verify_template"), mock.patch.object(template, "execute") as execute, mock.patch.object(os, "geteuid", return_value=1000):
                previous_umask = os.umask(0o077)
                try:
                    template.prepare_runner_instance({"paths": {"state": str(state)}}, instance, manifest)
                finally:
                    os.umask(previous_umask)
            self.assertEqual(stat.S_IMODE(instance.stat().st_mode), 0o750)
            data = json.loads((instance / "user-data").read_text().split("\n", 1)[1])
            self.assertNotIn("runcmd", data)
            self.assertNotIn("write_files", data)
            self.assertIn("-b", execute.call_args_list[0].args[0])
            self.assertEqual(execute.call_args_list[0].args[0][-1], "160G")
            self.assertIn("--network-config", execute.call_args_list[-1].args[0])
            network = json.loads((instance / "network-config").read_text())
            self.assertFalse(network["ethernets"]["ci"]["dhcp4-overrides"]["use-dns"])

    def test_build_refuses_unprivileged_start_before_any_mutation(self):
        with mock.patch.object(os, "geteuid", return_value=1000), mock.patch.object(template, "atomic_json") as write:
            with self.assertRaisesRegex(template.TemplateError, "root controller"):
                template.build_template({})
            write.assert_not_called()

    def test_failed_download_never_promotes_unverified_artifact(self):
        response = io.BytesIO(b"wrong digest")
        opener = mock.Mock()
        opener.open.return_value = response
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(template.urllib.request, "build_opener", return_value=opener):
            path = Path(directory) / "artifact"
            with self.assertRaisesRegex(template.TemplateError, "pinned checksum"):
                template.download("https://github.com/example", path, "0" * 64)
            self.assertFalse(path.exists())
            self.assertFalse(path.with_name("artifact.partial").exists())


class BuildDiagnosticsTests(unittest.TestCase):
    def test_failed_build_keeps_console_but_discards_candidate_and_prints_stage(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            state = Path(directory)
            instance_id = "a" * 32
            instance = state / "instances" / instance_id
            log = state / "logs/template-builds" / f"template-build-{instance_id}.log"
            log.parent.mkdir(mode=0o700, parents=True)
            log.write_text("Concrete apt error retained outside the discarded disk")
            lock = template.read_lock(template.DEFAULT_LOCK)

            def prepare(_config, _lock_path, path):
                path.mkdir(parents=True)
                (path / "disk.qcow2").write_bytes(b"candidate")
                (path / "seed.iso").write_bytes(b"seed")
                return lock

            stack.enter_context(mock.patch.object(template.os, "geteuid", return_value=0))
            stack.enter_context(mock.patch.object(template.os, "chown"))
            stack.enter_context(mock.patch.object(template.pwd, "getpwnam", return_value=SimpleNamespace(pw_uid=123, pw_gid=123)))
            stack.enter_context(mock.patch.object(template.uuid, "uuid4", return_value=SimpleNamespace(hex=instance_id)))
            stack.enter_context(mock.patch.object(template, "prepare_instance", side_effect=prepare))
            capture = stack.enter_context(mock.patch.object(template, "BuildConsoleCapture"))
            capture.return_value.__enter__.return_value.path = log
            capture.return_value.__exit__.return_value = False
            execute = stack.enter_context(mock.patch.object(template, "execute"))
            stop = stack.enter_context(mock.patch.object(template.subprocess, "run"))
            stack.enter_context(mock.patch.object(template, "_active", return_value=False))
            stream = stack.enter_context(mock.patch.object(template.socket, "socket")).return_value
            stream.recv.return_value = (b'{"event":"template_stage","stage":"apt_update"}\n'
                                       b'{"event":"failed","stage":"apt_update",'
                                       b'"error_kind":"command_failed","exit_code":100}\n')
            output = stack.enter_context(mock.patch.object(template.sys, "stderr", new_callable=io.StringIO))
            with self.assertRaisesRegex(template.TemplateError, "apt_update.*exit_code=100") as error:
                template.build_template({"paths": {"state": str(state)}})
            self.assertIn(str(log), str(error.exception))
            self.assertIn("Guest template stage: apt_update", output.getvalue())
            execute.assert_called_once_with(["systemctl", "start", f"nexpoly-runner-vm@{instance_id}.service"])
            stop.assert_called_once()
            self.assertEqual(stop.call_args.args[0][:2], ["systemctl", "stop"])
            self.assertFalse(instance.exists())
            self.assertFalse((state / "controller/template-build.json").exists())
            self.assertFalse((state / "templates/current.json").exists())
            self.assertIn("Concrete apt error", log.read_text())

    def test_failure_output_accepts_only_fixed_fields_and_bounded_codes(self):
        log = Path("/protected/template-build.log")
        event = {"stage": "apt_dependencies", "error_kind": "command_failed", "exit_code": 100,
                 "reason": "SECRET", "exception": "SECRET"}
        error = str(template.guest_build_failure(event, log))
        self.assertIn("apt_dependencies (command_failed, exit_code=100)", error)
        self.assertIn(str(log), error)
        self.assertNotIn("SECRET", error)
        for bad in ("SECRET", [], {}):
            event.update(stage=bad, error_kind=bad, exit_code=True, http_status=999)
            error = str(template.guest_build_failure(event, log))
            self.assertIn("failed at unknown;", error)
            self.assertNotIn("SECRET", error)
            self.assertNotIn("http_status", error)
            self.assertNotIn("exit_code", error)

    def test_capture_reads_real_serial_socket_and_survives_instance_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            address = str(state / "serial.sock")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(address)
                server.listen(1)
                server.settimeout(2)
                capture = template.BuildConsoleCapture(state, "a" * 32)
                capture.socket_path = address
                with capture:
                    with server.accept()[0] as peer:
                        peer.sendall(b"apt-get failed: concrete guest diagnostic\n")
                    capture.thread.join(timeout=2)
                    self.assertFalse(capture.thread.is_alive())
                self.assertIn(b"concrete guest diagnostic", capture.path.read_bytes())
                self.assertEqual(stat.S_IMODE(capture.path.stat().st_mode), 0o600)
                self.assertTrue(capture.path.is_file())

    def test_capture_retains_recent_output_with_a_fixed_size_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            address = str(state / "serial.sock")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(address)
                server.listen(1)
                server.settimeout(2)
                capture = template.BuildConsoleCapture(state, "a" * 32)
                capture.socket_path = address
                with capture, mock.patch.object(template, "BUILD_LOG_BYTES", 256 * 1024):
                    with server.accept()[0] as peer:
                        peer.sendall(b"x" * (384 * 1024) + b"FINAL ERROR\n")
                    capture.thread.join(timeout=2)
                    self.assertFalse(capture.thread.is_alive())
                self.assertLessEqual(capture.path.stat().st_size, 256 * 1024)
                self.assertTrue(capture.path.read_bytes().endswith(b"FINAL ERROR\n"))

    def test_capture_prunes_only_its_owned_logs_by_age_and_count(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            logs = state / "logs/template-builds"
            logs.mkdir(mode=0o700, parents=True)
            oldest = logs / ("template-build-" + "b" * 32 + ".log")
            oldest.write_text("old")
            os.utime(oldest, (0, 0))
            recent = []
            for number in range(template.BUILD_LOG_COUNT):
                path = logs / f"template-build-{number:032x}.log"
                path.write_text("recent")
                recent.append(path)
            unrelated = logs / "unrelated.log"
            unrelated.write_text("preserve")
            symlink = logs / ("template-build-" + "c" * 32 + ".log")
            symlink.symlink_to(unrelated)
            with template.BuildConsoleCapture(state, "a" * 32):
                pass
            self.assertFalse(oldest.exists())
            self.assertEqual(sum(path.exists() for path in recent), template.BUILD_LOG_COUNT - 1)
            self.assertEqual(unrelated.read_text(), "preserve")
            self.assertTrue(symlink.is_symlink())

    def test_capture_rejects_unprotected_or_substituted_log_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            logs = state / "logs/template-builds"
            logs.mkdir(mode=0o777, parents=True)
            logs.chmod(0o777)
            with self.assertRaisesRegex(template.TemplateError, "protected"):
                with template.BuildConsoleCapture(state, "a" * 32):
                    pass
            logs.rmdir()
            destination = state / "other"
            destination.mkdir()
            logs.symlink_to(destination, target_is_directory=True)
            with self.assertRaisesRegex(template.TemplateError, "protected"):
                with template.BuildConsoleCapture(state, "a" * 32):
                    pass
            logs.unlink()
            logs.mkdir(mode=0o700)
            path = logs / ("template-build-" + "a" * 32 + ".log")
            path.symlink_to(destination / "untouched")
            with self.assertRaises(FileExistsError):
                with template.BuildConsoleCapture(state, "a" * 32):
                    pass
            self.assertFalse((destination / "untouched").exists())


class TemplateDownloadTests(unittest.TestCase):
    def response(self, payload: bytes, length: str | None = None, status: int = 200, content_range: str | None = None):
        response = io.BytesIO(payload)
        response.headers = {} if length is None else {"Content-Length": length}
        response.status = status
        if content_range is not None:
            response.headers["Content-Range"] = content_range
        return response

    def digest(self, payload: bytes) -> str:
        return template.hashlib.sha256(payload).hexdigest()

    def test_valid_cached_artifact_is_reused_without_network_access(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "artifact"
            path.write_bytes(b"verified cache")
            with mock.patch.object(template.urllib.request, "build_opener") as build_opener:
                template.download("https://example.invalid/artifact", path, template.sha256(path))
            self.assertEqual(path.read_bytes(), b"verified cache")
            build_opener.assert_not_called()

    def test_incomplete_transfer_never_becomes_a_cache_artifact(self):
        for length, error in (("20", "length mismatch"), ("1", "length mismatch"), ("invalid", "Invalid download length")):
            with self.subTest(length=length):
                opener = mock.Mock()
                opener.open.return_value = self.response(b"short", length)
                with tempfile.TemporaryDirectory() as directory, mock.patch.object(template.urllib.request, "build_opener", return_value=opener), mock.patch.object(template.sys, "stderr", new_callable=io.StringIO):
                    path = Path(directory) / "artifact"
                    with self.assertRaisesRegex(template.TemplateError, error):
                        template.download("https://example.invalid/artifact", path)
                    self.assertFalse(path.exists())
                    self.assertFalse(path.with_name("artifact.partial").exists())

    def test_restarts_regular_partial_but_never_follows_a_partial_symlink(self):
        opener = mock.Mock()
        opener.open.return_value = self.response(b"new bytes", "9")
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(template.urllib.request, "build_opener", return_value=opener), mock.patch.object(template.sys, "stderr", new_callable=io.StringIO):
            root = Path(directory)
            path = root / "artifact"
            partial = root / "artifact.partial"
            partial.write_bytes(b"stale bytes")
            template.download("https://example.invalid/artifact", path)
            self.assertEqual(path.read_bytes(), b"new bytes")
            self.assertFalse(partial.exists())
            path.unlink()
            unrelated = root / "unrelated"
            unrelated.write_bytes(b"preserve")
            partial.symlink_to(unrelated)
            with self.assertRaisesRegex(template.TemplateError, "regular file"):
                template.download("https://example.invalid/artifact", path)
            self.assertEqual(unrelated.read_bytes(), b"preserve")
            self.assertTrue(partial.is_symlink())
            self.assertEqual(opener.open.call_count, 1)

    def test_total_deadline_applies_even_while_small_chunks_arrive(self):
        response = self.response(b"continuous trickle")
        original_read = response.read
        response.read1 = lambda size: original_read(1)
        opener = mock.Mock()
        opener.open.return_value = response
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(template.urllib.request, "build_opener", return_value=opener), mock.patch.object(template.time, "monotonic", side_effect=[0, 600, 1200, 1800]), mock.patch.object(template.sys, "stderr", new_callable=io.StringIO):
            path = Path(directory) / "artifact"
            with self.assertRaisesRegex(template.TemplateError, "total time limit"):
                template.download("https://example.invalid/artifact", path)
            self.assertFalse(path.exists())
            self.assertFalse(path.with_name("artifact.partial").exists())

    def test_progress_reports_bytes_without_url_or_proxy_secrets(self):
        opener = mock.Mock()
        opener.open.return_value = self.response(b"bytes", "5")
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(template.urllib.request, "build_opener", return_value=opener), mock.patch.object(template.time, "monotonic", side_effect=[0, 0, 11, 11, 12]), mock.patch.object(template.sys, "stderr", new_callable=io.StringIO) as output:
            path = Path(directory) / "artifact"
            template.download("https://user:URLSECRET@example.invalid/artifact?token=QUERYSECRET", path, proxy="http://PROXYSECRET@127.0.0.1:27892")
            self.assertEqual(path.read_bytes(), b"bytes")
            log = output.getvalue()
            self.assertIn("Opening download: artifact", log)
            self.assertIn("5/5 bytes", log)
            self.assertIn("Download complete", log)
            for secret in ("URLSECRET", "QUERYSECRET", "PROXYSECRET", "example.invalid"):
                self.assertNotIn(secret, log)
            opener.open.assert_called_once_with("https://user:URLSECRET@example.invalid/artifact?token=QUERYSECRET", timeout=template.IDLE_TIMEOUT)

    def test_network_failure_is_bounded_and_does_not_disclose_url(self):
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.URLError("https://user:SECRET@example.invalid/?token=TOKEN")
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(template.urllib.request, "build_opener", return_value=opener), mock.patch.object(template.sys, "stderr", new_callable=io.StringIO):
            path = Path(directory) / "artifact"
            with self.assertRaisesRegex(template.TemplateError, "Download failed for artifact: URLError") as caught:
                template.download("https://example.invalid/artifact", path)
            for secret in ("SECRET", "TOKEN", "example.invalid"):
                self.assertNotIn(secret, str(caught.exception))
            self.assertFalse(path.exists())
            self.assertFalse(path.with_name("artifact.partial").exists())

    def test_verified_complete_partial_is_promoted_without_network_access(self):
        opener = mock.Mock()
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(template.urllib.request, "build_opener", return_value=opener), mock.patch.object(template.sys, "stderr", new_callable=io.StringIO):
            path = Path(directory) / "artifact"
            partial = path.with_name("artifact.partial")
            partial.write_bytes(b"complete")
            template.download("https://example.invalid/artifact", path, self.digest(b"complete"))
            self.assertEqual(path.read_bytes(), b"complete")
            self.assertFalse(partial.exists())
            opener.open.assert_not_called()

    def test_pinned_partial_resumes_only_with_matching_range_and_final_hash(self):
        opener = mock.Mock()
        opener.open.return_value = self.response(b" bytes", "6", 206, "bytes 3-8/9")
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(template.urllib.request, "build_opener", return_value=opener), mock.patch.object(template.sys, "stderr", new_callable=io.StringIO) as output:
            path = Path(directory) / "artifact"
            partial = path.with_name("artifact.partial")
            partial.write_bytes(b"new")
            template.download("https://example.invalid/artifact", path, self.digest(b"new bytes"))
            self.assertEqual(path.read_bytes(), b"new bytes")
            self.assertFalse(partial.exists())
            request = opener.open.call_args.args[0]
            self.assertEqual(request.get_header("Range"), "bytes=3-")
            self.assertEqual(request.get_header("Accept-encoding"), "identity")
            self.assertIn("resumed offset 3 bytes", output.getvalue())

    def test_server_ignoring_range_restarts_instead_of_appending(self):
        opener = mock.Mock()
        opener.open.return_value = self.response(b"new bytes", "9")
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(template.urllib.request, "build_opener", return_value=opener), mock.patch.object(template.sys, "stderr", new_callable=io.StringIO) as output:
            path = Path(directory) / "artifact"
            partial = path.with_name("artifact.partial")
            partial.write_bytes(b"old")
            template.download("https://example.invalid/artifact", path, self.digest(b"new bytes"))
            self.assertEqual(path.read_bytes(), b"new bytes")
            self.assertIn("restarting from byte 0", output.getvalue())

    def test_invalid_or_rejected_range_discards_partial_without_promotion(self):
        cases = (("bytes 2-8/9", "7"), ("bytes 3-7/9", "5"), ("bytes 3-8/*", "6"), ("bytes 3-8/9", "5"), (None, "6"))
        for content_range, length in cases:
            with self.subTest(content_range=content_range, length=length):
                opener = mock.Mock()
                opener.open.return_value = self.response(b" bytes", length, 206, content_range)
                with tempfile.TemporaryDirectory() as directory, mock.patch.object(template.urllib.request, "build_opener", return_value=opener), mock.patch.object(template.sys, "stderr", new_callable=io.StringIO):
                    path = Path(directory) / "artifact"
                    partial = path.with_name("artifact.partial")
                    partial.write_bytes(b"new")
                    with self.assertRaisesRegex(template.TemplateError, "Invalid download range"):
                        template.download("https://example.invalid/artifact", path, self.digest(b"new bytes"))
                    self.assertFalse(path.exists())
                    self.assertFalse(partial.exists())
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.HTTPError("https://example.invalid", 416, "range rejected", {}, None)
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(template.urllib.request, "build_opener", return_value=opener), mock.patch.object(template.sys, "stderr", new_callable=io.StringIO):
            path = Path(directory) / "artifact"
            partial = path.with_name("artifact.partial")
            partial.write_bytes(b"new")
            with self.assertRaisesRegex(template.TemplateError, "HTTP 416; retry to restart"):
                template.download("https://example.invalid/artifact", path, self.digest(b"new bytes"))
            self.assertFalse(path.exists())
            self.assertFalse(partial.exists())

    def test_interrupted_pinned_transfer_is_preserved_and_can_resume_to_verified_completion(self):
        opener = mock.Mock()
        opener.open.return_value = self.response(b" b", "6", 206, "bytes 3-8/9")
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(template.urllib.request, "build_opener", return_value=opener), mock.patch.object(template.sys, "stderr", new_callable=io.StringIO):
            path = Path(directory) / "artifact"
            partial = path.with_name("artifact.partial")
            partial.write_bytes(b"new")
            with self.assertRaisesRegex(template.TemplateError, "length mismatch"):
                template.download("https://example.invalid/artifact", path, self.digest(b"new bytes"))
            self.assertFalse(path.exists())
            self.assertEqual(partial.read_bytes(), b"new b")
            opener.open.return_value = self.response(b"ytes", "4", 206, "bytes 5-8/9")
            template.download("https://example.invalid/artifact", path, self.digest(b"new bytes"))
            self.assertEqual(path.read_bytes(), b"new bytes")
            self.assertFalse(partial.exists())

    def test_pinned_partial_survives_network_timeout_interrupt_and_total_deadline(self):
        for failure, error_type in ((TimeoutError(), template.TemplateError), (urllib.error.URLError("timeout"), template.TemplateError), (KeyboardInterrupt(), KeyboardInterrupt)):
            with self.subTest(failure=type(failure).__name__):
                opener = mock.Mock()
                opener.open.side_effect = failure
                with tempfile.TemporaryDirectory() as directory, mock.patch.object(template.urllib.request, "build_opener", return_value=opener), mock.patch.object(template.sys, "stderr", new_callable=io.StringIO):
                    path = Path(directory) / "artifact"
                    partial = path.with_name("artifact.partial")
                    partial.write_bytes(b"new")
                    with self.assertRaises(error_type):
                        template.download("https://example.invalid/artifact", path, self.digest(b"new bytes"))
                    self.assertFalse(path.exists())
                    self.assertEqual(partial.read_bytes(), b"new")
        opener = mock.Mock()
        response = self.response(b"new bytes", "9")
        original_read = response.read
        response.read1 = lambda size: original_read(1)
        opener.open.return_value = response
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(template.urllib.request, "build_opener", return_value=opener), mock.patch.object(template.time, "monotonic", side_effect=[0, 0, 10, 1800]), mock.patch.object(template.sys, "stderr", new_callable=io.StringIO):
            path = Path(directory) / "artifact"
            with self.assertRaisesRegex(template.TemplateError, "total time limit"):
                template.download("https://example.invalid/artifact", path, self.digest(b"new bytes"))
            self.assertFalse(path.exists())
            self.assertEqual(path.with_name("artifact.partial").read_bytes(), b"n")

    def test_resumed_content_cannot_bypass_pinned_hash_verification(self):
        opener = mock.Mock()
        opener.open.return_value = self.response(b" bytes", "6", 206, "bytes 3-8/9")
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(template.urllib.request, "build_opener", return_value=opener), mock.patch.object(template.sys, "stderr", new_callable=io.StringIO):
            path = Path(directory) / "artifact"
            partial = path.with_name("artifact.partial")
            partial.write_bytes(b"bad")
            with self.assertRaisesRegex(template.TemplateError, "pinned checksum"):
                template.download("https://example.invalid/artifact", path, self.digest(b"new bytes"))
            self.assertFalse(path.exists())
            self.assertFalse(partial.exists())


class GuestBootstrapTests(unittest.TestCase):
    def payload(self):
        return {"repo_url": "https://github.com/lzq390/ZhijuPoly", "runner_name": "nexpoly-ci-" + "a" * 32, "registration_token": "SECRETBOOTSTRAPVALUE12345", "labels": ["nexpoly-ci-ephemeral"]}

    def test_guest_rejects_other_repository_labels_and_unknown_fields(self):
        self.assertEqual(bootstrap.validate_payload(self.payload()), self.payload())
        for key, value in (("repo_url", "https://github.com/other/repo"), ("labels", ["ubuntu-24.04"]), ("runner_name", "name; touch /tmp/pwned"), ("registration_token", "token\nSECRET"), ("unknown", "field")):
            payload = self.payload()
            payload[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                bootstrap.validate_payload(payload)

    def test_token_not_in_arguments_and_removed_before_job_start(self):
        payload = self.payload()
        commands = []

        def run(args, **kwargs):
            commands.append((args, dict(kwargs.get("env", {}))))
            return subprocess.CompletedProcess(args, 0)

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(bootstrap, "RUNTIME", Path(directory)), mock.patch.object(bootstrap.Path, "read_text", return_value=bootstrap.PROFILE), mock.patch.object(bootstrap, "read_payload", return_value=payload), mock.patch.object(bootstrap.subprocess, "run", side_effect=run), mock.patch.object(bootstrap, "emit") as emit, mock.patch.object(bootstrap, "isolated_registration"), mock.patch("shutil.rmtree"):
            bootstrap.register()
            self.assertEqual(bootstrap.main(), 0)
        config_args, config_env = commands[0]
        self.assertIn("--ephemeral", config_args)
        self.assertNotIn("--token", config_args)
        self.assertEqual(config_env["ACTIONS_RUNNER_INPUT_TOKEN"], "SECRETBOOTSTRAPVALUE12345")
        self.assertNotIn("ACTIONS_RUNNER_INPUT_TOKEN", commands[1][1])
        self.assertEqual(commands[-1][0], ["systemctl", "poweroff", "--no-block"])
        self.assertEqual(payload, {})
        self.assertNotIn("SECRETBOOTSTRAPVALUE12345", str(emit.call_args_list))

    def test_failure_event_never_includes_exception_or_token(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(bootstrap, "RUNTIME", Path(directory)), mock.patch.object(bootstrap.Path, "read_text", return_value=bootstrap.PROFILE), mock.patch.object(bootstrap, "isolated_registration", side_effect=ValueError("SECRET-IN-ERROR")), mock.patch.object(bootstrap.subprocess, "run") as run, mock.patch.object(bootstrap, "emit") as emit:
            self.assertEqual(bootstrap.main(), 1)
            self.assertNotIn("SECRET-IN-ERROR", str(emit.call_args_list))
            run.assert_called_once_with(["systemctl", "poweroff", "--no-block"], check=False)

    def test_parent_never_reads_registration_token_and_waits_for_child_exit(self):
        with mock.patch.object(bootstrap.os, "fork", return_value=123), mock.patch.object(bootstrap.os, "waitpid", return_value=(123, 0)) as wait, mock.patch.object(bootstrap, "register") as register:
            bootstrap.isolated_registration()
            register.assert_not_called()
            wait.assert_called_once_with(123, 0)
        with mock.patch.object(bootstrap.os, "fork", return_value=123), mock.patch.object(bootstrap.os, "waitpid", return_value=(123, 256)):
            with self.assertRaisesRegex(RuntimeError, "registration failed"):
                bootstrap.isolated_registration()

    def test_all_proxy_scopes_and_hook_paths_are_explicit(self):
        env = bootstrap.runner_environment()
        for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
            self.assertEqual(env[key], "http://10.0.2.2:27892")
        self.assertEqual(env["NO_PROXY"], "localhost,127.0.0.1,::1,postgres,backend,lab-postgres")
        self.assertTrue(env["ACTIONS_RUNNER_HOOK_JOB_STARTED"].endswith("job-started.sh"))

    def test_guest_ready_is_emitted_after_opening_receive_port(self):
        order = []
        payload = (json.dumps(self.payload()) + "\n").encode()
        with mock.patch.object(bootstrap.Path, "exists", return_value=True), mock.patch.object(bootstrap.os, "open", side_effect=lambda *args: order.append("open") or 7), mock.patch.object(bootstrap, "emit", side_effect=lambda *args: order.append("ready")), mock.patch.object(bootstrap.select, "select", return_value=([7], [], [])), mock.patch.object(bootstrap.os, "read", return_value=payload), mock.patch.object(bootstrap.os, "close"):
            self.assertEqual(bootstrap.read_payload(), self.payload())
        self.assertEqual(order, ["open", "ready"])

    def test_smoke_checks_reject_direct_guest_network_access(self):
        with mock.patch.object(smoke.socket, "create_connection", side_effect=TimeoutError):
            smoke.denied_direct("10.0.2.2", 27891)
        with mock.patch.object(smoke.socket, "create_connection") as connect:
            with self.assertRaisesRegex(RuntimeError, "direct network access"):
                smoke.denied_direct("1.1.1.1", 443)
            connect.return_value.close.assert_called_once()


class GuestSmokeHealthTests(unittest.TestCase):
    def prepare_smoke(self, stack):
        opener = mock.MagicMock()
        opener.open.return_value.__enter__.return_value.status = 200
        build = stack.enter_context(mock.patch.object(smoke.urllib.request, "build_opener", return_value=opener))
        stack.enter_context(mock.patch.object(smoke.Path, "is_file", return_value=True))
        stack.enter_context(mock.patch.object(smoke.Path, "read_text", return_value=json.dumps({
            "smoke_image": {"reference": "hello-world@sha256:" + "a" * 64}})))
        stack.enter_context(mock.patch.object(smoke.socket, "getaddrinfo", return_value=[
            (2, 1, 6, "", ("140.82.112.5", 443))]))
        connection = stack.enter_context(mock.patch.object(smoke.http.client, "HTTPConnection"))
        connection.return_value.getresponse.return_value.status = 403
        stack.enter_context(mock.patch.object(smoke, "denied_direct"))
        docker = stack.enter_context(mock.patch.object(smoke.subprocess, "run"))
        stack.enter_context(mock.patch("sys.stdout", new_callable=io.StringIO))
        return opener, build, docker

    def test_guest_transport_health_uses_web_https_without_anonymous_api_quota(self):
        with ExitStack() as stack:
            opener, build, docker = self.prepare_smoke(stack)
            smoke.main()
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://github.com/robots.txt")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("User-agent"), "nexpoly-runner-health/1.0")
        self.assertIsNone(request.get_header("Authorization"))
        self.assertEqual(build.call_args.args[0].proxies, {"https": "http://10.0.2.2:27892"})
        opener.open.assert_called_once()
        opener.open.return_value.__enter__.return_value.read.assert_called_once_with(1024)
        self.assertEqual(docker.call_count, 2)

    def test_guest_denied_web_probe_is_not_accepted_or_retried_elsewhere(self):
        with ExitStack() as stack:
            opener, build, docker = self.prepare_smoke(stack)
            opener.open.side_effect = template.urllib.error.HTTPError(
                "https://github.com/robots.txt", 403, "denied", {}, None)
            with self.assertRaises(template.urllib.error.HTTPError):
                smoke.main()
        opener.open.assert_called_once()
        build.assert_called_once()
        docker.assert_not_called()


if __name__ == "__main__":
    unittest.main()
