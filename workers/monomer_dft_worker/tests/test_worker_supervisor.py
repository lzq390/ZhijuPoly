from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]


def _write_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _process_is_running(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()[2]
    except (FileNotFoundError, IndexError, PermissionError):
        return False
    return state != "Z"


def _wait_for(predicate: object, *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if callable(predicate) and predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition was not satisfied before the timeout")


@dataclass(frozen=True)
class SupervisorSandbox:
    root: Path
    repo: Path
    controller: Path
    behavior_file: Path
    attempts_file: Path
    children_file: Path
    launches_file: Path
    pid_file: Path
    socket_path: Path
    env: dict[str, str]

    def run_controller(
        self,
        command: str,
        *,
        timeout: float = 15.0,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.controller), command],
            cwd=self.repo,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )

    def attempts(self) -> list[int]:
        if not self.attempts_file.exists():
            return []
        return [
            int(value)
            for value in self.attempts_file.read_text(encoding="utf-8").splitlines()
            if value
        ]

    def child_pids(self) -> list[int]:
        if not self.children_file.exists():
            return []
        return [
            int(value)
            for value in self.children_file.read_text(encoding="utf-8").splitlines()
            if value
        ]

    def launch_pids(self) -> list[int]:
        if not self.launches_file.exists():
            return []
        return [
            int(value)
            for value in self.launches_file.read_text(encoding="utf-8").splitlines()
            if value
        ]


def _make_sandbox(root: Path) -> SupervisorSandbox:
    repo = root / "repo"
    runner = repo / "workers/monomer_dft_worker/run_host_worker.sh"
    controller = repo / "scripts/monomer_dft_worker_ctl.sh"
    runner.parent.mkdir(parents=True)
    controller.parent.mkdir(parents=True)
    shutil.copy2(
        REPO_ROOT / "workers/monomer_dft_worker/run_host_worker.sh",
        runner,
    )
    shutil.copy2(REPO_ROOT / "scripts/monomer_dft_worker_ctl.sh", controller)
    runner.chmod(0o755)
    controller.chmod(0o755)

    preflight = repo / "scripts/preflight_monomer_dft_env.py"
    preflight.write_text("raise SystemExit(0)\n", encoding="utf-8")

    runtime = repo / ".runtime"
    python_wrapper = runtime / "venvs/monomer-dft-worker/bin/python"
    socket_path = runtime / "monomer-dft-worker-socket/worker.sock"
    socket_path.parent.mkdir(parents=True)

    behavior_file = root / "behavior.txt"
    attempts_file = root / "attempts.log"
    children_file = root / "children.log"
    launches_file = root / "launches.log"
    fake_uvicorn = root / "fake_uvicorn.py"
    fake_uvicorn.write_text(
        textwrap.dedent(
            f"""
            from __future__ import annotations

            import json
            import os
            import signal
            import socket
            import sys
            import time
            from pathlib import Path

            behavior_file = Path({str(behavior_file)!r})
            attempts_file = Path({str(attempts_file)!r})
            children_file = Path({str(children_file)!r})
            behaviors = behavior_file.read_text(encoding="utf-8").replace(",", " ").split()
            prior_attempts = (
                attempts_file.read_text(encoding="utf-8").splitlines()
                if attempts_file.exists()
                else []
            )
            attempt = len(prior_attempts) + 1
            with attempts_file.open("a", encoding="utf-8") as stream:
                stream.write(f"{{attempt}}\\n")
                stream.flush()
                os.fsync(stream.fileno())
            with children_file.open("a", encoding="utf-8") as stream:
                stream.write(f"{{os.getpid()}}\\n")
                stream.flush()
                os.fsync(stream.fileno())

            behavior = behaviors[min(attempt - 1, len(behaviors) - 1)]
            uds = Path(sys.argv[sys.argv.index("--uds") + 1])

            if behavior == "70":
                # Stay alive long enough for the supervisor to verify the new
                # process group, then deliberately leave a stale UDS inode.
                time.sleep(0.15)
                server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                server.bind(str(uds))
                server.listen(1)
                server.close()
                raise SystemExit(70)

            if behavior == "signal_launch":
                behavior = "healthy"

            if behavior != "healthy":
                raise SystemExit(int(behavior))

            stopping = False

            def request_stop(_signum: int, _frame: object) -> None:
                global stopping
                stopping = True

            signal.signal(signal.SIGTERM, request_stop)
            signal.signal(signal.SIGINT, request_stop)
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(uds))
            server.listen(8)
            server.settimeout(0.1)
            body = json.dumps({{"status": "ok"}}, separators=(",", ":")).encode()
            response = (
                b"HTTP/1.1 200 OK\\r\\n"
                b"Content-Type: application/json\\r\\n"
                + f"Content-Length: {{len(body)}}\\r\\n".encode()
                + b"Connection: close\\r\\n\\r\\n"
                + body
            )
            while not stopping:
                try:
                    connection, _ = server.accept()
                except TimeoutError:
                    continue
                with connection:
                    try:
                        connection.recv(8192)
                        connection.sendall(response)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
            server.close()
            # The runner, not this fake child, owns stale-socket cleanup.
            raise SystemExit(0)
            """
        ).lstrip(),
        encoding="utf-8",
    )

    _write_executable(
        python_wrapper,
        textwrap.dedent(
            f"""
            #!/usr/bin/env bash
            set -euo pipefail
            if [[ "${{1:-}}" == "-c" && "${{4:-}}" == "-m" && "${{5:-}}" == "uvicorn" ]] && \
                grep -Fqx 'signal_launch' {shlex.quote(str(behavior_file))}; then
              printf '%s\n' "$$" >> {shlex.quote(str(launches_file))}
              kill -TERM "$PPID"
            fi
            if [[ "${{1:-}}" == "-m" && "${{2:-}}" == "uvicorn" ]]; then
              shift 2
              exec {shlex.quote(sys.executable)} {shlex.quote(str(fake_uvicorn))} "$@"
            fi
            exec {shlex.quote(sys.executable)} "$@"
            """
        ).lstrip(),
    )

    fake_home = root / "home"
    _write_executable(
        fake_home / ".local/bin/nvidia-smi",
        textwrap.dedent(
            """
            #!/usr/bin/env bash
            set -euo pipefail
            case "$*" in
              *"--query-gpu=index,uuid"*)
                printf '1, GPU-test-device\n'
                ;;
              *"--query-compute-apps=gpu_uuid,pid"*)
                exit 0
                ;;
              *)
                exit 1
                ;;
            esac
            """
        ).lstrip(),
    )

    values = {
        "MONOMER_DFT_PYTHON": str(python_wrapper),
        "MONOMER_DFT_WORKER_UDS": str(socket_path),
        "MONOMER_DFT_JOB_ROOT": str(runtime / "monomer-dft-worker-runs"),
        "MONOMER_DFT_MAX_CONCURRENT_JOBS": "1",
        "MONOMER_DFT_MAX_QUEUED_JOBS": "8",
        "NEXPOLY_DFT_GPU_DEVICE": "1",
        "AIMNET_CACHE_DIR": str(runtime / "aimnet-cache"),
        "WARP_CACHE_PATH": str(runtime / "warp-cache"),
        "UV_CACHE_DIR": str(runtime / "uv-cache"),
        "MONOMER_DFT_START_TIMEOUT_SECONDS": "10",
        "MONOMER_DFT_FATAL_RESTART_MAX_ATTEMPTS": "2",
        "MONOMER_DFT_FATAL_RESTART_BACKOFF_SECONDS": "1",
        "MONOMER_DFT_FATAL_RESTART_MAX_BACKOFF_SECONDS": "1",
        "MONOMER_DFT_FATAL_RESTART_RESET_SECONDS": "60",
        "PYTHONPATH": "",
    }
    env_file = repo / ".env.monomer-dft.dev"
    env_file.write_text(
        "".join(f"{name}={shlex.quote(value)}\n" for name, value in values.items()),
        encoding="utf-8",
    )
    env_file.chmod(0o600)

    env = os.environ.copy()
    env["HOME"] = str(fake_home)
    env.pop("PYTHONPATH", None)
    return SupervisorSandbox(
        root=root,
        repo=repo,
        controller=controller,
        behavior_file=behavior_file,
        attempts_file=attempts_file,
        children_file=children_file,
        launches_file=launches_file,
        pid_file=runtime / "monomer-dft-worker.pid",
        socket_path=socket_path,
        env=env,
    )


def _terminate_fixture_processes(sandbox: SupervisorSandbox) -> None:
    sandbox.run_controller("stop", timeout=12.0)
    candidates = sandbox.child_pids() + sandbox.launch_pids()
    if sandbox.pid_file.exists():
        try:
            candidates.append(int(sandbox.pid_file.read_text(encoding="utf-8").split()[0]))
        except (IndexError, ValueError):
            pass
    for pid in candidates:
        if not _process_is_running(pid):
            continue
        try:
            command_line = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
        except (FileNotFoundError, PermissionError):
            continue
        if str(sandbox.root).encode() not in command_line:
            continue
        os.kill(pid, signal.SIGKILL)


@pytest.fixture
def supervisor_sandbox() -> SupervisorSandbox:
    # Keep the UDS path well below Linux's 107-byte sockaddr_un limit.
    root = Path(tempfile.mkdtemp(prefix="mdft-supervisor-", dir="/tmp"))
    sandbox = _make_sandbox(root)
    try:
        yield sandbox
    finally:
        _terminate_fixture_processes(sandbox)
        shutil.rmtree(root, ignore_errors=True)


def test_exit_70_restarts_once_then_becomes_healthy(
    supervisor_sandbox: SupervisorSandbox,
) -> None:
    sandbox = supervisor_sandbox
    sandbox.behavior_file.write_text("70\nhealthy\n", encoding="utf-8")

    started = sandbox.run_controller("start")

    assert started.returncode == 0, started.stdout + started.stderr
    assert sandbox.attempts() == [1, 2]
    assert sandbox.run_controller("status").returncode == 0
    assert sandbox.socket_path.is_socket()

    stopped = sandbox.run_controller("stop")
    assert stopped.returncode == 0, stopped.stdout + stopped.stderr
    _wait_for(lambda: all(not _process_is_running(pid) for pid in sandbox.child_pids()))
    assert not sandbox.pid_file.exists()
    assert not sandbox.socket_path.exists()


def test_repeated_exit_70_opens_circuit_without_hot_loop(
    supervisor_sandbox: SupervisorSandbox,
) -> None:
    sandbox = supervisor_sandbox
    sandbox.behavior_file.write_text("70\n", encoding="utf-8")

    started_at = time.monotonic()
    started = sandbox.run_controller("start")
    elapsed = time.monotonic() - started_at

    assert started.returncode == 2, started.stdout + started.stderr
    assert sandbox.attempts() == [1, 2, 3]
    assert 2.0 <= elapsed < 12.0
    assert "fatal restart circuit opened" in (started.stdout + started.stderr)
    assert not sandbox.pid_file.exists()
    assert not sandbox.socket_path.exists()
    assert all(not _process_is_running(pid) for pid in sandbox.child_pids())


def test_controller_stop_reaps_supervisor_and_child_group(
    supervisor_sandbox: SupervisorSandbox,
) -> None:
    sandbox = supervisor_sandbox
    sandbox.behavior_file.write_text("healthy\n", encoding="utf-8")
    started = sandbox.run_controller("start")
    assert started.returncode == 0, started.stdout + started.stderr

    supervisor_pid = int(sandbox.pid_file.read_text(encoding="utf-8").split()[0])
    child_pid = sandbox.child_pids()[-1]
    assert _process_is_running(supervisor_pid)
    assert _process_is_running(child_pid)

    stopped = sandbox.run_controller("stop")

    assert stopped.returncode == 0, stopped.stdout + stopped.stderr
    _wait_for(lambda: not _process_is_running(supervisor_pid))
    _wait_for(lambda: not _process_is_running(child_pid))
    assert not sandbox.pid_file.exists()
    assert not sandbox.socket_path.exists()


def test_term_during_child_session_launch_does_not_orphan_worker(
    supervisor_sandbox: SupervisorSandbox,
) -> None:
    sandbox = supervisor_sandbox
    # The fake Python wrapper signals its runner before the child launcher has
    # called setsid(), reproducing the empty/transitioning PGID signal window.
    sandbox.behavior_file.write_text("signal_launch\n", encoding="utf-8")

    started = sandbox.run_controller("start")

    assert started.returncode == 2, started.stdout + started.stderr
    assert sandbox.launch_pids()
    _wait_for(lambda: all(not _process_is_running(pid) for pid in sandbox.launch_pids()))
    assert all(not _process_is_running(pid) for pid in sandbox.child_pids())
    assert not sandbox.pid_file.exists()
    assert not sandbox.socket_path.exists()
