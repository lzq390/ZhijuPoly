from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from scripts import openscience_ui_release as release


class OpenScienceUIReleaseTests(unittest.TestCase):
    def test_canonical_plan_digest_is_stable(self) -> None:
        document = {"schema_version": 1, "values": ["b", "a"]}
        expected = "sha256:" + hashlib.sha256(
            b'{"schema_version":1,"values":["b","a"]}\n'
        ).hexdigest()
        self.assertEqual(release.digest(document), expected)

    def test_parse_and_replace_env_image_preserves_other_lines(self) -> None:
        original = (
            b"OPENSCIENCE_UI_BIND=0.0.0.0\n"
            b"OPENSCIENCE_UI_IMAGE=openscience-ui-poc:legacy\n"
            b"OPENSCIENCE_UI_PORT=9011\n"
        )
        candidate = "ghcr.io/lzq390/openscience-ui@sha256:" + "a" * 64
        replaced = release.replace_env_image(original, candidate)
        self.assertEqual(release.parse_env_image(replaced), candidate)
        self.assertIn(b"OPENSCIENCE_UI_BIND=0.0.0.0\n", replaced)
        self.assertIn(b"OPENSCIENCE_UI_PORT=9011\n", replaced)

    def test_env_image_assignment_must_be_unique(self) -> None:
        candidate = "ghcr.io/lzq390/openscience-ui@sha256:" + "a" * 64
        with self.assertRaisesRegex(release.ReleaseError, "count differs"):
            release.replace_env_image(
                b"OPENSCIENCE_UI_IMAGE=one\nOPENSCIENCE_UI_IMAGE=two\n",
                candidate,
            )

    def test_candidate_reference_is_digest_only(self) -> None:
        self.assertIsNone(release.IMAGE.fullmatch("ghcr.io/lzq390/openscience-ui:latest"))
        self.assertIsNotNone(
            release.IMAGE.fullmatch(
                "ghcr.io/lzq390/openscience-ui@sha256:" + "f" * 64
            )
        )

    def test_operation_id_is_narrow(self) -> None:
        self.assertIsNotNone(
            release.OPERATION_ID.fullmatch("openscience-20260820t021500z")
        )
        self.assertIsNone(release.OPERATION_ID.fullmatch("OpenScience-now"))
        self.assertIsNone(release.OPERATION_ID.fullmatch("openscience-../../tmp"))
        with self.assertRaisesRegex(release.ReleaseError, "operation ID is invalid"):
            release.validate_operation_id("../../tmp")

    def test_apply_persists_a_completed_terminal_journal(self) -> None:
        operation_id = "openscience-20260820t060000z"
        target_sha = "a" * 40
        candidate = "ghcr.io/lzq390/openscience-ui@sha256:" + "b" * 64
        original_env = b"OPENSCIENCE_UI_IMAGE=openscience-ui-poc:legacy\n"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            deployment = root / "deployment"
            state_root = root / "state"
            deployment.mkdir()
            env_path = deployment / ".env"
            compose_path = deployment / "docker-compose.yml"
            env_path.write_bytes(original_env)
            compose_path.write_text("services: {}\n", encoding="utf-8")
            plan = {
                "schema_version": 1,
                "operation_id": operation_id,
                "target_sha": target_sha,
                "candidate": {"reference": candidate},
                "deployment": {
                    "directory": str(deployment.resolve()),
                    "compose_sha256": release.file_sha256(compose_path),
                    "env_sha256": "sha256:"
                    + hashlib.sha256(original_env).hexdigest(),
                },
            }
            plan_sha256 = release.digest(plan)
            arguments = SimpleNamespace(
                operation_id=operation_id,
                state_root=state_root,
                deployment_dir=deployment,
                image=candidate,
                sha=target_sha,
                confirm_plan_sha256=plan_sha256,
            )
            with (
                mock.patch.object(release, "build_plan", return_value=plan),
                mock.patch.object(release, "run_canary"),
                mock.patch.object(release, "compose_up"),
                mock.patch.object(release, "verify_live_candidate"),
            ):
                result = release.apply_release(arguments)

            self.assertEqual(result["phase"], "completed")
            journal = json.loads(
                (state_root / operation_id / "journal.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(journal["phase"], "completed")
            self.assertEqual(journal["plan_sha256"], plan_sha256)
            self.assertEqual(release.parse_env_image(env_path.read_bytes()), candidate)

    def test_explicit_rollback_persists_a_terminal_journal(self) -> None:
        operation_id = "openscience-20260820t060100z"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            deployment = root / "deployment"
            state_root = root / "state"
            operation = state_root / operation_id
            deployment.mkdir()
            operation.mkdir(parents=True)
            plan = {"deployment": {"directory": str(deployment.resolve())}}
            plan_sha256 = release.digest(plan)
            release.write_json(operation / "plan.json", plan)
            release.write_json(
                operation / "journal.json",
                {"schema_version": 1, "phase": "completed"},
            )
            arguments = SimpleNamespace(
                operation_id=operation_id,
                state_root=state_root,
                deployment_dir=deployment,
                confirm_plan_sha256=plan_sha256,
            )
            with mock.patch.object(release, "restore_previous") as restore_previous:
                result = release.rollback_release(arguments)

            self.assertEqual(result["phase"], "rolled-back")
            restore_previous.assert_called_once()
            journal = json.loads(
                (operation / "journal.json").read_text(encoding="utf-8")
            )
            self.assertEqual(journal["phase"], "rolled-back")
            self.assertEqual(journal["plan_sha256"], plan_sha256)

    def test_unhealthy_candidate_is_recreated_from_the_previous_image(self) -> None:
        previous = b"OPENSCIENCE_UI_IMAGE=openscience-ui-poc:legacy\n"
        candidate_reference = (
            "ghcr.io/lzq390/openscience-ui@sha256:" + "a" * 64
        )
        candidate = release.replace_env_image(previous, candidate_reference)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            deployment = root / "deployment"
            operation = root / "operation"
            deployment.mkdir()
            operation.mkdir()
            (deployment / ".env").write_bytes(candidate)
            (operation / "env.before").write_bytes(previous)
            plan = {
                "candidate": {
                    "id": "sha256:" + "b" * 64,
                    "reference": candidate_reference,
                },
                "current": {"image_id": "sha256:" + "c" * 64},
                "deployment": {
                    "env_image": "openscience-ui-poc:legacy",
                    "env_sha256": "sha256:" + hashlib.sha256(previous).hexdigest(),
                },
            }
            unhealthy = {
                "Image": plan["candidate"]["id"],
                "Config": {"Image": candidate_reference},
                "State": {"Running": True, "Health": {"Status": "unhealthy"}},
            }
            restored = {
                "image_id": plan["current"]["image_id"],
                "configured_image": plan["deployment"]["env_image"],
            }
            with (
                mock.patch.object(release, "docker_inspect", return_value=unhealthy),
                mock.patch.object(release, "compose_up") as compose_up,
                mock.patch.object(release, "wait_container_healthy") as wait_healthy,
                mock.patch.object(release, "live_identity", return_value=restored),
                mock.patch.object(release, "wait_http"),
            ):
                release.restore_previous(
                    operation_dir=operation,
                    deployment_dir=deployment,
                    plan=plan,
                )
            self.assertEqual((deployment / ".env").read_bytes(), previous)
            compose_up.assert_called_once_with(deployment)
            wait_healthy.assert_called_once_with(release.LIVE_CONTAINER)

    def test_live_health_wait_allows_a_normal_starting_transition(self) -> None:
        starting = {
            "State": {
                "Status": "running",
                "Running": True,
                "Health": {"Status": "starting"},
                "Error": "",
            }
        }
        healthy = {
            "State": {
                "Status": "running",
                "Running": True,
                "Health": {"Status": "healthy"},
                "Error": "",
            }
        }
        with (
            mock.patch.object(release, "docker_inspect", side_effect=[starting, healthy]),
            mock.patch.object(release.time, "sleep") as sleep,
        ):
            release.wait_container_healthy(release.LIVE_CONTAINER, attempts=2)
        sleep.assert_called_once_with(1)

    def test_live_health_wait_is_bounded_and_reports_the_last_state(self) -> None:
        unhealthy = {
            "State": {
                "Status": "running",
                "Running": True,
                "Health": {"Status": "unhealthy"},
                "Error": "probe failed",
            }
        }
        with (
            mock.patch.object(release, "docker_inspect", return_value=unhealthy),
            mock.patch.object(release.time, "sleep") as sleep,
        ):
            with self.assertRaisesRegex(
                release.ReleaseError,
                "did not become healthy.*health='unhealthy'.*probe failed",
            ):
                release.wait_container_healthy(release.LIVE_CONTAINER, attempts=2)
        sleep.assert_called_once_with(1)

    def test_live_identity_rejects_resource_policy_drift(self) -> None:
        document = {
            "Id": "container-id",
            "Image": "sha256:" + "a" * 64,
            "Config": {"Image": "openscience-ui-poc:legacy"},
            "State": {"Running": True, "Health": {"Status": "healthy"}},
            "HostConfig": {
                "Memory": 256 * 1024 * 1024,
                "NanoCpus": 1_000_000_000,
                "PidsLimit": 128,
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges:true"],
                "RestartPolicy": {"Name": "unless-stopped"},
            },
            "NetworkSettings": {"Networks": {release.NETWORK: {}}},
        }
        with mock.patch.object(release, "docker_inspect", return_value=document):
            with self.assertRaisesRegex(release.ReleaseError, "resource or isolation"):
                release.live_identity()


if __name__ == "__main__":
    unittest.main()
