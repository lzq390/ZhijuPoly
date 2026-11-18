from __future__ import annotations

from contextlib import ExitStack
import importlib.util
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "ops/self_hosted_runner/guest" / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


smoke = load("background_apt_smoke", "smoke.py")
build = load("background_apt_build", "build.py")
UNITS = ("apt-daily.timer", "apt-daily-upgrade.timer", "apt-daily.service", "apt-daily-upgrade.service")


class BackgroundAptTests(unittest.TestCase):
    def states(self, overrides=None):
        values = []
        for unit in UNITS:
            fields = {"Id": unit, "UnitFileState": "masked", "ActiveState": "inactive", "Job": ""}
            fields.update((overrides or {}).get(unit, {}))
            values.append("\n".join(f"{key}={value}" for key, value in fields.items()))
        return "\n\n".join(values) + "\n"

    def context(self, stack, states=None, policy=None, locks=True):
        stack.enter_context(mock.patch.object(smoke, "check_needrestart_policy"))
        stack.enter_context(mock.patch.object(smoke, "apt_locks_available", return_value=locks))
        query = mock.Mock(return_value=self.states() if states is None else states)

        def run(command, **kwargs):
            self.assertTrue(kwargs["check"])
            self.assertLessEqual(kwargs["timeout"], 5)
            if command[0] == "apt-config":
                self.assertEqual(command[1:], ["shell", "VALUE0", "APT::Periodic::Enable",
                    "VALUE1", "APT::Periodic::Update-Package-Lists", "VALUE2",
                    "APT::Periodic::Download-Upgradeable-Packages", "VALUE3", "APT::Periodic::Unattended-Upgrade"])
                output = "\n".join(f"VALUE{index}='0'" for index in range(4)) if policy is None else policy
            else:
                self.assertEqual(command, ["systemctl", "show", "--no-pager",
                    "--property=Id,UnitFileState,ActiveState,Job", "--", *UNITS])
                output = query()
            return subprocess.CompletedProcess(command, 0, output)

        stack.enter_context(mock.patch.object(smoke.subprocess, "run", side_effect=run))
        return query

    def test_only_fixed_permanently_masked_idle_units_are_accepted(self):
        with ExitStack() as stack:
            query = self.context(stack)
            smoke.check_background_updates()
        query.assert_called_once()
        self.assertEqual(smoke.BACKGROUND_APT_UNITS, UNITS)
        self.assertNotIn("unattended-upgrades.service", UNITS)  # Shutdown coordinator, not an updater.

    def test_runtime_mask_missing_unit_or_enabled_periodic_policy_is_rejected(self):
        cases = [self.states({unit: {"UnitFileState": state}})
                 for unit in UNITS for state in ("enabled", "static", "masked-runtime")]
        cases.extend(["", self.states() + "\n" + self.states()])
        for states in cases:
            with self.subTest(states=states), ExitStack() as stack:
                self.context(stack, states=states)
                with self.assertRaises(RuntimeError):
                    smoke.check_background_updates()
        for policy in ("", "VALUE0='1'", "VALUE0='0'\nVALUE1='0'\nVALUE2='0'\nVALUE3='1'"):
            with self.subTest(policy=policy), ExitStack() as stack:
                query = self.context(stack, policy=policy)
                with self.assertRaisesRegex(RuntimeError, "configuration"):
                    smoke.check_background_updates()
                query.assert_not_called()

    def test_active_pending_or_locked_package_activity_cannot_pass_smoke(self):
        cases = [({"ActiveState": state}, True) for state in ("active", "activating", "deactivating")]
        cases.extend([({"Job": "100 /org/freedesktop/systemd1/job/100"}, True), ({}, False)])
        for change, locks in cases:
            with self.subTest(change=change, locks=locks), ExitStack() as stack:
                self.context(stack, states=self.states({"apt-daily-upgrade.service": change}), locks=locks)
                with self.assertRaisesRegex(RuntimeError, "deadline"):
                    smoke.check_background_updates()

    def test_existing_upgrade_finishes_naturally_and_wait_has_a_deadline_and_progress(self):
        for finishes in (True, False):
            with self.subTest(finishes=finishes), ExitStack() as stack:
                query = self.context(stack)
                busy = self.states({"apt-daily-upgrade.service": {"ActiveState": "activating"}})
                query.side_effect = [busy, busy, self.states()] if finishes else None
                query.return_value = busy
                clock = [0.0]
                stack.enter_context(mock.patch.object(smoke.time, "monotonic", side_effect=lambda: clock[0]))
                stack.enter_context(mock.patch.object(smoke.time, "sleep", side_effect=lambda delay: clock.__setitem__(0, clock[0] + delay)))
                output = stack.enter_context(mock.patch.object(smoke.sys, "stderr", new_callable=io.StringIO))
                if finishes:
                    smoke.check_background_updates(wait_seconds=61)
                    self.assertEqual(clock[0], 2)
                else:
                    with self.assertRaisesRegex(RuntimeError, "deadline"):
                        smoke.check_background_updates(wait_seconds=61)
                    self.assertEqual(clock[0], 61)
                    self.assertEqual(output.getvalue().count("Waiting for existing APT activity"), 3)
                self.assertIn("no process is terminated", output.getvalue())

    def test_real_posix_and_flock_locks_are_observed_without_modifying_files(self):
        for use_flock in (False, True):
            with self.subTest(flock=use_flock), tempfile.TemporaryDirectory() as directory:
                lock = Path(directory) / "lock"
                lock.write_text("unchanged fixture")
                child = subprocess.Popen([sys.executable, "-c",
                    "import fcntl,sys; f=open(sys.argv[1], 'r+'); "
                    "(fcntl.flock if sys.argv[2]=='1' else fcntl.lockf)(f, fcntl.LOCK_EX); "
                    "print('ready', flush=True); sys.stdin.read(1)", str(lock), str(int(use_flock))],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
                try:
                    self.assertEqual(child.stdout.readline().strip(), "ready")
                    with mock.patch.object(smoke, "APT_LOCKS", ((str(lock), use_flock),)):
                        self.assertFalse(smoke.apt_locks_available())
                        child.stdin.close()
                        child.wait(timeout=5)
                        self.assertTrue(smoke.apt_locks_available())
                    self.assertEqual(lock.read_text(), "unchanged fixture")
                finally:
                    if child.poll() is None:
                        child.kill()
                        child.wait(timeout=5)
                    child.stdout.close()
                    if not child.stdin.closed:
                        child.stdin.close()

    def test_lock_probe_does_not_follow_symlinks_or_create_missing_files(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target"
            target.write_text("untouched")
            link = Path(directory) / "lock"
            link.symlink_to(target)
            with mock.patch.object(smoke, "APT_LOCKS", ((str(link), False),)):
                with self.assertRaises(OSError):
                    smoke.apt_locks_available()
            missing = Path(directory) / "missing"
            with mock.patch.object(smoke, "APT_LOCKS", ((str(missing), False),)):
                self.assertTrue(smoke.apt_locks_available())
            self.assertFalse(missing.exists())
            self.assertEqual(target.read_text(), "untouched")

    @unittest.skipUnless(shutil.which("perl"), "Perl is required for the real needrestart config parser")
    def test_needrestart_effective_config_and_package_absent_snippet_are_parsed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            main, policy = root / "needrestart.conf", root / "99-nexpoly-ci.conf"
            policy.write_text(smoke.NEEDRESTART_POLICY)
            with mock.patch.object(smoke, "NEEDRESTART_MAIN", main), mock.patch.object(smoke, "NEEDRESTART_CONFIG", str(policy)):
                smoke.check_needrestart_policy()  # Package absent; future install keeps this snippet.
                main.write_text("$nrconf{restart}='a'; do '" + str(policy) + "';\n")
                smoke.check_needrestart_policy()  # Actual effective override, not string matching.
                main.write_text("do '" + str(policy) + "'; $nrconf{restart}='a';\n")
                with self.assertRaises(subprocess.CalledProcessError):
                    smoke.check_needrestart_policy()
                policy.write_text("not valid perl @@@")
                main.unlink()
                with self.assertRaises(subprocess.CalledProcessError):
                    smoke.check_needrestart_policy()

    def test_failed_quiescence_prevents_all_template_package_transactions(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            config = root / "etc/nexpoly-ci-runner"
            config.mkdir(parents=True)
            (config / "build-mode").touch()
            (root / "etc/os-release").write_text('VERSION_ID="24.04"\n')

            def guest_path(value):
                path = Path(value)
                return root / path.relative_to("/") if path.is_absolute() and not path.is_relative_to(root) else path

            stack.enter_context(mock.patch.object(build, "Path", side_effect=guest_path))
            stack.enter_context(mock.patch.object(build, "CONFIG", config))
            stack.enter_context(mock.patch.object(build.os, "geteuid", return_value=0))
            stack.enter_context(mock.patch.object(build, "emit"))
            stack.enter_context(mock.patch.dict(sys.modules, {"bootstrap": SimpleNamespace(runner_account=mock.Mock()), "smoke": smoke}))
            check = stack.enter_context(mock.patch.object(smoke, "check_background_updates", side_effect=RuntimeError("Background APT activity did not become idle before its deadline")))
            run = stack.enter_context(mock.patch.object(build, "run"))
            with self.assertRaisesRegex(RuntimeError, "deadline"):
                build.main()
            check.assert_called_once_with(wait_seconds=300)
            self.assertEqual(run.call_args_list, [mock.call("systemctl", "mask", *UNITS),
                mock.call("systemctl", "stop", "apt-daily.timer", "apt-daily-upgrade.timer")])
            self.assertEqual(build.CURRENT_STAGE, "apt_quiesce")
            self.assertTrue((root / "etc/needrestart/conf.d/99-nexpoly-ci.conf").is_file())


if __name__ == "__main__":
    unittest.main()
