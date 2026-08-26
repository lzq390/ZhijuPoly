#!/usr/bin/env python3
"""Run and seal the ingress-isolated production acceptance probes.

The caller owns ingress isolation.  This program deliberately accepts only a
loopback HTTP endpoint, uses public APIs, leaves job history intact, and
best-effort cancels every non-terminal job that it creates.
"""

from __future__ import annotations

import argparse
import ctypes
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import time
from typing import Any, Callable
import urllib.error
import urllib.parse
import urllib.request


OPERATION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{7,127}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
GPU_UUID_RE = re.compile(r"^GPU-[0-9a-f-]{36}$", re.IGNORECASE)
ASSET_RE = re.compile(r'''(?:src|href)=["'](/assets/[^"'?#]+)["']''')
TERMINAL = frozenset({"completed", "failed", "cancelled"})
DFT_MODELS = frozenset(
    {
        "aimnet2",
        "aimnet2-2025",
        "aimnet2-b973c",
        "aimnet2-nse",
        "aimnet2-pd",
        "aimnet2-rxn",
    }
)
FORBIDDEN_GUARD_KEYS = frozenset(
    {
        "pid",
        "pids",
        "uid",
        "username",
        "user",
        "cmd",
        "cmdline",
        "command",
        "command_line",
        "argv",
        "executable",
        "exe",
        "process_name",
        "cgroup",
        "unknown_processes",
    }
)
DFT_WORKER_PUBLIC_FIELDS = frozenset(
    {
        "schema_version",
        "calculation_types",
        "properties",
        "input_limits",
        "queue",
        "worker_status",
        "runtime_ready",
        "draining",
        "gpu_guard_mode",
        "gpu_guard_status",
        "gpu_contention_observed",
    }
)
PUBLIC_DFT_CONTENTION_WARNING = (
    "monomer DFT worker is ready; GPU contention is observed"
)
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_ASSET_BYTES = 64 * 1024 * 1024
MAX_AUTHORITY_BYTES = 64 * 1024
AUTHORITY_FIELDS = frozenset(
    {
        "schema_version",
        "phase",
        "operation_id",
        "target_sha",
        "target_tree",
        "descriptor_sha256",
        "staged_current_state_sha256",
        "control_release_id",
        "staged_at",
        "acceptance_not_before",
    }
)


class ProbeError(RuntimeError):
    """One production acceptance assertion failed."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ProbeError(message)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc(value: object, label: str) -> datetime:
    _require(isinstance(value, str) and value.endswith("Z"), f"{label} is not UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ProbeError(f"{label} is invalid") from exc
    _require(parsed.tzinfo is not None, f"{label} lacks a timezone")
    return parsed.astimezone(timezone.utc)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class LoopbackClient:
    def __init__(self, base_url: str) -> None:
        parsed = urllib.parse.urlsplit(base_url.rstrip("/"))
        _require(
            parsed.scheme == "http"
            and parsed.hostname == "127.0.0.1"
            and parsed.port is not None
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and parsed.path in {"", "/"},
            "base URL must be an explicit http://127.0.0.1:<port> endpoint",
        )
        self.base_url = f"http://127.0.0.1:{parsed.port}"
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirect(),
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
        max_bytes: int = MAX_JSON_BYTES,
    ) -> tuple[int, dict[str, str], bytes]:
        _require(path.startswith("/") and not path.startswith("//"), "HTTP path is unsafe")
        encoded = _canonical_bytes(body) if body is not None else None
        request_headers = {"Accept": "application/json", **(headers or {})}
        if encoded is not None:
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base_url + path,
            data=encoded,
            headers=request_headers,
            method=method,
        )
        try:
            response = self._opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            response = exc
        except OSError as exc:
            raise ProbeError(f"HTTP request failed: {method} {path}") from exc
        with response:
            payload = response.read(max_bytes + 1)
            _require(len(payload) <= max_bytes, f"HTTP response is oversized: {path}")
            return int(response.status), dict(response.headers.items()), payload


def _json_request(
    client: LoopbackClient,
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    expected: frozenset[int] = frozenset({200}),
    timeout: float = 30.0,
) -> tuple[int, dict[str, Any]]:
    status, _response_headers, raw = client.request(
        method,
        path,
        body=body,
        headers=headers,
        timeout=timeout,
    )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise ProbeError(f"HTTP response is not JSON: {method} {path}") from exc
    _require(isinstance(payload, dict), f"HTTP response is not an object: {path}")
    _require(status in expected, f"unexpected HTTP {status}: {method} {path}")
    return status, payload


def _wait_until(
    description: str,
    timeout_seconds: float,
    poll_seconds: float,
    probe: Callable[[], Any | None],
) -> Any:
    deadline = time.monotonic() + timeout_seconds
    while True:
        result = probe()
        if result is not None:
            return result
        if time.monotonic() >= deadline:
            raise ProbeError(f"timed out waiting for {description}")
        time.sleep(poll_seconds)


def _forbidden_keys(value: object) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() in FORBIDDEN_GUARD_KEYS:
                found.add(str(key))
            found.update(_forbidden_keys(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_forbidden_keys(item))
    return found


def _finite_number(value: object, label: str) -> float:
    _require(
        isinstance(value, (int, float)) and not isinstance(value, bool),
        f"{label} is not numeric",
    )
    number = float(value)
    _require(math.isfinite(number), f"{label} is not finite")
    return number


def run_dft_probe(
    client: LoopbackClient,
    *,
    operation_id: str,
    timeout_seconds: float,
    poll_seconds: float,
    expected_gpu_uuid: str,
    require_quarantined: bool,
) -> dict[str, Any]:
    _, status = _json_request(client, "GET", "/api/v1/monomer-dft/status")
    _, capabilities = _json_request(client, "GET", "/api/v1/monomer-dft/capabilities")
    _require(status.get("enabled") is True, "DFT submission is not enabled")
    _require(status.get("available") is True, "DFT is not available")
    _require(status.get("runtime_ready") is True, "DFT runtime is not ready")
    _require(status.get("active_jobs") == 0, "DFT acceptance requires zero active jobs")
    worker = capabilities.get("worker")
    _require(isinstance(worker, dict), "DFT worker capabilities are missing")
    _require(
        set(worker) == DFT_WORKER_PUBLIC_FIELDS,
        "DFT worker public projection fields differ from the router contract",
    )
    status_guard_fields = {
        name: status.get(name)
        for name in (
            "gpu_guard_mode",
            "gpu_guard_status",
            "gpu_contention_observed",
        )
    }
    worker_guard_fields = {
        name: worker.get(name)
        for name in (
            "gpu_guard_mode",
            "gpu_guard_status",
            "gpu_contention_observed",
        )
    }
    _require(
        all(name in status for name in status_guard_fields)
        and all(name in worker for name in worker_guard_fields),
        "DFT guard projection fields are missing",
    )
    _require(
        status_guard_fields["gpu_guard_mode"] in {"enforce", "observe"}
        and worker_guard_fields["gpu_guard_mode"] in {"enforce", "observe"},
        "DFT guard mode has an invalid type or value",
    )
    _require(
        type(status_guard_fields["gpu_contention_observed"]) is bool
        and type(worker_guard_fields["gpu_contention_observed"]) is bool,
        "DFT guard contention projection is not boolean",
    )
    _require(
        status_guard_fields == worker_guard_fields,
        "DFT status and worker guard projections differ",
    )
    _require(
        status_guard_fields["gpu_guard_mode"] == "observe",
        "DFT guard mode is not observe",
    )
    guard_status = status_guard_fields["gpu_guard_status"]
    _require(
        guard_status in {"ready", "quarantined", "missing", "stale", "invalid"},
        "DFT guard status has an invalid type or value",
    )
    contention_observed = status_guard_fields["gpu_contention_observed"]
    _require(
        (guard_status == "quarantined") is contention_observed,
        "DFT quarantined and contention projections are inconsistent",
    )
    if require_quarantined:
        _require(guard_status == "quarantined", "DFT guard is not quarantined")
    _require(not _forbidden_keys(status), "DFT status leaks private process fields")
    _require(not _forbidden_keys(capabilities), "DFT capabilities leak private process fields")
    _require(capabilities.get("available") is True, "DFT capabilities are unavailable")
    if guard_status == "quarantined":
        _require(
            status.get("available") is True
            and status.get("runtime_ready") is True
            and capabilities.get("available") is True
            and worker.get("runtime_ready") is True,
            "observe quarantine incorrectly disables the DFT runtime",
        )
        _require(
            status.get("message") == PUBLIC_DFT_CONTENTION_WARNING
            and capabilities.get("message") == PUBLIC_DFT_CONTENTION_WARNING,
            "observe quarantine public warning is missing or unsafe",
        )
    limits = capabilities.get("limits")
    _require(
        isinstance(limits, dict)
        and limits.get("max_concurrent_jobs") == 1
        and limits.get("max_queued_jobs") == 8
        and limits.get("max_active_jobs") == 9,
        "DFT queue contract is not 1 running plus 8 queued",
    )
    models = capabilities.get("models")
    _require(isinstance(models, list), "DFT model capabilities are missing")
    loaded_models = {
        item.get("id")
        for item in models
        if isinstance(item, dict) and item.get("available") is True
    }
    _require(loaded_models == DFT_MODELS, "DFT six-model warmup is incomplete")

    job_id: str | None = None
    completed: dict[str, Any] | None = None
    try:
        operation_token = hashlib.sha256(operation_id.encode("ascii")).hexdigest()[:32]
        idempotency_key = f"prod-accept-dft-{operation_token}"
        _, created = _json_request(
            client,
            "POST",
            "/api/v1/monomer-dft/jobs",
            body={
                "input": {
                    "smiles": "O",
                    "net_charge": 0,
                    "multiplicity": 1,
                    "psmiles_mode": None,
                },
                "model": "aimnet2",
                "conformer": {"seed": 1, "max_iterations": 500},
                "calculation_type": "single_point",
                "single_point": {"properties": ["energy", "charges", "forces"]},
            },
            headers={"Idempotency-Key": idempotency_key},
            expected=frozenset({202}),
        )
        job_id = created.get("job_id")
        _require(isinstance(job_id, str) and bool(job_id), "DFT job ID is missing")

        def completed_job() -> dict[str, Any] | None:
            _, current = _json_request(client, "GET", f"/api/v1/monomer-dft/jobs/{job_id}")
            return current if current.get("status") in TERMINAL else None

        completed = _wait_until(
            "DFT single-point completion",
            timeout_seconds,
            poll_seconds,
            completed_job,
        )
        _require(completed.get("status") == "completed", "DFT single-point did not complete")
        result = completed.get("result")
        _require(isinstance(result, dict), "DFT single-point result is missing")
        _require(
            result.get("schema_version") == 2
            and result.get("calculation_type") == "single_point"
            and result.get("engine") == "aimnet2"
            and result.get("model") == "aimnet2",
            "DFT single-point scientific contract is invalid",
        )
        atoms = result.get("atoms")
        properties = result.get("properties")
        _require(isinstance(atoms, dict) and atoms.get("count") == 3, "DFT water atom count differs")
        _require(isinstance(properties, dict), "DFT properties are missing")
        energy = properties.get("energy")
        charges = properties.get("charges")
        forces = properties.get("forces")
        _require(isinstance(energy, dict), "DFT energy is missing")
        energy_ev = _finite_number(energy.get("value_eV"), "DFT energy")
        _require(
            isinstance(charges, dict)
            and isinstance(charges.get("values_e"), list)
            and len(charges["values_e"]) == 3,
            "DFT charges are incomplete",
        )
        for index, charge in enumerate(charges["values_e"]):
            _finite_number(charge, f"DFT charge {index}")
        _require(
            isinstance(forces, dict)
            and isinstance(forces.get("values_eV_per_A"), list)
            and len(forces["values_eV_per_A"]) == 3,
            "DFT forces are incomplete",
        )
        for atom_index, force in enumerate(forces["values_eV_per_A"]):
            _require(
                isinstance(force, list) and len(force) == 3,
                f"DFT force {atom_index} is not a three-vector",
            )
            for axis_index, component in enumerate(force):
                _finite_number(
                    component,
                    f"DFT force {atom_index}:{axis_index}",
                )
        provenance = completed.get("provenance")
        _require(isinstance(provenance, dict), "DFT provenance is missing")
        _require(
            provenance.get("gpu_uuid") == expected_gpu_uuid
            or provenance.get("physical_gpu_uuid") == expected_gpu_uuid,
            "DFT single-point did not run on the expected GPU UUID",
        )
        return {
            "status": "passed",
            "guard_mode": "observe",
            "guard_status": guard_status,
            "contention_observed": contention_observed,
            "quarantine_exercised": guard_status == "quarantined",
            "available_during_guard_state": True,
            "warm_models": sorted(loaded_models),
            "queue": {"running": 1, "queued": 8, "active": 9},
            "single_point": {
                "job_id": job_id,
                "status": "completed",
                "model": "aimnet2",
                "atom_count": 3,
                "energy_eV": energy_ev,
                "gpu_uuid": expected_gpu_uuid,
                "result_sha256": _digest(result),
            },
        }
    finally:
        if job_id and (completed is None or completed.get("status") not in TERMINAL):
            try:
                _json_request(
                    client,
                    "POST",
                    f"/api/v1/monomer-dft/jobs/{job_id}/cancel",
                    expected=frozenset({200, 202}),
                )
            except Exception:
                pass


def _md_job(client: LoopbackClient, job_id: str) -> dict[str, Any]:
    _, payload = _json_request(client, "GET", f"/api/v1/monomer-md/jobs/{job_id}")
    return payload


def _cancel_md(client: LoopbackClient, job_id: str) -> None:
    _json_request(
        client,
        "POST",
        f"/api/v1/monomer-md/jobs/{job_id}/cancel",
        expected=frozenset({200, 202}),
    )


def run_md_probe(
    client: LoopbackClient,
    *,
    timeout_seconds: float,
    poll_seconds: float,
) -> dict[str, Any]:
    _, initial = _json_request(client, "GET", "/api/v1/monomer-md/status")
    _require(initial.get("available") is True, "MD is not available")
    _require(initial.get("runtime_ready") is True, "MD runtime is not ready")
    _require(initial.get("draining") is False, "MD is draining")
    _require(
        initial.get("active_jobs") == 0
        and initial.get("database_active_jobs") == 0
        and initial.get("formal_running_jobs") == 0
        and initial.get("formal_queued_jobs") == 0,
        "MD acceptance requires zero active jobs",
    )
    _require(
        initial.get("max_active_jobs") == 3
        and initial.get("formal_max_running_jobs") == 1
        and initial.get("formal_max_queued_jobs") == 2,
        "MD capacity is not 1 running plus 2 queued",
    )
    config = {
        "protocol": "Density",
        "params_dir": "managed_params",
        "output_dir": "managed_output",
        "working_dir": "managed_working",
        "temperature": 298,
        "natoms": 10000,
        "components": {"DMC": 249, "EC": 170, "LI": 34, "PF6": 34},
        "smiles": {
            "DMC": "COC(=O)OC",
            "EC": "O=C1OCCO1",
            "LI": "[Li+]",
            "PF6": "F[P-](F)(F)(F)(F)F",
        },
    }
    body = {"protocol": "Density", "run_mode": "formal", "config_json": config}
    jobs: list[str] = []
    evidence: dict[str, Any] = {}

    def job_in_state(job_id: str, desired: str) -> dict[str, Any] | None:
        current = _md_job(client, job_id)
        return current if current.get("status") == desired else None

    def capacity_is_zero() -> dict[str, Any] | None:
        _, current = _json_request(client, "GET", "/api/v1/monomer-md/status")
        if (
            current.get("active_jobs") == 0
            and current.get("database_active_jobs") == 0
            and current.get("formal_running_jobs") == 0
            and current.get("formal_queued_jobs") == 0
        ):
            return current
        return None

    def submit(index: int) -> tuple[int, dict[str, Any]]:
        return _json_request(
            client,
            "POST",
            "/api/v1/monomer-md/jobs",
            body=body,
            headers={"X-Forwarded-For": f"192.0.2.{index}"},
            expected=frozenset({202}),
        )

    try:
        _, first = submit(11)
        first_id = first.get("job_id")
        _require(isinstance(first_id, str) and bool(first_id), "first MD job ID is missing")
        jobs.append(first_id)
        _wait_until(
            "first MD job running",
            timeout_seconds,
            poll_seconds,
            lambda: job_in_state(first_id, "running"),
        )
        for index in (12, 13):
            _, created = submit(index)
            job_id = created.get("job_id")
            _require(isinstance(job_id, str) and bool(job_id), "queued MD job ID is missing")
            jobs.append(job_id)

        def full_queue() -> dict[str, Any] | None:
            _, md_status = _json_request(client, "GET", "/api/v1/monomer-md/status")
            items = [_md_job(client, job_id) for job_id in jobs]
            if (
                md_status.get("formal_running_jobs") == 1
                and md_status.get("formal_queued_jobs") == 2
                and items[0].get("status") == "running"
                and [item.get("queue_position") for item in items[1:]] == [1, 2]
            ):
                return {"status": md_status, "items": items}
            return None

        queue_snapshot = _wait_until(
            "MD 1-running plus 2-queued state",
            timeout_seconds,
            poll_seconds,
            full_queue,
        )
        fourth_status, fourth = _json_request(
            client,
            "POST",
            "/api/v1/monomer-md/jobs",
            body=body,
            headers={"X-Forwarded-For": "192.0.2.14"},
            expected=frozenset({429}),
        )
        detail = str(fourth.get("detail") or "")
        _require("capacity" in detail.casefold(), "fourth MD request was not rejected by capacity")

        queued_id = jobs[1]
        _cancel_md(client, queued_id)
        queued_terminal = _wait_until(
            "queued MD cancellation",
            timeout_seconds,
            poll_seconds,
            lambda: job_in_state(queued_id, "cancelled"),
        )
        _cancel_md(client, first_id)
        running_terminal = _wait_until(
            "running MD cancellation",
            timeout_seconds,
            poll_seconds,
            lambda: job_in_state(first_id, "cancelled"),
        )
        remaining_id = jobs[2]
        _wait_until(
            "remaining MD queue promotion",
            timeout_seconds,
            poll_seconds,
            lambda: job_in_state(remaining_id, "running"),
        )
        _cancel_md(client, remaining_id)
        _wait_until(
            "promoted MD cancellation",
            timeout_seconds,
            poll_seconds,
            lambda: job_in_state(remaining_id, "cancelled"),
        )
        final = _wait_until(
            "MD capacity return to zero",
            timeout_seconds,
            poll_seconds,
            capacity_is_zero,
        )
        evidence = {
            "status": "passed",
            "capacity": {"running": 1, "queued": 2, "active": 3},
            "job_ids": jobs,
            "queue_positions": [
                item.get("queue_position") for item in queue_snapshot["items"]
            ],
            "fourth_request": {"http_status": fourth_status, "detail": detail},
            "queued_cancel": {
                "job_id": queued_id,
                "terminal_status": queued_terminal.get("status"),
            },
            "running_cancel": {
                "job_id": first_id,
                "terminal_status": running_terminal.get("status"),
            },
            "remaining_job_promoted_and_cancelled": remaining_id,
            "final_active_jobs": final.get("active_jobs"),
        }
        return evidence
    finally:
        for job_id in jobs:
            try:
                current = _md_job(client, job_id)
                if current.get("status") not in TERMINAL:
                    _cancel_md(client, job_id)
            except Exception:
                pass


def run_read_only_api_probes(
    client: LoopbackClient,
    *,
    expected_property_records: int,
    knowledge_query: str,
) -> dict[str, Any]:
    _, options = _json_request(
        client,
        "GET",
        "/api/v1/database-browser/property-filter/options",
        timeout=30.0,
    )
    _require(
        options.get("data_source") == "postgres"
        and options.get("source_status") == "ready"
        and options.get("total_records") == expected_property_records,
        "property-filter catalog identity differs",
    )
    option_items = options.get("options")
    _require(isinstance(option_items, list) and option_items, "property-filter options are empty")
    option = next(
        (
            item
            for item in option_items
            if isinstance(item, dict)
            and isinstance(item.get("option_key"), str)
            and isinstance(item.get("histogram"), dict)
            and item["histogram"].get("total_count", 0) > 0
        ),
        None,
    )
    _require(isinstance(option, dict), "property-filter snapshot histogram is missing")
    option_key = str(option["option_key"])
    encoded_key = urllib.parse.quote(option_key, safe="")
    _, histogram_response = _json_request(
        client,
        "GET",
        f"/api/v1/database-browser/property-filter/histogram?option_key={encoded_key}",
        timeout=30.0,
    )
    histogram = histogram_response.get("histogram")
    _require(isinstance(histogram, dict), "property histogram is missing")
    counts = histogram.get("counts")
    _require(isinstance(counts, list), "property histogram counts are missing")
    total_count = histogram.get("total_count")
    _require(
        isinstance(total_count, int)
        and sum(int(value) for value in counts)
        + int(histogram.get("underflow_count", 0))
        + int(histogram.get("overflow_count", 0))
        == total_count,
        "property histogram counts do not add up",
    )

    _, structure = _json_request(
        client,
        "POST",
        "/api/v1/structure/2d",
        body={"smiles": "*CC*"},
    )
    svg = structure.get("structure_svg")
    _require(
        isinstance(svg, str) and svg.startswith("<?xml") and "<svg" in svg,
        "2D structure response is not SVG",
    )

    _, knowledge = _json_request(
        client,
        "POST",
        "/api/v1/knowledge/search",
        body={"query": knowledge_query, "top_k": 5, "page": 1, "page_size": 5},
        timeout=30.0,
    )
    results = knowledge.get("results")
    knowledge_groups = knowledge.get("groups")
    _require(
        knowledge.get("query") == knowledge_query
        and isinstance(knowledge_groups, list)
        and len(knowledge_groups) == 1
        and isinstance(knowledge_groups[0], dict)
        and isinstance(knowledge_groups[0].get("terms"), list)
        and bool(knowledge_groups[0]["terms"])
        and knowledge_groups[0]["terms"][0] == knowledge_query
        and isinstance(knowledge.get("total"), int)
        and knowledge["total"] > 0
        and isinstance(results, list)
        and results,
        "local knowledge probe returned no results",
    )
    knowledge_ids = [
        item.get("knowledge_id") for item in results if isinstance(item, dict)
    ]
    _require(all(isinstance(value, int) for value in knowledge_ids), "knowledge IDs are invalid")

    _, tg_assistant_status = _json_request(
        client,
        "GET",
        "/api/v1/assistant/tg/status",
        timeout=30.0,
    )
    _require(
        isinstance(tg_assistant_status.get("enabled"), bool)
        and isinstance(tg_assistant_status.get("configured"), bool)
        and isinstance(tg_assistant_status.get("image"), dict)
        and tg_assistant_status["image"].get("supported") is True
        and tg_assistant_status["image"].get("max_files") == 2
        and tg_assistant_status["image"].get("max_canvas_snapshots") == 1
        and tg_assistant_status["image"].get("max_user_upload_files") == 1
        and isinstance(tg_assistant_status["image"].get("max_bytes"), int)
        and tg_assistant_status["image"].get("max_total_bytes")
        == 2 * tg_assistant_status["image"].get("max_bytes")
        and tg_assistant_status["image"].get("accepted_mime_types")
        == ["image/png", "image/jpeg", "image/webp"],
        "Tg assistant status contract is invalid",
    )
    _, tg_assistant_guide = _json_request(
        client,
        "GET",
        "/api/v1/assistant/tg/guide",
        timeout=30.0,
    )
    guide_sections = tg_assistant_guide.get("sections")
    _require(
        tg_assistant_guide.get("module") == "reverseDesign"
        and tg_assistant_guide.get("version") == 3
        and tg_assistant_guide.get("language") == "zh-CN"
        and isinstance(guide_sections, list)
        and bool(guide_sections),
        "Tg assistant guide contract is invalid",
    )
    return {
        "status": "passed",
        "property_histogram": {
            "total_records": expected_property_records,
            "option_key": option_key,
            "total_count": total_count,
            "histogram_sha256": _digest(histogram),
        },
        "structure_2d": {
            "smiles": "*CC*",
            "svg_sha256": "sha256:" + hashlib.sha256(svg.encode("utf-8")).hexdigest(),
        },
        "knowledge": {
            "query": knowledge_query,
            "total": knowledge["total"],
            "returned": len(results),
            "group_count": len(knowledge_groups),
            "knowledge_ids": knowledge_ids,
            "response_projection_sha256": _digest(
                {"total": knowledge["total"], "knowledge_ids": knowledge_ids}
            ),
        },
        "tg_assistant": {
            "enabled": tg_assistant_status["enabled"],
            "configured": tg_assistant_status["configured"],
            "guide_version": tg_assistant_guide["version"],
            "guide_section_count": len(guide_sections),
        },
    }


def run_frontend_probe(client: LoopbackClient) -> dict[str, Any]:
    health_status, _health_headers, health_raw = client.request("GET", "/health")
    _require(health_status == 200, "frontend proxy health failed")
    try:
        health = json.loads(health_raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise ProbeError("frontend proxy health is not JSON") from exc
    _require(isinstance(health, dict), "frontend proxy health is not an object")

    status, headers, index_raw = client.request("GET", "/")
    _require(status == 200, "frontend index failed")
    index = index_raw.decode("utf-8")
    _require('<div id="root"></div>' in index, "frontend root mount is missing")
    index_digest = "sha256:" + hashlib.sha256(index_raw).hexdigest()
    assets = sorted(set(ASSET_RE.findall(index)))
    _require(any(path.endswith(".js") for path in assets), "frontend JS asset is missing")
    _require(any(path.endswith(".css") for path in assets), "frontend CSS asset is missing")
    asset_evidence: list[dict[str, Any]] = []
    for path in assets:
        asset_status, asset_headers, body = client.request(
            "GET", path, max_bytes=MAX_ASSET_BYTES
        )
        _require(asset_status == 200 and bool(body), f"frontend asset failed: {path}")
        asset_evidence.append(
            {
                "path": path,
                "bytes": len(body),
                "content_type": asset_headers.get("Content-Type", ""),
                "sha256": "sha256:" + hashlib.sha256(body).hexdigest(),
            }
        )
    routes = [
        "/structure-workbench",
        "/database",
        "/database-filter",
        "/knowledge",
        "/reverse-design",
        "/monomer-dft",
        "/monomer-md-simulation",
    ]
    route_evidence: list[str] = []
    for route in routes:
        route_status, _route_headers, route_raw = client.request("GET", route)
        _require(route_status == 200, f"frontend route failed: {route}")
        _require(
            "sha256:" + hashlib.sha256(route_raw).hexdigest() == index_digest,
            f"frontend route did not return the release index: {route}",
        )
        route_evidence.append(route)
    return {
        "status": "passed",
        "health_sha256": _digest(health),
        "index_sha256": index_digest,
        "index_content_type": headers.get("Content-Type", ""),
        "assets": asset_evidence,
        "routes": route_evidence,
    }


def _private_evidence_directory(path: Path) -> Path:
    _require(path.is_absolute(), "evidence directory must be absolute")
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ProbeError("evidence directory is unavailable") from exc
    _require(
        stat.S_ISDIR(metadata.st_mode)
        and not path.is_symlink()
        and metadata.st_uid == os.geteuid()
        and stat.S_IMODE(metadata.st_mode) == 0o700,
        "evidence directory must be a current-user-owned, non-symlink 0700 directory",
    )
    return path.resolve(strict=True)


def _file_snapshot(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_uid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _read_private_file(path: Path, *, maximum_bytes: int, label: str) -> bytes:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ProbeError(f"{label} is unavailable or unsafe") from exc
    try:
        before = os.fstat(descriptor)
        _require(
            stat.S_ISREG(before.st_mode)
            and before.st_nlink == 1
            and before.st_uid == os.geteuid()
            and stat.S_IMODE(before.st_mode) == 0o600
            and before.st_size <= maximum_bytes,
            f"{label} must be a current-user-owned, single-link 0600 file",
        )
        expected = _file_snapshot(before)
        raw = bytearray()
        while True:
            remaining = maximum_bytes + 1 - len(raw)
            _require(remaining > 0, f"{label} is oversized")
            chunk = os.read(descriptor, min(16 * 1024, remaining))
            if not chunk:
                break
            raw.extend(chunk)
            _require(len(raw) <= maximum_bytes, f"{label} is oversized")
        after = os.fstat(descriptor)
        _require(
            _file_snapshot(after) == expected and len(raw) == after.st_size,
            f"{label} changed while it was read",
        )
    finally:
        os.close(descriptor)
    try:
        path_after = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise ProbeError(f"{label} path changed while it was read") from exc
    _require(_file_snapshot(path_after) == expected, f"{label} path changed while it was read")
    return bytes(raw)


def _load_acceptance_authority(
    path: Path,
    *,
    operation_id: str,
    source_sha: str,
) -> tuple[Path, dict[str, Any], str]:
    _require(path.is_absolute(), "acceptance authority path must be absolute")
    _require(
        path.name == "acceptance-authority.json"
        and path.parent.name == operation_id
        and path.parent.parent.name == "prepared",
        "acceptance authority path is not scoped to the operation",
    )
    raw = _read_private_file(
        path,
        maximum_bytes=MAX_AUTHORITY_BYTES,
        label="acceptance authority",
    )
    try:
        authority = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise ProbeError("acceptance authority is not valid JSON") from exc
    _require(
        isinstance(authority, dict)
        and set(authority) == AUTHORITY_FIELDS
        and authority.get("schema_version") == 1
        and authority.get("phase") == "awaiting-acceptance"
        and authority.get("operation_id") == operation_id
        and authority.get("target_sha") == source_sha,
        "acceptance authority binding is invalid",
    )
    _require(SHA_RE.fullmatch(str(authority.get("target_tree", ""))) is not None, "invalid target tree")
    for name in ("descriptor_sha256", "staged_current_state_sha256"):
        _require(
            re.fullmatch(r"sha256:[0-9a-f]{64}", str(authority.get(name, ""))) is not None,
            f"invalid acceptance authority {name}",
        )
    _require(
        re.fullmatch(r"[0-9a-f]{64}", str(authority.get("control_release_id", "")))
        is not None,
        "invalid acceptance control release ID",
    )
    staged_at = _parse_utc(authority.get("staged_at"), "acceptance staged-at")
    not_before = _parse_utc(
        authority.get("acceptance_not_before"),
        "acceptance not-before",
    )
    _require(
        (not_before - staged_at).total_seconds() >= 900,
        "acceptance authority observation interval is shorter than 15 minutes",
    )
    directory = _private_evidence_directory(path.parent)
    canonical_file = _canonical_bytes(authority) + b"\n"
    _require(raw == canonical_file, "acceptance authority is not canonical JSON")
    authority_sha256 = "sha256:" + hashlib.sha256(raw).hexdigest()

    marker_path = path.parents[2] / "deploy-in-progress.json"
    marker_raw = _read_private_file(
        marker_path,
        maximum_bytes=MAX_JSON_BYTES,
        label="live staged deployment marker",
    )
    try:
        marker = json.loads(marker_raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise ProbeError("live staged deployment marker is not valid JSON") from exc
    _require(
        isinstance(marker, dict)
        and marker.get("schema_version") == 3
        and marker.get("action") == "deploy"
        and marker.get("phase") == "awaiting-acceptance"
        and marker.get("operation_id") == operation_id
        and marker.get("source_sha") == source_sha
        and marker.get("descriptor_sha256") == authority["descriptor_sha256"]
        and marker.get("candidate_state_sha256")
        == authority["staged_current_state_sha256"]
        and marker.get("acceptance_started_at") == authority["staged_at"]
        and marker.get("acceptance_not_before")
        == authority["acceptance_not_before"]
        and marker.get("acceptance_authority_path") == os.fspath(path)
        and marker.get("acceptance_authority_sha256") == authority_sha256,
        "live marker is not the authority's awaiting-acceptance stage",
    )
    executor_control = marker.get("executor_control")
    _require(
        isinstance(executor_control, dict)
        and executor_control.get("release_id") == authority["control_release_id"],
        "live marker control release differs from acceptance authority",
    )
    return directory, authority, authority_sha256


def _seal_report(directory: Path, operation_id: str, report: dict[str, Any]) -> Path:
    document = dict(report)
    document["report_sha256"] = _digest(report)
    payload = _canonical_bytes(document) + b"\n"
    output = directory / f"production-acceptance-{operation_id}.json"
    staging = directory / (
        f".{output.name}.{secrets.token_hex(16)}.staging"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        descriptor = os.open(staging, flags, 0o600)
    except OSError as exc:
        raise ProbeError("acceptance evidence staging path is unavailable or unsafe") from exc
    try:
        try:
            written = 0
            while written < len(payload):
                count = os.write(descriptor, payload[written:])
                _require(count > 0, "acceptance evidence staging write made no progress")
                written += count
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            renameat2 = getattr(libc, "renameat2", None)
            _require(renameat2 is not None, "atomic no-replace publication is unavailable")
            renameat2.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            renameat2.restype = ctypes.c_int
            result = renameat2(
                directory_fd,
                os.fsencode(staging.name),
                directory_fd,
                os.fsencode(output.name),
                1,  # RENAME_NOREPLACE
            )
            if result != 0:
                error_number = ctypes.get_errno()
                raise OSError(error_number, os.strerror(error_number), os.fspath(output))
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        staging = None
    except OSError as exc:
        raise ProbeError(
            "acceptance evidence output already exists or could not be atomically published"
        ) from exc
    finally:
        if staging is not None:
            try:
                staging.unlink()
            except FileNotFoundError:
                pass
    return output


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:9000")
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--authority-file", type=Path, required=True)
    parser.add_argument("--dft-timeout-seconds", type=float, default=900.0)
    parser.add_argument("--md-timeout-seconds", type=float, default=300.0)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument(
        "--expected-dft-gpu-uuid",
        default="GPU-89c7c52c-e252-0135-c157-24eee1a1ccbe",
    )
    parser.add_argument("--require-dft-quarantined", action="store_true")
    parser.add_argument("--expected-property-records", type=int, default=615_159)
    parser.add_argument("--knowledge-query", default="polyimide")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        _require(OPERATION_ID_RE.fullmatch(args.operation_id) is not None, "invalid operation ID")
        _require(SHA_RE.fullmatch(args.source_sha) is not None, "invalid source SHA")
        _require(
            GPU_UUID_RE.fullmatch(args.expected_dft_gpu_uuid) is not None,
            "invalid DFT GPU UUID",
        )
        _require(args.dft_timeout_seconds > 0, "DFT timeout must be positive")
        _require(args.md_timeout_seconds > 0, "MD timeout must be positive")
        _require(0 < args.poll_seconds <= 10, "poll interval must be in (0, 10]")
        _require(args.expected_property_records > 0, "property record count must be positive")
        _require(bool(args.knowledge_query.strip()), "knowledge query must be non-empty")
        evidence_directory, authority, authority_sha256 = _load_acceptance_authority(
            args.authority_file,
            operation_id=args.operation_id,
            source_sha=args.source_sha,
        )
        client = LoopbackClient(args.base_url)
        started_at = _utc_now()
        sections: dict[str, Any] = {}
        status = "passed"
        error: str | None = None
        try:
            sections["dft"] = run_dft_probe(
                client,
                operation_id=args.operation_id,
                timeout_seconds=args.dft_timeout_seconds,
                poll_seconds=args.poll_seconds,
                expected_gpu_uuid=args.expected_dft_gpu_uuid,
                require_quarantined=args.require_dft_quarantined,
            )
            sections["md"] = run_md_probe(
                client,
                timeout_seconds=args.md_timeout_seconds,
                poll_seconds=args.poll_seconds,
            )
            sections["read_only_apis"] = run_read_only_api_probes(
                client,
                expected_property_records=args.expected_property_records,
                knowledge_query=args.knowledge_query.strip(),
            )
            sections["frontend"] = run_frontend_probe(client)
        except Exception as exc:  # evidence is also required for a failed gate
            status = "failed"
            error = str(exc)[:1000]
        report = {
            "schema_version": 1,
            "status": status,
            "operation_id": args.operation_id,
            "source_sha": args.source_sha,
            "authority": authority,
            "authority_sha256": authority_sha256,
            "loopback_endpoint": client.base_url,
            "started_at": started_at,
            "finished_at": _utc_now(),
            "sections": sections,
            "error": error,
        }
        output = _seal_report(evidence_directory, args.operation_id, report)
        print(os.fspath(output))
        return 0 if status == "passed" else 1
    except (ProbeError, OSError, ValueError) as exc:
        print(f"production acceptance: {exc}", file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
