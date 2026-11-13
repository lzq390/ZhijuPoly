from __future__ import annotations

from contextlib import ExitStack
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import urllib.error


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "local_runner_guest_build", ROOT / "ops/self_hosted_runner/guest/build.py")
assert spec and spec.loader
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class GuestBuildDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.previous_stage = build.CURRENT_STAGE

    def tearDown(self):
        build.CURRENT_STAGE = self.previous_stage

    def test_invalid_stage_never_enters_the_event_stream(self):
        with mock.patch.object(build, "emit") as emit:
            with self.assertRaises(ValueError):
                build.stage("https://user:secret@example.com/download?token=secret")
        emit.assert_not_called()

    def test_failure_events_expose_only_fixed_categories_and_bounded_numbers(self):
        secret = "https://user:secret@example.com/download?token=secret"
        cases = [
            (subprocess.CalledProcessError(100, [secret], secret, secret),
             {"error_kind": "command_failed", "exit_code": 100}),
            (subprocess.CalledProcessError(-9, [secret]),
             {"error_kind": "command_failed", "exit_code": -9}),
            (subprocess.CalledProcessError(99999, [secret]),
             {"error_kind": "command_failed"}),
            (urllib.error.HTTPError(secret, 403, secret, {}, None),
             {"error_kind": "http_error", "http_status": 403}),
            (urllib.error.HTTPError(secret, 99999, secret, {}, None),
             {"error_kind": "http_error"}),
            (TimeoutError(secret), {"error_kind": "timeout"}),
            (urllib.error.URLError(TimeoutError(secret)), {"error_kind": "timeout"}),
            (urllib.error.URLError(secret), {"error_kind": "network_error"}),
            (PermissionError(secret), {"error_kind": "filesystem_error"}),
            (ValueError(secret), {"error_kind": "validation_error"}),
            (RuntimeError(secret), {"error_kind": "validation_error"}),
            (tarfile.ReadError(secret), {"error_kind": "validation_error"}),
            (Exception(secret), {"error_kind": "unexpected_error"}),
        ]
        build.CURRENT_STAGE = "runner_download"
        for error, expected in cases:
            with self.subTest(error=type(error).__name__, expected=expected):
                with mock.patch.object(build, "emit") as emit:
                    build.report_failure(error)
                emit.assert_called_once_with(
                    "failed", reason="template_build_failed", stage="runner_download", **expected)
                self.assertNotIn("secret", repr(emit.call_args))
                self.assertNotIn("https://", repr(emit.call_args))

    def test_invalid_current_stage_is_not_reported(self):
        build.CURRENT_STAGE = "secret"
        with mock.patch.object(build, "emit") as emit:
            build.report_failure(ValueError("secret"))
        self.assertEqual(emit.call_args.kwargs["stage"], "preflight")

    def test_entrypoint_reports_failed_phase_and_always_powers_off(self):
        error = subprocess.CalledProcessError(100, ["apt-get", "install"])
        events = []

        def fail():
            build.stage("apt_dependencies")
            raise error

        with mock.patch.object(build, "main", side_effect=fail), \
                mock.patch.object(build, "emit", side_effect=lambda event, **_: events.append(event)) as emit, \
                mock.patch.object(build.traceback, "print_exc", side_effect=lambda **_: events.append("traceback")) as trace, \
                mock.patch.object(build.sys, "stderr") as stderr, \
                mock.patch.object(build.subprocess, "run", side_effect=lambda *_, **__: events.append("poweroff")) as run:
            stderr.flush.side_effect = lambda: events.append("flush")
            with self.assertRaises(SystemExit) as caught:
                build.entrypoint()
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(events, ["template_stage", "traceback", "flush", "failed", "poweroff"])
        trace.assert_called_once_with(file=stderr)
        self.assertEqual(emit.call_args_list, [
            mock.call("template_stage", stage="apt_dependencies"),
            mock.call("failed", reason="template_build_failed", stage="apt_dependencies",
                      error_kind="command_failed", exit_code=100),
        ])
        run.assert_called_once_with(["systemctl", "poweroff", "--no-block"], check=False)

    def test_successful_entrypoint_powers_off_without_failure_event(self):
        with mock.patch.object(build, "main"), mock.patch.object(build, "emit") as emit, \
                mock.patch.object(build.subprocess, "run") as run:
            build.entrypoint()
        emit.assert_not_called()
        run.assert_called_once_with(["systemctl", "poweroff", "--no-block"], check=False)

    def test_real_preparation_flow_preinstalls_os_browser_dependencies_and_preserves_apt_indexes(self):
        # Redirect every guest path into a temporary tree. All subprocesses,
        # downloads, extraction and cleanup remain mocked; no host is prepared.
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            config = root / "etc/nexpoly-ci-runner"
            config.mkdir(parents=True)
            (config / "build-mode").touch()
            (root / "etc/os-release").write_text('VERSION_ID="24.04"\n')
            (config / "software-lock.json").write_text(json.dumps({
                "docker_apt": {"packages": [{"package": "docker-ce"}]},
                "buildx": {}, "runner": {"version": "2.338.2"},
            }))
            apt_lists = root / "var/lib/apt/lists"
            apt_lists.mkdir(parents=True)
            (apt_lists / "ubuntu_InRelease").write_text("release metadata fixture")
            (apt_lists / "ubuntu_Packages").write_text("package index fixture")

            def guest_path(value):
                path = Path(value)
                if path.is_absolute() and not path.is_relative_to(root):
                    return root / path.relative_to("/")
                return path

            def downloaded(_artifact, destination):
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"mock artifact")

            def remove_guest_tree(path, **kwargs):
                mapped = guest_path(path)
                self.assertTrue(mapped.is_relative_to(root))
                shutil.rmtree(mapped, **kwargs)

            stack.enter_context(mock.patch.object(build, "CONFIG", config))
            stack.enter_context(mock.patch.object(build, "Path", side_effect=guest_path))
            stack.enter_context(mock.patch.object(build.os, "geteuid", return_value=0))
            stack.enter_context(mock.patch.dict(os.environ))
            run = stack.enter_context(mock.patch.object(build, "run"))
            stack.enter_context(mock.patch.object(build, "download", side_effect=downloaded))
            stack.enter_context(mock.patch.object(build.tarfile, "open"))
            stack.enter_context(mock.patch.object(build, "shutil", SimpleNamespace(rmtree=remove_guest_tree)))
            emit = stack.enter_context(mock.patch.object(build, "emit"))
            build.main()
            self.assertEqual(
                [call.kwargs["stage"] for call in emit.call_args_list if call.args == ("template_stage",)],
                ["preflight", "apt_update", "apt_dependencies", "docker_packages",
                 "buildx_download", "runner_download", "runner_dependencies",
                 "guest_configuration", "docker_start", "smoke", "seal"])
            self.assertEqual(emit.call_args, mock.call(
                "template_ready", profile="nexpoly-ci-ephemeral-v1",
                runner_version="2.338.2", smoke_passed=True))
            # Independent reviewed set: omitting a font/library must not silently
            # put the package download back into the limited per-job CI budget.
            expected_browser_packages = {
                "xvfb", "fonts-noto-color-emoji", "fonts-unifont", "libfontconfig1",
                "libfreetype6", "xfonts-cyrillic", "xfonts-scalable", "fonts-liberation",
                "fonts-ipafont-gothic", "fonts-wqy-zenhei", "fonts-tlwg-loma-otf",
                "fonts-freefont-ttf", "libasound2t64", "libatk-bridge2.0-0t64",
                "libatk1.0-0t64", "libatspi2.0-0t64", "libcairo2", "libcups2t64",
                "libdbus-1-3", "libdrm2", "libgbm1", "libglib2.0-0t64", "libnspr4",
                "libnss3", "libpango-1.0-0", "libx11-6", "libxcb1", "libxcomposite1",
                "libxdamage1", "libxext6", "libxfixes3", "libxkbcommon0", "libxrandr2",
            }
            system_install = next(call.args for call in run.call_args_list
                                  if call.args[:4] == ("apt-get", "install", "-y", "--no-install-recommends"))
            self.assertTrue(expected_browser_packages.issubset(system_install[4:]))
            self.assertEqual(set(build.CHROMIUM_SYSTEM_PACKAGES), expected_browser_packages)
            run.assert_any_call("apt-get", "clean")
            self.assertEqual((apt_lists / "ubuntu_InRelease").read_text(), "release metadata fixture")
            self.assertEqual((apt_lists / "ubuntu_Packages").read_text(), "package index fixture")


if __name__ == "__main__":
    unittest.main()
