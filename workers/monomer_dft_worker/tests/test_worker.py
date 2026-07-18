from __future__ import annotations

import os
import shutil
import signal
import stat
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from workers.monomer_dft_worker.app.artifacts import (
    atomic_write_bytes,
    describe_artifact,
)
from workers.monomer_dft_worker.app.config import REPO_ROOT, WorkerSettings
from workers.monomer_dft_worker.app.engine import EngineExecution
from workers.monomer_dft_worker.app.main import create_app
from workers.monomer_dft_worker.app.runtime import RuntimeProbe


def _copy_executable(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    destination.chmod(0o700)


def _write_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o700)


def _ctl_fixture(
    tmp_path: Path,
    *,
    timeout: str = "1",
    healthy: bool = False,
) -> tuple[Path, Path, Path]:
    repo = tmp_path / "repo"
    controller = repo / "scripts/monomer_dft_worker_ctl.sh"
    runner = repo / "workers/monomer_dft_worker/run_host_worker.sh"
    preflight = repo / "scripts/preflight_monomer_dft_env.py"
    python = repo / ".runtime/venvs/monomer-dft-worker/bin/python"
    # Keep the temporary AF_UNIX path below Linux's 107-byte limit even when
    # pytest's own tmp_path prefix is long.
    uds = repo / ".runtime/s/w.sock"
    spawned_pid = repo / "spawned.pid"

    _copy_executable(REPO_ROOT / "scripts/monomer_dft_worker_ctl.sh", controller)
    if healthy:
        runner_content = """#!/usr/bin/python3
import os
import pathlib
import socket

path = os.environ["MONOMER_DFT_WORKER_UDS"]
pathlib.Path(os.environ["MONOMER_DFT_TEST_PID_FILE"]).write_text(str(os.getpid()))
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(path)
os.chmod(path, 0o777)
server.listen(4)
while True:
    connection, _ = server.accept()
    with connection:
        connection.recv(4096)
        body = b'{"status":"ok"}'
        response = (
            b"HTTP/1.1 200 OK\\r\\n"
            b"Content-Type: application/json\\r\\n"
            + f"Content-Length: {len(body)}\\r\\n".encode()
            + b"Connection: close\\r\\n\\r\\n"
            + body
        )
        connection.sendall(response)
"""
    else:
        runner_content = """#!/usr/bin/python3
import os
import pathlib
import signal

pathlib.Path(os.environ["MONOMER_DFT_TEST_PID_FILE"]).write_text(str(os.getpid()))
while True:
    signal.pause()
"""
    _write_executable(runner, runner_content)
    _write_executable(preflight, "#!/usr/bin/env python3\n")
    _write_executable(python, '#!/usr/bin/env bash\nexec /usr/bin/python3 "$@"\n')
    uds.parent.mkdir(parents=True)

    env_file = repo / ".env.monomer-dft.dev"
    env_file.write_text(
        "\n".join(
            (
                f"MONOMER_DFT_PYTHON={python}",
                f"MONOMER_DFT_WORKER_UDS={uds}",
                "MONOMER_DFT_MAX_CONCURRENT_JOBS=1",
                "NEXPOLY_DFT_GPU_DEVICE=1",
                "PYTHONPATH=",
                f"MONOMER_DFT_START_TIMEOUT_SECONDS={timeout}",
                f"MONOMER_DFT_TEST_PID_FILE={spawned_pid}",
                "APP_POSTGRES_DSN=postgresql://must-not-reach-worker",
                "NEXPOLY_DFT_POSTGRES_PASSWORD=must-not-reach-worker",
                "",
            )
        ),
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    return controller, repo, spawned_pid


def _settings(tmp_path: Path) -> WorkerSettings:
    return WorkerSettings(
        python=tmp_path / "venv/bin/python",
        uds=tmp_path / "socket/worker.sock",
        job_root=tmp_path / "runs",
        max_concurrent_jobs=1,
        physical_gpu="3",
        logical_device="cuda:0",
        aimnet_cache_dir=tmp_path / "models",
        warp_cache_path=tmp_path / "warp",
        model_name="aimnet2",
        worker_version="test",
    )


class FakeRuntime:
    def __init__(self, *, load_error: Exception | None = None):
        self.load_error = load_error
        self.load_calls = 0
        self.close_calls = 0
        self.loaded = False

    def load(self) -> None:
        self.load_calls += 1
        if self.load_error is not None:
            raise self.load_error
        self.loaded = True

    def close(self) -> None:
        self.close_calls += 1
        self.loaded = False

    def probe(self) -> RuntimeProbe:
        return RuntimeProbe(
            ready=self.loaded,
            model_loaded=self.loaded,
            model_name="aimnet2",
            model_file="/isolated/aimnet2_wb97m_d3_0.pt",
            model_sha256="abc",
            aimnet_origin="/isolated/site-packages/aimnet/__init__.py",
            torch_version="2.9.1+cu128",
            cuda_runtime="12.8",
            gpu_name="Fake RTX 4090",
            visible_gpu_count=1 if self.loaded else 0,
            logical_device="cuda:0",
            loaded_at_unix=1.0 if self.loaded else None,
            error=None,
        )


class ImmediateEngine:
    def execute(
        self,
        request,
        output_directory,
        *,
        admitted,
        progress,
        cancelled,
        provenance,
        queue_wait_ms,
    ) -> EngineExecution:
        assert not cancelled()
        admitted()
        progress("single_point", 50, None)
        output_directory.mkdir(parents=True, exist_ok=True)
        path = output_directory / "scientific_result.json"
        atomic_write_bytes(path, b'{"schema_version":1}')
        descriptor = describe_artifact(
            artifact_id="scientific_result",
            path=path,
            media_type="application/json",
        )
        timings = {"queue_wait_ms": queue_wait_ms, "total_ms": 1.0}
        return EngineExecution(
            result={"schema_version": 1, "timings": timings, "provenance": provenance},
            timings=timings,
            artifacts=((descriptor, path),),
        )


def test_lifespan_preloads_model_and_exposes_worker_protocol(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    app = create_app(_settings(tmp_path), runtime)  # type: ignore[arg-type]

    with TestClient(app) as client:
        assert runtime.load_calls == 1
        response = client.get("/health")
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "ok"
        assert payload["runtime_ready"] is True
        assert payload["runtime"]["model_name"] == "aimnet2"
        assert payload["runtime"]["logical_device"] == "cuda:0"
        assert payload["runtime"]["visible_gpu_count"] == 1
        assert payload["max_concurrent_jobs"] == 1
        invalid = client.post("/jobs", json={})
        assert invalid.status_code == 422
        assert invalid.json()["error"]["code"] == "invalid_request"
        assert client.get("/capabilities").status_code == 200
        assert client.get("/jobs?state=active").status_code == 200
        assert client.get("/docs").status_code == 404

    assert runtime.close_calls == 1


def test_lifespan_fails_closed_when_model_preload_fails(tmp_path: Path) -> None:
    runtime = FakeRuntime(load_error=FileNotFoundError("model missing"))
    app = create_app(_settings(tmp_path), runtime)  # type: ignore[arg-type]

    with pytest.raises(FileNotFoundError, match="model missing"):
        with TestClient(app):
            pass

    assert runtime.load_calls == 1
    assert runtime.close_calls == 1


def test_full_job_http_protocol_and_manifest_artifact_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = FakeRuntime()
    app = create_app(
        _settings(tmp_path),
        runtime,  # type: ignore[arg-type]
        ImmediateEngine(),  # type: ignore[arg-type]
    )
    body = {
        "schema_version": 2,
        "enqueue_sequence": 1,
        "job_id": "api-job",
        "attempt_token": "a" * 32,
        "input": {"smiles": "O", "net_charge": 0, "multiplicity": 1},
        "calculation_type": "single_point",
        "model": "aimnet2",
        "conformer": {"seed": 1, "max_iterations": 20},
        "single_point": {"properties": ["energy"]},
    }

    with TestClient(app) as client:
        invalid_type = {**body, "input": {**body["input"], "net_charge": "0"}}
        invalid_response = client.post("/jobs", json=invalid_type)
        assert invalid_response.status_code == 422
        assert invalid_response.json()["error"]["code"] == "invalid_request"
        submitted = client.post("/jobs", json=body)
        assert submitted.status_code == 202
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            snapshot = client.get("/jobs/api-job").json()
            if snapshot["status"] == "completed":
                break
            time.sleep(0.01)
        assert snapshot["status"] == "completed"
        assert snapshot["artifact_state"] == "available"

        def fail_if_revalidated(_payload) -> None:
            raise AssertionError("HTTP idempotent replay must precede RDKit validation")

        monkeypatch.setattr(
            app.state.manager,
            "validate_submission",
            fail_if_revalidated,
        )
        replayed = client.post("/jobs", json=body)
        assert replayed.status_code == 200
        assert replayed.json()["artifact_state"] == "available"
        assert client.get("/jobs?state=active").json()["total"] == 0

        artifact = client.get("/jobs/api-job/artifacts/scientific_result")
        assert artifact.status_code == 200
        assert artifact.content == b'{"schema_version":1}'
        assert artifact.headers["etag"]
        bundle = client.get("/jobs/api-job/bundle")
        assert bundle.status_code == 200
        assert bundle.headers["content-type"] == "application/zip"

        assert client.post("/drain").json()["accepting_jobs"] is False
        assert client.post("/resume").json()["accepting_jobs"] is True
        deleted = client.delete("/jobs/api-job/artifacts").json()
        assert deleted["deleted_artifacts"] == 1
        deleted_snapshot = client.get("/jobs/api-job").json()
        assert deleted_snapshot["artifact_state"] == "deleted"
        assert deleted_snapshot["artifacts"] == []
        missing = client.get("/jobs/api-job/artifacts/scientific_result")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "artifact_not_found"


def test_http_fenced_cancel_closes_unknown_dispatch_claim(
    tmp_path: Path,
) -> None:
    runtime = FakeRuntime()
    app = create_app(
        _settings(tmp_path),
        runtime,  # type: ignore[arg-type]
        ImmediateEngine(),  # type: ignore[arg-type]
    )
    body = {
        "schema_version": 2,
        "enqueue_sequence": 73,
        "job_id": "cancel-before-submit",
        "attempt_token": "c" * 32,
        "input": {"smiles": "O", "net_charge": 0, "multiplicity": 1},
        "calculation_type": "single_point",
        "model": "aimnet2",
        "conformer": {"seed": 1, "max_iterations": 20},
        "single_point": {"properties": ["energy"]},
    }

    with TestClient(app) as client:
        legacy_unknown = client.post("/jobs/cancel-before-submit/cancel")
        assert legacy_unknown.status_code == 404
        assert legacy_unknown.json()["error"]["code"] == "job_not_found"
        assert client.get("/jobs/cancel-before-submit").status_code == 404
        assert client.delete("/jobs/cancel-before-submit/artifacts").status_code == 404

        fenced = client.post(
            "/jobs/cancel-before-submit/cancel",
            json=body,
        )
        assert fenced.status_code == 200
        cancelled = fenced.json()
        assert cancelled["status"] == "cancelled"
        assert cancelled["request_sha256"]
        assert cancelled["enqueue_sequence"] == 73
        assert cancelled["request"]["job_id"] == "cancel-before-submit"
        assert cancelled["artifact_state"] == "none"

        repeated = client.post(
            "/jobs/cancel-before-submit/cancel",
            json={**body, "request_sha256": cancelled["request_sha256"]},
        )
        assert repeated.status_code == 200
        assert repeated.json() == cancelled

        late_submit = client.post("/jobs", json=body)
        assert late_submit.status_code == 200
        assert late_submit.json()["status"] == "cancelled"
        assert client.get("/jobs?state=active").json()["total"] == 0

        different_payload = {
            **body,
            "input": {"smiles": "N", "net_charge": 0, "multiplicity": 1},
        }
        conflict = client.post(
            "/jobs/cancel-before-submit/cancel",
            json=different_payload,
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "job_conflict"

        path_conflict = client.post("/jobs/other-job/cancel", json=body)
        assert path_conflict.status_code == 409
        assert path_conflict.json()["error"]["code"] == "job_conflict"


def test_shell_entrypoints_have_valid_syntax() -> None:
    scripts = (
        REPO_ROOT / "workers/monomer_dft_worker/run_host_worker.sh",
        REPO_ROOT / "scripts/monomer_dft_worker_ctl.sh",
    )

    for script in scripts:
        completed = subprocess.run(
            ["bash", "-n", str(script)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr


def test_runner_uses_repo_root_and_fully_qualified_worker_module() -> None:
    runner = (
        REPO_ROOT / "workers/monomer_dft_worker/run_host_worker.sh"
    ).read_text(encoding="utf-8")
    controller = (REPO_ROOT / "scripts/monomer_dft_worker_ctl.sh").read_text(
        encoding="utf-8"
    )

    module = "workers.monomer_dft_worker.app.main:app"
    assert 'cd "$REPO_ROOT"' in runner
    assert f"-m uvicorn {module}" in runner
    assert module in controller
    assert "-m uvicorn app.main:app" not in runner


def test_runner_rejects_original_aimnet_pythonpath(tmp_path: Path) -> None:
    runner = REPO_ROOT / "workers/monomer_dft_worker/run_host_worker.sh"
    completed = subprocess.run(
        [str(runner)],
        env={
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": "/data/lzq/gith/aimnetcentral",
            "MONOMER_DFT_PYTHON": str(tmp_path / "missing-python"),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "PYTHONPATH must not reference" in completed.stderr


def test_pid_controller_requires_process_identity_checks() -> None:
    controller = (REPO_ROOT / "scripts/monomer_dft_worker_ctl.sh").read_text()

    assert "/proc/$pid/stat" in controller
    assert "MONOMER_DFT_WORKER_INSTANCE=$REPO_ROOT" in controller
    assert 'is_managed_process "$MANAGED_PID" "$MANAGED_START_TICKS"' in controller
    assert "refusing to signal it" in controller
    assert '"$MONOMER_DFT_PYTHON" "$PREFLIGHT"' in controller
    assert 'mktemp "$RUNTIME_ROOT/.monomer-dft-worker.pid.tmp.XXXXXX"' in controller
    assert 'mv -T -- "$SPAWN_PID_TMP" "$PID_FILE"' in controller
    assert "trap 'cleanup_startup' ERR" in controller
    assert "trap 'cleanup_startup' EXIT" in controller
    assert "CUDA_DEVICE_ORDER=PCI_BUS_ID" in controller
    assert "env -i" in controller


def test_controller_rejects_pre_exec_launcher_command_identity(tmp_path: Path) -> None:
    controller, repo, _ = _ctl_fixture(tmp_path)
    runner = repo / "workers/monomer_dft_worker/run_host_worker.sh"
    launcher = subprocess.Popen(
        [
            "/usr/bin/python3",
            "-c",
            "import time; time.sleep(30)",
            str(runner),
        ],
        env={
            "PATH": "/usr/bin:/bin",
            "MONOMER_DFT_WORKER_INSTANCE": str(repo),
        },
    )
    try:
        inspected = subprocess.run(
            [
                "/usr/bin/bash",
                "-c",
                'source "$1"; process_has_worker_command "$2"',
                "bash",
                str(controller),
                str(launcher.pid),
            ],
            env={"PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        assert inspected.returncode != 0
    finally:
        launcher.terminate()
        launcher.wait(timeout=5)


def test_runner_rejects_intermediate_socket_parent_symlink(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    runner = repo / "workers/monomer_dft_worker/run_host_worker.sh"
    python = repo / ".runtime/venvs/monomer-dft-worker/bin/python"
    socket_parent = repo / ".runtime/s"
    external = tmp_path / "external"
    external.mkdir()
    _copy_executable(
        REPO_ROOT / "workers/monomer_dft_worker/run_host_worker.sh", runner
    )
    _write_executable(python, "#!/usr/bin/env bash\nexit 99\n")
    socket_parent.symlink_to(external, target_is_directory=True)

    completed = subprocess.run(
        [str(runner)],
        env={
            "PATH": "/usr/bin:/bin",
            "MONOMER_DFT_PYTHON": str(python),
            "MONOMER_DFT_WORKER_UDS": str(socket_parent / "worker.sock"),
            "MONOMER_DFT_MAX_CONCURRENT_JOBS": "1",
            "NEXPOLY_DFT_GPU_DEVICE": "1",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "symlink component" in completed.stderr
    assert list(external.iterdir()) == []


def test_controller_rejects_symlinked_lock_before_opening_it(tmp_path: Path) -> None:
    controller, repo, _ = _ctl_fixture(tmp_path)
    external_lock = tmp_path / "external.lock"
    external_lock.write_text("must stay unchanged", encoding="utf-8")
    lock = repo / ".runtime/monomer-dft-worker.ctl.lock"
    lock.symlink_to(external_lock)

    completed = subprocess.run(
        [str(controller), "status"],
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "control lock file must not be a symlink" in completed.stderr
    assert external_lock.read_text(encoding="utf-8") == "must stay unchanged"


def test_controller_rejects_symlinked_uds_parent(tmp_path: Path) -> None:
    controller, repo, _ = _ctl_fixture(tmp_path)
    socket_parent = repo / ".runtime/s"
    socket_parent.rmdir()
    external = tmp_path / "external-socket-dir"
    external.mkdir()
    socket_parent.symlink_to(external, target_is_directory=True)

    completed = subprocess.run(
        [str(controller), "health"],
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "symlink component" in completed.stderr
    assert list(external.iterdir()) == []


def test_invalid_start_timeout_is_rejected_before_spawn(tmp_path: Path) -> None:
    controller, _, spawned_pid = _ctl_fixture(tmp_path, timeout="invalid")

    completed = subprocess.run(
        [str(controller), "start"],
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )

    assert completed.returncode == 2
    assert (
        "MONOMER_DFT_START_TIMEOUT_SECONDS must be a positive integer"
        in completed.stderr
    )
    assert not spawned_pid.exists()


def test_start_timeout_trap_terminates_and_reaps_spawned_worker(tmp_path: Path) -> None:
    controller, repo, spawned_pid = _ctl_fixture(tmp_path, timeout="1")

    completed = subprocess.run(
        [str(controller), "start"],
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert completed.returncode == 2
    assert "did not become healthy" in completed.stderr
    pid = int(spawned_pid.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert not (repo / ".runtime/monomer-dft-worker.pid").exists()
    assert list((repo / ".runtime").glob(".monomer-dft-worker.pid.tmp.*")) == []


def test_successful_worker_survives_controller_exit_in_new_session_and_socket_is_private(
    tmp_path: Path,
) -> None:
    controller, repo, spawned_pid = _ctl_fixture(tmp_path, timeout="5", healthy=True)
    worker_pid: int | None = None
    stopped: subprocess.CompletedProcess[str] | None = None
    try:
        completed = subprocess.run(
            [str(controller), "start"],
            env={"PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )

        assert completed.returncode == 0, (completed.stdout, completed.stderr)
        pid_fields = (repo / ".runtime/monomer-dft-worker.pid").read_text().split()
        worker_pid = int(pid_fields[0])
        assert int(spawned_pid.read_text(encoding="utf-8")) == worker_pid
        assert os.getsid(worker_pid) == worker_pid
        os.kill(worker_pid, 0)
        worker_environment = (Path("/proc") / str(worker_pid) / "environ").read_bytes()
        assert b"APP_POSTGRES_DSN=" not in worker_environment
        assert b"NEXPOLY_DFT_POSTGRES_PASSWORD=" not in worker_environment

        socket_path = repo / ".runtime/s/w.sock"
        assert stat.S_IMODE(socket_path.stat().st_mode) == 0o600
        os.kill(worker_pid, signal.SIGHUP)
        time.sleep(0.1)
        os.kill(worker_pid, 0)
        assert stat.S_IMODE(socket_path.stat().st_mode) == 0o600
        status_result = subprocess.run(
            [str(controller), "status"],
            env={"PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        assert status_result.returncode == 0, (
            status_result.stdout,
            status_result.stderr,
        )
    finally:
        if worker_pid is not None:
            stopped = subprocess.run(
                [str(controller), "stop"],
                env={"PATH": "/usr/bin:/bin"},
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )

    assert worker_pid is not None
    assert stopped is not None
    assert stopped.returncode == 0, (stopped.stdout, stopped.stderr)
    deadline = time.monotonic() + 2
    process_state: str | None = None
    while time.monotonic() < deadline:
        stat_path = Path(f"/proc/{worker_pid}/stat")
        if not stat_path.exists():
            process_state = None
            break
        process_state = stat_path.read_text(encoding="utf-8").split()[2]
        if process_state == "Z":
            break
        time.sleep(0.02)
    assert process_state in (None, "Z")
    assert not (repo / ".runtime/monomer-dft-worker.pid").exists()
    assert not (repo / ".runtime/s/w.sock").exists()


def test_signal_window_recovers_start_ticks_and_reaps_spawned_worker(
    tmp_path: Path,
) -> None:
    controller, repo, spawned_pid = _ctl_fixture(tmp_path, timeout="30")
    fake_bin = tmp_path / "fake-bin"
    delayed_once = tmp_path / "awk-delayed-once"
    _write_executable(
        fake_bin / "awk",
        """#!/usr/bin/env bash
set -euo pipefail
if [[ ! -e "$AWK_DELAY_ONCE_FILE" ]]; then
  /usr/bin/touch "$AWK_DELAY_ONCE_FILE"
  /usr/bin/sleep 2
fi
exec /usr/bin/awk "$@"
""",
    )
    process = subprocess.Popen(
        [str(controller), "start"],
        env={
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "AWK_DELAY_ONCE_FILE": str(delayed_once),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 5
    while not spawned_pid.exists() and time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            pytest.fail(
                f"controller exited before the interrupt window: {stdout}\n{stderr}"
            )
        time.sleep(0.01)
    assert spawned_pid.exists()
    worker_pid = int(spawned_pid.read_text(encoding="utf-8"))

    process.terminate()
    stdout, stderr = process.communicate(timeout=10)

    assert process.returncode == 143, (stdout, stderr)
    assert "recovered startup identity" in stdout
    with pytest.raises(ProcessLookupError):
        os.kill(worker_pid, 0)
    assert not (repo / ".runtime/monomer-dft-worker.pid").exists()
    assert list((repo / ".runtime").glob(".monomer-dft-worker.pid.tmp.*")) == []
