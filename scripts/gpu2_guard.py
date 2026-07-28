#!/usr/bin/env python3
"""Audit configuration-level production ownership of physical GPU2.

The guard is deliberately non-destructive. It records unknown GPU2 compute
processes and lets DFT admission fail closed without killing host workloads.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import subprocess
import tempfile
from typing import Any


GPU_INDEX = "2"
GPU_UUID = "GPU-89c7c52c-e252-0135-c157-24eee1a1ccbe"
DEFAULT_STATE = pathlib.Path(
    "/data/lzq/gith/nexpoly-runtime/state/gpu2-guard.json"
)
ALLOWED_UNITS = (
    "nexpoly-monomer-md-worker.service",
    "nexpoly-monomer-dft-worker.service",
)


def run(*command: str) -> str:
    return subprocess.run(
        command,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout


def unit_main_pid(unit: str) -> int | None:
    try:
        raw = run(
            "systemctl",
            "--user",
            "show",
            "--property=MainPID",
            "--value",
            unit,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    return int(raw) if raw.isdigit() and int(raw) > 0 else None


def production_backend_containers() -> dict[str, int]:
    try:
        identifiers = run(
            "docker",
            "ps",
            "--filter",
            "label=com.docker.compose.project=nexpoly",
            "--filter",
            "label=com.docker.compose.service=backend",
            "--format",
            "{{.ID}}",
        ).split()
    except (OSError, subprocess.CalledProcessError):
        return {}
    containers: dict[str, int] = {}
    for identifier in identifiers:
        try:
            payload = json.loads(run("docker", "inspect", identifier))[0]
            pid = int(payload["State"]["Pid"])
        except (
            IndexError,
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
            subprocess.CalledProcessError,
        ):
            continue
        if pid > 0:
            containers[str(payload["Id"])] = pid
    return containers


def parent_pid(pid: int) -> int | None:
    try:
        fields = pathlib.Path(f"/proc/{pid}/stat").read_text().split()
        return int(fields[3])
    except (OSError, IndexError, ValueError):
        return None


def is_descendant(pid: int, roots: set[int]) -> bool:
    seen: set[int] = set()
    current: int | None = pid
    while current and current > 1 and current not in seen:
        if current in roots:
            return True
        seen.add(current)
        current = parent_pid(current)
    return False


def gpu_processes() -> tuple[str, list[dict[str, Any]]]:
    observed_uuid = run(
        "nvidia-smi",
        "--query-gpu=uuid",
        "--format=csv,noheader",
        "-i",
        GPU_INDEX,
    ).strip()
    if observed_uuid != GPU_UUID:
        raise RuntimeError(
            f"GPU2 UUID mismatch: expected {GPU_UUID}, observed {observed_uuid}"
        )
    raw = run(
        "nvidia-smi",
        "--query-compute-apps=pid,gpu_uuid,process_name",
        "--format=csv,noheader,nounits",
        "-i",
        GPU_INDEX,
    )
    processes: list[dict[str, Any]] = []
    for line in raw.splitlines():
        columns = [value.strip() for value in line.split(",", 2)]
        if len(columns) != 3 or not columns[0].isdigit():
            continue
        processes.append(
            {
                "pid": int(columns[0]),
                "gpu_uuid": columns[1],
                "process_name": columns[2],
            }
        )
    return observed_uuid, processes


def collect() -> dict[str, Any]:
    gpu_uuid, processes = gpu_processes()
    containers = production_backend_containers()
    unit_pids = {
        unit: pid
        for unit in ALLOWED_UNITS
        if (pid := unit_main_pid(unit)) is not None
    }
    roots = set(containers.values()) | set(unit_pids.values())
    allowed: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = []
    for process in processes:
        pid = int(process["pid"])
        item = dict(process)
        if is_descendant(pid, roots):
            item["authority"] = "production-service"
            allowed.append(item)
        else:
            try:
                cgroup = pathlib.Path(f"/proc/{pid}/cgroup").read_text()
            except OSError:
                cgroup = ""
            container_id = next(
                (identifier for identifier in containers if identifier in cgroup),
                None,
            )
            if container_id is not None:
                item["authority"] = "production-backend"
                allowed.append(item)
            else:
                item["cgroup"] = cgroup.strip()
                unknown.append(item)
    return {
        "schema_version": 1,
        "observed_at": dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "gpu_index": GPU_INDEX,
        "gpu_uuid": gpu_uuid,
        "status": "ready" if not unknown else "quarantined",
        "allowed_processes": allowed,
        "unknown_processes": unknown,
        "authorities": {
            "backend_containers": sorted(containers),
            "systemd_user_units": unit_pids,
        },
    }


def atomic_write(path: pathlib.Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as stream:
        json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
        temporary = pathlib.Path(stream.name)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=pathlib.Path, default=DEFAULT_STATE)
    parser.add_argument(
        "--require-ready",
        action="store_true",
        help="return non-zero when an unknown GPU2 process is present",
    )
    args = parser.parse_args()
    payload = collect()
    atomic_write(args.state, payload)
    print(json.dumps(payload, sort_keys=True))
    return 2 if args.require_ready and payload["status"] != "ready" else 0


if __name__ == "__main__":
    raise SystemExit(main())
