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
import posixpath
import re
import sys
from typing import Any, Mapping


SCHEMA_VERSION = 1
PRODUCTION_BASELINE_SHA = "b875829c3f008b5ee733d8ffced3093e4cbb07c5"
PRODUCTION_BASELINE_TREE = "4f68c10a39c6943f7ff13af33d547ebb8f5d7a00"
PRODUCTION_BASELINE_ORIGIN_SHA256 = (
    "sha256:"
    "a88f7eb8b9cee4e1f38bd69b804093ab830b6227b741b820bff4b04b1329fafb"
)
PRODUCTION_BASELINE_HEAD_REF_SHA256 = (
    "sha256:"
    "599bbcd7b7f94b50d9b83318ba0dd4b8e1ba9e39d1d3ee73d1fbbd70496d0f93"
)
PRODUCTION_BASELINE_SNAPSHOT = {
    "device": 66_304,
    "inode": 40_763_411,
    "mtime_ns": 1_783_904_390_143_505_793,
    "head": PRODUCTION_BASELINE_SHA,
    "tree": PRODUCTION_BASELINE_TREE,
    "status_sha256": (
        "sha256:"
        "ddcaa922298dfd90458a991016d362d91ce977cd0cfc2522d26f2125ac097931"
    ),
    "status_boundary_count": 25,
    "tracked_path_count": 321,
    "ignored_path_count": 115,
    "untracked_path_count": 0,
    "inventory_entry_count": 507,
    "content_bytes": 11_953_024_420,
    "tracked_content_bytes": 162_602_279,
    "ignored_content_bytes": 11_790_422_141,
    "untracked_content_bytes": 0,
    "boundary_sha256": (
        "sha256:"
        "f47849ef010b29edef466bcbcab63c4d52c4cca498731bdcb7d2d02e788cad2c"
    ),
    "inventory_sha256": (
        "sha256:"
        "9508364fee2ebcfc45e5c340c1db30f0242340e672a4de190ed49984223d58d5"
    ),
    "git_authority_entry_count": 26,
    "git_authority_content_bytes": 60_126,
    "git_authority_sha256": (
        "sha256:"
        "c5de05bdd91d6f7c3230632aac6aa6b161a5de1b882ff302605ea181359ac75e"
    ),
    "git_config_sha256": (
        "sha256:"
        "d122838c3d6989e4c463adcdcd988499f54eaf2f35121f42efe1938aa3f959be"
    ),
    "git_origin_url_count": 1,
    "git_origin_sha256": PRODUCTION_BASELINE_ORIGIN_SHA256,
    "git_ref_count": 4,
    "git_refs_sha256": (
        "sha256:"
        "58ae4cd4f2368445812e072fc977c076a405f2939728555503db161ab3d95c28"
    ),
    "git_head_ref_sha256": PRODUCTION_BASELINE_HEAD_REF_SHA256,
}
GPU_UUIDS = {
    "1": "GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771",
    "2": "GPU-89c7c52c-e252-0135-c157-24eee1a1ccbe",
    "3": "GPU-0818ca6b-d9b6-af6a-71bf-afe3777ee3a5",
}
EXTERNAL_RESERVATIONS_SHA256 = (
    "sha256:"
    "90f4b7fed8ee3b4d4f6f4b225adefd9a225e27bb2b21b9ea89e4652bfdf76569"
)
GPU3_BLOCKED_REASON = (
    "GPU3 has an unmanaged Docker DeviceRequest; remove only after host audit"
)
AIMNET2_MODEL_SHA256 = (
    "sha256:"
    "f0f7c054539ad3261bd36f9b11c56d12f87cb723e25bea7521755bbd3ec24e28"
)
AIMNET2_MODEL_REGISTRY_KEY = "aimnet2-wb97m-d3_0"
AIMNET2_MODEL_FILENAME = "aimnet2_wb97m_d3_0.pt"
WATER_COORDINATES_ANGSTROM = [
    [0.0, 0.0, 0.1173],
    [0.0, 0.7572, -0.4692],
    [0.0, -0.7572, -0.4692],
]
CCO_BASELINE_EV = -4221.547007510834
CCO_TOLERANCE_EV = 0.001
PYTHON_VERSION_RE = re.compile(r"^3\.12\.[0-9]+$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{64}$")
CONTAINER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
PROJECT_NAME_RE = re.compile(
    r"^nexpoly_dft_fresh_[a-z0-9][a-z0-9_-]{0,40}$"
)
INSTANCE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
LEASE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
GPU3_REQUEST_ID_RE = re.compile(r"^dft-acceptance-3-[0-9a-f]{32}$")
GPU3_REJECTION_REQUEST_ID_RE = re.compile(
    r"^dft-acceptance-gpu3-reject-[0-9a-f]{32}$"
)
GPU_SCOPE_CGROUP_RE = re.compile(
    r"^/user\.slice/user-(?P<uid>[1-9][0-9]*)\.slice/"
    r"user@(?P=uid)\.service/nexpoly\.slice/nexpoly-gpu\.slice/"
    r"nexpoly-gpu-jobs\.slice/nexpoly-gpu-job-"
    r"(?P<lease_id>[0-9a-f]{32})\.scope$"
)
DEFAULT_MAX_AGE_SECONDS = 15 * 60
MAX_FUTURE_SKEW_SECONDS = 60
GPU2_AUDIT_INTERVAL_MS = 250
GPU2_MAX_SAMPLE_GAP_MS = 750


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


def canonical_json_file_digest(value: object) -> str:
    """Digest the canonical JSON-plus-newline private evidence format."""

    return "sha256:" + hashlib.sha256(
        canonical_json_bytes(value) + b"\n"
    ).hexdigest()


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


def _parsed_timestamp(value: object, name: str) -> dt.datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        _fail(f"{name} is not canonical UTC")
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        _fail(f"{name} is invalid")
    if parsed.tzinfo != dt.UTC:
        _fail(f"{name} is not UTC")
    return parsed


def _timestamp(value: object) -> str:
    _parsed_timestamp(value, "GPU acceptance capture time")
    assert isinstance(value, str)
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


def _validate_gpu2_audit(
    value: object,
    *,
    before: dict[str, Any],
    after: dict[str, Any],
) -> dict[str, Any]:
    audit = _exact(
        value,
        {
            "started_at",
            "finished_at",
            "interval_ms",
            "sample_count",
            "samples",
            "sampled_at",
            "samples_sha256",
            "drift_detected",
        },
        "GPU2 continuous audit",
    )
    started = _parsed_timestamp(audit["started_at"], "GPU2 audit start")
    finished = _parsed_timestamp(audit["finished_at"], "GPU2 audit finish")
    if finished < started:
        _fail("GPU2 continuous audit interval is reversed")
    interval = audit["interval_ms"]
    count = audit["sample_count"]
    samples = audit["samples"]
    sampled_at = audit["sampled_at"]
    if (
        isinstance(interval, bool)
        or not isinstance(interval, int)
        or interval != GPU2_AUDIT_INTERVAL_MS
        or isinstance(count, bool)
        or not isinstance(count, int)
        or count < 2
        or not isinstance(samples, list)
        or len(samples) != count
        or not isinstance(sampled_at, list)
        or len(sampled_at) != count
        or audit["drift_detected"] is not False
    ):
        _fail("GPU2 continuous audit sampling contract is invalid")
    validated_samples = [
        _validate_snapshot(sample, f"GPU2 audit sample {index}")
        for index, sample in enumerate(samples)
    ]
    if (
        validated_samples[0] != before
        or validated_samples[-1] != after
        or any(sample != before for sample in validated_samples)
    ):
        _fail("GPU2 changed during the continuous audit window")
    sample_times = [
        _parsed_timestamp(value, f"GPU2 audit sample {index} time")
        for index, value in enumerate(sampled_at)
    ]
    maximum_gap = dt.timedelta(milliseconds=GPU2_MAX_SAMPLE_GAP_MS)
    if (
        sample_times[0] != started
        or sample_times[-1] > finished
        or finished - sample_times[-1] > maximum_gap
        or any(
            current < previous or current - previous > maximum_gap
            for previous, current in zip(
                sample_times,
                sample_times[1:],
                strict=False,
            )
        )
    ):
        _fail("GPU2 continuous audit has a sampling gap or time drift")
    _digest(audit["samples_sha256"], "GPU2 audit samples")
    if audit["samples_sha256"] != canonical_json_digest(validated_samples):
        _fail("GPU2 audit sample digest differs from its evidence")
    return audit


def _validate_process_identity(value: object, name: str) -> dict[str, Any]:
    identity = _exact(
        value,
        {
            "pid",
            "process_start_ticks",
            "cwd",
            "command_sha256",
        },
        name,
    )
    for field in ("pid", "process_start_ticks"):
        item = identity[field]
        if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
            _fail(f"{name} {field} is invalid")
    if not isinstance(identity["cwd"], str) or not identity["cwd"].startswith("/"):
        _fail(f"{name} cwd is invalid")
    _digest(identity["command_sha256"], f"{name} command")
    return identity


def _validate_control_plane(
    value: object,
    *,
    authority: Mapping[str, str],
    authority_images: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    control = _exact(
        value,
        {
            "mode",
            "image_mode",
            "project_name",
            "authority",
            "broker",
            "worker",
            "containers",
            "cleanup",
        },
        "GPU acceptance control plane",
    )
    if (
        control["mode"] != "fresh_exact_f"
        or control["image_mode"] != "published_exact"
        or not isinstance(control["project_name"], str)
        or PROJECT_NAME_RE.fullmatch(control["project_name"]) is None
        or _exact(
            control["authority"],
            {"sha", "tree"},
            "control-plane authority",
        )
        != {"sha": authority["sha"], "tree": authority["tree"]}
    ):
        _fail("GPU acceptance did not use a fresh exact-F control plane")
    broker = _exact(
        control["broker"],
        {
            "instance_id",
            "process",
            "initial_leases",
            "final_leases",
            "socket_sha256",
        },
        "fresh Broker evidence",
    )
    if (
        not isinstance(broker["instance_id"], str)
        or not broker["instance_id"]
        or broker["initial_leases"] != []
        or broker["final_leases"] != []
    ):
        _fail("fresh Broker lifecycle or lease inventory is incomplete")
    _validate_process_identity(broker["process"], "fresh Broker process")
    _digest(broker["socket_sha256"], "fresh Broker socket identity")
    worker = _exact(
        control["worker"],
        {
            "instance_id",
            "process",
            "authority_sha",
            "fresh",
        },
        "fresh Worker evidence",
    )
    if (
        not isinstance(worker["instance_id"], str)
        or INSTANCE_ID_RE.fullmatch(worker["instance_id"]) is None
        or worker["authority_sha"] != authority["sha"]
        or worker["fresh"] is not True
    ):
        _fail("fresh Worker identity differs from exact F")
    _validate_process_identity(worker["process"], "fresh Worker process")
    containers = _exact(
        control["containers"],
        {"project_name", "backend", "web", "postgres"},
        "fresh Compose evidence",
    )
    if containers["project_name"] != control["project_name"]:
        _fail("fresh Compose project identity drifted")
    for role in ("backend", "web"):
        container = _exact(
            containers[role],
            {
                "container_id",
                "image_id",
                "digest_ref",
                "index_digest",
                "platform_digest",
                "source_revision",
                "source",
                "version",
                "started_at",
            },
            f"fresh {role} container",
        )
        if (
            not isinstance(container["container_id"], str)
            or CONTAINER_ID_RE.fullmatch(container["container_id"]) is None
        ):
            _fail(f"fresh {role} container identity is invalid")
        _digest(container["image_id"], f"fresh {role} image ID")
        for name in ("index_digest", "platform_digest"):
            _digest(container[name], f"fresh {role} {name}")
        _parsed_timestamp(container["started_at"], f"fresh {role} start time")
        expected = authority_images[role]
        if container != {
            "container_id": container["container_id"],
            "image_id": expected["image_id"],
            "digest_ref": expected["digest_ref"],
            "index_digest": expected["index_digest"],
            "platform_digest": expected["platform_digest"],
            "source_revision": expected["revision"],
            "source": expected["source"],
            "version": expected["version"],
            "started_at": container["started_at"],
        }:
            _fail(f"fresh {role} container did not run the exact F OCI image")
    postgres = _exact(
        containers["postgres"],
        {"container_id", "image_id", "source_revision", "started_at"},
        "fresh postgres container",
    )
    if (
        not isinstance(postgres["container_id"], str)
        or CONTAINER_ID_RE.fullmatch(postgres["container_id"]) is None
        or postgres["source_revision"] is not None
    ):
        _fail("fresh postgres container identity is invalid")
    _digest(postgres["image_id"], "fresh postgres image ID")
    _parsed_timestamp(postgres["started_at"], "fresh postgres start time")
    cleanup = _exact(
        control["cleanup"],
        {
            "worker_stopped",
            "containers_removed",
            "volume_removed",
            "network_removed",
            "broker_drained",
            "broker_stopped",
            "mps_indices_stopped",
            "leases_empty",
            "candidate_image_tags",
            "candidate_image_tags_sha256",
            "candidate_images_absent_before",
            "candidate_images_removed",
            "ordinary_dev_images_before_sha256",
            "ordinary_dev_images_after_sha256",
            "ordinary_dev_images_unchanged",
        },
        "fresh control-plane cleanup",
    )
    for field in (
        "worker_stopped",
        "containers_removed",
        "volume_removed",
        "network_removed",
        "broker_drained",
        "broker_stopped",
        "leases_empty",
        "candidate_images_absent_before",
        "candidate_images_removed",
        "ordinary_dev_images_unchanged",
    ):
        _true(cleanup[field], f"fresh cleanup {field}")
    for field in (
        "candidate_image_tags_sha256",
        "ordinary_dev_images_before_sha256",
        "ordinary_dev_images_after_sha256",
    ):
        _digest(cleanup[field], f"fresh cleanup {field}")
    if (
        cleanup["candidate_image_tags"] != []
        or cleanup["candidate_image_tags_sha256"]
        != canonical_json_digest([])
        or cleanup["ordinary_dev_images_before_sha256"]
        != cleanup["ordinary_dev_images_after_sha256"]
    ):
        _fail("final-main cleanup changed or retained a development image tag")
    if (
        not isinstance(cleanup["mps_indices_stopped"], list)
        or cleanup["mps_indices_stopped"] not in ([1], [1, 3])
        or 2 in cleanup["mps_indices_stopped"]
    ):
        _fail("fresh MPS cleanup did not preserve the GPU2 hard fence")
    return control


def _validate_production_cas(value: object) -> dict[str, Any]:
    cas = _exact(
        value,
        {"before", "after", "unchanged"},
        "production repository CAS",
    )
    fields = set(PRODUCTION_BASELINE_SNAPSHOT)
    snapshots = []
    for name in ("before", "after"):
        snapshot = _exact(cas[name], fields, f"production CAS {name}")
        for field in (
            "device",
            "inode",
            "mtime_ns",
            "status_boundary_count",
            "tracked_path_count",
            "ignored_path_count",
            "untracked_path_count",
            "inventory_entry_count",
            "content_bytes",
            "tracked_content_bytes",
            "ignored_content_bytes",
            "untracked_content_bytes",
            "git_authority_entry_count",
            "git_authority_content_bytes",
            "git_origin_url_count",
            "git_ref_count",
        ):
            item = snapshot[field]
            if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                _fail(f"production CAS {name} {field} is invalid")
        if snapshot != PRODUCTION_BASELINE_SNAPSHOT:
            _fail(
                f"production CAS {name} differs from the complete fixed baseline"
            )
        if (
            snapshot["status_boundary_count"]
            > snapshot["inventory_entry_count"]
            or snapshot["tracked_path_count"]
            + snapshot["ignored_path_count"]
            + snapshot["untracked_path_count"]
            > snapshot["inventory_entry_count"]
            or snapshot["tracked_content_bytes"]
            + snapshot["ignored_content_bytes"]
            + snapshot["untracked_content_bytes"]
            != snapshot["content_bytes"]
        ):
            _fail(f"production CAS {name} inventory counts are inconsistent")
        _sha(snapshot["head"], f"production CAS {name} head")
        _sha(snapshot["tree"], f"production CAS {name} tree")
        for field in (
            "status_sha256",
            "boundary_sha256",
            "inventory_sha256",
            "git_authority_sha256",
            "git_config_sha256",
            "git_origin_sha256",
            "git_refs_sha256",
            "git_head_ref_sha256",
        ):
            _digest(snapshot[field], f"production CAS {name} {field}")
        snapshots.append(snapshot)
    if cas["unchanged"] is not True or snapshots[0] != snapshots[1]:
        _fail("production repository changed during GPU acceptance")
    return cas


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
            "audit",
            "processes_unchanged",
            "memory_unchanged",
        },
        "GPU2 evidence",
    )
    before = _validate_snapshot(gpu["before"], "GPU2 before snapshot")
    after = _validate_snapshot(gpu["after"], "GPU2 after snapshot")
    _validate_gpu2_audit(gpu["audit"], before=before, after=after)
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


def validate_gpu3_direct_result(value: object) -> dict[str, Any]:
    """Validate the exact scientific payload emitted by the leased GPU3 child."""

    result = _exact(
        value,
        {
            "status",
            "preflight",
            "model",
            "model_sha256",
            "device",
            "water",
            "ase",
            "cco",
            "elapsed_seconds",
            "gpu_index",
            "gpu_uuid",
        },
        "GPU3 direct scientific result",
    )
    preflight = _exact(
        result["preflight"],
        {"default_model_path"},
        "GPU3 direct preflight",
    )
    model_path = preflight["default_model_path"]
    if (
        not isinstance(model_path, str)
        or not model_path.startswith("/")
        or posixpath.normpath(model_path) != model_path
        or posixpath.basename(model_path) != AIMNET2_MODEL_FILENAME
        or any(
            ord(character) < 0x20 or ord(character) == 0x7F
            for character in model_path
        )
    ):
        _fail("GPU3 direct model path is not the locked isolated checkpoint")
    if (
        result["status"] != "ok"
        or result["model"] != "aimnet2"
        or result["model_sha256"] != AIMNET2_MODEL_SHA256
        or result["device"] != "cuda:0"
        or result["gpu_index"] != 3
        or result["gpu_uuid"] != GPU_UUIDS["3"]
    ):
        _fail("GPU3 direct runtime or model identity is invalid")
    _digest(result["model_sha256"], "GPU3 direct model")

    water = _exact(
        result["water"],
        {
            "numbers",
            "coordinates_angstrom",
            "energy_eV",
            "charge_sum_e",
            "max_force_eV_per_A",
            "forces_shape",
            "hessian_shape",
            "hessian_symmetry_max_abs_eV_per_A2",
        },
        "GPU3 direct water result",
    )
    water_energy = _finite_number(
        water["energy_eV"], "GPU3 direct water energy"
    )
    if (
        water["numbers"] != [8, 1, 1]
        or any(
            isinstance(number, bool) or not isinstance(number, int)
            for number in water["numbers"]
        )
        or water["coordinates_angstrom"] != WATER_COORDINATES_ANGSTROM
        or any(
            isinstance(coordinate, bool)
            or not isinstance(coordinate, (int, float))
            or not math.isfinite(float(coordinate))
            for row in water["coordinates_angstrom"]
            for coordinate in row
        )
        or water["forces_shape"] != [3, 3]
        or water["hessian_shape"] != [3, 3, 3, 3]
        or abs(
            _finite_number(
                water["charge_sum_e"], "GPU3 direct water charge sum"
            )
        )
        > 1.0e-4
        or _finite_number(
            water["max_force_eV_per_A"], "GPU3 direct water maximum force"
        )
        < 0
        or not (
            0
            <= _finite_number(
                water["hessian_symmetry_max_abs_eV_per_A2"],
                "GPU3 direct Hessian symmetry",
            )
            < 1.0e-3
        )
    ):
        _fail("GPU3 direct water energy/forces/Hessian proof is invalid")

    ase = _exact(
        result["ase"],
        {"energy_eV", "max_force_eV_per_A"},
        "GPU3 direct ASE result",
    )
    ase_energy = _finite_number(ase["energy_eV"], "GPU3 direct ASE energy")
    if (
        _finite_number(
            ase["max_force_eV_per_A"], "GPU3 direct ASE maximum force"
        )
        < 0
        or abs(ase_energy - water_energy) >= 1.0e-4
    ):
        _fail("GPU3 direct ASE proof differs from the direct water result")

    cco = _exact(
        result["cco"],
        {
            "smiles",
            "canonical_smiles",
            "seed",
            "rdkit_max_iters",
            "force_field",
            "atom_count",
            "energy_eV",
            "baseline_eV",
            "delta_eV",
            "tolerance_eV",
        },
        "GPU3 direct CCO result",
    )
    cco_energy = _finite_number(cco["energy_eV"], "GPU3 direct CCO energy")
    baseline = _finite_number(
        cco["baseline_eV"], "GPU3 direct CCO baseline"
    )
    delta = _finite_number(cco["delta_eV"], "GPU3 direct CCO delta")
    tolerance = _finite_number(
        cco["tolerance_eV"], "GPU3 direct CCO tolerance"
    )
    if (
        cco["smiles"] != "CCO"
        or cco["canonical_smiles"] != "CCO"
        or isinstance(cco["seed"], bool)
        or cco["seed"] != 1
        or isinstance(cco["rdkit_max_iters"], bool)
        or cco["rdkit_max_iters"] != 500
        or cco["force_field"] not in {"MMFF94", "UFF"}
        or cco["atom_count"] != 9
        or baseline != CCO_BASELINE_EV
        or tolerance != CCO_TOLERANCE_EV
        or not math.isclose(
            delta,
            cco_energy - baseline,
            rel_tol=0.0,
            abs_tol=1.0e-9,
        )
        or abs(delta) > tolerance
    ):
        _fail("GPU3 direct CCO baseline proof is invalid")
    elapsed = _finite_number(
        result["elapsed_seconds"], "GPU3 direct elapsed time"
    )
    if not 0 < elapsed <= 600:
        _fail("GPU3 direct elapsed time is outside the bounded execution")
    return result


def validate_gpu3_actual_lease(value: object) -> dict[str, Any]:
    """Validate exact Broker/scope provenance for the direct GPU3 child."""

    lease = _exact(
        value,
        {
            "lease_id",
            "fencing_token",
            "broker_instance_id",
            "gpu_index",
            "gpu_uuid",
            "request_id",
            "workload_pid",
            "process_start_ticks",
            "workload_cgroup",
            "report_sha256",
            "model_copy_sha256",
            "model_copy_path_sha256",
            "model_copy_removed",
        },
        "GPU3 actual lease",
    )
    cgroup = lease["workload_cgroup"]
    cgroup_match = (
        GPU_SCOPE_CGROUP_RE.fullmatch(cgroup)
        if isinstance(cgroup, str)
        else None
    )
    if (
        not isinstance(lease["lease_id"], str)
        or INSTANCE_ID_RE.fullmatch(lease["lease_id"]) is None
        or isinstance(lease["fencing_token"], bool)
        or not isinstance(lease["fencing_token"], int)
        or lease["fencing_token"] <= 0
        or not isinstance(lease["broker_instance_id"], str)
        or INSTANCE_ID_RE.fullmatch(lease["broker_instance_id"]) is None
        or lease["gpu_index"] != 3
        or lease["gpu_uuid"] != GPU_UUIDS["3"]
        or not isinstance(lease["request_id"], str)
        or GPU3_REQUEST_ID_RE.fullmatch(lease["request_id"]) is None
        or isinstance(lease["workload_pid"], bool)
        or not isinstance(lease["workload_pid"], int)
        or lease["workload_pid"] <= 0
        or isinstance(lease["process_start_ticks"], bool)
        or not isinstance(lease["process_start_ticks"], int)
        or lease["process_start_ticks"] <= 0
        or cgroup_match is None
        or cgroup_match.group("lease_id") != lease["lease_id"]
        or lease["model_copy_sha256"] != AIMNET2_MODEL_SHA256
        or lease["model_copy_removed"] is not True
    ):
        _fail("GPU3 actual Broker lease or transient scope is invalid")
    for field in (
        "report_sha256",
        "model_copy_sha256",
        "model_copy_path_sha256",
    ):
        _digest(lease[field], f"GPU3 actual {field}")
    return lease


def _validate_gpu3_rejection_status(
    value: object,
    name: str,
) -> dict[str, Any]:
    projection = _exact(
        value,
        {
            "schema_version",
            "broker_instance_id",
            "draining",
            "gpu3_uuid",
            "gpu3_usage_mib",
            "gpu3_lease_ids",
            "gpu3_quarantined",
            "waiters",
        },
        name,
    )
    if (
        projection["schema_version"] != 1
        or not isinstance(projection["broker_instance_id"], str)
        or INSTANCE_ID_RE.fullmatch(projection["broker_instance_id"]) is None
        or projection["draining"] is not False
        or projection["gpu3_uuid"] != GPU_UUIDS["3"]
        or isinstance(projection["gpu3_usage_mib"], bool)
        or projection["gpu3_usage_mib"] != 0
        or projection["gpu3_lease_ids"] != []
        or projection["gpu3_quarantined"] is not False
        or isinstance(projection["waiters"], bool)
        or projection["waiters"] != 0
    ):
        _fail(f"{name} is not the exact unblocked GPU3 projection")
    return projection


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
        gpu = _exact(
            value,
            base_fields | {"result", "lease"},
            "GPU3 actual evidence",
        )
        if gpu["cuda_started"] is not True or gpu["fencing_verified"] is not True:
            _fail("GPU3 actual mode lacks CUDA and fencing proof")
        result = validate_gpu3_direct_result(gpu["result"])
        lease = validate_gpu3_actual_lease(gpu["lease"])
        if (
            result["gpu_index"] != lease["gpu_index"]
            or result["gpu_uuid"] != lease["gpu_uuid"]
            or lease["report_sha256"] != canonical_json_file_digest(result)
            or lease["model_copy_path_sha256"]
            != canonical_json_digest(
                {"path": result["preflight"]["default_model_path"]}
            )
            or gpu["evidence_sha256"]
            != canonical_json_digest({"result": result, "lease": lease})
        ):
            _fail("GPU3 actual science is not bound to its Broker lease")
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
                "inspection_before_sha256",
                "inspection_after_sha256",
                "observed_before_at",
                "observed_after_at",
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
        for name in ("inspection_before_sha256", "inspection_after_sha256"):
            _digest(claim[name], f"GPU3 Docker {name}")
        observed_before = _parsed_timestamp(
            claim["observed_before_at"], "GPU3 Docker first observation"
        )
        observed_after = _parsed_timestamp(
            claim["observed_after_at"], "GPU3 Docker second observation"
        )
        if (
            observed_after < observed_before
            or claim["inspection_before_sha256"]
            != claim["inspection_after_sha256"]
        ):
            _fail("GPU3 Docker claim changed across the rejection CAS")
        rejection = _exact(
            gpu["rejection"],
            {
                "code",
                "gpu_index",
                "gpu_uuid",
                "placement",
                "request_id",
                "blocked_reason",
                "broker_instance_id",
                "before_status",
                "before_status_sha256",
                "after_status",
                "after_status_sha256",
                "claim_sha256",
                "broker_report_sha256",
            },
            "GPU3 Broker rejection",
        )
        before_status = _validate_gpu3_rejection_status(
            rejection["before_status"],
            "GPU3 Broker rejection before status",
        )
        after_status = _validate_gpu3_rejection_status(
            rejection["after_status"],
            "GPU3 Broker rejection after status",
        )
        if (
            rejection["code"] != "gpu_capacity_unavailable"
            or rejection["gpu_index"] != 3
            or rejection["gpu_uuid"] != GPU_UUIDS["3"]
            or rejection["placement"] != "overflow"
            or rejection["blocked_reason"] != GPU3_BLOCKED_REASON
            or rejection["claim_sha256"] != canonical_json_digest(claim)
            or rejection["broker_instance_id"]
            != before_status["broker_instance_id"]
            or before_status != after_status
            or rejection["before_status_sha256"]
            != canonical_json_digest(before_status)
            or rejection["after_status_sha256"]
            != canonical_json_digest(after_status)
            or not isinstance(rejection["request_id"], str)
            or GPU3_REJECTION_REQUEST_ID_RE.fullmatch(
                rejection["request_id"]
            )
            is None
        ):
            _fail("GPU3 external claim lacks the exact Broker rejection")
        for name in (
            "claim_sha256",
            "before_status_sha256",
            "after_status_sha256",
            "broker_report_sha256",
        ):
            _digest(rejection[name], f"GPU3 Broker rejection {name}")
        if rejection["broker_report_sha256"] != canonical_json_digest(
            {
                key: rejection[key]
                for key in rejection
                if key != "broker_report_sha256"
            }
        ):
            _fail("GPU3 Broker rejection summary differs from retained evidence")
        if gpu["evidence_sha256"] != canonical_json_digest(
            {
                "claim": claim,
                "rejection": rejection,
                "blocked_reason": gpu["blocked_reason"],
            }
        ):
            _fail("GPU3 external-fence digest differs from its evidence")
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
        {"broker_science", "broker_uds_backend_e2e"},
        "GPU acceptance coverage",
    )
    science = _exact(
        coverage["broker_science"],
        {
            "status",
            "gpu_index",
            "gpu_uuid",
            "properties",
            "completed_job_id",
            "worker_instance_id",
            "execution_path",
            "parent_lease_id",
            "lease_id",
            "fencing_token",
            "broker_instance_id",
            "atom_count",
            "atomic_numbers",
            "energy_eV",
            "forces_shape",
            "max_force_eV_per_A",
            "hessian_shape",
            "hessian_symmetry_max_abs_eV_per_A2",
            "hessian_symmetry_relative_error",
            "hessian_symmetric_within_tolerance",
            "scientific_result_sha256",
            "hessian_artifact_sha256",
            "artifact_manifest_sha256",
            "bundle_manifest_sha256",
            "bundle_sha256",
            "completed_journal_sha256",
            "provenance_sha256",
            "aimnet_commit",
            "aimnet_wheel_sha256",
            "model_sha256",
            "model_registry_key",
        },
        "Broker scientific coverage",
    )
    if (
        science["status"] != "passed"
        or science["gpu_index"] != 1
        or science["gpu_uuid"] != GPU_UUIDS["1"]
        or science["properties"] != ["energy", "forces", "hessian"]
        or not isinstance(science["completed_job_id"], str)
        or UUID_RE.fullmatch(science["completed_job_id"]) is None
        or not isinstance(science["worker_instance_id"], str)
        or INSTANCE_ID_RE.fullmatch(science["worker_instance_id"]) is None
        or science["execution_path"] != "primary"
        or not isinstance(science["parent_lease_id"], str)
        or LEASE_ID_RE.fullmatch(science["parent_lease_id"]) is None
        or not isinstance(science["lease_id"], str)
        or LEASE_ID_RE.fullmatch(science["lease_id"]) is None
        or science["lease_id"] == science["parent_lease_id"]
        or isinstance(science["fencing_token"], bool)
        or not isinstance(science["fencing_token"], int)
        or science["fencing_token"] <= 0
        or not isinstance(science["broker_instance_id"], str)
        or not science["broker_instance_id"]
        or science["atom_count"] != 3
        or science["atomic_numbers"] != [8, 1, 1]
        or science["forces_shape"] != [3, 3]
        or science["hessian_shape"] != [9, 9]
        or science["hessian_symmetric_within_tolerance"] is not True
        or _finite_number(
            science["max_force_eV_per_A"], "Broker science force"
        )
        < 0
        or _finite_number(
            science["hessian_symmetry_max_abs_eV_per_A2"],
            "Broker science Hessian symmetry",
        )
        < 0
        or _finite_number(
            science["hessian_symmetry_relative_error"],
            "Broker science Hessian relative symmetry",
        )
        < 0
    ):
        _fail("Broker energy/forces/Hessian acceptance is incomplete")
    _finite_number(science["energy_eV"], "Broker science energy")
    for name in (
        "scientific_result_sha256",
        "hessian_artifact_sha256",
        "artifact_manifest_sha256",
        "bundle_manifest_sha256",
        "bundle_sha256",
        "completed_journal_sha256",
        "provenance_sha256",
        "aimnet_wheel_sha256",
        "model_sha256",
    ):
        _digest(science[name], f"Broker science {name}")
    _sha(science["aimnet_commit"], "Broker science AIMNet commit")
    if (
        not isinstance(science["model_registry_key"], str)
        or not science["model_registry_key"]
    ):
        _fail("Broker science model registry identity is missing")

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
            "fresh_worker_instance_id",
            "cancelled_journal_sha256",
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
        or e2e["completed_job_id"] != science["completed_job_id"]
        or e2e["fresh_worker_instance_id"] != science["worker_instance_id"]
    ):
        _fail("Broker + UDS + Backend acceptance binding is incomplete")
    for name in ("submit", "poll", "cancel", "journal", "artifact", "bundle", "fencing"):
        _true(e2e[name], f"Broker E2E {name}")
    for name in ("cancelled_journal_sha256",):
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
    observed_at: dt.datetime | None = None,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    """Validate and return one exact, self-sealed GPU acceptance report."""

    report = _exact(
        value,
        {
            "schema_version",
            "status",
            "run_kind",
            "captured_at",
            "authority",
            "bridge",
            "images",
            "runtime",
            "control_plane",
            "production_cas",
            "coverage",
            "gpus",
            "report_sha256",
        },
        "GPU acceptance report",
    )
    if (
        report["schema_version"] != SCHEMA_VERSION
        or report["status"] != "passed"
        or report["run_kind"] != "final-main"
    ):
        _fail("GPU acceptance report did not pass the supported schema")
    captured_at = _parsed_timestamp(
        report["captured_at"], "GPU acceptance capture time"
    )
    if observed_at is None:
        observed_at = dt.datetime.now(dt.UTC)
    if observed_at.tzinfo is None:
        _fail("GPU acceptance observation time lacks a timezone")
    observed_at = observed_at.astimezone(dt.UTC)
    if (
        isinstance(max_age_seconds, bool)
        or not isinstance(max_age_seconds, int)
        or max_age_seconds <= 0
        or captured_at
        > observed_at + dt.timedelta(seconds=MAX_FUTURE_SKEW_SECONDS)
        or observed_at - captured_at > dt.timedelta(seconds=max_age_seconds)
    ):
        _fail("GPU acceptance report is stale or from the future")
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
    validated_runtime = _validate_runtime(
        report["runtime"],
        runtime_contract=runtime_contract,
        runtime_contract_sha256=runtime_contract_sha256,
    )
    control = _validate_control_plane(
        report["control_plane"],
        authority=expected_authority,
        authority_images=authority_images,
    )
    _validate_production_cas(report["production_cas"])
    gpus = _exact(report["gpus"], {"1", "2", "3"}, "per-GPU evidence")
    _validate_gpu1(gpus["1"])
    _validate_gpu2(gpus["2"])
    gpu3 = _validate_gpu3(gpus["3"])
    audit_started = _parsed_timestamp(
        gpus["2"]["audit"]["started_at"], "GPU2 audit start"
    )
    audit_finished = _parsed_timestamp(
        gpus["2"]["audit"]["finished_at"], "GPU2 audit finish"
    )
    container_times = [
        _parsed_timestamp(
            control["containers"][role]["started_at"],
            f"fresh {role} start time",
        )
        for role in ("backend", "web", "postgres")
    ]
    if (
        audit_finished > captured_at
        or captured_at - audit_finished > dt.timedelta(seconds=60)
        or any(
            started < audit_started or started > audit_finished
            for started in container_times
        )
    ):
        _fail("GPU acceptance timestamps do not bind one fresh audit interval")
    if gpu3["mode"] == "externally_fenced":
        claim_started = _parsed_timestamp(
            gpu3["claim"]["observed_before_at"],
            "GPU3 Docker first observation",
        )
        claim_finished = _parsed_timestamp(
            gpu3["claim"]["observed_after_at"],
            "GPU3 Docker second observation",
        )
        if (
            claim_started < audit_started
            or claim_finished > audit_finished
        ):
            _fail("GPU3 external-fence proof escaped the GPU2 audit interval")
    coverage = _validate_coverage(report["coverage"], gpu3_mode=gpu3["mode"])
    if control["cleanup"]["mps_indices_stopped"] != (
        [1, 3] if gpu3["mode"] == "actual" else [1]
    ):
        _fail("fresh MPS lifecycle differs from the accepted GPU3 mode")
    science = coverage["broker_science"]
    if (
        gpus["1"]["evidence_sha256"] != canonical_json_digest(science)
        or science["worker_instance_id"] != control["worker"]["instance_id"]
        or science["broker_instance_id"] != control["broker"]["instance_id"]
        or science["aimnet_commit"] != validated_runtime["source"]["commit"]
        or science["aimnet_wheel_sha256"]
        != validated_runtime["wheel"]["sha256"]
        or science["model_sha256"] != AIMNET2_MODEL_SHA256
        or science["model_registry_key"] != AIMNET2_MODEL_REGISTRY_KEY
    ):
        _fail("Broker science is not bound to the fresh exact-F runtime")
    if (
        gpu3["mode"] == "actual"
        and gpu3["lease"]["broker_instance_id"]
        != control["broker"]["instance_id"]
    ):
        _fail("GPU3 actual lease is not bound to the fresh Broker instance")
    if (
        gpu3["mode"] == "externally_fenced"
        and gpu3["rejection"]["broker_instance_id"]
        != control["broker"]["instance_id"]
    ):
        _fail(
            "GPU3 external rejection is not bound to the fresh Broker instance"
        )
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
