from __future__ import annotations
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import Mock, patch

from scripts import dev_gpu_cohort as cohort


class CohortTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for path in (self.root / ".runtime", self.root / ".runtime/cohorts",
                     self.root / ".runtime/cohorts/round-two"):
            path.mkdir(mode=0o700)
        self.path = self.root / ".runtime/cohorts/round-two/cohort.json"
        resources = dict(cpu_quota_percent=100, memory_max_bytes=3 * 1024**3,
                         memory_swap_max_bytes=0, tasks_max=512)
        self.value = dict(schema_version=1, cohort_id="round-two", repo_root=str(self.root),
                          source_sha="a" * 40, source_tree="b" * 40,
                          compose_project="nexpoly_accept_r2_test", frontend_port=36965,
                          backend_port=35699, auth_env_file=None,
                          md_resources=resources, dft_resources=resources,
                          backend_resources=resources, prior_backend=None)
        self.session = "c" * 32
        self.config = self.load_value(self.value)
        self.config.prepare()

    def load_value(self, value):
        self.path.write_bytes(cohort.canonical_json(value))
        self.path.chmod(0o600)
        digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        return cohort.DevGpuCohort.load(self.path, digest, repo_root=self.root)

    def test_fixed_private_environment_and_source(self):
        env = self.config.environment()
        self.assertEqual(env["NEXPOLY_DEV_COMPOSE_PROJECT"], self.value["compose_project"])
        self.assertEqual(env["NEXPOLY_DEV_GPU_LAUNCHER_ENABLED"], "false")
        self.assertEqual(env["MONOMER_DFT_GPU_SCOPE_MEMORY_SWAP_MAX_BYTES"], "0")
        self.assertTrue(env["NEXPOLY_GPU_STATE_ROOT"].startswith(str(self.path.parent) + "/"))
        self.assertNotEqual(self.config.backend_image, "nexpoly-dev-backend:latest")
        self.config.require_source("a" * 40, "b" * 40)
        with self.assertRaises(cohort.CohortError):
            self.config.require_source("d" * 40, "b" * 40)

    def test_rejects_partial_profiles_main_project_and_unknown_schema(self):
        for mutation in (
            {"compose_project": "nexpoly_dev"}, {"compose_project": "nexpoly"},
            {"schema_version": True}, {"extra": "unsupported"},
            {"frontend_port": True}, {"backend_port": 36965},
            {"cohort_id": "../other"}, {"source_sha": "bad"},
            {"md_resources": {"cpu_quota_percent": 100}},
            {"dft_resources": dict(self.value["dft_resources"], tasks_max=True)},
        ):
            with self.subTest(mutation=mutation), self.assertRaises(cohort.CohortError):
                self.load_value(dict(self.value, **mutation))

    def test_rejects_digest_replacement_symlink_hardlink_and_permissions(self):
        original = self.config.sha256
        self.path.chmod(0o640)
        with self.assertRaises(cohort.CohortError):
            cohort.DevGpuCohort.load(self.path, original, repo_root=self.root)
        self.path.chmod(0o600)
        sibling = self.path.parent / "linked.json"
        os.link(self.path, sibling)
        with self.assertRaises(cohort.CohortError):
            self.config.verify()
        sibling.unlink()
        target = self.path.parent / "target.json"
        self.path.rename(target)
        self.path.symlink_to(target)
        with self.assertRaises(OSError):
            cohort.DevGpuCohort.load(self.path, original, repo_root=self.root)
        self.path.unlink()
        target.rename(self.path)
        self.path.write_bytes(cohort.canonical_json(dict(self.value, backend_port=35700)))
        with self.assertRaises(cohort.CohortError):
            self.config.verify()

    def test_rejects_parent_link_and_path_outside_layout_before_read(self):
        original = self.path.parent
        renamed = original.with_name("other")
        original.rename(renamed)
        original.symlink_to(renamed, target_is_directory=True)
        with self.assertRaises(cohort.CohortError):
            self.config.verify()
        with patch.object(cohort, "private_bytes", side_effect=AssertionError("must not read outside")):
            with self.assertRaises(ValueError):
                cohort.DevGpuCohort.load(self.root / "outside.json", "d" * 64, repo_root=self.root)

    def test_auth_file_only_metadata_is_validated(self):
        auth = self.path.parent / "backend.auth.env"
        auth.write_text("UNRELATED_SENTINEL=metadata-only\n")
        auth.chmod(0o600)
        original = cohort.private_bytes
        def guarded(path, **kwargs):
            self.assertNotEqual(path, auth, "auth content must not be read")
            return original(path, **kwargs)
        with patch.object(cohort, "private_bytes", side_effect=guarded):
            config = self.load_value(dict(self.value, auth_env_file=str(auth)))
        self.assertEqual(config.auth_env_file, auth)
        auth.chmod(0o644)
        with self.assertRaises(cohort.CohortError):
            self.load_value(dict(self.value, auth_env_file=str(auth)))

    def test_prepare_is_private_and_refuses_replaced_compose(self):
        self.config.prepare()
        self.config.prepare()
        self.config.require_prepared()
        for name in ("md-socket", "dft-runs", "home", "tmp"):
            self.assertEqual(self.config.path_for(name).stat().st_mode & 0o777, 0o700)
        compose = self.config.path_for("cohort-compose.yml")
        self.assertEqual(compose.stat().st_mode & 0o777, 0o600)
        self.assertIn(b"name: nexpoly_dev_default", compose.read_bytes())
        compose.write_bytes(b"services: {}\n")
        with self.assertRaises(cohort.CohortError):
            self.config.prepare()

    def test_reservation_rebind_changes_only_exact_backend_dev_project(self):
        source = dict(schema_version=1, blocked_gpu_uuids={"gpu3": "retained"},
                      managed_docker_claims={"backend-dev": dict(component="backend", environment="dev",
                          compose_project="nexpoly_dev", compose_service="backend",
                          gpu_uuids=["GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771"]),
                          "backend-prod": {"preserved": True}})
        expected = copy.deepcopy(source)
        expected["managed_docker_claims"]["backend-dev"]["compose_project"] = self.config.compose_project
        self.assertEqual(json.loads(self.config.reservations(cohort.canonical_json(source))), expected)
        source["managed_docker_claims"]["backend-dev"]["gpu_uuids"] = ["gpu2"]
        with self.assertRaises(cohort.CohortError):
            self.config.reservations(cohort.canonical_json(source))

    def test_write_once_never_replaces_another_binding(self):
        path = self.config.path_for("binding.json")
        cohort.write_once(path, {"pid": 1})
        cohort.write_once(path, {"pid": 1})
        with self.assertRaises(cohort.CohortError):
            cohort.write_once(path, {"pid": 2})
        self.assertEqual(cohort.private_json(path), {"pid": 1})
        self.assertEqual(path.stat().st_nlink, 1)

    def test_backend_binding_requires_exact_project_session_image_and_hash(self):
        identifier, digest = "d" * 64, "e" * 64
        labels = {"com.docker.compose.project": self.config.compose_project,
                  "com.docker.compose.service": "backend", "com.nexpoly.gpu.registration": "backend-dev",
                  "com.nexpoly.gpu.component": "backend", "com.nexpoly.gpu.environment": "dev",
                  "com.nexpoly.gpu.session-id": self.session, "com.nexpoly.dev.config-hash": digest,
                  "com.nexpoly.dev.cohort-operation-id": "9" * 32,
                  "com.nexpoly.dev.cohort-sha256": self.config.sha256}
        identity = dict(id=identifier, image="sha256:" + "f" * 64, labels=labels)
        record = cohort.backend_binding(self.config, self.session, identifier, digest, identity)
        self.assertEqual(record["container_id"], identifier)
        for key in labels:
            changed = copy.deepcopy(identity)
            changed["labels"][key] = "another-authority"
            with self.subTest(key=key), self.assertRaises(cohort.CohortError):
                cohort.backend_binding(self.config, self.session, identifier, digest, changed)
        with self.assertRaises(cohort.CohortError):
            cohort.backend_binding(self.config, self.session, "a" * 64, digest, identity)

    def test_dft_full_argv_and_start_are_not_prefix_authority(self):
        runner = str(self.root / "workers/monomer_dft_worker/run_host_worker.sh")
        for observed in ((12, ["bash", runner, "extra"]), (13, ["bash", runner]),
                         (12, ["bash", "/different/runner.sh"])):
            with patch.object(cohort, "process_identity", return_value=observed), self.assertRaises(cohort.CohortError):
                cohort.dft_binding(self.config, self.session, 123, 12)

    def test_same_content_inode_replacement_is_rejected(self):
        replacement = self.path.with_name("replacement.json")
        replacement.write_bytes(self.path.read_bytes())
        replacement.chmod(0o600)
        replacement.replace(self.path)
        with self.assertRaises(cohort.CohortError):
            self.config.verify()

    def test_different_sessions_have_distinct_immutable_backend_records(self):
        first = self.config.backend_record_path(self.session)
        second = self.config.backend_record_path("f" * 32)
        self.assertNotEqual(first, second)

    def test_recreate_refuses_same_project_replacement_before_any_mutation(self):
        state = dict(schema_version=1, cohort_sha256=self.config.sha256,
                     cohort_device=self.config.device, cohort_inode=self.config.inode,
                     source_sha=self.config.source_sha, source_tree=self.config.source_tree,
                     container_id="d" * 64, image_id="sha256:" + "e" * 64,
                     config_hash="f" * 64, kind="gpu", session_id=self.session, operation_id="9" * 32)
        cohort.write_once(self.config.path_for("backend-service.json"), state)
        replacement = dict(id="b" * 64, image=state["image_id"],
                           labels={"com.nexpoly.dev.cohort-sha256": self.config.sha256,
                                   "com.nexpoly.dev.cohort-operation-id": state["operation_id"],
                                   "com.nexpoly.dev.config-hash": state["config_hash"]})
        with patch.object(cohort, "backend_service_ids", return_value={"b" * 64}), \
                patch.object(cohort, "docker_identity", return_value=replacement), \
                patch.object(cohort.subprocess, "run") as command:
            with self.assertRaises(cohort.CohortError):
                cohort.recreate_backend(self.config, kind="cpu", config_hash="a" * 64, session_id=self.session)
            command.assert_not_called()

    def test_recreate_refuses_another_active_session(self):
        state = dict(schema_version=1, cohort_sha256=self.config.sha256,
                     cohort_device=self.config.device, cohort_inode=self.config.inode,
                     source_sha=self.config.source_sha, source_tree=self.config.source_tree,
                     container_id="d" * 64, image_id="sha256:" + "e" * 64,
                     config_hash="f" * 64, kind="gpu", session_id=self.session, operation_id="9" * 32)
        cohort.write_once(self.config.path_for("backend-service.json"), state)
        with patch.object(cohort.subprocess, "run") as command, self.assertRaises(cohort.CohortError):
            cohort.recreate_backend(self.config, kind="cpu", config_hash="a" * 64, session_id="b" * 32)
        command.assert_not_called()

    def seed_cpu_snapshot(self):
        service = dict(image=self.config.backend_image,
                       container_name=self.config.compose_project + "-backend",
                       command=["uvicorn", "app.main:app"],
                       volumes=[dict(type="bind", source=str(self.config.path_for(name)), target=target,
                                     read_only=ro, bind={"create_host_path": False})
                                for name, target, ro in self.config.runtime_mounts()],
                       environment={"PUBLIC_SENTINEL": "$literal"},
                       labels={"com.nexpoly.dev.cohort-sha256": self.config.sha256,
                               "com.nexpoly.dev.config-hash": "uncomputed",
                               "com.nexpoly.dev.cohort-operation-id": "unprepared"})
        normalized = copy.deepcopy(service)
        normalized["labels"].pop("com.nexpoly.dev.config-hash")
        normalized["labels"].pop("com.nexpoly.dev.cohort-operation-id")
        digest = hashlib.sha256(json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        record = dict(schema_version=1, cohort_sha256=self.config.sha256,
                      cohort_device=self.config.device, cohort_inode=self.config.inode,
                      source_sha=self.config.source_sha, source_tree=self.config.source_tree,
                      image_id="sha256:" + "f" * 64, config_hash=digest,
                      dependency_lock="sha256:" + "1" * 64, build_config="sha256:" + "2" * 64,
                      md_lock="sha256:" + "3" * 64, dft_lock="sha256:" + "4" * 64,
                      runtime_environment={"NEXPOLY_ASSET_ROOT": "/public-mock/assets", "BYTEFF2_ROOT": "/public-mock/byteff"},
                      compose={"services": {"backend": service}})
        cohort.write_once(self.config.path_for("backend-cpu-snapshot.json"), record)
        return record

    def native_mock(self, *, fail_start=False):
        live, events = {}, []
        gpu_hash = "e" * 64
        def run(command, **kwargs):
            command = tuple(command)
            events.append(command)
            if command[:2] == ("docker", "compose"):
                self.assertIn("--no-recreate", command)
                self.assertIn("--pull", command)
                self.assertNotIn("--force-recreate", command)
                native_env = kwargs["env"]
                operation = native_env["NEXPOLY_DEV_GPU_COHORT_OPERATION_ID"]
                identifier = operation * 2
                gpu = any(item.endswith("docker-compose.dev-gpu-session.yml") for item in command)
                digest = gpu_hash
                if not gpu:
                    self.assertNotIn("--env-file", command)
                    config_path = Path(command[command.index("-f") + 1])
                    service = cohort.private_json(config_path)["services"]["backend"]
                    self.assertEqual(service["image"], "sha256:" + "f" * 64)
                    self.assertNotIn("build", service)
                    self.assertNotIn("depends_on", service)
                    self.assertEqual(service["environment"]["PUBLIC_SENTINEL"], "$$literal")
                    self.assertFalse(any(v.get("target") == "/app/backend" for v in service["volumes"]))
                    digest = service["labels"]["com.nexpoly.dev.config-hash"]
                    self.assertEqual(service["labels"]["com.nexpoly.dev.cohort-operation-id"], operation)
                labels = {"com.docker.compose.project": self.config.compose_project,
                          "com.docker.compose.service": "backend",
                          "com.nexpoly.dev.cohort-sha256": self.config.sha256,
                          "com.nexpoly.dev.cohort-operation-id": operation,
                          "com.nexpoly.dev.config-hash": digest}
                if gpu:
                    labels.update({"com.nexpoly.gpu.registration": "backend-dev",
                                   "com.nexpoly.gpu.component": "backend",
                                   "com.nexpoly.gpu.environment": "dev",
                                   "com.nexpoly.gpu.session-id": self.session})
                live[identifier] = dict(id=identifier, image="sha256:" + "f" * 64,
                                        name="/" + self.config.compose_project + "-backend",
                                        devices=["gpu1"] if gpu else [], running=False, labels=labels)
            elif command[:2] == ("docker", "start"):
                identity = live[command[2]]
                if identity["devices"]:
                    early = cohort.private_json(self.config.backend_record_path(self.session))
                    self.assertEqual(early["container_id"], command[2])
                self.assertEqual(cohort.private_json(self.config.path_for("backend-service.json"))["container_id"], command[2])
                if fail_start:
                    raise subprocess.CalledProcessError(1, command)
                identity["running"] = True
            elif command[:2] == ("docker", "stop"):
                live[command[2]]["running"] = False
            elif command[:3] == ("docker", "rm", "--force"):
                del live[command[3]]
            else:
                self.fail("unexpected native command: " + repr(command))
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        self.enterContext(patch.object(cohort, "backend_service_ids", side_effect=lambda _: set(live)))
        self.enterContext(patch.object(cohort, "docker_identity", side_effect=lambda i: copy.deepcopy(live.get(i))))
        self.enterContext(patch.object(cohort.subprocess, "run", side_effect=run))
        return live, events, gpu_hash

    def test_exact_bind_precedes_start_and_old_sha_cpu_restore_uses_only_frozen_config(self):
        frozen = self.seed_cpu_snapshot()
        live, events, gpu_hash = self.native_mock()
        identifier = cohort.recreate_backend(self.config, kind="gpu", config_hash=gpu_hash, session_id=self.session)
        self.assertTrue(live[identifier]["running"])
        cohort.stop_backend(self.config, self.session)
        # Pretend the checkout advanced from A to B: recovery may neither call
        # git nor render current Compose files. The mock rejects both commands.
        restored = cohort.recreate_backend(self.config, kind="cpu", config_hash=frozen["config_hash"], session_id=self.session)
        self.assertNotEqual(restored, identifier)
        self.assertEqual(set(live), {restored})
        self.assertEqual(cohort.verify_cpu_backend(self.config), restored)
        self.assertEqual(cohort.cpu_environment(self.config)["CURRENT_SOURCE_REVISION"], "a" * 40)
        self.assertIn(("docker", "rm", "--force", identifier), events)

    def test_stop_uses_lifecycle_lock_and_does_not_race_create(self):
        fd = os.open(self.config.path_for("backend-lifecycle.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(cohort.subprocess, "run") as command:
                with self.assertRaisesRegex(cohort.CohortError, "in progress"):
                    cohort.stop_backend(self.config, self.session)
                command.assert_not_called()
        finally:
            os.close(fd)

    def test_failed_start_is_collected_only_after_exact_writer_exits(self):
        self.seed_cpu_snapshot()
        live, events, gpu_hash = self.native_mock(fail_start=True)
        with self.assertRaises(subprocess.CalledProcessError):
            cohort.recreate_backend(self.config, kind="gpu", config_hash=gpu_hash, session_id=self.session)
        identifier = next(iter(live))
        with self.assertRaisesRegex(cohort.CohortError, "writer is still active"):
            cohort.collect_pending_backend(self.config, self.session)
        with patch.object(cohort, "process_identity", side_effect=FileNotFoundError):
            cohort.collect_pending_backend(self.config, self.session)
        self.assertFalse(live)
        self.assertIn(("docker", "rm", "--force", identifier), events)
        self.assertFalse(self.config.path_for("backend-pending.json").exists())

    def test_maintenance_stop_requires_full_exit_then_allows_new_baseline(self):
        frozen = self.seed_cpu_snapshot()
        live, events, gpu_hash = self.native_mock()
        identifier = cohort.recreate_backend(self.config, kind="gpu", config_hash=gpu_hash, session_id=self.session)
        cohort.stop_backend(self.config, self.session)
        blocker = self.config.path_for("gpu-session/controller.json")
        blocker.parent.mkdir(mode=0o700, exist_ok=True)
        blocker.write_text("public mock controller sentinel")
        with self.assertRaisesRegex(cohort.CohortError, "retains"):
            cohort.retire_backend(self.config, self.session)
        blocker.unlink()
        cohort.retire_backend(self.config, self.session)
        self.assertEqual(cohort.private_json(self.config.path_for("backend-service.json"))["kind"], "retired")
        new_id = cohort.recreate_backend(self.config, kind="cpu", config_hash=frozen["config_hash"], session_id=None)
        self.assertNotEqual(new_id, identifier)
        self.assertEqual(cohort.verify_cpu_backend(self.config), new_id)

    def test_retirement_retries_after_transient_lifecycle_lock_failure(self):
        self.seed_cpu_snapshot()
        live, events, gpu_hash = self.native_mock()
        cohort.recreate_backend(self.config, kind="gpu", config_hash=gpu_hash, session_id=self.session)
        cohort.stop_backend(self.config, self.session)
        fd = os.open(self.config.path_for("backend-lifecycle.lock"), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(cohort.CohortError, "in progress"):
                cohort.retire_backend(self.config, self.session)
        finally:
            os.close(fd)
        cohort.retire_backend(self.config, None)
        self.assertEqual(cohort.private_json(self.config.path_for("backend-service.json"))["kind"], "retired")

    def test_missing_or_linked_runtime_mount_is_rejected_before_docker(self):
        self.config.path_for("dft-spool").rmdir()
        with patch.object(cohort.subprocess, "run") as command:
            with self.assertRaises(FileNotFoundError):
                cohort.recreate_backend(self.config, kind="gpu", config_hash="e"*64, session_id=self.session)
            command.assert_not_called()
        self.config.path_for("dft-spool").symlink_to(self.config.path_for("md-runs"), target_is_directory=True)
        with self.assertRaises(cohort.CohortError):
            self.config.require_prepared()

    def test_maintenance_refuses_malformed_or_foreign_CPU_pointer(self):
        frozen = self.seed_cpu_snapshot()
        path = self.config.path_for("backend-service.json")
        foreign = dict(schema_version=1, cohort_sha256=self.config.sha256,
                       cohort_device=self.config.device, cohort_inode=self.config.inode,
                       source_sha="d"*40, source_tree=self.config.source_tree, kind="cpu",
                       image_id=frozen["image_id"], config_hash=frozen["config_hash"],
                       container_id="d"*64, session_id=None, operation_id="9"*32)
        for value in ({"kind": "cpu"}, foreign):
            path.write_bytes(cohort.canonical_json(value))
            path.chmod(0o600)
            with patch.object(cohort.subprocess, "run") as command:
                with self.assertRaises(cohort.CohortError):
                    cohort.retire_backend(self.config, None)
                command.assert_not_called()

    def test_metadata_without_config_is_rejected_by_controller_and_shell_entries(self):
        from scripts import dev_gpu_session as session
        with self.assertRaises(session.DevGpuSessionError):
            session._select_cohort(None, None, (1, 2))
        scripts = Path(__file__).resolve().parents[1]
        for file, function in (("dev_server_gpu.sh", "apply_native_cohort_environment"),
                               ("monomer_dft_worker_ctl.sh", "apply_native_cohort_paths")):
            source = (scripts / file).read_text()
            body = source.split(function + "() {", 1)[1].split("\n}", 1)[0]
            program = 'set -e; NATIVE_COHORT_CONFIG_INPUT=; NATIVE_COHORT_SHA256_INPUT=; NATIVE_COHORT_DEVICE_INPUT=1; NATIVE_COHORT_INODE_INPUT=2; fail(){ exit 2; }; '+function+'() {'+body+'\n}; '+function
            result = subprocess.run(("bash", "-c", program), capture_output=True)
            self.assertNotEqual(result.returncode, 0, file)

    def test_invalid_runtime_config_keeps_controller_isolated_without_recovery_command(self):
        from scripts import dev_gpu_session as session
        controller = object.__new__(session.SessionController)
        controller._cohort = Mock()
        controller._cohort.verify.side_effect = cohort.CohortError("replaced inode")
        controller.session_id = self.session
        controller.direct_start = False
        controller._state = Mock()
        with patch.object(session.subprocess, "run") as command:
            self.assertFalse(controller._recovery_command("gpu-session-stop-owned-internal"))
            command.assert_not_called()
        self.assertEqual(controller._state.call_args.args[0], "isolation-waiting")

    def test_shell_recovery_keeps_A_identity_when_HEAD_advances_to_B(self):
        self.seed_cpu_snapshot()
        scripts = Path(__file__).resolve().parents[1]
        source = (scripts / "dev_server_gpu.sh").read_text()
        block = source.split(': "${NEXPOLY_ASSET_ROOT:', 1)[1]
        block = ': "${NEXPOLY_ASSET_ROOT:' + block.split("\nDEV_COMPOSE_FILES=", 1)[0]
        assignments = "\n".join("export " + key + "=" + value for key, value in cohort.cpu_environment(self.config).items())
        program = ('set -eu; NEXPOLY_ASSET_ROOT=/public-mock; NATIVE_COHORT_CONFIG_INPUT=/public-mock/cohort.json; '
                   'COHORT_FROZEN_SOURCE_MODE=true; NEXPOLY_DEV_USER_ISOLATION_ENABLED=false; '
                   'NEXPOLY_DEV_GPU_COHORT_COMPOSE_FILE=/public-mock/compose.yml; '
                   'NEXPOLY_BUILD_REVISION=' + 'a'*40 + '; '
                   'git(){ printf "%s\\n" ' + 'd'*40 + '; }; sha256sum(){ printf "%s\\n" ' + 'e'*64 + '; }; '
                   'apply_frozen_cpu_environment(){\n' + assignments + '\n};\n' + block +
                   '\n[[ "$CURRENT_SOURCE_REVISION" == ' + 'a'*40 + ' && "$CURRENT_SOURCE_TREE" == ' + 'b'*40 +
                   ' && "$NEXPOLY_BUILD_REVISION" == ' + 'a'*40 + ' ]]')
        result = subprocess.run(("bash", "-c", program), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

        worker_block = source.split('WORKER_ENABLED="', 1)[1].split('\nassert_dev_runtime_path()', 1)[0]
        worker_block = 'WORKER_ENABLED="' + worker_block
        program = ('set -eu; ROOT_DIR=/public-mock; COHORT_FROZEN_SOURCE_MODE=true; BYTEFF2_ROOT=/public-mock/assets; '
                   'CURRENT_SOURCE_REVISION=' + 'a'*40 + '; CURRENT_SOURCE_TREE=' + 'b'*40 + ';\n' + assignments +
                   '\nsha256sum(){ echo "must not read HEAD B locks" >&2; return 99; };\n' + worker_block +
                   '\n[[ "$WORKER_LOCK_SHA256" == sha256:' + '3'*64 + ' && "$DFT_WORKER_LOCK_SHA256" == sha256:' + '4'*64 +
                   ' && "$DFT_WORKER_VERSION" == dev:' + 'a'*40 + ':' + 'b'*40 + ':sha256:' + '4'*64 + ' ]]')
        result = subprocess.run(("bash", "-c", program), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cohort_DFT_stop_does_not_load_changed_checkout_dotenv(self):
        scripts = Path(__file__).resolve().parents[1]
        source = (scripts / "monomer_dft_worker_ctl.sh").read_text()
        main = source.split("main() {", 1)[1].split('\nif [[ "${BASH_SOURCE[0]}"', 1)[0]
        lock = self.root / "public-mock-ctl.lock"
        program = ('set -eu; NATIVE_COHORT_CONFIG_INPUT=/public-mock/config; LOCK_FILE=' + str(lock) + '; '
                   'STOPPED=no; apply_native_cohort_paths(){ :; }; initialize_runtime_root(){ :; }; '
                   'assert_safe_runtime_file(){ :; }; flock(){ :; }; load_env(){ exit 87; }; '
                   'configure_paths(){ :; }; stop_worker(){ STOPPED=yes; }; cohort_control(){ :; }; '
                   'main(){' + main + '\nmain stop; [[ "$STOPPED" == yes ]]')
        result = subprocess.run(("bash", "-c", program), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
