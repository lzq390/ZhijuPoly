#!/usr/bin/env python3
"""Fail closed unless every GPU selected for the Worker has no compute PID."""

from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import dataclass


QUERY_TIMEOUT_SECONDS = 15


class GateError(RuntimeError):
    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


@dataclass(frozen=True)
class Gpu:
    index: str
    uuid: str


def _query(executable: str, query: str) -> str:
    try:
        completed = subprocess.run(
            [
                executable,
                query,
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=QUERY_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        raise GateError("nvidia_smi_missing") from None
    except (OSError, subprocess.SubprocessError):
        raise GateError("nvidia_smi_query_failed") from None
    if completed.returncode != 0:
        raise GateError("nvidia_smi_query_failed")
    return completed.stdout


def _csv_rows(payload: str, *, columns: int) -> list[list[str]]:
    rows: list[list[str]] = []
    for original in payload.splitlines():
        line = original.strip()
        if not line or line.lower().startswith("no running processes"):
            continue
        values = [value.strip() for value in line.split(",")]
        if len(values) != columns or any(not value for value in values):
            raise GateError("nvidia_smi_invalid_output")
        rows.append(values)
    return rows


def _installed_gpus(executable: str) -> list[Gpu]:
    rows = _csv_rows(
        _query(executable, "--query-gpu=index,uuid"), columns=2
    )
    gpus = [Gpu(index=index, uuid=uuid) for index, uuid in rows]
    if not gpus or len({gpu.index for gpu in gpus}) != len(gpus):
        raise GateError("nvidia_smi_invalid_output")
    if len({gpu.uuid for gpu in gpus}) != len(gpus):
        raise GateError("nvidia_smi_invalid_output")
    return gpus


def _resolve_devices(device_spec: str, gpus: list[Gpu]) -> set[str]:
    if device_spec == "all":
        return {gpu.uuid for gpu in gpus}
    tokens = device_spec.split(",")
    if any(not token or token != token.strip() for token in tokens):
        raise GateError("invalid_gpu_selection")

    selected: set[str] = set()
    for token in tokens:
        matches = [gpu for gpu in gpus if gpu.index == token]
        if not matches and token.startswith("GPU-"):
            matches = [gpu for gpu in gpus if gpu.uuid.startswith(token)]
        if len(matches) != 1:
            raise GateError("invalid_gpu_selection")
        selected.add(matches[0].uuid)
    if not selected:
        raise GateError("invalid_gpu_selection")
    return selected


def _active_compute_processes(
    executable: str, selected: set[str], allowed_pids: set[str]
) -> tuple[int, int]:
    rows = _csv_rows(
        _query(executable, "--query-compute-apps=pid,gpu_uuid"), columns=2
    )
    occupied = 0
    allowed = 0
    for pid, gpu_uuid in rows:
        if not pid.isdigit():
            raise GateError("nvidia_smi_invalid_output")
        if gpu_uuid in selected:
            if pid in allowed_pids:
                allowed += 1
            else:
                occupied += 1
    return occupied, allowed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device-spec", required=True)
    parser.add_argument("--nvidia-smi", default="nvidia-smi")
    parser.add_argument("--allow-pid", action="append", default=[])
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if any(not pid.isdigit() or int(pid) <= 0 for pid in args.allow_pid):
            raise GateError("invalid_allowed_pid")
        selected = _resolve_devices(
            args.device_spec, _installed_gpus(args.nvidia_smi)
        )
        occupied, allowed = _active_compute_processes(
            args.nvidia_smi, selected, set(args.allow_pid)
        )
    except GateError as exc:
        print(
            json.dumps(
                {"error_category": exc.category, "idle": False},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 1

    summary = {
        "gpu_count": len(selected),
        "idle": occupied == 0,
        "allowed_worker_processes": allowed,
        "occupied_processes": occupied,
    }
    if occupied:
        summary["error_category"] = "gpu_busy"
    print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    return 0 if occupied == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
