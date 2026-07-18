#!/usr/bin/env python3
"""Strict schema and cross-binding rules for real monomer DFT GPU acceptance.

The acceptance report is intentionally self-sealing and contains the evidence
needed by the standalone production-readiness control release.  A generic
"skipped" GPU is never accepted: GPU3 must either execute an admitted,
fenced workload or be proven unavailable because of one exact foreign Docker
claim.  Physical GPU2 must have byte-for-byte identical process and memory
snapshots before and after the acceptance run.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from typing import Any, Mapping


SCHEMA_VERSION = 1
GPU_UUIDS = {
    "1": "GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771",
    "2": "GPU-89c7c52c-e252-0135-c157-24eee1a1ccbe",
    "3": "GPU-0818ca6b-d9b6-af6a-71bf-afe3777ee3a5",
}
EXTERNAL_RESERVATIONS_SHA256 = (
    "sha256:"
    "06e2f23078b181fc296181d241c139b5539b1aaaa4f25b601939c7e33a62a9e4"
)
GPU3_BLOCKED_REASON = (
    "GPU3 has an unmanaged Docker DeviceRequest; remove only after host audit"
)
PYTHON_VERSION_RE = re.compile(r"^3\.12\.[0-9]+$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{64}$")
CONTAINER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class GpuAcceptanceError(RuntimeError):
    """The supplied report does not prove the required GPU acceptance."""


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def canonical_json_digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def seal_report(report: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy sealed over every field except ``report_sha256``."""

    payload = dict(report)
    payload.pop("report_sha256", None)
    payload["report_sha256"] = canonical_json_digest(payload)
    return payload


def _fail(message: str) -> None:
    raise GpuAcceptanceError(message)


def _exact(value: object, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        _fail(f"{name} fields differ from the acceptance schema")
    return dict(value)


def _sha(value: object, name: str) -> str:
    if not isinstance(value, str) or SHA_RE.fullmatch(value) is None:
        _fail(f"{name} is not a full Git object ID")
    return value


def _digest(value: object, name: str) -> str:
    if not isinstance(value, str) or DIGEST_RE.fullmatch(value) is None:
        _fail(f"{name} is not a sha256 digest")
    return value


def _true(value: object, name: str) -> None:
    if value is not True:
        _fail(f"{name} is not proven")


def _timestamp(value: object) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        _fail("GPU acceptance capture time is not canonical UTC")
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        _fail("GPU acceptance capture time is invalid")
    if parsed.tzinfo != dt.UTC:
        _fail("GPU acceptance capture time is not UTC")
    return value


def _finite_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(f"{name} is not numeric")
    number = float(value)
    if not math.isfinite(number):
        _fail(f"{name} is not finite")
    return number


def _validate_image(
    value: object,
    *,
    role: str,
    authority_sha: str,
    expected: Mapping[str, Any],
) -> dict[str, Any]:
    fields = {
        "role",
        "digest_ref",
        "index_digest",
        "platform_digest",
        "image_id",
        "revision",
        "source",
        "version",
    }
    image = _exact(value, fields, f"GPU acceptance {role} image")
    for name in ("index_digest", "platform_digest", "image_id"):
        _digest(image[name], f"GPU acceptance {role} {name}")
    if (
        image != dict(expected)
        or image["role"] != role
        or image["revision"] != authority_sha
        or image["version"] != f"sha-{authority_sha}"
        or not isinstance(image["digest_ref"], str)
        or image["digest_ref"].rsplit("@", 1)[-1] != image["index_digest"]
    ):
        _fail(f"GPU acceptance {role} image differs from exact F OCI evidence")
    return image


def _validate_runtime(
    value: object,
    *,
    runtime_contract: Mapping[str, Any],
    runtime_contract_sha256: str,
) -> dict[str, Any]:
    runtime = _exact(
        value,
        {
            "contract_sha256",
            "python_version",
            "uv_version",
            "build_lock_sha256",
            "source",
            "wheel",
            "model_registry_sha256",
            "models_sha256",
        },
        "GPU acceptance runtime",
    )
    source_lock = runtime_contract["source"]
    wheel_lock = runtime_contract["wheel"]
    expected_source = {
        "commit": source_lock["commit"],
        "tree": source_lock["tree"],
        "archive_sha256": source_lock["archive_inventory_sha256"],
    }
    expected_wheel = {
        "filename": wheel_lock["filename"],
        "sha256": wheel_lock["sha256"],
        "inventory_sha256": wheel_lock["inventory_sha256"],
        "record_sha256": wheel_lock["record_sha256"],
    }
    source = _exact(
        runtime["source"],
        {"commit", "tree", "archive_sha256"},
        "GPU acceptance AIMNet source",
    )
    wheel = _exact(
        runtime["wheel"],
        {"filename", "sha256", "inventory_sha256", "record_sha256"},
        "GPU acceptance AIMNet wheel",
    )
    _sha(source["commit"], "GPU acceptance AIMNet commit")
    _sha(source["tree"], "GPU acceptance AIMNet tree")
    _digest(source["archive_sha256"], "GPU acceptance AIMNet archive")
    for name in ("sha256", "inventory_sha256", "record_sha256"):
        _digest(wheel[name], f"GPU acceptance wheel {name}")
    if (
        runtime["contract_sha256"] != runtime_contract_sha256
        or not isinstance(runtime["python_version"], str)
        or PYTHON_VERSION_RE.fullmatch(runtime["python_version"]) is None
        or runtime["uv_version"] != runtime_contract["uv_version"]
        or runtime["build_lock_sha256"]
        != runtime_contract["build_lock_sha256"]
        or source != expected_source
        or wheel != expected_wheel
        or runtime["model_registry_sha256"]
        != runtime_contract["registry_sha256"]
        or runtime["models_sha256"] != runtime_contract["models_sha256"]
    ):
        _fail("GPU acceptance runtime differs from the exact AIMNet lock")
    for name in (
        "contract_sha256",
        "build_lock_sha256",
        "model_registry_sha256",
        "models_sha256",
    ):
        _digest(runtime[name], f"GPU acceptance runtime {name}")
    return runtime


def _validate_process(value: object) -> dict[str, Any]:
    process = _exact(
        value,
        {"pid", "process_start_ticks", "process_name", "used_memory_mib"},
        "GPU2 process",
    )
    for name in ("pid", "process_start_ticks", "used_memory_mib"):
        if (
            isinstance(process[name], bool)
            or not isinstance(process[name], int)
            or process[name] < (0 if name == "used_memory_mib" else 1)
        ):
            _fail(f"GPU2 process {name} is invalid")
    if (
        not isinstance(process["process_name"], str)
        or not process["process_name"]
        or len(process["process_name"]) > 512
    ):
        _fail("GPU2 process name is invalid")
    return process


def _validate_snapshot(value: object, name: str) -> dict[str, Any]:
    snapshot = _exact(
        value,
        {"index", "uuid", "memory_used_mib", "compute_processes"},
        name,
    )
    if (
        snapshot["index"] != 2
        or snapshot["uuid"] != GPU_UUIDS["2"]
        or isinstance(snapshot["memory_used_mib"], bool)
        or not isinstance(snapshot["memory_used_mib"], int)
        or snapshot["memory_used_mib"] < 0
        or not isinstance(snapshot["compute_processes"], list)
    ):
        _fail(f"{name} has an invalid GPU2 identity")
    processes = [_validate_process(item) for item in snapshot["compute_processes"]]
    if processes != sorted(
        processes,
        key=lambda item: (
            item["pid"],
            item["process_start_ticks"],
            item["process_name"],
            item["used_memory_mib"],
        ),
    ):
        _fail(f"{name} process inventory is not canonical")
    identities = {
        (item["pid"], item["process_start_ticks"]) for item in processes
    }
    if len(identities) != len(processes):
        _fail(f"{name} contains duplicate process identities")
    return snapshot


def _validate_gpu1(value: object) -> dict[str, Any]:
    gpu = _exact(
        value,
        {
            "index",
            "uuid",
            "mode",
            "cuda_started",
            "fencing_verified",
            "evidence_sha256",
        },
        "GPU1 evidence",
    )
    if (
        gpu["index"] != 1
        or gpu["uuid"] != GPU_UUIDS["1"]
        or gpu["mode"] != "actual"
    ):
        _fail("GPU1 was not actually accepted")
    _true(gpu["cuda_started"], "GPU1 CUDA execution")
    _true(gpu["fencing_verified"], "GPU1 fencing")
    _digest(gpu["evidence_sha256"], "GPU1 evidence")
    return gpu


def _validate_gpu2(value: object) -> dict[str, Any]:
    gpu = _exact(
        value,
        {
            "index",
            "uuid",
            "mode",
            "cuda_started",
            "before",
            "after",
            "processes_unchanged",
            "memory_unchanged",
        },
        "GPU2 evidence",
    )
    before = _validate_snapshot(gpu["before"], "GPU2 before snapshot")
    after = _validate_snapshot(gpu["after"], "GPU2 after snapshot")
    if (
        gpu["index"] != 2
        or gpu["uuid"] != GPU_UUIDS["2"]
        or gpu["mode"] != "unchanged"
        or gpu["cuda_started"] is not False
        or before != after
    ):
        _fail("production GPU2 changed or was contacted by acceptance")
    _true(gpu["processes_unchanged"], "GPU2 process stability")
    _true(gpu["memory_unchanged"], "GPU2 memory stability")
    return gpu


def _validate_gpu3(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail("GPU3 evidence is not an object")
    mode = value.get("mode")
    base_fields = {
        "index",
        "uuid",
        "mode",
        "cuda_started",
        "fencing_verified",
        "evidence_sha256",
    }
    if mode == "actual":
        gpu = _exact(value, base_fields, "GPU3 actual evidence")
        if gpu["cuda_started"] is not True or gpu["fencing_verified"] is not True:
            _fail("GPU3 actual mode lacks CUDA and fencing proof")
    elif mode == "externally_fenced":
        gpu = _exact(
            value,
            base_fields
            | {
                "reservations_sha256",
                "blocked_reason",
                "claim",
                "rejection",
            },
            "GPU3 external-fence evidence",
        )
        if gpu["cuda_started"] is not False or gpu["fencing_verified"] is not True:
            _fail("GPU3 external-fence mode started CUDA or lacks fencing proof")
        if gpu["reservations_sha256"] != EXTERNAL_RESERVATIONS_SHA256:
            _fail("GPU3 reservation policy differs from the governed config")
        if gpu["blocked_reason"] != GPU3_BLOCKED_REASON:
            _fail("GPU3 governed blocked reason differs")
        _digest(gpu["reservations_sha256"], "GPU3 reservations config")
        claim = _exact(
            gpu["claim"],
            {
                "kind",
                "container_id",
                "container_name",
                "device_request_sha256",
            },
            "GPU3 foreign claim",
        )
        if (
            claim["kind"] != "docker"
            or not isinstance(claim["container_id"], str)
            or CONTAINER_ID_RE.fullmatch(claim["container_id"]) is None
            or not isinstance(claim["container_name"], str)
            or CONTAINER_NAME_RE.fullmatch(claim["container_name"]) is None
        ):
            _fail("GPU3 foreign Docker claim identity is invalid")
        _digest(claim["device_request_sha256"], "GPU3 Docker DeviceRequest")
        rejection = _exact(
            gpu["rejection"],
            {
                "code",
                "gpu_index",
                "gpu_uuid",
                "placement",
                "broker_report_sha256",
            },
            "GPU3 Broker rejection",
        )
        if (
            rejection["code"] != "gpu_capacity_unavailable"
            or rejection["gpu_index"] != 3
            or rejection["gpu_uuid"] != GPU_UUIDS["3"]
            or rejection["placement"] != "overflow"
        ):
            _fail("GPU3 external claim lacks the exact Broker rejection")
        _digest(rejection["broker_report_sha256"], "GPU3 Broker rejection")
    else:
        _fail("GPU3 must be actual or externally_fenced; skipped is forbidden")
    if gpu["index"] != 3 or gpu["uuid"] != GPU_UUIDS["3"]:
        _fail("GPU3 identity differs from the approved host inventory")
    _digest(gpu["evidence_sha256"], "GPU3 evidence")
    return gpu


def _validate_coverage(
    value: object,
    *,
    gpu3_mode: str,
) -> dict[str, Any]:
    coverage = _exact(
        value,
        {"direct_science", "broker_uds_backend_e2e"},
        "GPU acceptance coverage",
    )
    direct = _exact(
        coverage["direct_science"],
        {
            "status",
            "gpu_index",
            "gpu_uuid",
            "properties",
            "energy_eV",
            "max_force_eV_per_A",
            "hessian_symmetry_max_abs_eV_per_A2",
            "report_sha256",
        },
        "direct science coverage",
    )
    if (
        direct["status"] != "passed"
        or direct["gpu_index"] != 1
        or direct["gpu_uuid"] != GPU_UUIDS["1"]
        or direct["properties"] != ["energy", "forces", "hessian"]
        or _finite_number(direct["max_force_eV_per_A"], "direct force") < 0
        or _finite_number(
            direct["hessian_symmetry_max_abs_eV_per_A2"],
            "direct Hessian symmetry",
        )
        < 0
    ):
        _fail("direct energy/forces/Hessian acceptance is incomplete")
    _finite_number(direct["energy_eV"], "direct energy")
    _digest(direct["report_sha256"], "direct science report")

    e2e = _exact(
        coverage["broker_uds_backend_e2e"],
        {
            "status",
            "transport",
            "gpu_indices",
            "overflow_test_status",
            "completed_job_id",
            "cancelled_job_id",
            "submit",
            "poll",
            "cancel",
            "journal",
            "artifact",
            "bundle",
            "fencing",
            "completed_journal_sha256",
            "cancelled_journal_sha256",
            "artifact_sha256",
            "bundle_sha256",
            "provenance_sha256",
        },
        "Broker UDS Backend coverage",
    )
    expected_overflow = "passed" if gpu3_mode == "actual" else "externally_fenced"
    if (
        e2e["status"] != "passed"
        or e2e["transport"] != "broker+uds+backend"
        # The public Backend E2E job is independently proven to have run on
        # GPU1. GPU3 actual mode is a separate Broker-leased direct overflow
        # calculation and must never be represented as Backend provenance.
        or e2e["gpu_indices"] != [1]
        or e2e["overflow_test_status"] != expected_overflow
        or not isinstance(e2e["completed_job_id"], str)
        or UUID_RE.fullmatch(e2e["completed_job_id"]) is None
        or not isinstance(e2e["cancelled_job_id"], str)
        or UUID_RE.fullmatch(e2e["cancelled_job_id"]) is None
        or e2e["completed_job_id"] == e2e["cancelled_job_id"]
    ):
        _fail("Broker + UDS + Backend acceptance binding is incomplete")
    for name in ("submit", "poll", "cancel", "journal", "artifact", "bundle", "fencing"):
        _true(e2e[name], f"Broker E2E {name}")
    for name in (
        "completed_journal_sha256",
        "cancelled_journal_sha256",
        "artifact_sha256",
        "bundle_sha256",
        "provenance_sha256",
    ):
        _digest(e2e[name], f"Broker E2E {name}")
    return coverage


def validate_report(
    value: object,
    *,
    authority: Mapping[str, str],
    bridge: Mapping[str, str],
    authority_images: Mapping[str, Mapping[str, Any]],
    runtime_contract: Mapping[str, Any],
    runtime_contract_sha256: str,
) -> dict[str, Any]:
    """Validate and return one exact, self-sealed GPU acceptance report."""

    report = _exact(
        value,
        {
            "schema_version",
            "status",
            "captured_at",
            "authority",
            "bridge",
            "images",
            "runtime",
            "coverage",
            "gpus",
            "report_sha256",
        },
        "GPU acceptance report",
    )
    if report["schema_version"] != SCHEMA_VERSION or report["status"] != "passed":
        _fail("GPU acceptance report did not pass the supported schema")
    _timestamp(report["captured_at"])
    expected_authority = {"sha": authority["sha"], "tree": authority["tree"]}
    expected_bridge = {"sha": bridge["sha"], "tree": bridge["tree"]}
    if (
        _exact(report["authority"], {"sha", "tree"}, "GPU authority")
        != expected_authority
        or _exact(report["bridge"], {"sha", "tree"}, "GPU bridge")
        != expected_bridge
    ):
        _fail("GPU acceptance report differs from exact F/B Git authority")
    for group_name, group in (
        ("authority", expected_authority),
        ("bridge", expected_bridge),
    ):
        _sha(group["sha"], f"GPU {group_name} commit")
        _sha(group["tree"], f"GPU {group_name} tree")
    images = _exact(report["images"], {"backend", "web"}, "GPU images")
    for role in ("backend", "web"):
        _validate_image(
            images[role],
            role=role,
            authority_sha=authority["sha"],
            expected=authority_images[role],
        )
    _validate_runtime(
        report["runtime"],
        runtime_contract=runtime_contract,
        runtime_contract_sha256=runtime_contract_sha256,
    )
    gpus = _exact(report["gpus"], {"1", "2", "3"}, "per-GPU evidence")
    _validate_gpu1(gpus["1"])
    _validate_gpu2(gpus["2"])
    gpu3 = _validate_gpu3(gpus["3"])
    _validate_coverage(report["coverage"], gpu3_mode=gpu3["mode"])
    _digest(report["report_sha256"], "GPU acceptance report seal")
    if report["report_sha256"] != canonical_json_digest(
        {key: report[key] for key in report if key != "report_sha256"}
    ):
        _fail("GPU acceptance report seal differs from its content")
    return report


def _load_json(path: Path) -> object:
    if path.is_symlink() or not path.is_file():
        raise GpuAcceptanceError("GPU acceptance report is unavailable or unsafe")
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--authority-sha", required=True)
    parser.add_argument("--authority-tree", required=True)
    parser.add_argument("--bridge-sha", required=True)
    parser.add_argument("--bridge-tree", required=True)
    parser.add_argument("--images", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        script_root = Path(__file__).resolve().parent
        sys.path.insert(0, str(script_root))
        import monomer_dft_runtime_contract as runtime_contract_module

        images = _load_json(args.images)
        if not isinstance(images, dict):
            _fail("authority image input is not an object")
        validated = validate_report(
            _load_json(args.report),
            authority={"sha": args.authority_sha, "tree": args.authority_tree},
            bridge={"sha": args.bridge_sha, "tree": args.bridge_tree},
            authority_images=images,
            runtime_contract=runtime_contract_module.RUNTIME_CONTRACT,
            runtime_contract_sha256=(
                runtime_contract_module.RUNTIME_CONTRACT_SHA256
            ),
        )
    except (GpuAcceptanceError, OSError, json.JSONDecodeError) as exc:
        print(f"GPU acceptance report: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "status": "ok",
                "report_sha256": validated["report_sha256"],
                "gpu3_mode": validated["gpus"]["3"]["mode"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
