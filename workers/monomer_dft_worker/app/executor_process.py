"""GPU executor child entry point.

This module intentionally imports CUDA-bearing packages only inside ``main``.
The parent sets CUDA_VISIBLE_DEVICES before starting this interpreter.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import queue
import select
import socket
import threading
import time
from pathlib import Path
from typing import Any

from .executor_ipc import protocol_message, receive_frame, send_frame, validate_message


def _failure_payload(exc: BaseException) -> tuple[str, str, bool, dict[str, Any], bool]:
    # Imports remain child-only; the ASGI supervisor never imports the scientific
    # engine or its CUDA adapters through this entry point.
    from .chemistry import ChemistryValidationError
    from .engine import ComputationCancelled, ScientificComputationError

    if isinstance(exc, ComputationCancelled):
        return "cancelled", "Calculation was cancelled.", False, {}, False
    if isinstance(exc, ChemistryValidationError):
        return exc.code, str(exc), False, exc.details, False
    if isinstance(exc, ScientificComputationError):
        terminate = exc.code in {"gpu_oom", "cuda_fatal"}
        return exc.code, str(exc), exc.retryable, exc.details, terminate
    lowered = str(exc).lower()
    if isinstance(exc, MemoryError) or "out of memory" in lowered:
        return (
            "gpu_oom",
            "The calculation exceeded available GPU memory.",
            True,
            {},
            True,
        )
    if any(
        marker in lowered
        for marker in (
            "cuda",
            "cublas",
            "cudnn",
            "device-side assert",
            "illegal memory access",
            "xid",
        )
    ):
        return (
            "cuda_fatal",
            "The GPU runtime entered an unsafe state.",
            True,
            {},
            True,
        )
    return (
        "internal_error",
        "The executor encountered an unexpected calculation error.",
        True,
        {"exception_type": type(exc).__name__},
        False,
    )


def _validate_identity(identity: Any) -> dict[str, Any]:
    if not isinstance(identity, dict):
        raise ValueError("execution identity must be an object")
    required_strings = (
        "job_id",
        "attempt_token",
        "request_sha256",
        "lease_id",
        "gpu_uuid",
    )
    if any(not isinstance(identity.get(key), str) or not identity[key] for key in required_strings):
        raise ValueError("execution identity contains an invalid string field")
    for key in ("enqueue_sequence", "fencing_token"):
        value = identity.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"execution identity {key} must be a positive integer")
    return dict(identity)


def _serve(stream: socket.socket, *, mode: str, model: str, gpu_index: str) -> int:
    expected_gpu_uuid = os.environ.get("NEXPOLY_DFT_EXECUTOR_GPU_UUID", "")
    send_frame(
        stream,
        protocol_message(
            "spawned",
            mode=mode,
            gpu_index=gpu_index,
            expected_gpu_uuid=expected_gpu_uuid,
            cuda_visible_devices=os.environ.get("CUDA_VISIBLE_DEVICES", ""),
            model=model,
            pid=os.getpid(),
        ),
    )
    authorization = receive_frame(stream)
    validate_message(authorization, "authorize_cuda")
    if (
        authorization.get("gpu_uuid") != expected_gpu_uuid
        or not isinstance(authorization.get("lease_id"), str)
        or not authorization.get("lease_id")
        or isinstance(authorization.get("fencing_token"), bool)
        or not isinstance(authorization.get("fencing_token"), int)
        or authorization["fencing_token"] < 1
    ):
        raise ValueError("executor CUDA authorization does not match its lease")

    # CUDA selection and Broker workload registration are now complete. These
    # imports are deliberately below the parent authorization boundary.
    from .config import load_settings
    from .engine import AimnetComputeBackend, ScientificEngine
    from .runtime import AimnetRuntime
    from .schemas import JobSubmitRequest

    settings = dataclasses.replace(
        load_settings(),
        physical_gpu=gpu_index,
        model_name=model,
        preload_all_models=mode == "primary",
        warmup_models=True,
    )
    runtime = AimnetRuntime(settings)
    load_started = time.perf_counter()
    runtime.load()
    model_load_ms = (time.perf_counter() - load_started) * 1000.0
    engine = ScientificEngine(AimnetComputeBackend(runtime))
    probe = runtime.probe().to_dict()
    actual_gpu_uuid = probe.get("gpu_uuid")
    if not isinstance(actual_gpu_uuid, str) or not actual_gpu_uuid:
        raise RuntimeError("executor runtime did not report its CUDA device UUID")
    send_frame(
        stream,
        protocol_message(
            "ready",
            mode=mode,
            gpu_index=gpu_index,
            gpu_uuid=actual_gpu_uuid,
            cuda_visible_devices=os.environ.get("CUDA_VISIBLE_DEVICES", ""),
            model=model,
            model_load_ms=model_load_ms,
            probe=probe,
            pid=os.getpid(),
        ),
    )

    try:
        while True:
            command = receive_frame(stream)
            command_type = validate_message(command)
            if command_type == "shutdown":
                send_frame(stream, protocol_message("stopped"))
                return 0
            if command_type != "execute":
                raise ValueError("executor accepts only execute or shutdown while idle")

            identity = _validate_identity(command.get("identity"))
            request = JobSubmitRequest.model_validate(command.get("request"))
            if (
                request.job_id != identity["job_id"]
                or request.attempt_token != identity["attempt_token"]
                or request.request_sha256 != identity["request_sha256"]
                or request.enqueue_sequence != identity["enqueue_sequence"]
            ):
                raise ValueError("execution identity does not match the scientific request")
            if mode == "overflow" and request.model != model:
                raise ValueError("overflow executor model does not match the request")

            output_directory = Path(str(command.get("output_directory")))
            expected_output = (
                settings.job_root
                / request.job_id
                / request.attempt_token
                / "artifacts"
            )
            if (
                not output_directory.is_absolute()
                or output_directory != expected_output
                or output_directory.is_symlink()
            ):
                raise ValueError("executor output directory is outside the durable attempt")
            provenance = command.get("provenance")
            if not isinstance(provenance, dict):
                raise ValueError("execution provenance must be an object")
            queue_wait_ms = float(command.get("queue_wait_ms", 0.0))
            execution_timings = command.get("execution_timings", {})
            if not isinstance(execution_timings, dict):
                raise ValueError("execution timings must be an object")
            cancelled = threading.Event()
            events: queue.Queue[dict[str, Any]] = queue.Queue()

            def progress(stage: str, percent: int, message: str | None) -> None:
                events.put(
                    protocol_message(
                        "progress",
                        identity=identity,
                        stage=stage,
                        percent=percent,
                        message=message,
                    )
                )

            def compute() -> None:
                try:
                    execution = engine.execute(
                        request,
                        output_directory,
                        progress=progress,
                        cancelled=cancelled.is_set,
                        provenance=provenance,
                        queue_wait_ms=queue_wait_ms,
                        execution_timings={
                            str(key): float(value)
                            for key, value in execution_timings.items()
                        },
                    )
                    events.put(
                        protocol_message(
                            "result",
                            identity=identity,
                            result=execution.result,
                            timings=execution.timings,
                            artifacts=[
                                {
                                    "descriptor": descriptor.model_dump(mode="json"),
                                    "path": os.fspath(path),
                                }
                                for descriptor, path in execution.artifacts
                            ],
                        )
                    )
                except BaseException as exc:  # converted to a stable IPC error.
                    code, message, retryable, details, terminate = _failure_payload(exc)
                    events.put(
                        protocol_message(
                            "error",
                            identity=identity,
                            code=code,
                            message=message,
                            retryable=retryable,
                            details=details,
                            terminate_executor=terminate,
                        )
                    )

            worker = threading.Thread(target=compute, name="aimnet-execution", daemon=False)
            worker.start()
            terminal: dict[str, Any] | None = None
            while terminal is None:
                try:
                    event = events.get(timeout=0.05)
                except queue.Empty:
                    event = None
                if event is not None:
                    send_frame(stream, event)
                    if event["type"] in {"result", "error"}:
                        terminal = event
                readable, _, _ = select.select([stream], [], [], 0)
                if readable:
                    control = receive_frame(stream)
                    control_type = validate_message(control)
                    if control_type == "cancel":
                        if control.get("identity") != identity:
                            raise ValueError("cancel identity does not match the active attempt")
                        cancelled.set()
                    else:
                        raise ValueError("only cancel is allowed during execution")
            worker.join()
            if terminal.get("terminate_executor"):
                return 71 if terminal.get("code") == "gpu_oom" else 72
            if mode == "overflow":
                return 0
    finally:
        runtime.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--fd", required=True, type=int)
    parser.add_argument("--mode", required=True, choices=("primary", "overflow"))
    parser.add_argument("--model", required=True)
    parser.add_argument("--gpu-index", required=True, choices=("1", "2", "3"))
    args = parser.parse_args(argv)

    expected_gpu = os.getenv("NEXPOLY_DFT_EXECUTOR_GPU_DEVICE")
    expected_gpu_uuid = os.getenv("NEXPOLY_DFT_EXECUTOR_GPU_UUID")
    visible_gpu = os.getenv("CUDA_VISIBLE_DEVICES")
    if (
        expected_gpu != args.gpu_index
        or not expected_gpu_uuid
        or visible_gpu not in {args.gpu_index, expected_gpu_uuid}
    ):
        raise RuntimeError("executor GPU was not selected before process initialization")
    if os.getenv("MONOMER_DFT_EXECUTOR_PROCESS") != "1":
        raise RuntimeError("executor process marker is missing")
    stream = socket.socket(fileno=args.fd)
    try:
        return _serve(
            stream,
            mode=args.mode,
            model=args.model,
            gpu_index=args.gpu_index,
        )
    finally:
        stream.close()


if __name__ == "__main__":  # pragma: no cover - exercised through subprocess tests.
    raise SystemExit(main())
