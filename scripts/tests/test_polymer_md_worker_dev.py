"""Private Worker entry-point checks use only synthetic files and local sockets."""
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import socket
import stat
import tempfile
import threading

import pytest

from scripts import polymer_md_worker_dev as worker


def digest_json(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def private_json(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)


@pytest.fixture
def deployment():
    # CI's isolated TMPDIR can itself consume the Unix socket path budget.
    temporary = tempfile.TemporaryDirectory(prefix="pmd-", dir="/tmp")
    try:
        yield deployment_files(Path(temporary.name))
    finally:
        temporary.cleanup()


def deployment_files(home):
    home.chmod(0o700)
    runtime = home / "runtime"
    runtime.mkdir(mode=0o700)
    for directory in (runtime / "jobs", runtime / "socket"):
        directory.mkdir(mode=0o700)
    runner, python = home / "runner-python", home / "worker-python"
    for path in (runner, python):
        path.write_bytes(b"synthetic executable; must never be executed\n")
        path.chmod(0o700)
    source = worker.ROOT / "workers/polymer_md_worker/app"
    from workers.polymer_md_worker.app.runtime import ADAPTER_NAMES, SOURCE_SHA
    files = {"workers/polymer_md_worker/app/" + name: b"# synthetic adapter\n" for name in ADAPTER_NAMES}
    files["workers/polymer_md_worker/app/runtime.py"] = (source / "runtime.py").read_bytes()
    files["workers/polymer_md_worker/app/main.py"] = b"# synthetic HTTP entry point; never imported\n"
    inventory = [{"path": name, "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                 for name, data in sorted(files.items())]
    release_digest = digest_json(inventory)
    release = home / "releases" / release_digest
    release.mkdir(mode=0o700, parents=True)
    for name, data in files.items():
        path = release / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(0o400)
    index = {"version": 1, "source_digest": release_digest, "files": inventory}
    (release / "release.json").write_text(json.dumps(index))
    (release / "release.json").chmod(0o400)
    adapter = hashlib.sha256()
    for name in ADAPTER_NAMES:
        adapter.update(name.encode())
        adapter.update(files["workers/polymer_md_worker/app/" + name])
    sealed_runtime = {"source_sha": SOURCE_SHA, "patch_version": "nexpoly-md-10",
        "adapter_fingerprint": adapter.hexdigest(), "python": str(runner),
        "verified_files": [{"path": str(runner), "sha256": hashlib.sha256(runner.read_bytes()).hexdigest()}],
        "engine_probe": {"passed": True}}
    sealed_runtime["runtime_fingerprint"] = digest_json(sealed_runtime)
    private_json(runtime / "manifest.json", sealed_runtime)
    token = runtime / "authorization.token"
    token.write_text("synthetic_private_authorization_token_1234567890\n")
    token.chmod(0o600)
    values = {"schema_version": 1, "runtime_root": str(runtime), "release_root": str(release),
        "runtime_manifest": str(runtime / "manifest.json"), "runner_python": str(runner), "worker_python": str(python),
        "job_root": str(runtime / "jobs"), "socket_path": str(runtime / "socket/worker.sock"),
        "start_authorization_url": "http://127.0.0.1:39871/internal/polymer-md",
        "start_authorization_token_file": str(token)}
    config = runtime / "worker.json"
    private_json(config, values)
    return config, values, sealed_runtime


def prepared(deployment):
    config = worker.load_configuration(deployment[0])
    digest, runtime = worker.verify_configuration(config)
    return config, digest, runtime


def health(digest, runtime):
    return {"protocol_version": 1, "worker_release_digest": digest, "worker_instance_id": "synthetic-instance",
        "runtime": runtime, "runtime_ready": True, "analysis_ready": True, "scope_ready": True,
        "start_authorization_version": 1, "ready": False, "accepting_jobs": False,
        "draining": True, "recovering": False, "fatal_error": None, "monitor_degraded": False,
        "cpu_count": 16, "memory_bytes": 32 * 1024**3, "gpu_count": 0, "max_running_jobs": 1,
        "queued_jobs": 0, "running_jobs": 0}


def test_frozen_release_and_external_md10_engine_are_verified_without_execution(deployment):
    config, digest, runtime = prepared(deployment)
    assert runtime["ready"] is True
    assert digest == config.path("release_root").name
    assert not config.path("runner_python").is_relative_to(config.path("runtime_root"))
    assert not list(config.path("release_root").rglob("__pycache__"))


def test_configured_unix_socket_must_fit_the_platform_limit(deployment):
    path, values, _ = deployment
    original = path.parent
    longer = original.with_name("runtime-" + "x" * 100)
    original.rename(longer)
    values = {key: str(longer) + value[len(str(original)):] if isinstance(value, str)
              and value.startswith(str(original)) else value for key, value in values.items()}
    path = longer / path.name
    private_json(path, values)
    assert len(os.fsencode(values["socket_path"])) > 107
    with pytest.raises(ValueError, match="Unix socket path is too long"):
        worker.load_configuration(path)


@pytest.mark.parametrize("name", ["__pycache__", "cached.pyc", "cached.pyo"])
def test_uninventoried_bytecode_cannot_override_verified_release_source(deployment, name):
    config = worker.load_configuration(deployment[0])
    path = config.path("release_root") / name
    if name == "__pycache__":
        path.mkdir(mode=0o700)
    else:
        path.write_bytes(b"synthetic compiled cache; never executed")
    with pytest.raises(ValueError, match="compiled Python caches"):
        worker.verify_configuration(config)


@pytest.mark.parametrize("field,value", [("DATABASE_URL", "postgresql://synthetic:secret@invalid/db"),
    ("start_drained", False), ("schema_version", True), ("worker_python", "relative/python"),
    ("job_root", "/tmp/old-jobs"), ("socket_path", "/tmp/shared-worker.sock"),
    ("start_authorization_url", "http://synthetic:secret@127.0.0.1:39871/internal/polymer-md"),
    ("start_authorization_url", "https://example.invalid/internal/polymer-md")])
def test_unexpected_fields_credentials_nonprivate_layout_and_relative_paths_are_rejected(deployment, field, value):
    path, values, _ = deployment
    private_json(path, {**values, field: value})
    with pytest.raises(ValueError):
        worker.load_configuration(path)


def test_duplicate_json_keys_are_rejected(deployment):
    path, _, _ = deployment
    path.write_text('{"schema_version":1,"schema_version":1}')
    with pytest.raises(ValueError, match="duplicate"):
        worker.load_configuration(path)


@pytest.mark.parametrize("target,mode", [("config", 0o644), ("token", 0o644), ("jobs", 0o755), ("socket", 0o755)])
def test_private_configuration_permissions_are_required(deployment, target, mode):
    path, values, _ = deployment
    targets = {"config": path, "token": Path(values["start_authorization_token_file"]),
               "jobs": Path(values["job_root"]), "socket": Path(values["socket_path"]).parent}
    targets[target].chmod(mode)
    with pytest.raises(ValueError, match="private paths"):
        worker.load_configuration(path)


@pytest.mark.parametrize("target", ["config", "token", "runner", "jobs", "parent"])
def test_symlink_files_and_ancestors_are_rejected(deployment, target):
    path, values, _ = deployment
    targets = {"config": path, "token": Path(values["start_authorization_token_file"]),
               "runner": Path(values["runner_python"]), "jobs": Path(values["job_root"]), "parent": path.parent}
    original = targets[target]
    moved = original.with_name(original.name + "-real")
    original.rename(moved)
    original.symlink_to(moved, target_is_directory=moved.is_dir())
    with pytest.raises(ValueError, match="symbolic"):
        worker.load_configuration(path)


def test_legacy_runtime_is_rejected_before_its_files_are_considered(deployment, monkeypatch):
    path, values, _ = deployment
    legacy = path.parent.parent / ".local/share/nexpoly/polymer-md-dev/runtime"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: path.parent.parent))
    private_json(path, {**values, "runtime_root": str(legacy)})
    with pytest.raises(ValueError, match="legacy"):
        worker.load_configuration(path)


@pytest.mark.parametrize("damage", ["release_bytes", "release_writable", "runner_bytes", "md9", "other_python"])
def test_release_and_engine_identity_fail_closed(deployment, damage):
    path, values, runtime = deployment
    if damage.startswith("release"):
        target = Path(values["release_root"]) / "workers/polymer_md_worker/app/main.py"
        target.chmod(0o600)
        if damage == "release_bytes":
            target.write_bytes(b"changed source")
    elif damage == "runner_bytes":
        Path(values["runner_python"]).write_bytes(b"changed external engine")
    else:
        runtime["patch_version" if damage == "md9" else "python"] = "nexpoly-md-9" if damage == "md9" else "/other/python"
        runtime["runtime_fingerprint"] = digest_json({key: value for key, value in runtime.items() if key != "runtime_fingerprint"})
        private_json(Path(values["runtime_manifest"]), runtime)
    config = worker.load_configuration(path)
    if damage == "runner_bytes":
        _, result = worker.verify_configuration(config)
        with pytest.raises(ValueError, match="not ready"):
            worker.run_worker(config, result)
    else:
        with pytest.raises(ValueError):
            worker.verify_configuration(config)


def test_start_environment_is_clean_drained_cpu_only_and_pinned(deployment, monkeypatch):
    config, _, _ = prepared(deployment)
    for key in ("DATABASE_URL", "PGPASSWORD", "AWS_SECRET_ACCESS_KEY", "HTTP_PROXY", "LD_PRELOAD", "PYTHONHOME"):
        monkeypatch.setenv(key, "synthetic-private-value")
    monkeypatch.setenv("POLYMER_MD_START_DRAINED", "false")
    monkeypatch.setenv("POLYMER_MD_RUNNER_PYTHON", "/wrong/python")
    monkeypatch.setenv("PATH", "/wrong/bin")
    environment = worker.clean_environment(config)
    assert "synthetic-private-value" not in environment.values()
    assert environment["PATH"] == "/usr/bin:/bin"
    assert environment["POLYMER_MD_START_DRAINED"] == "true"
    assert environment["POLYMER_MD_RUNNER_PYTHON"] == str(config.path("runner_python"))
    assert environment["CUDA_VISIBLE_DEVICES"] == "" and environment["NVIDIA_VISIBLE_DEVICES"] == "none"
    assert environment["PYTHONPATH"] == str(config.path("release_root"))


class ExecIntercepted(Exception):
    pass


def test_run_worker_binds_private_socket_and_executes_exactly_one_worker(deployment, monkeypatch):
    config, _, runtime = prepared(deployment)
    captured = {}
    monkeypatch.setattr(worker.os, "chdir", lambda path: captured.update(cwd=path))
    old_umask = os.umask(0o077)
    def execute(python, argv, env):
        captured.update(python=python, argv=argv, environment=env)
        assert stat.S_IMODE(config.path("socket_path").stat().st_mode) == 0o600
        assert os.get_inheritable(int(argv[argv.index("--fd") + 1]))
        raise ExecIntercepted
    monkeypatch.setattr(worker.os, "execve", execute)
    try:
        with pytest.raises(ExecIntercepted):
            worker.run_worker(config, runtime)
    finally:
        os.umask(old_umask)
    assert captured["argv"][-3:] == ["--workers", "1", "--no-access-log"]
    assert captured["python"] == str(config.path("worker_python"))
    assert captured["cwd"] == config.path("release_root")
    assert not config.path("socket_path").exists()


def test_launch_lock_fences_a_second_launcher_without_touching_its_socket(deployment):
    config, _, runtime = prepared(deployment)
    path = config.path("runtime_root") / ".worker-launch.lock"
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            worker.run_worker(config, runtime)
        assert not config.path("socket_path").exists()
    finally:
        os.close(descriptor)


def test_owned_stale_socket_is_replaced_but_cleaned_if_exec_fails(deployment, monkeypatch):
    config, _, runtime = prepared(deployment)
    path = config.path("socket_path")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stale:
        stale.bind(str(path))
        path.chmod(0o600)
    monkeypatch.setattr(worker.os, "chdir", lambda _path: None)
    def fail(*_args):
        assert stat.S_ISSOCK(path.lstat().st_mode)
        raise ExecIntercepted
    monkeypatch.setattr(worker.os, "execve", fail)
    old_umask = os.umask(0o077)
    try:
        with pytest.raises(ExecIntercepted):
            worker.run_worker(config, runtime)
    finally:
        os.umask(old_umask)
    assert not path.exists()


@pytest.mark.parametrize("occupied", ["regular", "live", "symlink"])
def test_start_never_overwrites_an_occupied_path_or_live_worker(deployment, occupied):
    config, _, runtime = prepared(deployment)
    path = config.path("socket_path")
    listener = None
    if occupied == "regular":
        path.write_text("preserve")
    elif occupied == "symlink":
        path.symlink_to(config.path("start_authorization_token_file"))
    else:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(path))
        path.chmod(0o600)
        listener.listen(1)
    try:
        with pytest.raises(ValueError):
            worker.run_worker(config, runtime)
        assert path.exists()
    finally:
        if listener:
            listener.close()


def fake_control(monkeypatch, config, responses):
    calls = []
    class Connection:
        def __init__(self, *_args, **_kwargs):
            self.sock = None
        def close(self):
            self.sock.close()
    class Socket:
        def settimeout(self, _timeout): pass
        def connect(self, path): calls.append(("connect", path))
        def close(self): pass
    def request(connection, method, path):
        assert connection.auto_open == 0
        calls.append((method, path))
        return responses.pop(0)
    monkeypatch.setattr(worker, "_socket_info", lambda _path: None)
    monkeypatch.setattr(worker.http.client, "HTTPConnection", Connection)
    monkeypatch.setattr(worker.socket, "socket", lambda *_args: Socket())
    monkeypatch.setattr(worker, "_request", request)
    return calls


def test_check_accepts_healthy_drained_worker_and_prints_only_safe_summary(deployment, monkeypatch, capsys):
    config, digest, runtime = prepared(deployment)
    response = health(digest, runtime)
    response["token"] = "synthetic-private-value"
    response["runtime"]["verified_files"][0]["private"] = "synthetic-private-value"
    calls = fake_control(monkeypatch, config, [response])
    assert worker.control(config, "check", digest, runtime) == 0
    output = capsys.readouterr().out
    assert "synthetic-private-value" not in output and "verified_files" not in output
    assert json.loads(output)["draining"] is True
    assert calls[-1] == ("GET", "/health")


@pytest.mark.parametrize("field,value", [("worker_release_digest", "different"), ("worker_instance_id", ""),
    ("runtime", {"runtime_fingerprint": "different"})])
def test_control_rejects_an_unexpected_instance_before_mutation(deployment, monkeypatch, field, value):
    config, digest, runtime = prepared(deployment)
    response = {**health(digest, runtime), field: value}
    calls = fake_control(monkeypatch, config, [response])
    with pytest.raises(ValueError, match="identity"):
        worker.control(config, "resume", digest, runtime)
    assert not any(method == "POST" for method, _ in calls)


@pytest.mark.parametrize("field,value", [("scope_ready", False), ("analysis_ready", False), ("runtime_ready", False),
    ("recovering", True), ("fatal_error", "cleanup_unconfirmed"), ("monitor_degraded", True),
    ("cpu_count", 8), ("memory_bytes", 16 * 1024**3), ("gpu_count", 1)])
def test_resume_requires_fixed_resources_and_all_readiness_guards(deployment, monkeypatch, field, value):
    config, digest, runtime = prepared(deployment)
    calls = fake_control(monkeypatch, config, [{**health(digest, runtime), field: value}])
    with pytest.raises(ValueError, match="before resume"):
        worker.control(config, "resume", digest, runtime)
    assert not any(method == "POST" for method, _ in calls)


def test_resume_and_drain_use_same_connection_and_confirm_instance(deployment, monkeypatch):
    config, digest, runtime = prepared(deployment)
    initial = health(digest, runtime)
    resumed = {**initial, "ready": True, "accepting_jobs": True, "draining": False}
    calls = fake_control(monkeypatch, config, [initial, resumed])
    assert worker.control(config, "resume", digest, runtime) == 0
    assert calls[-2:] == [("GET", "/health"), ("POST", "/resume")]
    changed = {**initial, "worker_instance_id": "different-instance"}
    fake_control(monkeypatch, config, [resumed, changed])
    with pytest.raises(ValueError, match="identity"):
        worker.control(config, "drain", digest, runtime)


def test_closed_health_connection_is_not_reopened_for_resume(deployment):
    config, digest, runtime = prepared(deployment)
    path = config.path("socket_path")
    requests = []
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(path))
        path.chmod(0o600)
        listener.listen(1)
        listener.settimeout(1)
        def serve():
            with listener.accept()[0] as client:
                data = b""
                while b"\r\n\r\n" not in data:
                    data += client.recv(4096)
                requests.append(data.split(b"\r\n")[0])
                body = json.dumps(health(digest, runtime)).encode()
                client.sendall(b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: "
                               + str(len(body)).encode() + b"\r\n\r\n" + body)
        thread = threading.Thread(target=serve)
        thread.start()
        try:
            with pytest.raises(http.client.NotConnected):
                worker.control(config, "resume", digest, runtime)
        finally:
            thread.join(timeout=2)
        assert not thread.is_alive()
        assert requests == [b"GET /health HTTP/1.1"]
        # A reconnect would remain in this listener's accept queue.
        with pytest.raises(socket.timeout):
            listener.accept()


def test_drain_remains_available_when_runtime_is_not_ready(deployment, monkeypatch):
    config, digest, runtime = prepared(deployment)
    runtime = {**runtime, "ready": False}
    response = {**health(digest, runtime), "runtime_ready": False}
    calls = fake_control(monkeypatch, config, [response, response])
    assert worker.control(config, "drain", digest, runtime) == 0
    assert calls[-1] == ("POST", "/drain")


def test_cli_requires_explicit_config_and_has_no_provision_or_service_actions():
    with pytest.raises(SystemExit):
        worker.main(["run-worker"])
    with pytest.raises(SystemExit):
        worker.main(["init", "--config", "/unused/config.json"])
