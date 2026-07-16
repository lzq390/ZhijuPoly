from __future__ import annotations

import os
from pathlib import Path

import pytest

from workers.monomer_md_worker.app.byteff2_env import (
    CORE_OPENMM_FILES,
    REQUIRED_OPENMM_FILES,
    TRANSPORT_OPENMM_FILES,
    build_byteff2_environment,
    validate_openmm_contract,
    validate_openmm_environment,
)
from workers.monomer_md_worker.app.config import WorkerSettings, load_settings


def _settings(tmp_path: Path, openmm_dir: Path | None) -> WorkerSettings:
    return WorkerSettings(
        mode="real",
        app_postgres_dsn="postgresql://db/app",
        job_table="md.monomer_md_jobs",
        job_id_column="job_id",
        status_column="status",
        result_column="result_data",
        error_column="error_message",
        output_dir_column="artifact_root",
        artifacts_column="artifacts",
        completed_steps_column="completed_steps",
        progress_percent_column="progress_percent",
        progress_stage_column="progress_stage",
        progress_message_column="progress_message",
        worker_id_column="worker_id",
        worker_job_id_column="worker_job_id",
        worker_version_column="worker_version",
        started_at_column="started_at",
        finished_at_column="finished_at",
        updated_at_column="updated_at",
        byteff2_root=tmp_path / "byteff2",
        byteff2_python="python",
        byteff2_demo_command=None,
        job_root=tmp_path / "runs",
        default_steps=300,
        max_steps=300,
        report_interval=10,
        timeout_seconds=30,
        health_probe_timeout_seconds=30,
        max_concurrent_jobs=1,
        max_active_jobs=1,
        cuda_visible_devices="1",
        worker_id="test-worker",
        worker_version="test",
        byteff2_openmm_dir=openmm_dir,
    )


def _create_openmm_tree(root: Path) -> None:
    for relative_path in REQUIRED_OPENMM_FILES:
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()


def test_load_settings_reads_runtime_contract_and_keeps_gpu_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    openmm_dir = tmp_path / "openmm"
    monkeypatch.setenv("BYTEFF2_OPENMM_DIR", str(openmm_dir))
    monkeypatch.setenv("MONOMER_MD_TRANSPORT_CUDA_SMOKE_ENABLED", "false")
    for name in (
        "MONOMER_MD_DEFAULT_STEPS",
        "MONOMER_MD_MAX_STEPS",
        "MONOMER_MD_HEALTH_PROBE_TIMEOUT_SECONDS",
        "MONOMER_MD_GPU_BROKER_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = load_settings()

    assert settings.default_steps == 300
    assert settings.max_steps == 300
    assert settings.health_probe_timeout_seconds == 30
    assert settings.byteff2_openmm_dir == openmm_dir
    assert settings.transport_cuda_smoke_enabled is False
    assert settings.gpu_broker_environment in {"dev", "prod"}
    assert settings.lease_seconds > settings.heartbeat_interval_seconds


def test_load_settings_rejects_invalid_transport_smoke_boolean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MONOMER_MD_TRANSPORT_CUDA_SMOKE_ENABLED", "sometimes")

    with pytest.raises(
        ValueError,
        match="MONOMER_MD_TRANSPORT_CUDA_SMOKE_ENABLED must be a boolean",
    ):
        load_settings()


def test_environment_prepends_deduplicates_and_preserves_paths(tmp_path: Path):
    openmm_dir = tmp_path / "openmm"
    _create_openmm_tree(openmm_dir)
    settings = _settings(tmp_path, openmm_dir)
    library_dir = openmm_dir / "lib"
    plugin_dir = library_dir / "plugins"
    environment = build_byteff2_environment(
        settings,
        {
            "PATH": "/usr/bin",
            "PYTHONPATH": f"/legacy/python{os.pathsep}{settings.byteff2_root}",
            "LD_LIBRARY_PATH": os.pathsep.join(
                [str(plugin_dir), "/legacy/lib", str(library_dir)]
            ),
        },
    )

    values = environment.as_dict()
    assert environment.openmm_error is None
    assert values["BYTEFF2_ROOT"] == str(settings.byteff2_root)
    assert values["OPENMM_DIR"] == str(openmm_dir)
    assert values["OPENMM_PLUGIN_DIR"] == str(plugin_dir)
    assert values["LD_LIBRARY_PATH"].split(os.pathsep) == [
        str(library_dir),
        str(plugin_dir),
        "/legacy/lib",
    ]
    assert values["PYTHONPATH"].split(os.pathsep) == [
        str(settings.byteff2_root),
        str(settings.byteff2_root / "submodules" / "bytemol"),
        "/legacy/python",
    ]
    assert values["CUDA_VISIBLE_DEVICES"] == "1"
    assert values["PATH"] == "/usr/bin"
    with pytest.raises(TypeError):
        environment.values["OPENMM_DIR"] = "changed"  # type: ignore[index]


def test_environment_deduplicates_inherited_bytemol_path(tmp_path: Path):
    settings = _settings(tmp_path, None)
    bytemol_root = settings.byteff2_root / "submodules" / "bytemol"

    environment = build_byteff2_environment(
        settings,
        {
            "PYTHONPATH": os.pathsep.join(
                [str(bytemol_root), "/legacy/python", str(bytemol_root)]
            )
        },
    )

    assert environment.as_dict()["PYTHONPATH"].split(os.pathsep) == [
        str(settings.byteff2_root),
        str(bytemol_root),
        "/legacy/python",
    ]


@pytest.mark.parametrize(
    ("openmm_dir", "expected"),
    [
        (None, "BYTEFF2_OPENMM_DIR is required"),
        (Path("relative/openmm"), "must be an absolute path"),
    ],
)
def test_environment_rejects_missing_or_relative_root(openmm_dir, expected):
    validation = validate_openmm_contract(openmm_dir)

    assert validation.paths_injectable is False
    assert expected in (validate_openmm_environment(openmm_dir) or "")


def test_environment_reports_missing_core_library(tmp_path: Path):
    openmm_dir = tmp_path / "openmm"
    _create_openmm_tree(openmm_dir)
    (openmm_dir / "lib/libOpenMM.so").unlink()

    assert "libOpenMM.so" in (validate_openmm_environment(openmm_dir) or "")


def test_environment_reports_missing_cuda_plugin(tmp_path: Path):
    openmm_dir = tmp_path / "openmm"
    _create_openmm_tree(openmm_dir)
    (openmm_dir / "lib/plugins/libVelocityVerletPluginCUDA.so").unlink()

    assert "libVelocityVerletPluginCUDA.so" in (
        validate_openmm_environment(openmm_dir) or ""
    )


@pytest.mark.parametrize("missing_file", REQUIRED_OPENMM_FILES)
def test_valid_root_is_injected_even_when_one_required_asset_is_missing(
    tmp_path: Path, missing_file: Path
):
    openmm_dir = tmp_path / "openmm"
    _create_openmm_tree(openmm_dir)
    (openmm_dir / missing_file).unlink()

    environment = build_byteff2_environment(
        _settings(tmp_path, openmm_dir), {"LD_LIBRARY_PATH": "/legacy/lib"}
    )
    validation = validate_openmm_contract(openmm_dir)

    assert environment.paths_injectable is True
    assert environment.transport_error is not None
    assert missing_file.name in environment.transport_error
    assert environment.as_dict()["OPENMM_DIR"] == str(openmm_dir)
    assert environment.as_dict()["LD_LIBRARY_PATH"].split(os.pathsep)[:2] == [
        str(openmm_dir / "lib"),
        str(openmm_dir / "lib/plugins"),
    ]
    if missing_file in CORE_OPENMM_FILES:
        assert validation.core_assets_error is not None
        assert validation.transport_assets_error is None
    if missing_file in TRANSPORT_OPENMM_FILES:
        assert validation.core_assets_error is None
        assert validation.transport_assets_error is not None


def test_invalid_contract_does_not_inject_bad_paths(tmp_path: Path):
    invalid_openmm_dir = tmp_path / "missing-openmm"
    settings = _settings(tmp_path, invalid_openmm_dir)
    base = {
        "OPENMM_DIR": "/known/good/openmm",
        "OPENMM_PLUGIN_DIR": "/known/good/openmm/lib/plugins",
        "LD_LIBRARY_PATH": "/known/good/openmm/lib",
    }

    environment = build_byteff2_environment(settings, base)

    assert environment.paths_injectable is False
    assert environment.openmm_error is not None
    assert environment.as_dict()["OPENMM_DIR"] == base["OPENMM_DIR"]
    assert environment.as_dict()["OPENMM_PLUGIN_DIR"] == base["OPENMM_PLUGIN_DIR"]
    assert environment.as_dict()["LD_LIBRARY_PATH"] == base["LD_LIBRARY_PATH"]


def test_default_environment_drops_poisoned_manager_runtime_controls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    openmm_dir = tmp_path / "openmm"
    _create_openmm_tree(openmm_dir)
    settings = _settings(tmp_path, openmm_dir)
    poisoned = {
        "CUDA_MPS_PIPE_DIRECTORY": "/poisoned/mps",
        "CUDA_MPS_ACTIVE_THREAD_PERCENTAGE": "99",
        "LD_PRELOAD": "/poisoned/preload.so",
        "LD_LIBRARY_PATH": "/poisoned/lib",
        "OPENMM_DIR": "/poisoned/openmm",
        "PYTHONHOME": "/poisoned/python",
        "PYTHONPATH": "/poisoned/pythonpath",
        "PIP_TARGET": "/poisoned/pip",
        "TORCH_HOME": "/poisoned/torch",
        "HF_HOME": "/poisoned/huggingface",
        "OMP_NUM_THREADS": "999",
    }
    for key, value in poisoned.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PATH", "/safe/bin")

    values = build_byteff2_environment(settings).as_dict()

    assert values["PATH"] == "/safe/bin"
    assert values["PYTHONNOUSERSITE"] == "1"
    assert values["PYTHONDONTWRITEBYTECODE"] == "1"
    assert values["OPENMM_DIR"] == str(openmm_dir)
    assert values["LD_LIBRARY_PATH"].split(os.pathsep) == [
        str(openmm_dir / "lib"),
        str(openmm_dir / "lib/plugins"),
    ]
    assert values["PYTHONPATH"].split(os.pathsep) == [
        str(settings.byteff2_root),
        str(settings.byteff2_root / "submodules/bytemol"),
    ]
    for key in (
        "CUDA_MPS_PIPE_DIRECTORY",
        "CUDA_MPS_ACTIVE_THREAD_PERCENTAGE",
        "LD_PRELOAD",
        "PYTHONHOME",
        "PIP_TARGET",
        "TORCH_HOME",
        "HF_HOME",
        "OMP_NUM_THREADS",
    ):
        assert key not in values
