from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
SMOKE_PATH = REPO_ROOT / "scripts" / "monomer_md_smoke.py"
SPEC = importlib.util.spec_from_file_location("monomer_md_smoke", SMOKE_PATH)
assert SPEC and SPEC.loader
monomer_md_smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(monomer_md_smoke)


class MonomerMdSmokeTests(unittest.TestCase):
    def test_authoritative_smoke_validates_steps_artifacts_and_capacity(self) -> None:
        responses = [
            {
                "schema_version": 1,
                "operation_id": "deploy-operation-1",
                "job_id": "job-1",
                "status": "submitted",
                "validated": False,
                "capability": "c" * 64,
            },
            {
                "job_id": "job-1",
                "status": "completed",
                "requested_steps": 300,
                "completed_steps": 300,
                "byteff2_git_sha": "a" * 40,
                "result": {
                    "summary": {"n_steps": 300},
                    "not_equilibrated": True,
                    "physical_density_estimate": False,
                    "warnings": ["demo only"],
                },
                "artifacts": {
                    "state": {"path": "npt_state.csv"},
                    "trajectory": {"path": "npt.dcd"},
                },
            },
            {
                "schema_version": 1,
                "operation_id": "deploy-operation-1",
                "job_id": "job-1",
                "status": "validated",
                "validated": True,
            },
            {
                "schema_version": 1,
                "operation_id": "deploy-operation-1",
                "job_id": "job-1",
                "status": "cleaned",
                "validated": True,
            },
            {"active_jobs": 0, "database_active_jobs": 0, "can_submit": True},
        ]
        with mock.patch.object(monomer_md_smoke, "request_json", side_effect=responses):
            job_id = monomer_md_smoke.run_smoke(
                "http://example",
                30,
                "a" * 40,
                "deploy-operation-1",
                "b" * 40,
            )

        self.assertEqual(job_id, "job-1")

    def test_validation_failure_still_requests_exact_cleanup(self) -> None:
        requests: list[tuple[str, dict[str, object] | None]] = []
        responses = [
            {
                "schema_version": 1,
                "operation_id": "deploy-operation-2",
                "job_id": "job-2",
                "status": "submitted",
                "validated": False,
                "capability": "d" * 64,
            },
            {
                "job_id": "job-2",
                "status": "completed",
                "requested_steps": 299,
                "completed_steps": 299,
            },
            {
                "schema_version": 1,
                "operation_id": "deploy-operation-2",
                "job_id": "job-2",
                "status": "cleaned",
                "validated": False,
            },
        ]

        def request(url: str, *, body=None):  # type: ignore[no-untyped-def]
            requests.append((url, body))
            return responses.pop(0)

        with (
            mock.patch.object(monomer_md_smoke, "request_json", side_effect=request),
            self.assertRaisesRegex(RuntimeError, "requested step count"),
        ):
            monomer_md_smoke.run_smoke(
                "http://example",
                30,
                "a" * 40,
                "deploy-operation-2",
                "b" * 40,
            )

        cleanup_url, cleanup_body = requests[-1]
        self.assertTrue(cleanup_url.endswith("/internal/deployment/monomer-md-canary/cleanup"))
        self.assertEqual(
            cleanup_body,
            {
                "operation_id": "deploy-operation-2",
                "source_sha": "b" * 40,
                "expected_byteff2_commit": "a" * 40,
                "capability": "d" * 64,
            },
        )

    def test_cleaned_validated_retry_does_not_resubmit_or_poll_job(self) -> None:
        responses = [
            {
                "schema_version": 1,
                "operation_id": "deploy-operation-3",
                "job_id": "job-3",
                "status": "cleaned",
                "validated": True,
                "capability": "e" * 64,
            },
            {
                "schema_version": 1,
                "operation_id": "deploy-operation-3",
                "job_id": "job-3",
                "status": "cleaned",
                "validated": True,
            },
            {"active_jobs": 0, "database_active_jobs": 0, "can_submit": True},
        ]
        request = mock.Mock(side_effect=responses)
        with mock.patch.object(monomer_md_smoke, "request_json", request):
            job_id = monomer_md_smoke.run_smoke(
                "http://example",
                30,
                "a" * 40,
                "deploy-operation-3",
                "b" * 40,
            )

        self.assertEqual(job_id, "job-3")
        self.assertEqual(request.call_count, 3)
        self.assertTrue(
            request.call_args_list[0].args[0].endswith(
                "/internal/deployment/monomer-md-canary/submit"
            )
        )

    def test_cleanup_intent_retry_finishes_cleanup_without_polling_deleted_job(
        self,
    ) -> None:
        responses = [
            {
                "schema_version": 1,
                "operation_id": "deploy-operation-4",
                "job_id": "job-4",
                "status": "cleanup-intent",
                "validated": True,
                "capability": "f" * 64,
            },
            {
                "schema_version": 1,
                "operation_id": "deploy-operation-4",
                "job_id": "job-4",
                "status": "cleaned",
                "validated": True,
            },
            {"active_jobs": 0, "database_active_jobs": 0, "can_submit": True},
        ]
        request = mock.Mock(side_effect=responses)
        with mock.patch.object(monomer_md_smoke, "request_json", request):
            job_id = monomer_md_smoke.run_smoke(
                "http://example",
                30,
                "a" * 40,
                "deploy-operation-4",
                "b" * 40,
            )

        self.assertEqual(job_id, "job-4")
        self.assertEqual(request.call_count, 3)
        self.assertTrue(
            request.call_args_list[1].args[0].endswith(
                "/internal/deployment/monomer-md-canary/cleanup"
            )
        )
        self.assertFalse(
            any(
                "/api/v1/monomer-md/jobs/" in call.args[0]
                for call in request.call_args_list
            )
        )

    def test_unvalidated_cleanup_retry_requires_old_capability_before_rearm(
        self,
    ) -> None:
        completed = {
            "job_id": "job-5",
            "status": "completed",
            "requested_steps": 300,
            "completed_steps": 300,
            "byteff2_git_sha": "a" * 40,
            "result": {
                "summary": {"n_steps": 300},
                "not_equilibrated": True,
                "physical_density_estimate": False,
                "warnings": ["demo only"],
            },
            "artifacts": {
                "state": {"path": "npt_state.csv"},
                "trajectory": {"path": "npt.dcd"},
            },
        }
        responses = [
            {
                "schema_version": 1,
                "operation_id": "deploy-operation-5",
                "job_id": "job-5",
                "status": "cleanup-intent",
                "validated": False,
                "capability": "1" * 64,
            },
            {
                "schema_version": 1,
                "operation_id": "deploy-operation-5",
                "job_id": "job-5",
                "status": "cleaned",
                "validated": False,
            },
            {
                "schema_version": 1,
                "operation_id": "deploy-operation-5",
                "job_id": "job-5",
                "status": "submitted",
                "validated": False,
                "capability": "2" * 64,
            },
            completed,
            {
                "schema_version": 1,
                "operation_id": "deploy-operation-5",
                "job_id": "job-5",
                "status": "validated",
                "validated": True,
            },
            {
                "schema_version": 1,
                "operation_id": "deploy-operation-5",
                "job_id": "job-5",
                "status": "cleaned",
                "validated": True,
            },
            {"active_jobs": 0, "database_active_jobs": 0, "can_submit": True},
        ]
        request = mock.Mock(side_effect=responses)
        with mock.patch.object(monomer_md_smoke, "request_json", request):
            monomer_md_smoke.run_smoke(
                "http://example",
                30,
                "a" * 40,
                "deploy-operation-5",
                "b" * 40,
            )

        rearm_body = request.call_args_list[2].kwargs["body"]
        self.assertEqual(rearm_body["capability"], "1" * 64)
        cleanup_body = request.call_args_list[5].kwargs["body"]
        self.assertEqual(cleanup_body["capability"], "2" * 64)


if __name__ == "__main__":
    unittest.main()
