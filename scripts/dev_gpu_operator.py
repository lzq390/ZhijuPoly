#!/usr/bin/env python3
"""Owner-private bridge from the 9001 dev Backend to the GPU1 session workflow."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
from uuid import uuid4


SCHEMA_VERSION = 1
QUEUE_TIMEOUT_SECONDS = 30 * 60
MAX_REQUEST_BYTES = 8 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
SOURCE_RE = re.compile(r"^[0-9a-f]{40}$")
OPERATOR_PHASES = {
    "stopped",
    "recovering",
    "queued",
    "starting",
    "ready",
    "failed",
    "unavailable",
}
CONTROLLER_STARTING_STATES = {
    "auditing",
    "plane-ready",
    "stabilizing",
    "starting",
}
CONTROLLER_RECOVERY_STATES = {
    "audit-failed",
    "broker-failed",
    "cleanup-blocked",
    "contaminated",
    "gpu3-drift",
    "isolation-waiting",
    "recovered",
    "startup-failed",
}
ACTIVE_OPERATION_PHASES = {"queued", "starting"}


class OperatorError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _utc_after(seconds: int) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(seconds=seconds)
    ).isoformat().replace("+00:00", "Z")


def _process_start_ticks(pid: int) -> int:
    raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    closing = raw.rfind(")")
    if closing < 0:
        raise OperatorError("operator process identity is unavailable")
    return int(raw[closing + 2 :].split()[19])


def _require_directory(path: Path, *, mode: int = 0o700) -> None:
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != mode
    ):
        raise OperatorError(f"unsafe GPU operator directory: {path}")


def _require_regular_file(path: Path, *, mode: int = 0o600) -> None:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != mode
    ):
        raise OperatorError(f"unsafe GPU operator file: {path}")


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        payload = (
            json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        os.write(descriptor, payload)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _load_json(path: Path) -> dict[str, Any]:
    _require_regular_file(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OperatorError(f"invalid GPU operator state: {path}") from exc
    if not isinstance(value, dict):
        raise OperatorError(f"invalid GPU operator state: {path}")
    return value


def prepare_runtime(repository: Path) -> tuple[Path, Path]:
    repository = repository.resolve(strict=True)
    runtime_root = repository / ".runtime"
    if runtime_root.is_symlink():
        raise OperatorError("development runtime root must not be a symlink")
    runtime_root.mkdir(mode=0o700, exist_ok=True)
    os.chmod(runtime_root, 0o700)
    _require_directory(runtime_root)

    private_dir = runtime_root / "gpu-operator"
    client_dir = runtime_root / "gpu-operator-client"
    for directory in (private_dir, client_dir):
        if directory.is_symlink():
            raise OperatorError("GPU operator directory must not be a symlink")
        directory.mkdir(mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
        _require_directory(directory)

    for filename in ("operator.log", "recover.log"):
        path = private_dir / filename
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_APPEND
            | os.O_CREAT
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        os.fchmod(descriptor, 0o600)
        os.close(descriptor)
        _require_regular_file(path)
    return private_dir, client_dir


class DevGpuOperator:
    def __init__(
        self,
        *,
        repository: Path,
        source_sha: str,
        source_tree: str,
        python_executable: str = "/usr/bin/python3",
    ) -> None:
        if not SOURCE_RE.fullmatch(source_sha) or not SOURCE_RE.fullmatch(source_tree):
            raise OperatorError("GPU operator requires full Git source identities")
        self.repository = repository.resolve(strict=True)
        self.private_dir = self.repository / ".runtime" / "gpu-operator"
        self.client_dir = self.repository / ".runtime" / "gpu-operator-client"
        self.socket_path = self.client_dir / "operator.sock"
        self.identity_path = self.private_dir / "operator.json"
        self.operation_path = self.private_dir / "operation.json"
        self.recover_log_path = self.private_dir / "recover.log"
        self.source_sha = source_sha
        self.source_tree = source_tree
        self.python_executable = python_executable
        self._lock = threading.Lock()
        self._shutdown = threading.Event()
        self._operation_thread: threading.Thread | None = None
        self._child: subprocess.Popen[bytes] | None = None
        self._operation = self._load_previous_operation()

    def _load_previous_operation(self) -> dict[str, Any] | None:
        if not self.operation_path.exists():
            return None
        operation = _load_json(self.operation_path)
        if operation.get("phase") in ACTIVE_OPERATION_PHASES:
            operation.update(
                {
                    "phase": "failed",
                    "message": "GPU 恢复操作因 operator 重启而中断，请重新点击",
                    "updated_at": _utc_now(),
                }
            )
            _atomic_json(self.operation_path, operation)
        return operation

    def _controller_status(self) -> dict[str, Any]:
        completed = subprocess.run(
            [
                self.python_executable,
                "-I",
                str(self.repository / "scripts" / "dev_gpu_session.py"),
                "status",
            ],
            cwd=self.repository,
            text=True,
            capture_output=True,
            timeout=8,
            check=False,
        )
        if completed.returncode != 0:
            raise OperatorError("GPU controller 状态不可用")
        try:
            value = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise OperatorError("GPU controller 返回了无效状态") from exc
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != 1
            or not isinstance(value.get("status"), str)
            or value.get("gpu_index") != 1
        ):
            raise OperatorError("GPU controller 返回了无效状态")
        return value

    def _validate_candidate(self) -> None:
        for argument, expected in (
            ("HEAD", self.source_sha),
            ("HEAD^{tree}", self.source_tree),
        ):
            completed = subprocess.run(
                ["git", "--no-optional-locks", "rev-parse", "--verify", argument],
                cwd=self.repository,
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            )
            if completed.returncode != 0 or completed.stdout.strip() != expected:
                raise OperatorError("当前代码身份已变化，请先重新启动开发环境")

    def _write_operation(self, operation: dict[str, Any]) -> None:
        operation["updated_at"] = _utc_now()
        _atomic_json(self.operation_path, operation)
        self._operation = operation

    def _new_operation(self, phase: str, message: str) -> dict[str, Any]:
        now = _utc_now()
        return {
            "schema_version": SCHEMA_VERSION,
            "operation_id": uuid4().hex,
            "phase": phase,
            "message": message,
            "source_sha": self.source_sha,
            "source_tree": self.source_tree,
            "requested_at": now,
            "deadline_at": _utc_after(QUEUE_TIMEOUT_SECONDS),
            "updated_at": now,
        }

    def _operation_active_locked(self) -> bool:
        return (
            self._operation is not None
            and self._operation.get("phase") in ACTIVE_OPERATION_PHASES
            and self._operation_thread is not None
            and self._operation_thread.is_alive()
        )

    def _public_status_locked(
        self, controller: dict[str, Any]
    ) -> dict[str, Any]:
        controller_state = str(controller["status"])
        operation = self._operation
        operation_active = self._operation_active_locked()

        if operation_active:
            phase = str(operation["phase"])
            message = str(operation["message"])
            can_recover = False
            operation_id = operation["operation_id"]
        elif controller_state == "ready":
            phase = "ready"
            message = "GPU1 相关服务已就绪"
            can_recover = False
            operation_id = operation.get("operation_id") if operation else None
        elif controller_state in CONTROLLER_STARTING_STATES:
            phase = "starting"
            message = "GPU1 session 正在启动"
            can_recover = False
            operation_id = operation.get("operation_id") if operation else None
        elif controller_state in CONTROLLER_RECOVERY_STATES:
            phase = "recovering"
            message = "GPU1 session 正在安全回收，可排队一次恢复"
            can_recover = True
            operation_id = operation.get("operation_id") if operation else None
        elif controller_state == "stopped":
            if operation is not None and operation.get("phase") == "failed":
                phase = "failed"
                message = str(operation["message"])
            else:
                phase = "stopped"
                message = "GPU 服务未启动，点击后将恢复 GPU1 相关服务"
            can_recover = True
            operation_id = operation.get("operation_id") if operation else None
        else:
            phase = "recovering"
            message = f"GPU1 session 处于 {controller_state}，可排队一次恢复"
            can_recover = True
            operation_id = operation.get("operation_id") if operation else None

        return {
            "schema_version": SCHEMA_VERSION,
            "operator_available": True,
            "phase": phase,
            "controller_status": controller_state,
            "can_recover": can_recover,
            "operation_id": operation_id,
            "message": message[:512],
            "source_sha": self.source_sha,
            "source_tree": self.source_tree,
            "updated_at": (
                operation.get("updated_at") if operation is not None else _utc_now()
            ),
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            # While one recovery owns the controller transition, report that
            # operation directly instead of launching a competing status probe.
            if self._operation_active_locked():
                return self._public_status_locked({"status": "stopped"})
            try:
                controller = self._controller_status()
            except OperatorError as exc:
                operation_id = (
                    self._operation.get("operation_id")
                    if self._operation is not None
                    else None
                )
                return {
                    "schema_version": SCHEMA_VERSION,
                    "operator_available": True,
                    "phase": "unavailable",
                    "controller_status": "unavailable",
                    "can_recover": False,
                    "operation_id": operation_id,
                    "message": str(exc),
                    "source_sha": self.source_sha,
                    "source_tree": self.source_tree,
                    "updated_at": _utc_now(),
                }
            return self._public_status_locked(controller)

    def recover(self) -> dict[str, Any]:
        with self._lock:
            if self._operation_active_locked():
                return self._public_status_locked({"status": "stopped"})
            # Serialize the final idle probe with operation creation so two
            # clicks cannot start competing recovery children.
            controller = self._controller_status()
            current = self._public_status_locked(controller)
            if current["phase"] in {"queued", "starting", "ready"}:
                return current
            if not current["can_recover"]:
                return current
            self._validate_candidate()
            controller_state = str(controller["status"])
            operation = self._new_operation(
                "queued" if controller_state != "stopped" else "starting",
                (
                    "正在等待当前 GPU1 session 安全回收"
                    if controller_state != "stopped"
                    else "正在启动 GPU1 相关服务"
                ),
            )
            self._write_operation(operation)
            thread = threading.Thread(
                target=self._run_recovery,
                args=(operation["operation_id"], time.monotonic() + QUEUE_TIMEOUT_SECONDS),
                name="gpu-operator-recovery",
                daemon=True,
            )
            self._operation_thread = thread
            thread.start()
            return self._public_status_locked(controller)

    def _finish_operation(self, operation_id: str, phase: str, message: str) -> None:
        with self._lock:
            if (
                self._operation is None
                or self._operation.get("operation_id") != operation_id
            ):
                return
            operation = dict(self._operation)
            operation.update({"phase": phase, "message": message})
            self._write_operation(operation)

    def _run_recovery(self, operation_id: str, deadline: float) -> None:
        try:
            while time.monotonic() < deadline and not self._shutdown.is_set():
                controller = self._controller_status()
                controller_state = str(controller["status"])
                if controller_state == "ready":
                    self._finish_operation(
                        operation_id, "ready", "GPU1 相关服务已就绪"
                    )
                    return
                controller_record = (
                    self.repository
                    / ".runtime"
                    / "gpu-session"
                    / "controller.json"
                )
                if controller_state == "stopped" and not controller_record.exists():
                    break
                if controller_state in CONTROLLER_STARTING_STATES:
                    self._finish_operation(
                        operation_id, "starting", "GPU1 session 正在启动"
                    )
                else:
                    self._finish_operation(
                        operation_id,
                        "queued",
                        "正在等待当前 GPU1 session 安全回收",
                    )
                time.sleep(1)
            else:
                self._finish_operation(
                    operation_id,
                    "failed",
                    "等待 GPU1 session 安全回收超过 30 分钟，请重新点击",
                )
                return

            self._finish_operation(
                operation_id, "starting", "正在启动 GPU1 相关服务"
            )
            log_descriptor = os.open(
                self.recover_log_path,
                os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                os.fchmod(log_descriptor, 0o600)
                os.write(
                    log_descriptor,
                    f"\n[{_utc_now()}] recovery {operation_id}\n".encode(),
                )
                environment = os.environ.copy()
                environment["NEXPOLY_DEV_GPU_SESSION_EXECUTE"] = "1"
                environment["NEXPOLY_DEV_GPU_DIRECT_START"] = "1"
                child = subprocess.Popen(
                    [
                        str(self.repository / "scripts" / "dev_server_gpu.sh"),
                        "gpu-session-up",
                    ],
                    cwd=self.repository,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=log_descriptor,
                    stderr=subprocess.STDOUT,
                    close_fds=True,
                )
                with self._lock:
                    self._child = child
                remaining = deadline - time.monotonic()
                try:
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(
                            child.args,
                            QUEUE_TIMEOUT_SECONDS,
                        )
                    return_code = child.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    os.write(
                        log_descriptor,
                        (
                            f"[{_utc_now()}] recovery timed out; "
                            "terminating launcher child\n"
                        ).encode(),
                    )
                    child.terminate()
                    try:
                        child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=10)
                    self._finish_operation(
                        operation_id,
                        "failed",
                        "GPU 服务启动超过 30 分钟，已终止启动命令；请查看 recover.log",
                    )
                    return
            finally:
                os.close(log_descriptor)
                with self._lock:
                    self._child = None

            controller = self._controller_status()
            if return_code == 0 and controller.get("status") == "ready":
                self._finish_operation(
                    operation_id, "ready", "GPU1 相关服务已就绪"
                )
            else:
                self._finish_operation(
                    operation_id,
                    "failed",
                    "GPU 服务恢复失败，请查看本地 recover.log",
                )
        except Exception as exc:
            self._finish_operation(
                operation_id,
                "failed",
                f"GPU 服务恢复失败：{str(exc)[:420]}",
            )

    def request_shutdown(self) -> dict[str, Any]:
        with self._lock:
            if self._operation_thread is not None and self._operation_thread.is_alive():
                raise OperatorError("GPU 恢复操作尚未结束，operator 拒绝关闭")
            self._shutdown.set()
        return {"schema_version": SCHEMA_VERSION, "shutdown": True}

    def _handle_connection(self, connection: socket.socket) -> None:
        connection.settimeout(5)
        credentials = connection.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
        )
        _peer_pid, peer_uid, _peer_gid = struct.unpack("3i", credentials)
        if peer_uid != os.geteuid():
            raise OperatorError("GPU operator peer uid is not authorized")

        payload = bytearray()
        while b"\n" not in payload:
            chunk = connection.recv(1024)
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > MAX_REQUEST_BYTES:
                raise OperatorError("GPU operator request is too large")
        try:
            request = json.loads(bytes(payload).split(b"\n", 1)[0])
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OperatorError("GPU operator request is invalid") from exc
        if (
            not isinstance(request, dict)
            or set(request) != {"schema_version", "command"}
            or request.get("schema_version") != SCHEMA_VERSION
            or request.get("command") not in {"status", "recover", "shutdown"}
        ):
            raise OperatorError("GPU operator request is not allowed")
        command = request["command"]
        if command == "status":
            result = self.status()
        elif command == "recover":
            result = self.recover()
        else:
            result = self.request_shutdown()
        response = {"ok": True, "result": result}
        encoded = (json.dumps(response, separators=(",", ":")) + "\n").encode()
        if len(encoded) > MAX_RESPONSE_BYTES:
            raise OperatorError("GPU operator response is too large")
        connection.sendall(encoded)

    def _cleanup_identity(self, start_ticks: int) -> None:
        try:
            identity = _load_json(self.identity_path)
            if (
                identity.get("pid") == os.getpid()
                and identity.get("start_ticks") == start_ticks
            ):
                self.identity_path.unlink()
        except (FileNotFoundError, OperatorError, OSError):
            pass
        try:
            metadata = self.socket_path.lstat()
            if stat.S_ISSOCK(metadata.st_mode) and metadata.st_uid == os.geteuid():
                self.socket_path.unlink()
        except FileNotFoundError:
            pass

    def serve(self) -> None:
        prepare_runtime(self.repository)
        _require_directory(self.private_dir)
        _require_directory(self.client_dir)
        for path in (self.socket_path, self.identity_path):
            if path.exists() or path.is_symlink():
                raise OperatorError(f"GPU operator retains stale identity: {path}")

        start_ticks = _process_start_ticks(os.getpid())
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(self.socket_path))
            os.chmod(self.socket_path, 0o600)
            metadata = self.socket_path.lstat()
            if (
                not stat.S_ISSOCK(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise OperatorError("GPU operator socket permissions are unsafe")
            _atomic_json(
                self.identity_path,
                {
                    "schema_version": SCHEMA_VERSION,
                    "pid": os.getpid(),
                    "start_ticks": start_ticks,
                    "source_sha": self.source_sha,
                    "source_tree": self.source_tree,
                },
            )
            server.listen(8)
            server.settimeout(0.5)

            def request_exit(_signum: int, _frame: object) -> None:
                if self._operation_thread is None or not self._operation_thread.is_alive():
                    self._shutdown.set()

            signal.signal(signal.SIGTERM, request_exit)
            signal.signal(signal.SIGINT, request_exit)
            while not self._shutdown.is_set():
                try:
                    connection, _address = server.accept()
                except socket.timeout:
                    continue
                with connection:
                    try:
                        self._handle_connection(connection)
                    except Exception as exc:
                        error = {
                            "ok": False,
                            "error": {
                                "code": "operator_error",
                                "message": str(exc)[:512],
                            },
                        }
                        try:
                            connection.sendall(
                                (
                                    json.dumps(error, separators=(",", ":")) + "\n"
                                ).encode()
                            )
                        except OSError:
                            pass
        finally:
            server.close()
            self._cleanup_identity(start_ticks)


def request_operator(
    socket_path: Path, command: str, timeout: float
) -> dict[str, Any]:
    _require_directory(socket_path.parent)
    metadata = socket_path.lstat()
    if (
        not stat.S_ISSOCK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise OperatorError("GPU operator socket permissions are unsafe")
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(str(socket_path))
        client.sendall(
            (
                json.dumps(
                    {"schema_version": SCHEMA_VERSION, "command": command},
                    separators=(",", ":"),
                )
                + "\n"
            ).encode()
        )
        payload = bytearray()
        while b"\n" not in payload:
            chunk = client.recv(4096)
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > MAX_RESPONSE_BYTES:
                raise OperatorError("GPU operator response is too large")
    finally:
        client.close()
    try:
        response = json.loads(bytes(payload).split(b"\n", 1)[0])
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OperatorError("GPU operator response is invalid") from exc
    if not isinstance(response, dict) or response.get("ok") is not True:
        error = response.get("error") if isinstance(response, dict) else None
        message = error.get("message") if isinstance(error, dict) else None
        raise OperatorError(str(message or "GPU operator request failed"))
    result = response.get("result")
    if not isinstance(result, dict):
        raise OperatorError("GPU operator response is invalid")
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--repository", required=True, type=Path)
    serve = subparsers.add_parser("serve")
    serve.add_argument("--repository", required=True, type=Path)
    serve.add_argument("--source-sha", required=True)
    serve.add_argument("--source-tree", required=True)
    request = subparsers.add_parser("request")
    request.add_argument("--socket", required=True, type=Path)
    request.add_argument(
        "--command", required=True, choices=("status", "recover", "shutdown")
    )
    request.add_argument("--timeout", type=float, default=5.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    args = parse_args(argv)
    try:
        if args.command == "prepare":
            prepare_runtime(args.repository)
        elif args.command == "serve":
            DevGpuOperator(
                repository=args.repository,
                source_sha=args.source_sha,
                source_tree=args.source_tree,
            ).serve()
        else:
            result = request_operator(
                args.socket, args.command, max(0.1, args.timeout)
            )
            print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except (OperatorError, OSError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
