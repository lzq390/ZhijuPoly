from __future__ import annotations

import importlib.util
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "production_acceptance_probes",
    ROOT / "scripts" / "production_acceptance_probes.py",
)
assert SPEC is not None and SPEC.loader is not None
PROBES = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROBES)

GPU_UUID = "GPU-89c7c52c-e252-0135-c157-24eee1a1ccbe"


class FakeClient:
    base_url = "http://127.0.0.1:9000"

    def response(self, method, path, body, headers):  # noqa: ANN001
        raise NotImplementedError

    def request(
        self,
        method: str,
        path: str,
        *,
        body=None,
        headers=None,
        timeout=30.0,
        max_bytes=PROBES.MAX_JSON_BYTES,
    ):
        del timeout
        status, response_headers, payload = self.response(
            method,
            path,
            body,
            headers or {},
        )
        raw = payload if isinstance(payload, bytes) else PROBES._canonical_bytes(payload)
        if len(raw) > max_bytes:
            raise AssertionError("fixture exceeded client bound")
        return status, response_headers, raw


def dft_status() -> dict[str, object]:
    return {
        "enabled": True,
        "available": True,
        "schema_ready": True,
        "worker_status": "ok",
        "runtime_ready": True,
        "gpu_guard_mode": "observe",
        "gpu_guard_status": "quarantined",
        "gpu_contention_observed": True,
        "draining": False,
        "active_jobs": 0,
        "max_active_jobs": 9,
        "message": PROBES.PUBLIC_DFT_CONTENTION_WARNING,
    }


def dft_capabilities() -> dict[str, object]:
    return {
        "available": True,
        "limits": {
            "max_concurrent_jobs": 1,
            "max_queued_jobs": 8,
            "max_active_jobs": 9,
        },
        "models": [
            {"id": model, "available": True}
            for model in sorted(PROBES.DFT_MODELS)
        ],
        "worker": {
            "schema_version": 1,
            "calculation_types": ["single_point", "optimization"],
            "properties": ["energy", "charges", "forces"],
            "input_limits": {"max_atoms": 300},
            "queue": {"max_queued_jobs": 8},
            "worker_status": "ok",
            "runtime_ready": True,
            "draining": False,
            "gpu_guard_mode": "observe",
            "gpu_guard_status": "quarantined",
            "gpu_contention_observed": True,
        },
        "message": PROBES.PUBLIC_DFT_CONTENTION_WARNING,
    }


def completed_dft_job() -> dict[str, object]:
    result = {
        "schema_version": 2,
        "calculation_type": "single_point",
        "engine": "aimnet2",
        "model": "aimnet2",
        "atoms": {"count": 3},
        "properties": {
            "energy": {"value_eV": -76.0},
            "charges": {"values_e": [-0.8, 0.4, 0.4]},
            "forces": {"values_eV_per_A": [[0.0, 0.0, 0.0]] * 3},
        },
    }
    return {
        "job_id": "11111111-1111-4111-8111-111111111111",
        "status": "completed",
        "result": result,
        "provenance": {"gpu_uuid": GPU_UUID},
    }


class DftClient(FakeClient):
    def response(self, method, path, body, headers):  # noqa: ANN001
        if method == "GET" and path.endswith("/status"):
            return 200, {}, dft_status()
        if method == "GET" and path.endswith("/capabilities"):
            return 200, {}, dft_capabilities()
        if method == "POST" and path.endswith("/jobs"):
            assert headers["Idempotency-Key"].startswith("prod-accept-dft-")
            assert body["calculation_type"] == "single_point"
            return 202, {}, completed_dft_job()
        if method == "GET" and "/jobs/" in path:
            return 200, {}, completed_dft_job()
        raise AssertionError((method, path))


class MutatingDftClient(DftClient):
    def __init__(
        self,
        *,
        status_mutator=None,
        capabilities_mutator=None,
        result_mutator=None,
    ) -> None:
        self.status_mutator = status_mutator
        self.capabilities_mutator = capabilities_mutator
        self.result_mutator = result_mutator

    def response(self, method, path, body, headers):  # noqa: ANN001
        response = super().response(method, path, body, headers)
        payload = response[2]
        if method == "GET" and path.endswith("/status") and self.status_mutator:
            self.status_mutator(payload)
        elif (
            method == "GET"
            and path.endswith("/capabilities")
            and self.capabilities_mutator
        ):
            self.capabilities_mutator(payload)
        elif method == "GET" and "/jobs/" in path and self.result_mutator:
            self.result_mutator(payload["result"])
        return response


class MdClient(FakeClient):
    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, object]] = {}
        self.created = 0
        self.cancelled: list[str] = []

    def status(self) -> dict[str, object]:
        active = [job for job in self.jobs.values() if job["status"] not in PROBES.TERMINAL]
        running = sum(job["status"] == "running" for job in active)
        queued = sum(job.get("queue_position") is not None for job in active)
        return {
            "available": True,
            "runtime_ready": True,
            "draining": False,
            "active_jobs": len(active),
            "database_active_jobs": len(active),
            "formal_running_jobs": running,
            "formal_queued_jobs": queued,
            "max_active_jobs": 3,
            "formal_max_running_jobs": 1,
            "formal_max_queued_jobs": 2,
        }

    def response(self, method, path, body, headers):  # noqa: ANN001
        if method == "GET" and path.endswith("/status"):
            return 200, {}, self.status()
        if method == "POST" and path.endswith("/jobs"):
            self.created += 1
            assert headers["X-Forwarded-For"] == f"192.0.2.{10 + self.created}"
            assert body["run_mode"] == "formal"
            if self.created == 4:
                return 429, {}, {"detail": "formal ByteFF2 monomer MD capacity is full"}
            job_id = f"job-{self.created}"
            self.jobs[job_id] = {
                "job_id": job_id,
                "status": "running" if self.created == 1 else "submitted",
                "queue_position": None if self.created == 1 else self.created - 1,
            }
            return 202, {}, {"job_id": job_id, "status": "submitted"}
        if method == "GET" and "/jobs/" in path:
            return 200, {}, self.jobs[path.rsplit("/", 1)[-1]]
        if method == "POST" and path.endswith("/cancel"):
            job_id = path.split("/")[-2]
            self.cancelled.append(job_id)
            self.jobs[job_id]["status"] = "cancelled"
            self.jobs[job_id]["queue_position"] = None
            if job_id == "job-2":
                self.jobs["job-3"]["queue_position"] = 1
            if job_id == "job-1":
                self.jobs["job-3"]["status"] = "running"
                self.jobs["job-3"]["queue_position"] = None
            return 202, {}, self.jobs[job_id]
        raise AssertionError((method, path))


class ReadAndFrontendClient(FakeClient):
    INDEX = b'<html><body><div id="root"></div><script src="/assets/app.js"></script><link href="/assets/app.css"></body></html>'

    def response(self, method, path, body, headers):  # noqa: ANN001
        del headers
        if method == "GET" and path == "/api/v1/database-browser/property-filter/options":
            histogram = {
                "counts": [2, 3],
                "underflow_count": 0,
                "overflow_count": 0,
                "total_count": 5,
            }
            return 200, {}, {
                "data_source": "postgres",
                "source_status": "ready",
                "total_records": 615159,
                "options": [{"option_key": "Tg:C", "histogram": histogram}],
            }
        if method == "GET" and path.startswith("/api/v1/database-browser/property-filter/histogram?"):
            return 200, {}, {
                "histogram": {
                    "counts": [2, 3],
                    "underflow_count": 0,
                    "overflow_count": 0,
                    "total_count": 5,
                }
            }
        if method == "POST" and path == "/api/v1/structure/2d":
            assert body == {"smiles": "*CC*"}
            return 200, {}, {"structure_svg": "<?xml version='1.0'?><svg></svg>"}
        if method == "POST" and path == "/api/v1/knowledge/search":
            groups = body.get("groups") or [{"terms": [body["query"]]}]
            return 200, {}, {
                "query": body["query"],
                "groups": groups,
                "total": 1,
                "results": [{"knowledge_id": 42, "abstract": "must not be sealed"}],
            }
        if method == "GET" and path == "/api/v1/assistant/tg/status":
            return 200, {}, {
                "enabled": False,
                "configured": True,
                "image": {
                    "supported": True,
                    "max_files": 2,
                    "max_canvas_snapshots": 1,
                    "max_user_upload_files": 1,
                    "max_bytes": 5242880,
                    "max_total_bytes": 10485760,
                    "accepted_mime_types": ["image/png", "image/jpeg", "image/webp"],
                },
            }
        if method == "GET" and path == "/api/v1/assistant/tg/guide":
            return 200, {}, {
                "module": "reverseDesign",
                "version": 3,
                "language": "zh-CN",
                "defaults": {
                    "target_tg": 450.0,
                    "similarity_threshold": 0.7,
                    "candidate_size": 200,
                },
                "sections": [{"id": "purpose", "title": "模块用途", "content": ["guide"]}],
            }
        if method == "GET" and path == "/health":
            return 200, {"Content-Type": "application/json"}, {"status": "ok"}
        if method == "GET" and path in {"/assets/app.js", "/assets/app.css"}:
            content_type = "application/javascript" if path.endswith("js") else "text/css"
            return 200, {"Content-Type": content_type}, b"release-asset"
        if method == "GET" and path in {
            "/",
            "/structure-workbench",
            "/homopolymer-property-prediction",
            "/database",
            "/database-filter",
            "/knowledge",
            "/reverse-design",
            "/monomer-dft",
            "/monomer-md-simulation",
        }:
            return 200, {"Content-Type": "text/html"}, self.INDEX
        raise AssertionError((method, path))


class ProductionAcceptanceProbeTests(unittest.TestCase):
    def run_dft(self, client, *, require_quarantined=False):  # noqa: ANN001
        return PROBES.run_dft_probe(
            client,
            operation_id="deploy-test-0001",
            timeout_seconds=1,
            poll_seconds=0.001,
            expected_gpu_uuid=GPU_UUID,
            require_quarantined=require_quarantined,
        )

    def test_dft_quarantine_remains_available_and_single_point_completes(self) -> None:
        result = self.run_dft(DftClient(), require_quarantined=True)
        self.assertEqual(result["warm_models"], sorted(PROBES.DFT_MODELS))
        self.assertTrue(result["quarantine_exercised"])
        self.assertEqual(result["single_point"]["status"], "completed")

    def test_dft_ready_guard_projection_is_equivalent_and_accepted(self) -> None:
        def ready(payload):  # noqa: ANN001
            projection = payload.get("worker", payload)
            projection["gpu_guard_status"] = "ready"
            projection["gpu_contention_observed"] = False

        result = self.run_dft(
            MutatingDftClient(
                status_mutator=ready,
                capabilities_mutator=ready,
            )
        )
        self.assertEqual(result["guard_status"], "ready")
        self.assertFalse(result["contention_observed"])

    def test_dft_guard_projection_rejects_missing_wrong_type_and_mismatch(self) -> None:
        cases = {
            "missing": (
                lambda payload: payload.pop("gpu_guard_mode"),
                None,
                "missing",
            ),
            "wrong contention type": (
                lambda payload: payload.__setitem__("gpu_contention_observed", 1),
                lambda payload: payload["worker"].__setitem__(
                    "gpu_contention_observed", 1
                ),
                "not boolean",
            ),
            "projection mismatch": (
                None,
                lambda payload: payload["worker"].__setitem__(
                    "gpu_guard_status", "ready"
                ),
                "projections differ",
            ),
            "quarantine equivalence": (
                lambda payload: payload.__setitem__("gpu_contention_observed", False),
                lambda payload: payload["worker"].__setitem__(
                    "gpu_contention_observed", False
                ),
                "inconsistent",
            ),
        }
        for label, (status_mutator, capabilities_mutator, message) in cases.items():
            with self.subTest(label=label):
                with self.assertRaisesRegex(PROBES.ProbeError, message):
                    self.run_dft(
                        MutatingDftClient(
                            status_mutator=status_mutator,
                            capabilities_mutator=capabilities_mutator,
                        )
                    )

    def test_dft_worker_projection_is_exact_and_quarantine_warning_is_fixed(self) -> None:
        with self.assertRaisesRegex(PROBES.ProbeError, "projection fields"):
            self.run_dft(
                MutatingDftClient(
                    capabilities_mutator=lambda payload: payload["worker"].__setitem__(
                        "extra_public_detail", "unexpected"
                    )
                )
            )
        for endpoint in ("status", "capabilities"):
            with self.subTest(endpoint=endpoint):
                status_mutator = None
                capabilities_mutator = None
                if endpoint == "status":
                    status_mutator = lambda payload: payload.__setitem__(
                        "message", "GPU contention: pid=12"
                    )
                else:
                    capabilities_mutator = lambda payload: payload.__setitem__(
                        "message", "GPU contention: username=private"
                    )
                with self.assertRaisesRegex(PROBES.ProbeError, "warning"):
                    self.run_dft(
                        MutatingDftClient(
                            status_mutator=status_mutator,
                            capabilities_mutator=capabilities_mutator,
                        )
                    )

    def test_dft_public_projection_rejects_private_process_fields(self) -> None:
        class LeakingClient(DftClient):
            def response(self, method, path, body, headers):  # noqa: ANN001
                result = super().response(method, path, body, headers)
                if method == "GET" and path.endswith("/capabilities"):
                    result[2]["debug"] = {
                        "argv": ["python", "private.py"],
                        "executable": "/private/python",
                    }
                return result

        with self.assertRaisesRegex(PROBES.ProbeError, "leak"):
            self.run_dft(LeakingClient(), require_quarantined=True)

    def test_dft_scientific_vectors_are_exact_shape_and_finite(self) -> None:
        cases = {
            "charge nan": lambda result: result["properties"]["charges"].__setitem__(
                "values_e", [float("nan"), 0.4, 0.4]
            ),
            "charge inf": lambda result: result["properties"]["charges"].__setitem__(
                "values_e", [float("inf"), 0.4, 0.4]
            ),
            "force outer shape": lambda result: result["properties"]["forces"].__setitem__(
                "values_eV_per_A", [[0.0, 0.0, 0.0]] * 2
            ),
            "force inner shape": lambda result: result["properties"]["forces"].__setitem__(
                "values_eV_per_A", [[0.0, 0.0], [0.0] * 3, [0.0] * 3]
            ),
            "force nan": lambda result: result["properties"]["forces"].__setitem__(
                "values_eV_per_A",
                [[float("nan"), 0.0, 0.0], [0.0] * 3, [0.0] * 3],
            ),
            "force inf": lambda result: result["properties"]["forces"].__setitem__(
                "values_eV_per_A",
                [[float("inf"), 0.0, 0.0], [0.0] * 3, [0.0] * 3],
            ),
        }
        for label, mutate in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(PROBES.ProbeError):
                    self.run_dft(MutatingDftClient(result_mutator=mutate))

    def test_md_proves_one_running_two_queued_and_both_cancellations(self) -> None:
        client = MdClient()
        result = PROBES.run_md_probe(
            client,
            timeout_seconds=1,
            poll_seconds=0.001,
        )
        self.assertEqual(result["capacity"], {"running": 1, "queued": 2, "active": 3})
        self.assertEqual(result["queue_positions"], [None, 1, 2])
        self.assertEqual(result["fourth_request"]["http_status"], 429)
        self.assertEqual(client.cancelled, ["job-2", "job-1", "job-3"])
        self.assertEqual(result["final_active_jobs"], 0)

    def test_read_only_and_frontend_projections_do_not_seal_content(self) -> None:
        client = ReadAndFrontendClient()
        api = PROBES.run_read_only_api_probes(
            client,
            expected_property_records=615159,
            knowledge_query="polyimide",
        )
        frontend = PROBES.run_frontend_probe(client)
        self.assertEqual(api["property_histogram"]["total_records"], 615159)
        self.assertEqual(api["knowledge"]["group_count"], 1)
        self.assertEqual(api["tg_assistant"]["guide_version"], 3)
        self.assertNotIn("must not be sealed", json.dumps(api))
        self.assertEqual(len(frontend["assets"]), 2)
        self.assertIn("/homopolymer-property-prediction", frontend["routes"])
        self.assertIn("/reverse-design", frontend["routes"])
        self.assertIn("/monomer-dft", frontend["routes"])

    def test_loopback_and_private_evidence_are_fail_closed(self) -> None:
        with self.assertRaisesRegex(PROBES.ProbeError, "127.0.0.1"):
            PROBES.LoopbackClient("http://localhost:9000")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "evidence"
            root.mkdir(mode=0o700)
            os.chmod(root, 0o700)
            private = PROBES._private_evidence_directory(root)
            report = {"schema_version": 1, "status": "passed"}
            output = PROBES._seal_report(private, "deploy-test-0001", report)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            document = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(document["report_sha256"], PROBES._digest(report))
            with self.assertRaises(PROBES.ProbeError):
                PROBES._seal_report(private, "deploy-test-0001", report)
            os.chmod(root, 0o755)
            with self.assertRaisesRegex(PROBES.ProbeError, "0700"):
                PROBES._private_evidence_directory(root)

    def test_report_publish_ignores_truncated_staging_and_never_replaces_final(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "evidence"
            root.mkdir(mode=0o700)
            os.chmod(root, 0o700)
            operation_id = "deploy-test-0001"
            truncated = root / (
                f".production-acceptance-{operation_id}.json.crashed.staging"
            )
            truncated.write_bytes(b'{"truncated":')
            os.chmod(truncated, 0o600)
            report = {"schema_version": 1, "status": "passed"}
            output = PROBES._seal_report(root, operation_id, report)
            expected = dict(report, report_sha256=PROBES._digest(report))
            self.assertEqual(output.read_bytes(), PROBES._canonical_bytes(expected) + b"\n")
            self.assertEqual(output.stat().st_nlink, 1)
            self.assertEqual(truncated.read_bytes(), b'{"truncated":')
            with self.assertRaises(PROBES.ProbeError):
                PROBES._seal_report(
                    root,
                    operation_id,
                    {"schema_version": 1, "status": "failed"},
                )
            self.assertEqual(output.read_bytes(), PROBES._canonical_bytes(expected) + b"\n")

    def test_report_staging_write_failure_never_exposes_partial_final(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "evidence"
            root.mkdir(mode=0o700)
            os.chmod(root, 0o700)
            original_write = os.write
            calls = 0

            def fail_after_prefix(descriptor, payload):  # noqa: ANN001
                nonlocal calls
                calls += 1
                if calls == 1:
                    return original_write(descriptor, payload[:8])
                raise OSError("simulated interrupted staging write")

            with mock.patch.object(PROBES.os, "write", side_effect=fail_after_prefix):
                with self.assertRaises(PROBES.ProbeError):
                    PROBES._seal_report(
                        root,
                        "deploy-test-0001",
                        {"schema_version": 1, "status": "passed"},
                    )
            final = root / "production-acceptance-deploy-test-0001.json"
            self.assertFalse(final.exists())
            output = PROBES._seal_report(
                root,
                "deploy-test-0001",
                {"schema_version": 1, "status": "passed"},
            )
            json.loads(output.read_text(encoding="utf-8"))

    def test_authority_binds_staged_operation_and_fixed_evidence_directory(self) -> None:
        authority = {
            "schema_version": 1,
            "phase": "awaiting-acceptance",
            "operation_id": "deploy-test-0001",
            "target_sha": "a" * 40,
            "target_tree": "b" * 40,
            "descriptor_sha256": "sha256:" + "c" * 64,
            "staged_current_state_sha256": "sha256:" + "d" * 64,
            "control_release_id": "e" * 64,
            "staged_at": "2026-08-14T06:00:00Z",
            "acceptance_not_before": "2026-08-14T06:15:00Z",
        }
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            operation = state / "prepared" / "deploy-test-0001"
            operation.mkdir(mode=0o700, parents=True)
            os.chmod(operation, 0o700)
            path = operation / "acceptance-authority.json"
            path.write_bytes(PROBES._canonical_bytes(authority) + b"\n")
            os.chmod(path, 0o600)
            marker = {
                "schema_version": 3,
                "action": "deploy",
                "phase": "awaiting-acceptance",
                "operation_id": "deploy-test-0001",
                "source_sha": "a" * 40,
                "descriptor_sha256": "sha256:" + "c" * 64,
                "candidate_state_sha256": "sha256:" + "d" * 64,
                "executor_control": {"release_id": "e" * 64},
                "acceptance_started_at": "2026-08-14T06:00:00Z",
                "acceptance_not_before": "2026-08-14T06:15:00Z",
                "acceptance_authority_path": os.fspath(path),
                "acceptance_authority_sha256": "sha256:"
                + hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            marker_path = state / "deploy-in-progress.json"
            marker_path.write_bytes(PROBES._canonical_bytes(marker) + b"\n")
            os.chmod(marker_path, 0o600)
            directory, loaded, digest = PROBES._load_acceptance_authority(
                path,
                operation_id="deploy-test-0001",
                source_sha="a" * 40,
            )
            self.assertEqual(directory, operation)
            self.assertEqual(loaded, authority)
            self.assertEqual(digest, marker["acceptance_authority_sha256"])
            tampered = dict(authority, phase="acceptance-started")
            path.write_bytes(PROBES._canonical_bytes(tampered) + b"\n")
            os.chmod(path, 0o600)
            with self.assertRaisesRegex(PROBES.ProbeError, "binding"):
                PROBES._load_acceptance_authority(
                    path,
                    operation_id="deploy-test-0001",
                    source_sha="a" * 40,
                )


if __name__ == "__main__":
    unittest.main()
