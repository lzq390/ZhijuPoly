"""Pure safety-boundary tests for the development MCP gateway transaction.

All filesystem writes are confined to TemporaryDirectory fixtures. Docker,
network calls, and publication entry points are replaced before invocation.
"""
from __future__ import annotations

import copy
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from scripts import property_filter_mcp_gateway_dev as gateway


class GatewayDevelopmentTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.addCleanup(self.make_writable)
        for name, value in (("ROOT", self.root / "checkout"),
                            ("STATE", self.root / "state"),
                            ("TRUSTED_ROOT", self.root / "releases")):
            patcher = mock.patch.object(gateway, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        forbidden = mock.patch.object(gateway.subprocess, "run", side_effect=AssertionError("Unexpected subprocess"))
        self.subprocess = forbidden.start()
        self.addCleanup(forbidden.stop)
        network = mock.patch.object(gateway.urllib.request.OpenerDirector, "open", side_effect=AssertionError("Unexpected network request"))
        network.start()
        self.addCleanup(network.stop)

    def make_writable(self) -> None:
        for path in sorted(self.root.rglob("*"), key=lambda item: len(item.parts)):
            if not path.is_symlink():
                path.chmod(0o700 if path.is_dir() else 0o600)

    def write_file(self, relative: str, payload: bytes = b"fixture\n", *, mode: int = 0o600) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        path.chmod(mode)
        return path

    def baseline(self) -> tuple[dict, dict]:
        release = gateway.TRUSTED_ROOT / "baseline"
        source = release / "source"
        for name in ("frontend/vite.config.ts", "frontend/build/existing.ts", "frontend/package.json",
                     "contracts/schema.json", "backend/tests/fixtures/example.json", "polymer_md/catalog.json"):
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("baseline " + name + "\n")
        manifest = gateway.inventory(source, sealed=False)
        source_hash = gateway.digest(gateway.canonical(manifest))
        for path in [source, *source.rglob("*")]:
            path.chmod(0o555 if path.is_dir() else 0o444)
        image = "sha256:" + "a" * 64
        labels = {gateway.OVERLAY + "sha256": source_hash,
                  gateway.OVERLAY + "revision": "b" * 40,
                  "arbitrary-preserved-label": "preserved"}
        service = {"image": image, "working_dir": "/app/frontend",
                   "command": ["sh", "-lc", "npm run dev -- --host 0.0.0.0"],
                   "environment": {"DEV_PROXY_TARGET": "http://backend-dev:8000", "SECRET_FIXTURE": "never-display-this-secret"},
                   "labels": labels, "networks": ["default"],
                   "ports": [{"host_ip": "0.0.0.0", "published": "9001", "target": 5173, "protocol": "tcp"}],
                   "volumes": [{"type": "bind", "source": str(source / relative), "target": target,
                                "read_only": True, "bind": {"create_host_path": False}}
                               for target, relative in gateway.SOURCE_MOUNTS.items()] + [
                                   {"type": "volume", "source": "frontend-deps", "target": "/app/frontend/node_modules"}],
                   "restart": "unless-stopped", "init": True}
        document = {"services": {gateway.SERVICE: service},
                    "networks": {"default": {"external": True, "name": gateway.NETWORK}},
                    "volumes": {"frontend-deps": {"external": True, "name": "live-frontend-deps"}}}
        promote = release / "promote.compose.private.json"
        gateway.write_private(promote, gateway.canonical(document) + b"\n")
        gateway.write_private(release / "source.sha256.json", gateway.canonical(manifest) + b"\n")
        info = {"Id": "live-container-id", "Name": "/" + gateway.LIVE, "Image": image,
                "State": {"Running": True},
                "Config": {"Image": image, "WorkingDir": service["working_dir"], "Cmd": service["command"],
                           "Env": [key + "=" + value for key, value in service["environment"].items()] + ["PATH=/usr/bin"],
                           "Labels": {**labels, "com.docker.compose.project": gateway.PROJECT,
                                      "com.docker.compose.service": gateway.SERVICE,
                                      "com.docker.compose.project.config_files": str(promote),
                                      "com.docker.compose.project.working_dir": str(release)}},
                "NetworkSettings": {"Networks": {gateway.NETWORK: {}},
                                    "Ports": {"5173/tcp": [{"HostIp": "0.0.0.0", "HostPort": "9001"}]}},
                "Mounts": [{"Type": "bind", "Source": str(source / relative), "Destination": target, "RW": False}
                           for target, relative in gateway.SOURCE_MOUNTS.items()] + [
                               {"Type": "volume", "Name": "live-frontend-deps", "Destination": "/app/frontend/node_modules", "RW": True}]}
        baseline = gateway.verify_live(info)
        return baseline, info

    def operation(self, baseline: dict) -> dict:
        operation = "1234567890abcdef"
        return {"operation": operation, "owner": str(gateway.STATE), "phase": "promoted", "baseline": baseline,
                "candidate_project": "nexpoly_mcp_gateway_" + operation,
                "candidate_volume": "nexpoly_mcp_gateway_" + operation + "_deps",
                "protected_ids": ["backend-id", "database-id"],
                "http_baseline": {"/": {"status": 200, "content_type": "text/html"}},
                "promote_path": str(gateway.TRUSTED_ROOT / "new-release/promote.compose.private.json"),
                "promote_sha256": "c" * 64, "source_hash": "d" * 64, "promoted_id": "promoted-container-id"}

    def rollback_backup(self, record: dict) -> Path:
        path = gateway.STATE / "operations" / record["operation"] / "rollback.compose.private.json"
        gateway.write_private(path, gateway.read_private(Path(record["baseline"]["promote_path"])))
        return path

    def test_complete_sealed_manifest_accepts_fixture_and_rejects_path_traversal(self) -> None:
        baseline, _ = self.baseline()
        source = Path(baseline["release"]) / "source"
        self.assertEqual(gateway.validate_manifest(source, baseline["manifest"], baseline["source_hash"]), baseline["manifest"])
        for name in ("../outside", "/absolute", "frontend/../outside", "frontend//vite.config.ts", "frontend/./vite.config.ts", "frontend\\outside"):
            manifest = {name: "c" * 64}
            with self.subTest(name=name), self.assertRaises(gateway.GatewayError):
                gateway.validate_manifest(source, manifest, gateway.digest(gateway.canonical(manifest)))

    def test_manifest_rejects_changed_hash_missing_and_unlisted_files(self) -> None:
        baseline, _ = self.baseline()
        source = Path(baseline["release"]) / "source"
        with self.assertRaises(gateway.GatewayError):
            gateway.validate_manifest(source, baseline["manifest"], "0" * 64)
        for name, manifest in (("changed", {**baseline["manifest"], "frontend/vite.config.ts": "0" * 64}),
                               ("missing", {key: value for key, value in baseline["manifest"].items() if key != "frontend/vite.config.ts"}),
                               ("unlisted", {**baseline["manifest"], "unlisted": "d" * 64})):
            with self.subTest(name=name), self.assertRaises(gateway.GatewayError):
                gateway.validate_manifest(source, manifest, gateway.digest(gateway.canonical(manifest)))

    def test_inventory_rejects_writable_symlink_special_and_foreign_owned_source(self) -> None:
        baseline, _ = self.baseline()
        source = Path(baseline["release"]) / "source"
        path = source / "frontend/vite.config.ts"
        path.chmod(0o644)
        with self.assertRaises(gateway.GatewayError):
            gateway.inventory(source)
        path.chmod(0o444)
        source.chmod(0o755)
        link = source / "alias"
        link.symlink_to(path)
        source.chmod(0o555)
        with self.assertRaises(gateway.GatewayError):
            gateway.inventory(source)
        source.chmod(0o755)
        link.unlink()
        fifo = source / "pipe"
        os.mkfifo(fifo, 0o400)
        source.chmod(0o555)
        with self.assertRaises(gateway.GatewayError):
            gateway.inventory(source)
        source.chmod(0o755)
        fifo.unlink()
        source.chmod(0o555)
        with mock.patch.object(gateway.os, "getuid", return_value=os.getuid() + 1), self.assertRaises(gateway.GatewayError):
            gateway.inventory(source)

    def test_source_root_and_ancestor_symlinks_are_rejected(self) -> None:
        baseline, _ = self.baseline()
        alias = self.root / "release-alias"
        alias.symlink_to(Path(baseline["release"]), target_is_directory=True)
        with self.assertRaises(gateway.GatewayError):
            gateway.inventory(alias / "source")
        with self.assertRaises(gateway.GatewayError):
            gateway.read_private(alias / "source.sha256.json")

    def test_private_files_require_owner_mode_and_refuse_symlink_replacement(self) -> None:
        path = self.write_file("private/config", b"secret")
        self.assertEqual(gateway.read_private(path), b"secret")
        for mode in (0o640, 0o644, 0o400):
            path.chmod(mode)
            with self.subTest(mode=oct(mode)), self.assertRaises(gateway.GatewayError):
                gateway.read_private(path)
        path.chmod(0o600)
        with mock.patch.object(gateway.os, "getuid", return_value=os.getuid() + 1), self.assertRaises(gateway.GatewayError):
            gateway.read_private(path)
        link = path.parent / "link"
        link.symlink_to(path)
        with self.assertRaises(gateway.GatewayError):
            gateway.write_private(link, b"replacement")
        self.assertEqual(path.read_bytes(), b"secret")

    def test_private_directory_has_owner_only_mode_and_refuses_symlink_ancestry(self) -> None:
        directory = self.root / "private/deep"
        gateway.private_directory(directory)
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        alias = self.root / "alias"
        alias.symlink_to(directory.parent, target_is_directory=True)
        with self.assertRaises(gateway.GatewayError):
            gateway.private_directory(alias / "new")

    def test_live_fixture_identity_and_foreign_container_rejection(self) -> None:
        baseline, info = self.baseline()
        self.assertEqual(baseline["container_id"], info["Id"])
        for field in ("name", "running", "project", "service", "working_dir", "compose_path", "network"):
            changed = copy.deepcopy(info)
            if field == "name":
                changed["Name"] = "/foreign"
            elif field == "running":
                changed["State"]["Running"] = False
            elif field == "network":
                changed["NetworkSettings"]["Networks"]["foreign-network"] = {}
            else:
                key = {"project": "project", "service": "service", "working_dir": "project.working_dir", "compose_path": "project.config_files"}[field]
                changed["Config"]["Labels"]["com.docker.compose." + key] = "/foreign"
            with self.subTest(field=field), self.assertRaises(gateway.GatewayError):
                gateway.verify_live(changed)

    def test_live_port_mount_environment_command_and_label_drift_are_rejected(self) -> None:
        _, info = self.baseline()
        mutations = {
            "port": lambda value: value["NetworkSettings"]["Ports"]["5173/tcp"][0].update(HostPort="9000"),
            "bind": lambda value: value["NetworkSettings"]["Ports"]["5173/tcp"][0].update(HostIp="127.0.0.1"),
            "writable_mount": lambda value: value["Mounts"][0].update(RW=True),
            "foreign_mount": lambda value: value["Mounts"][0].update(Source="/foreign"),
            "extra_mount": lambda value: value["Mounts"].append({"Destination": "/extra"}),
            "deps": lambda value: value["Mounts"][-1].update(Name="foreign-deps"),
            "env_value": lambda value: value["Config"]["Env"].append("DEV_PROXY_TARGET=http://foreign"),
            "env_unrecorded": lambda value: value["Config"]["Env"].append("DEV_MCP_PROXY_TARGET=http://foreign"),
            "vite_unrecorded": lambda value: value["Config"]["Env"].append("VITE_FOREIGN=private"),
            "command": lambda value: value["Config"].update(Cmd=["other-command"]),
            "label": lambda value: value["Config"]["Labels"].update({gateway.OVERLAY + "revision": "foreign"}),
        }
        for name, mutate in mutations.items():
            changed = copy.deepcopy(info)
            mutate(changed)
            with self.subTest(name=name), self.assertRaises(gateway.GatewayError) as raised:
                gateway.verify_live(changed)
            self.assertNotIn("never-display-this-secret", str(raised.exception))

    def test_live_promote_rejects_extra_service_and_private_config_modes(self) -> None:
        baseline, info = self.baseline()
        path = Path(baseline["promote_path"])
        document = copy.deepcopy(baseline["document"])
        document["services"]["backend-dev"] = {}
        gateway.write_private(path, gateway.canonical(document))
        with self.assertRaises(gateway.GatewayError):
            gateway.verify_live(info)
        gateway.write_private(path, gateway.canonical(baseline["document"]))
        path.chmod(0o640)
        with self.assertRaises(gateway.GatewayError):
            gateway.verify_live(info)

    def test_patch_changes_only_proxy_settings_source_prefix_and_overlay_labels(self) -> None:
        baseline, _ = self.baseline()
        before = copy.deepcopy(baseline)
        source = self.root / "new-source"
        patched = gateway.patch_document(baseline, source, "c" * 64, "d" * 40)
        self.assertEqual(baseline, before)
        expected = copy.deepcopy(baseline["document"])
        service = expected["services"][gateway.SERVICE]
        service["environment"].update(DEV_MCP_PROXY_TARGET=gateway.TARGET, DEV_MCP_PROXY_ALLOWED_HOSTS=gateway.ALLOWED_HOSTS)
        service["labels"].update({gateway.OVERLAY + "baseline": "baseline", gateway.OVERLAY + "revision": "d" * 40,
                                  gateway.OVERLAY + "sha256": "c" * 64})
        for mount in service["volumes"]:
            if mount["target"] in gateway.SOURCE_MOUNTS:
                mount["source"] = str(source / gateway.SOURCE_MOUNTS[mount["target"]])
        self.assertEqual(patched, expected)

    def test_candidate_uses_loopback_independent_port_and_dependency_volume(self) -> None:
        baseline, _ = self.baseline()
        document = baseline["document"]
        before = copy.deepcopy(document)
        candidate = gateway.build_candidate_compose(document, "candidate-unique-deps", "op-123")
        self.assertEqual(document, before)
        service = candidate["services"][gateway.SERVICE]
        self.assertEqual(set(candidate["services"]), {gateway.SERVICE})
        self.assertEqual(service["ports"], [{"host_ip": "127.0.0.1", "published": str(gateway.CANDIDATE_PORT), "target": 5173, "protocol": "tcp"}])
        self.assertNotEqual(gateway.CANDIDATE_PORT, 9001)
        self.assertEqual(candidate["volumes"]["frontend-deps"], {"external": True, "name": "candidate-unique-deps"})
        self.assertEqual(service["labels"][gateway.OWNER_LABEL], str(gateway.STATE))
        self.assertEqual(service["labels"][gateway.OP_LABEL], "op-123")
        self.assertEqual(service["environment"], {**document["services"][gateway.SERVICE]["environment"],
                         "DEV_MCP_PROXY_ALLOWED_HOSTS": gateway.ALLOWED_HOSTS + f",127.0.0.1:{gateway.CANDIDATE_PORT}"})

    def test_source_preparation_copies_independent_files_and_changes_exactly_two_paths(self) -> None:
        baseline, _ = self.baseline()
        before = copy.deepcopy(baseline)
        checkout = gateway.ROOT
        patches = {gateway.PATCH_FILES[0]: b"reviewed Vite gateway patch\n",
                   gateway.PATCH_FILES[1]: b"reviewed standalone gateway module\n"}
        for relative, payload in patches.items():
            path = checkout / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        operation_dir = self.root / "operation"
        operation_dir.mkdir(mode=0o700)
        self.subprocess.side_effect = lambda command, **kwargs: subprocess.CompletedProcess(command, 0, stdout=patches[command[2].split(":", 1)[1]], stderr=b"")
        with mock.patch.object(gateway, "BASELINE_VITE_SHA256", baseline["manifest"][gateway.PATCH_FILES[0]]), \
                mock.patch.object(gateway, "run", return_value="f" * 40 + "\n"):
            release, document = gateway.prepare_source(baseline, operation_dir)
            retry_dir = self.root / "retry-operation"
            retry_dir.mkdir(mode=0o700)
            retry_release, retry_document = gateway.prepare_source(baseline, retry_dir)
        self.assertNotEqual(release, retry_release)
        self.assertEqual(gateway.inventory(release / "source"), gateway.inventory(retry_release / "source"))
        self.assertEqual(document["services"][gateway.SERVICE]["labels"][gateway.OVERLAY + "sha256"],
                         retry_document["services"][gateway.SERVICE]["labels"][gateway.OVERLAY + "sha256"])
        manifest = json.loads(gateway.read_private(release / "source.sha256.json"))
        changed = {name for name in set(manifest) | set(baseline["manifest"]) if manifest.get(name) != baseline["manifest"].get(name)}
        self.assertEqual(changed, set(gateway.PATCH_FILES))
        self.assertEqual(len(manifest), len(baseline["manifest"]) + 1)
        self.assertEqual(baseline, before)
        original_source = Path(baseline["release"]) / "source"
        self.assertEqual(gateway.inventory(original_source), baseline["manifest"])
        for name in baseline["manifest"]:
            self.assertNotEqual((original_source / name).stat().st_ino, (release / "source" / name).stat().st_ino)
        for name, payload in patches.items():
            self.assertEqual((release / "source" / name).read_bytes(), payload)
        self.assertEqual(gateway.inventory(release / "source"), manifest)
        self.assertEqual(document["services"][gateway.SERVICE]["environment"]["DEV_MCP_PROXY_TARGET"], gateway.TARGET)
        self.assertEqual(stat.S_IMODE((release / "promote.compose.private.json").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((release / "source.sha256.json").stat().st_mode), 0o600)

    def test_source_preparation_rejects_unknown_baseline_before_any_copy(self) -> None:
        baseline, _ = self.baseline()
        with self.assertRaises(gateway.GatewayError):
            gateway.prepare_source(baseline, self.root / "operation")
        self.subprocess.assert_not_called()
        self.assertFalse((self.root / "operation").exists())

    def test_source_preparation_rejects_patch_not_matching_recorded_commit(self) -> None:
        baseline, _ = self.baseline()
        for relative in gateway.PATCH_FILES:
            path = gateway.ROOT / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"uncommitted patch")
        self.subprocess.side_effect = None
        self.subprocess.return_value = subprocess.CompletedProcess([], 0, stdout=b"committed different patch", stderr=b"")
        with mock.patch.object(gateway, "BASELINE_VITE_SHA256", baseline["manifest"][gateway.PATCH_FILES[0]]), \
                mock.patch.object(gateway, "run", return_value="f" * 40 + "\n"), self.assertRaises(gateway.GatewayError):
            gateway.prepare_source(baseline, self.root / "operation")
        self.assertFalse((self.root / "operation").exists())

    def test_compose_explicitly_isolates_project_config_and_environment(self) -> None:
        config = self.root / "candidate/compose.private.json"
        with mock.patch.object(gateway, "run", return_value="") as run:
            gateway.compose(config, "isolated-project", "up", "-d", "--no-deps", "--force-recreate", gateway.SERVICE)
        run.assert_called_once_with(["docker", "compose", "--env-file", "/dev/null", "--project-directory", str(config.parent),
                                     "-p", "isolated-project", "-f", str(config), "up", "-d", "--no-deps", "--force-recreate", gateway.SERVICE])
        self.assertNotIn("--remove-orphans", run.call_args.args[0])

    def test_cas_requires_same_container_private_compose_and_source_identity(self) -> None:
        baseline, _ = self.baseline()
        with mock.patch.object(gateway, "verify_live", return_value=baseline):
            gateway.check_cas(baseline)
        for key in ("container_id", "promote_path", "promote_sha256", "source_hash"):
            changed = {**baseline, key: "changed"}
            with self.subTest(key=key), mock.patch.object(gateway, "verify_live", return_value=changed), self.assertRaises(gateway.GatewayError):
                gateway.check_cas(baseline)

    def test_protected_container_identity_changes_are_rejected(self) -> None:
        with mock.patch.object(gateway, "container_ids", return_value={"frontend", "backend", "database"}):
            self.assertEqual(gateway.protected_ids("frontend"), ["backend", "database"])
            gateway.check_protected({"protected_ids": ["backend", "database"]})
        with mock.patch.object(gateway, "container_ids", return_value={"backend", "new-database"}), self.assertRaises(gateway.GatewayError):
            gateway.check_protected({"protected_ids": ["backend", "database"]})

    def test_subprocess_suppresses_raw_private_diagnostics_and_compose_selectors(self) -> None:
        secret = "DO-NOT-PRINT-PRIVATE-TOKEN"
        self.subprocess.side_effect = None
        self.subprocess.return_value = subprocess.CompletedProcess([], 1, stdout=secret, stderr=secret)
        with mock.patch.dict(os.environ, {"COMPOSE_FILE": "/foreign/private", "COMPOSE_PROJECT_NAME": "foreign", "PATH": "/usr/bin"}), self.assertRaises(gateway.GatewayError) as raised:
            gateway.run(["docker", "inspect", "fixture"])
        self.assertNotIn(secret, str(raised.exception))
        self.assertNotIn("COMPOSE_FILE", self.subprocess.call_args.kwargs["env"])
        self.assertNotIn("COMPOSE_PROJECT_NAME", self.subprocess.call_args.kwargs["env"])
        for failure in (OSError(secret), subprocess.TimeoutExpired([secret], 1, output=secret, stderr=secret)):
            self.subprocess.side_effect = failure
            with self.subTest(failure=type(failure).__name__), self.assertRaises(gateway.GatewayError) as raised:
                gateway.run(["docker", "inspect", "fixture"])
            self.assertNotIn(secret, str(raised.exception))

    def test_saved_record_rejects_foreign_owner_candidate_names_and_traversal(self) -> None:
        baseline, _ = self.baseline()
        record = self.operation(baseline)
        gateway.save_record(record)
        self.assertEqual(gateway.saved_record(), record)
        for key in ("owner", "candidate_project", "candidate_volume"):
            changed = {**record, key: "foreign"}
            gateway.save_record(changed)
            with self.subTest(key=key), self.assertRaises(gateway.GatewayError):
                gateway.saved_record()
        gateway.write_private(gateway.STATE / "latest.json", gateway.canonical({"operation": "../../outside"}))
        with self.assertRaises(gateway.GatewayError):
            gateway.saved_record()

    def test_cleanup_refuses_foreign_or_protected_container_before_removal(self) -> None:
        baseline, _ = self.baseline()
        record = self.operation(baseline)
        owned = {"Id": "candidate-id", "Name": "/" + record["candidate_project"] + "-frontend-dev-1",
                 "Config": {"Labels": {gateway.OWNER_LABEL: str(gateway.STATE), gateway.OP_LABEL: record["operation"]}}}
        for mutation in ("owner", "operation", "name", "id", "protected"):
            info = copy.deepcopy(owned)
            trial = copy.deepcopy(record)
            if mutation == "owner":
                info["Config"]["Labels"][gateway.OWNER_LABEL] = "/foreign"
            elif mutation == "operation":
                info["Config"]["Labels"][gateway.OP_LABEL] = "foreign"
            elif mutation == "name":
                info["Name"] = "/foreign-project-frontend-dev-1"
            elif mutation == "id":
                info["Id"] = "different-id"
            else:
                trial["protected_ids"].append("candidate-id")
            with self.subTest(mutation=mutation), mock.patch.object(gateway, "run", return_value="candidate-id\n") as run, \
                    mock.patch.object(gateway, "inspect_container", return_value=info), self.assertRaises(gateway.GatewayError):
                gateway.cleanup(trial)
            self.assertFalse(any(call.args[0][1:2] == ["rm"] for call in run.call_args_list))

    def test_cleanup_refuses_foreign_or_live_dependency_volume_before_removal(self) -> None:
        baseline, _ = self.baseline()
        record = self.operation(baseline)
        owned = {"Name": record["candidate_volume"],
                 "Labels": {gateway.OWNER_LABEL: str(gateway.STATE), gateway.OP_LABEL: record["operation"]}}
        for mutation in ("owner", "operation", "name", "live"):
            volume = copy.deepcopy(owned)
            trial = copy.deepcopy(record)
            if mutation == "owner":
                volume["Labels"][gateway.OWNER_LABEL] = "/foreign"
            elif mutation == "operation":
                volume["Labels"][gateway.OP_LABEL] = "foreign"
            elif mutation == "name":
                volume["Name"] = "foreign"
            else:
                trial["candidate_volume"] = baseline["dependency_volume"]
                volume["Name"] = baseline["dependency_volume"]
            def run(command, **kwargs):
                return json.dumps([volume]) if command[1:3] == ["volume", "inspect"] else ""
            with self.subTest(mutation=mutation), mock.patch.object(gateway, "run", side_effect=run) as mocked, self.assertRaises(gateway.GatewayError):
                gateway.cleanup(trial)
            self.assertFalse(any(call.args[0][1:3] == ["volume", "rm"] for call in mocked.call_args_list))

    def test_cleanup_removes_only_verified_owned_candidate_objects(self) -> None:
        baseline, _ = self.baseline()
        record = self.operation(baseline)
        labels = {gateway.OWNER_LABEL: str(gateway.STATE), gateway.OP_LABEL: record["operation"]}
        info = {"Id": "candidate-id", "Name": "/" + record["candidate_project"] + "-frontend-dev-1", "Config": {"Labels": labels}}
        volume = {"Name": record["candidate_volume"], "Labels": labels}
        def run(command, **kwargs):
            if command[1] == "ps":
                return "candidate-id\n"
            if command[1:3] == ["volume", "inspect"]:
                return json.dumps([volume])
            return ""
        with mock.patch.object(gateway, "run", side_effect=run) as mocked, mock.patch.object(gateway, "inspect_container", return_value=info):
            gateway.cleanup(record)
        commands = [call.args[0] for call in mocked.call_args_list]
        self.assertIn(["docker", "rm", "--force", "candidate-id"], commands)
        self.assertIn(["docker", "volume", "rm", record["candidate_volume"]], commands)
        self.assertFalse(any("down" in command or "--remove-orphans" in command for command in commands))

    def test_candidate_creation_copies_live_dependencies_readonly_and_starts_only_frontend(self) -> None:
        baseline, _ = self.baseline()
        record = self.operation(baseline)
        with mock.patch.object(gateway.socket, "socket") as socket, mock.patch.object(gateway, "run", return_value="") as run, \
                mock.patch.object(gateway, "compose") as compose:
            path = gateway.start_candidate(record, baseline["document"])
        socket.return_value.__enter__.return_value.bind.assert_called_once_with(("127.0.0.1", gateway.CANDIDATE_PORT))
        commands = [call.args[0] for call in run.call_args_list]
        copier = next(command for command in commands if command[1] == "run")
        self.assertIn("type=volume,source=" + baseline["dependency_volume"] + ",target=/from,readonly", copier)
        self.assertIn("type=volume,source=" + record["candidate_volume"] + ",target=/to", copier)
        self.assertEqual(copier[copier.index("--network") + 1], "none")
        compose.assert_called_once_with(path, record["candidate_project"], "up", "-d", "--no-deps", "--force-recreate", gateway.SERVICE)
        candidate = json.loads(gateway.read_private(path))
        self.assertEqual(candidate["volumes"]["frontend-deps"]["name"], record["candidate_volume"])

    def test_candidate_creation_refuses_existing_or_live_dependency_volume(self) -> None:
        baseline, _ = self.baseline()
        record = self.operation(baseline)
        for mode in ("existing", "live"):
            trial = copy.deepcopy(record)
            if mode == "live":
                trial["candidate_volume"] = baseline["dependency_volume"]
            with self.subTest(mode=mode), mock.patch.object(gateway.socket, "socket"), \
                    mock.patch.object(gateway, "run", return_value="existing") as run, mock.patch.object(gateway, "compose") as compose, \
                    self.assertRaises(gateway.GatewayError):
                gateway.start_candidate(trial, baseline["document"])
            self.assertFalse(any(call.args[0][1:3] == ["volume", "create"] for call in run.call_args_list))
            compose.assert_not_called()

    def test_restore_refuses_foreign_frontend_or_modified_rollback_bytes(self) -> None:
        baseline, _ = self.baseline()
        record = self.operation(baseline)
        backup = self.rollback_backup(record)
        foreign = {"Id": "foreign-id", "Config": {"Labels": {"com.docker.compose.project": "foreign"}}}
        with mock.patch.object(gateway, "run", return_value=json.dumps([foreign])), mock.patch.object(gateway, "compose") as compose, self.assertRaises(gateway.GatewayError):
            gateway.restore(record)
        compose.assert_not_called()
        gateway.write_private(backup, b"modified private rollback")
        with mock.patch.object(gateway, "run") as run, mock.patch.object(gateway, "compose") as compose, self.assertRaises(gateway.GatewayError):
            gateway.restore(record)
        run.assert_not_called()
        compose.assert_not_called()

    def test_restore_owned_promotion_uses_saved_baseline_and_frontend_only_command(self) -> None:
        baseline, _ = self.baseline()
        record = self.operation(baseline)
        self.rollback_backup(record)
        promoted = {"Id": record["promoted_id"], "Config": {"Labels": {
            "com.docker.compose.project": gateway.PROJECT, "com.docker.compose.service": gateway.SERVICE,
            "com.docker.compose.project.config_files": record["promote_path"], gateway.OVERLAY + "sha256": record["source_hash"]}}}
        restored = {**baseline, "container_id": "restored-container-id"}
        with mock.patch.object(gateway, "run", return_value=json.dumps([promoted])), mock.patch.object(gateway, "compose") as compose, \
                mock.patch.object(gateway, "verify_live", return_value=restored), \
                mock.patch.object(gateway, "probe_baseline", return_value=record["http_baseline"]), mock.patch.object(gateway, "check_protected"):
            gateway.restore(record)
        compose.assert_called_once_with(Path(baseline["promote_path"]), gateway.PROJECT, "up", "-d", "--no-deps", "--force-recreate", gateway.SERVICE)
        self.assertEqual(record["phase"], "rolled_back")
        self.assertEqual(gateway.saved_record()["restored_id"], "restored-container-id")

    def test_stopped_baseline_can_be_restored_after_partial_recreation_failure(self) -> None:
        baseline, stopped = self.baseline()
        stopped["State"]["Running"] = False
        record = self.operation(baseline)
        record["phase"] = "promoting"
        self.rollback_backup(record)
        restored = {**baseline, "container_id": "restored-container-id"}
        with mock.patch.object(gateway, "run", return_value=json.dumps([stopped])), mock.patch.object(gateway, "compose") as compose, \
                mock.patch.object(gateway, "verify_live", return_value=restored) as verify_live, mock.patch.object(gateway, "check_cas") as cas, \
                mock.patch.object(gateway, "probe_baseline", return_value=record["http_baseline"]), mock.patch.object(gateway, "check_protected"):
            gateway.restore(record)
        cas.assert_not_called()
        verify_live.assert_called_once_with()
        compose.assert_called_once_with(Path(baseline["promote_path"]), gateway.PROJECT, "up", "-d", "--no-deps", "--force-recreate", gateway.SERVICE)
        self.assertEqual(record["phase"], "rolled_back")

    def exercise_restore_validation_retry(self, failure: str) -> None:
        baseline, restored_info = self.baseline()
        record = self.operation(baseline)
        record["phase"] = "promoting"
        self.rollback_backup(record)
        promoted = {"Id": record["promoted_id"], "Config": {"Labels": {
            "com.docker.compose.project": gateway.PROJECT, "com.docker.compose.service": gateway.SERVICE,
            "com.docker.compose.project.config_files": record["promote_path"], gateway.OVERLAY + "sha256": record["source_hash"]}}}
        restored_info["Id"] = "restored-container-id"
        with mock.patch.object(gateway, "run", return_value=json.dumps([promoted])) as run, \
                mock.patch.object(gateway, "compose") as compose, \
                mock.patch.object(gateway, "inspect_container", return_value=restored_info), \
                mock.patch.object(gateway, "probe_baseline", return_value=record["http_baseline"]) as probe, \
                mock.patch.object(gateway, "check_protected") as protected, mock.patch.object(gateway.time, "sleep"), \
                mock.patch.object(gateway, "container_ids", return_value={"restored-container-id", "backend-id", "database-id"}), \
                mock.patch.object(gateway, "owned_candidate_ids", return_value=[]), mock.patch.object(gateway, "cleanup"):
            failing = probe if failure == "probe" else protected
            failing.side_effect = gateway.GatewayError("temporarily unavailable")
            with self.assertRaises(gateway.GatewayError):
                gateway.restore(record)
            saved = gateway.saved_record()
            self.assertEqual(saved["phase"], "restoring")
            self.assertEqual(saved["restored_id"], restored_info["Id"])
            failing.side_effect = None
            run.return_value = json.dumps([restored_info])
            result = gateway.rollback()
        self.assertEqual(result["phase"], "rolled_back")
        self.assertEqual(gateway.saved_record()["restored_id"], restored_info["Id"])
        compose.assert_called_once_with(Path(baseline["promote_path"]), gateway.PROJECT, "up", "-d", "--no-deps", "--force-recreate", gateway.SERVICE)

    def test_restore_probe_failure_retries_checks_on_recorded_container_without_recreation(self) -> None:
        self.exercise_restore_validation_retry("probe")

    def test_restore_protected_check_failure_retries_checks_on_recorded_container_without_recreation(self) -> None:
        self.exercise_restore_validation_retry("protected")

    def test_restore_retry_refuses_different_container_or_baseline_identity(self) -> None:
        baseline, info = self.baseline()
        record = self.operation(baseline)
        record.update(phase="restoring", restored_id="restored-container-id")
        info["Id"] = record["restored_id"]
        self.rollback_backup(record)
        for mutation in ("different-id", "environment", "source-hash"):
            changed = copy.deepcopy(info)
            if mutation == "different-id":
                changed["Id"] = "another-publication-id"
            elif mutation == "environment":
                changed["Config"]["Env"].append("DEV_PROXY_TARGET=http://other-backend")
            else:
                changed["Config"]["Labels"][gateway.OVERLAY + "sha256"] = "f" * 64
            with self.subTest(mutation=mutation), mock.patch.object(gateway, "inspect_container", return_value=changed), \
                    mock.patch.object(gateway, "run") as run, mock.patch.object(gateway, "compose") as compose, \
                    mock.patch.object(gateway, "probe_baseline") as probe, self.assertRaises(gateway.GatewayError):
                gateway.restore(record)
            run.assert_not_called()
            compose.assert_not_called()
            probe.assert_not_called()

    def test_restore_does_not_adopt_unrecorded_recreated_baseline(self) -> None:
        baseline, info = self.baseline()
        record = self.operation(baseline)
        record["phase"] = "rollback_required"
        info["Id"] = "unrecorded-container-id"
        self.rollback_backup(record)
        with mock.patch.object(gateway, "run", return_value=json.dumps([info])), \
                mock.patch.object(gateway, "compose") as compose, self.assertRaises(gateway.GatewayError):
            gateway.restore(record)
        compose.assert_not_called()

    def test_reapplying_promoted_operation_is_idempotent_and_does_not_prepare(self) -> None:
        baseline, _ = self.baseline()
        record = self.operation(baseline)
        with mock.patch.object(gateway, "operation_lock", return_value=contextlib.nullcontext()), \
                mock.patch.object(gateway, "saved_record", return_value=record), mock.patch.object(gateway, "verify_promoted"), \
                mock.patch.object(gateway, "check_protected") as check_protected, mock.patch.object(gateway, "cleanup"), \
                mock.patch.object(gateway, "status", return_value={"phase": "promoted"}), mock.patch.object(gateway, "prepare_source") as prepare:
            result = gateway.apply()
        self.assertTrue(result["already_applied"])
        prepare.assert_not_called()
        check_protected.assert_not_called()

    def test_candidate_validation_failure_never_promotes_or_restores_live_frontend(self) -> None:
        baseline, _ = self.baseline()
        mcp = {"State": {"Running": True}, "NetworkSettings": {"Networks": {gateway.NETWORK: {}}},
               "Config": {"Labels": {"com.docker.compose.project": gateway.mcp_dev.PROJECT,
                                      "com.docker.compose.service": gateway.mcp_dev.SERVICE,
                                      "com.docker.compose.project.working_dir": str(gateway.ROOT)}}}
        with mock.patch.object(gateway, "verify_live", return_value=baseline), mock.patch.object(gateway.mcp_dev, "verify_project_ownership"), \
                mock.patch.object(gateway, "inspect_container", return_value=mcp), mock.patch.object(gateway, "protected_ids", return_value=["backend-id"]), \
                mock.patch.object(gateway, "probe_baseline", return_value={}), \
                mock.patch.object(gateway, "prepare_source", return_value=(Path(baseline["release"]), baseline["document"])), \
                mock.patch.object(gateway, "start_candidate"), mock.patch.object(gateway, "smoke", side_effect=RuntimeError("private-token")), \
                mock.patch.object(gateway, "cleanup"), mock.patch.object(gateway, "compose") as compose, mock.patch.object(gateway, "restore") as restore, \
                self.assertRaises(gateway.GatewayError) as raised:
            gateway.apply()
        self.assertNotIn("private-token", str(raised.exception))
        compose.assert_not_called()
        restore.assert_not_called()
        self.assertEqual(gateway.saved_record()["phase"], "failed_before_promotion")

    def test_promoted_state_write_failure_restores_instead_of_claiming_no_publication(self) -> None:
        baseline, baseline_info = self.baseline()
        mcp = {"State": {"Running": True}, "NetworkSettings": {"Networks": {gateway.NETWORK: {}}},
               "Config": {"Labels": {"com.docker.compose.project": gateway.mcp_dev.PROJECT,
                                      "com.docker.compose.service": gateway.mcp_dev.SERVICE,
                                      "com.docker.compose.project.working_dir": str(gateway.ROOT)}}}
        current_info = copy.deepcopy(baseline_info)
        persistence = gateway.save_record
        phases = []

        def save(record):
            phases.append(record["phase"])
            if record["phase"] == "promoted":
                raise OSError("one failed state write after successful live checks")
            persistence(record)

        def inspect(name):
            return mcp if name == "nexpoly_mcp_dev-property-filter-mcp-1" else current_info

        def compose(*args):
            current_info["Id"] = "restored-container-id" if current_info["Id"] == "promoted-container-id" else "promoted-container-id"

        with mock.patch.object(gateway.mcp_dev, "verify_project_ownership"), \
                mock.patch.object(gateway, "inspect_container", side_effect=inspect), \
                mock.patch.object(gateway, "run", side_effect=lambda *a, **kw: json.dumps([current_info])), \
                mock.patch.object(gateway, "protected_ids", return_value=[]), \
                mock.patch.object(gateway, "probe_baseline", return_value={}), \
                mock.patch.object(gateway, "prepare_source", return_value=(Path(baseline["release"]), baseline["document"])), \
                mock.patch.object(gateway, "start_candidate"), mock.patch.object(gateway, "smoke", return_value={}), \
                mock.patch.object(gateway, "cleanup"), mock.patch.object(gateway, "check_protected"), \
                mock.patch.object(gateway, "compose", side_effect=compose) as compose_calls, \
                mock.patch.object(gateway, "save_record", side_effect=save), self.assertRaises(gateway.GatewayError) as raised:
            gateway.apply()
        self.assertIn("the original frontend was restored", str(raised.exception))
        self.assertEqual(compose_calls.call_count, 2)
        self.assertEqual(phases[-3:], ["promoted", "restoring", "rolled_back"])
        self.assertNotIn("failed_before_promotion", phases)
        saved = gateway.saved_record()
        self.assertEqual(saved["phase"], "rolled_back")
        self.assertEqual(saved["promoted_id"], "promoted-container-id")
        self.assertEqual(saved["restored_id"], "restored-container-id")

    def test_manual_rollback_snapshots_current_other_containers_instead_of_historical_ids(self) -> None:
        baseline, _ = self.baseline()
        record = self.operation(baseline)
        with mock.patch.object(gateway, "operation_lock", return_value=contextlib.nullcontext()), \
                mock.patch.object(gateway, "saved_record", return_value=record), \
                mock.patch.object(gateway, "run", return_value=json.dumps([{"Id": record["promoted_id"]}])), \
                mock.patch.object(gateway, "container_ids", return_value={record["promoted_id"], "updated-backend-id", "updated-database-id", "candidate-id"}), \
                mock.patch.object(gateway, "owned_candidate_ids", return_value=["candidate-id"]), \
                mock.patch.object(gateway, "restore") as restore, mock.patch.object(gateway, "cleanup"), \
                mock.patch.object(gateway, "status", return_value={"phase": "rolled_back"}):
            result = gateway.rollback()
        self.assertEqual(result["action"], "rollback")
        restore.assert_called_once_with(record)
        self.assertEqual(record["protected_ids"], ["updated-backend-id", "updated-database-id"])

    def test_http_probe_uses_fixed_loopback_no_environment_proxy_or_redirects(self) -> None:
        response = mock.MagicMock()
        response.status = 401
        response.headers = {"Content-Type": "application/json; charset=utf-8"}
        response.read.return_value = b'{"error":"unauthorized"}'
        response.__enter__.return_value = response
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(gateway.urllib.request, "build_opener", return_value=opener) as build:
            result = gateway.http_request(gateway.CANDIDATE_PORT, "/mcp", payload={}, token="private-token")
        self.assertEqual(result, (401, "application/json", b'{"error":"unauthorized"}'))
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, f"http://127.0.0.1:{gateway.CANDIDATE_PORT}/mcp")
        self.assertEqual(request.get_header("Authorization"), "Bearer private-token")
        self.assertEqual(build.call_args.args[0].proxies, {})
        self.assertIsInstance(build.call_args.args[1], gateway.mcp_dev.NoRedirect)
        with mock.patch.object(gateway.urllib.request, "build_opener", return_value=opener):
            gateway.http_request(9001, "/")
        page_request = opener.open.call_args.args[0]
        self.assertEqual(page_request.get_header("Accept"), "*/*")
        self.assertIsNone(page_request.get_header("Authorization"))
        for port, path in ((9000, "/mcp"), (9011, "/mcp"), (9001, "//foreign/mcp"), (9001, "http://foreign/mcp")):
            with self.subTest(port=port, path=path), self.assertRaises(gateway.GatewayError):
                gateway.http_request(port, path)

    def test_mcp_smoke_exercises_catalog_search_histogram_with_private_fixture_token(self) -> None:
        token = "t" * 48
        option = {"option_key": "raw:fixture", "min_value": 1.5, "max_value": 3.5, "unit": "MPa", "measurement_count": 7}
        def http(port, path, *, payload=None, token=None):
            self.assertEqual(port, gateway.CANDIDATE_PORT)
            self.assertEqual(path, "/mcp")
            if token is None:
                return 401, "application/json", b"{}"
            method = payload["method"]
            if method == "initialize":
                result = {"protocolVersion": "2025-11-25"}
            elif method == "tools/list":
                result = {"tools": [{"name": name} for name in gateway.mcp_dev.TOOLS]}
            else:
                name = payload["params"]["name"]
                if name == "list_filter_properties":
                    output = {"ok": True, "dataset_revision": "fixture-revision", "options": [option]}
                elif name == "search_polymers":
                    self.assertEqual(payload["params"]["arguments"]["dataset_revision"], "fixture-revision")
                    self.assertEqual(payload["params"]["arguments"]["filters"], [{"option_key": option["option_key"], "min_value": 1.5, "max_value": 3.5}])
                    output = {"ok": True, "matched_material_count": 3}
                else:
                    self.assertEqual(name, "get_property_histogram")
                    output = {"ok": True, "unit": "MPa", "histogram": {"total_count": 7}}
                result = {"structuredContent": output}
            return 200, "application/json", gateway.canonical({"jsonrpc": "2.0", "id": payload["id"], "result": result})
        credentials = f"NEXPOLY_MCP_URL={gateway.mcp_dev.BASE_URL}/mcp\nNEXPOLY_MCP_TOKEN={token}\n"
        with mock.patch.object(gateway, "http_request", side_effect=http) as http_request, \
                mock.patch.object(gateway, "probe_baseline", return_value={}), mock.patch.object(gateway.mcp_dev, "read_private", return_value=credentials):
            result = gateway.smoke(gateway.CANDIDATE_PORT, {})
        self.assertEqual(result["search"], "ok")
        self.assertEqual(result["histogram"], "ok")
        self.assertNotIn(token, json.dumps(result))
        self.assertEqual(len(http_request.call_args_list), 6)
        self.assertTrue(all(call.kwargs["token"] == token for call in http_request.call_args_list[1:]))

    def test_main_suppresses_unexpected_private_exception_text(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        with mock.patch.object(gateway.sys, "argv", ["gateway", "status"]), mock.patch.object(gateway.os, "umask"), \
                mock.patch.object(gateway, "status", side_effect=RuntimeError("Authorization: Bearer private-secret")), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            self.assertEqual(gateway.main(), 1)
        self.assertEqual(output.getvalue(), "")
        self.assertNotIn("private-secret", error.getvalue())
        self.assertEqual(json.loads(error.getvalue())["error"], "property_filter_mcp_gateway_failed")


if __name__ == "__main__":
    unittest.main()
