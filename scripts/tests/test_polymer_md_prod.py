"""Production launch contracts use synthetic files, Git objects and no engines."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
from types import SimpleNamespace

import pytest

from scripts import polymer_md_analysis_dev as analysis_dev
from scripts import polymer_md_analysis_prod as analysis
from scripts import polymer_md_analysis_container as container
from scripts import polymer_md_prod_contract as contract
from scripts import polymer_md_worker_dev as worker
from scripts import polymer_md_worker_prod as production
from scripts.tests.test_polymer_md_worker_dev import deployment_files, digest_json, private_json


def commit(source):
    environment = {**os.environ, "GIT_AUTHOR_NAME": "Synthetic test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Synthetic test", "GIT_COMMITTER_EMAIL": "test@example.invalid"}
    subprocess.run(["git", "-C", str(source), "add", "."], check=True, capture_output=True, env=environment)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", "Synthetic frozen source"], check=True, capture_output=True, env=environment)
    return subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()


@pytest.fixture
def deployment(monkeypatch):
    with tempfile.TemporaryDirectory(prefix="pmdp-", dir="/tmp") as temporary:
        home = Path(temporary)
        path, values, runtime = deployment_files(home)
        release = Path(values["release_root"])
        files = json.loads((release / "release.json").read_text())["files"]
        for name in ("scripts/polymer_md_worker_dev.py", "scripts/polymer_md_worker_prod.py",
                     "scripts/polymer_md_prod_contract.py", "scripts/polymer_md_analysis_dev.py"):
            target = release / name
            target.parent.mkdir(mode=0o700, exist_ok=True)
            data = b"# synthetic source; never executed\n"
            target.write_bytes(data)
            target.chmod(0o400)
            files.append({"path": name, "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        files.sort(key=lambda item: item["path"])
        digest = digest_json(files)
        index = release / "release.json"
        index.chmod(0o600)
        index.write_text(json.dumps({"version": 1, "source_digest": digest, "files": files}))
        index.chmod(0o400)
        moved_release = release.with_name(digest)
        release.rename(moved_release)
        source = home / "source"
        source.mkdir(mode=0o700)
        for item in files:
            destination = source / item["path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(moved_release / item["path"], destination)
        subprocess.run(["git", "init", "-q", str(source)], check=True, capture_output=True)
        sha = commit(source)
        base = home / "production"
        root = base / "state" / "p9000-20261102" / "pmd"
        root.parent.mkdir(parents=True, mode=0o700)
        source_base = home / "private-releases"
        source_parent = source_base / "p9000-20261102"
        source_parent.mkdir(mode=0o700, parents=True)
        moved_source = source_parent / sha
        source.rename(moved_source)
        source = moved_source
        original = path.parent
        original.rename(root)
        for name in ("tmp", "home"):
            (root / name).mkdir(mode=0o700)
        values = {key: str(root) + value[len(str(original)):] if isinstance(value, str)
                  and value.startswith(str(original)) else value for key, value in values.items()}
        sockets = home / "run" / "nexpoly-p9000-pmd"
        sockets.mkdir(mode=0o700, parents=True)
        values.update(schema_version=2, environment="prod", operation_id="p9000-20261102", source_sha=sha,
                      source_root=str(source), release_root=str(moved_release), socket_root=str(sockets), socket_path=str(sockets / "worker.sock"))
        path = root / "worker.json"
        private_json(path, values)
        monkeypatch.setattr(contract, "PRODUCTION_RUNTIME_ROOT", base)
        monkeypatch.setattr(contract, "PRODUCTION_SOURCE_ROOT", source_base)
        monkeypatch.setattr(contract, "PRODUCTION_SOCKET_PARENT", sockets.parent)
        yield path, values, runtime


def test_production_worker_binds_operation_source_frozen_release_and_read_only_engine(deployment, monkeypatch, capsys):
    path, values, _ = deployment
    config = worker.load_configuration(path, _production=True)
    digest, runtime = worker.verify_configuration(config)
    assert runtime["ready"] and digest == Path(values["release_root"]).name
    assert len(os.fsencode(config.path("socket_path"))) <= 107
    monkeypatch.setenv("APP_POSTGRES_DSN", "synthetic credential must not escape")
    environment = worker.clean_environment(config)
    assert environment["POLYMER_MD_ENVIRONMENT"] == "prod"
    assert environment["NEXPOLY_OPERATION_ID"] == values["operation_id"]
    assert environment["POLYMER_MD_SOURCE_SHA"] == values["source_sha"]
    assert environment["USAGE_ENTITLEMENTS_ENABLED"] == "false"
    assert environment["POLYMER_MD_RETENTION_ENABLED"] == "false"
    assert environment["POLYMER_MD_START_DRAINED"] == "true"
    assert environment["CUDA_VISIBLE_DEVICES"] == "" and environment["NVIDIA_VISIBLE_DEVICES"] == "none"
    assert "APP_POSTGRES_DSN" not in environment
    assert production.main(["verify", "--config", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["scientific_acceptance"] == "not_evaluated"
    with pytest.raises(ValueError):
        worker.load_configuration(path)


@pytest.mark.parametrize("field,value", [("source_sha", "abc123"), ("environment", "dev"),
    ("operation_id", "other-operation"), ("operation_id", True), ("source_sha", True),
    ("socket_root", "/tmp/shared"), ("job_root", "/tmp/old-jobs")])
def test_production_configuration_cannot_select_a_partial_or_unrelated_deployment(deployment, field, value):
    path, values, _ = deployment
    private_json(path, {**values, field: value})
    with pytest.raises(ValueError):
        worker.load_configuration(path, _production=True)


def test_clean_committed_source_must_match_every_frozen_worker_byte(deployment):
    path, values, _ = deployment
    source = Path(values["source_root"])
    (source / "workers/polymer_md_worker/app/main.py").write_text("# changed committed source\n")
    values["source_sha"] = commit(source)
    moved_source = source.with_name(values["source_sha"])
    source.rename(moved_source)
    values["source_root"] = str(moved_source)
    private_json(path, values)
    with pytest.raises(ValueError, match="differs from the configured source"):
        worker.verify_configuration(worker.load_configuration(path, _production=True))


@pytest.mark.parametrize("layout", ["other-operation", "different-sha", "old-runtime-layout"])
def test_production_source_requires_its_exact_operation_and_sha_outside_runtime(deployment, layout):
    path, values, _ = deployment
    source = Path(values["source_root"])
    if layout == "other-operation":
        invalid = source.parent.parent / "other-operation" / values["source_sha"]
    elif layout == "different-sha":
        invalid = source.with_name("a" * 40)
    else:
        invalid = Path(values["runtime_root"]).parent / "releases" / values["source_sha"]
    private_json(path, {**values, "source_root": str(invalid)})
    with pytest.raises(ValueError, match="private full-SHA"):
        contract.validate_production_binding({**values, "source_root": str(invalid)})


def test_development_entry_points_still_reject_production(monkeypatch):
    monkeypatch.setenv("POLYMER_MD_ENVIRONMENT", "prod")
    with pytest.raises(ValueError, match="development entry point"):
        contract.reject_production_from_dev()
    with pytest.raises(ValueError, match="development entry point"):
        analysis_dev.main()


def test_production_worker_namespace_preserves_socket_and_isolates_writes(deployment):
    if not Path(contract.BWRAP).is_file():
        pytest.skip("bubblewrap is not installed on this test host")
    _, values, _ = deployment
    with socket.socket(socket.AF_UNIX) as listener:
        code = ("import os,stat,json; print(json.dumps({'source_ro':bool(os.statvfs(" + repr(values["source_root"]) + ").f_flag & os.ST_RDONLY),"
                "'runtime_ro':bool(os.statvfs(" + repr(values["runtime_root"]) + ").f_flag & os.ST_RDONLY),"
                "'socket_fd':stat.S_ISSOCK(os.fstat(" + str(listener.fileno()) + ").st_mode),"
                "'gpu_device_visible':os.path.exists('/dev/nvidia0')}))")
        executable, command = contract.sandbox_worker_command(values, ["/usr/bin/python3", "-I", "-B", "-c", code])
        result = subprocess.run(command, pass_fds=(listener.fileno(),), text=True, capture_output=True, timeout=15)
    if result.returncode and "Operation not permitted" in result.stderr:
        pytest.skip("this CI host does not permit bubblewrap user namespaces")
    assert result.returncode == 0, result.stderr
    assert executable == contract.BWRAP
    assert json.loads(result.stdout) == {"source_ro": True, "runtime_ro": False, "socket_fd": True, "gpu_device_visible": False}


def test_production_worker_cannot_skip_a_missing_mount_sandbox(deployment, monkeypatch):
    monkeypatch.setattr(contract, "BWRAP", "/missing/bwrap")
    with pytest.raises(ValueError, match="requires bubblewrap"):
        contract.sandbox_worker_command(deployment[1], ["/usr/bin/python3", "-c", "pass"])


@pytest.mark.parametrize("retention_enabled", [False, True])
def test_worker_record_retention_policy_matches_its_maintenance_configuration(tmp_path, monkeypatch, retention_enabled):
    from uuid import uuid4
    from polymer_md.contracts import ScientificRequest, SubmitRequest, request_sha256
    from workers.polymer_md_worker.app.config import load_settings
    from workers.polymer_md_worker.app.manager import JobManager
    monkeypatch.setenv("POLYMER_MD_RETENTION_ENABLED", "true" if retention_enabled else "false")
    manager = JobManager.__new__(JobManager)
    manager.settings = load_settings()
    manager.instance_id, manager.release_digest = uuid4().hex, "a" * 64
    science = ScientificRequest(smiles="*CC*", seed=123)
    request = SubmitRequest(job_id=str(uuid4()), owner_user_id=str(uuid4()), attempt_token="b" * 64,
        request_sha256=request_sha256(science), enqueue_sequence=1, scientific_request=science)
    policy = manager._new_record(request)["snapshot"]["results"]["retention_policy"]
    assert policy == ({"artifact_retention_days": 7, "metadata_retention_days": 30} if retention_enabled else
        {"enabled": False, "artifact_retention_days": None, "metadata_retention_days": None})


def analysis_configuration(deployment, monkeypatch):
    _, worker_values, _ = deployment
    source = Path(worker_values["source_root"])
    files = {name: analysis_dev.digest(source / name) for name in analysis_dev.source_names(source)}
    manifest = {"protocol_version": 1, "python": worker_values["worker_python"], "dependencies": {},
                "dependency_locks": [], "files": files}
    fingerprint = digest_json(manifest)
    release = source.parent / "analysis-releases" / fingerprint
    release.mkdir(mode=0o700, parents=True)
    for name in files:
        path = release / "source" / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copyfile(source / name, path)
        path.chmod(0o400)
    (release / "manifest.json").write_text(json.dumps(manifest))
    (release / "manifest.json").chmod(0o400)
    runtime = Path(worker_values["runtime_root"])
    spool = runtime / "analysis-spool"
    spool.mkdir(mode=0o700)
    values = {key: worker_values[key] for key in contract.PRODUCTION_FIELDS}
    values.update(schema_version=2, runtime_root=str(runtime), job_root=worker_values["job_root"],
        release_root=str(release), spool_root=str(spool), socket_path=str(Path(worker_values["socket_root"]) / "analysis.sock"))
    path = runtime / "analysis.json"
    private_json(path, values)
    monkeypatch.setattr(analysis_dev, "dependencies", lambda _: {})
    return path, values, manifest


def test_production_analysis_verifies_source_and_fails_without_a_real_read_only_mount(deployment, monkeypatch):
    path, values, manifest = analysis_configuration(deployment, monkeypatch)
    loaded = analysis.load_configuration(path)
    assert analysis.verify_configuration(loaded) == manifest
    with pytest.raises(ValueError, match="mounted read-only"):
        analysis.run_analysis(values, manifest)


def test_production_analysis_exec_is_cpu_only_with_independent_bounded_scratch(deployment, monkeypatch):
    _, values, manifest = analysis_configuration(deployment, monkeypatch)
    monkeypatch.setenv("APP_POSTGRES_DSN", "synthetic credential")
    monkeypatch.setenv("POLYMER_MD_ANALYSIS_SPOOL_BUDGET_BYTES", "1")
    captured = {}

    class ExecIntercepted(Exception):
        pass

    def execute(python, argv, environment):
        captured.update(python=python, argv=argv, environment=environment)
        raise ExecIntercepted

    monkeypatch.setattr(analysis.os, "statvfs", lambda _: SimpleNamespace(f_flag=os.ST_RDONLY))
    monkeypatch.setattr(analysis.os, "chdir", lambda _: None)
    monkeypatch.setattr(analysis.os, "execve", execute)
    previous = os.umask(0o077)
    try:
        with pytest.raises(ExecIntercepted):
            analysis.run_analysis(values, manifest)
    finally:
        os.umask(previous)
    environment = captured["environment"]
    assert "APP_POSTGRES_DSN" not in environment
    assert environment["POLYMER_MD_ANALYSIS_SPOOL_BUDGET_BYTES"] == str(48 * 1024**3)
    assert environment["POLYMER_MD_ANALYSIS_JOB_ROOT"] == values["job_root"]
    assert environment["POLYMER_MD_ANALYSIS_SPOOL_ROOT"] != values["job_root"]
    assert environment["CUDA_VISIBLE_DEVICES"] == "" and environment["NVIDIA_VISIBLE_DEVICES"] == "none"
    assert captured["argv"].count("workers.polymer_md_analysis") == 1


def test_production_units_fix_both_resource_envelopes_and_read_only_jobs():
    root = Path(__file__).resolve().parents[2]
    worker_unit = (root / "ops/systemd/nexpoly-polymer-md-prod.service.in").read_text()
    reader_unit = (root / "ops/systemd/nexpoly-polymer-md-analysis-prod.service.in").read_text()
    assert "CPUQuota=1600%" in worker_unit and "MemoryMax=34359738368" in worker_unit
    assert "CPUQuota=100%" in reader_unit and "MemoryMax=2147483648" in reader_unit
    assert "ProtectSystem=strict" in reader_unit and "ReadOnlyPaths=@JOB_ROOT@" in reader_unit
    assert "ReadWritePaths=@SPOOL_ROOT@ @SOCKET_ROOT@" in reader_unit
    assert "POLYMER_MD_ENVIRONMENT=prod" in worker_unit and "CUDA_VISIBLE_DEVICES=" in worker_unit


def test_reader_container_uses_only_an_immutable_image_and_actual_read_only_binds(deployment, monkeypatch):
    path, values, _ = analysis_configuration(deployment, monkeypatch)
    for image in ("python:latest", "nexpoly-analysis:latest", "not-a-digest"):
        with pytest.raises(ValueError, match="sealed image"):
            container.render_compose(values, path, image)
    with pytest.raises(ValueError, match="container interpreter"):
        container.render_compose(values, path, "sha256:" + "a" * 64)
    manifest_path = Path(values["release_root"]) / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest.update(python="/usr/local/bin/python3.11", dependency_locks=[{
        "path": "/opt/nexpoly-analysis/requirements.lock", "sha256": hashlib.sha256(
            (container.ROOT / "workers/polymer_md_analysis/requirements-runtime.lock").read_bytes()).hexdigest()}])
    manifest_path.chmod(0o600)
    manifest_path.write_text(json.dumps(manifest))
    manifest_path.chmod(0o400)
    document = container.render_compose(values, path, "sha256:" + "a" * 64)
    service = document["services"]["polymer-md-analysis"]
    assert service["read_only"] and service["network_mode"] == "none"
    assert service["cpus"] == 1 and service["mem_limit"] == service["memswap_limit"] == 2 * 1024**3
    assert service["cap_drop"] == ["ALL"] and "gpus" not in service and "devices" not in service
    mounts = {item["source"]: item for item in service["volumes"]}
    for name in ("runtime_root", "source_root", "release_root", "job_root"):
        assert mounts[values[name]]["read_only"]
    for name in ("spool_root", "socket_root"):
        assert not mounts[values[name]]["read_only"]
    assert service["command"][-2:] == ["--config", str(path)]
    assert all(not item["bind"]["create_host_path"] for item in mounts.values())
