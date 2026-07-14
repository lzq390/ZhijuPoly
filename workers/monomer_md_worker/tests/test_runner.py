from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys

import pytest

from workers.monomer_md_worker.app.byteff2_env import REQUIRED_OPENMM_FILES
from workers.monomer_md_worker.app.config import WorkerSettings
from workers.monomer_md_worker.app.models import JobRequest
from workers.monomer_md_worker.app.runner import MonomerMdRunner


def _settings(tmp_path: Path) -> WorkerSettings:
    return WorkerSettings(
        mode="dry-run",
        app_postgres_dsn=None,
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
        health_probe_timeout_seconds=5,
        max_concurrent_jobs=1,
        max_active_jobs=1,
        cuda_visible_devices="2",
        worker_id="test-worker",
        worker_version="test",
    )


def test_dry_run_result_has_frontend_shape(tmp_path: Path):
    runner = MonomerMdRunner(_settings(tmp_path))
    request = JobRequest(job_id="job-1", smiles="CCO", canonical_smiles="CCO", steps=300)

    run_result = asyncio.run(runner.run(request, 300))
    result = run_result.result

    assert result["job_id"] == "job-1"
    assert result["summary"]["n_steps"] == 300
    assert result["summary"]["sample_count"] == 30
    assert result["summary"]["final_density_g_cm3"] > 0
    assert result["density_series"]["points"][-1]["step"] == 300
    assert result["temperature_series"]["points"][-1]["value"] == 298.15
    assert result["energy_series"]["points"] == []
    assert result["trajectory_preview"] is None
    assert result["artifacts"]["npt_state_csv"]["path"] == "npt_state.csv"
    assert result["physical_density_estimate"] is False
    assert (run_result.output_dir / "density_demo_results.json").exists()
    assert (run_result.output_dir / "npt_state.csv").exists()
    assert (run_result.output_dir / "npt.dcd").exists()


def test_real_density_runner_receives_openmm_environment(tmp_path: Path, monkeypatch):
    settings = _settings(tmp_path)
    object.__setattr__(settings, "mode", "real")
    settings.byteff2_root.mkdir()
    openmm_dir = tmp_path / "openmm"
    for relative_path in REQUIRED_OPENMM_FILES:
        path = openmm_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    object.__setattr__(settings, "byteff2_openmm_dir", openmm_dir)
    captured_env = {}

    class CompletedProcess:
        async def wait(self):
            return 0

        def kill(self):
            raise AssertionError("successful density demo should not be killed")

    async def fake_create_subprocess_exec(*command, **kwargs):
        captured_env.update(kwargs["env"])
        output_dir = Path(command[command.index("--output-dir") + 1])
        (output_dir / "density_demo_results.json").write_text(
            '{"density_g_cm3": 1.0}', encoding="utf-8"
        )
        (output_dir / "npt_state.csv").write_text(
            "step,density_g_cm3,temperature_k\n1000,1.0,298.15\n",
            encoding="utf-8",
        )
        (output_dir / "npt.dcd").write_bytes(b"test")
        return CompletedProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    runner = MonomerMdRunner(settings)
    request = JobRequest(job_id="job-1", smiles="CCO", canonical_smiles="CCO", steps=1000)

    asyncio.run(runner.run(request, 1000))

    assert captured_env["OPENMM_DIR"] == str(openmm_dir)
    assert captured_env["OPENMM_PLUGIN_DIR"] == str(openmm_dir / "lib/plugins")
    assert captured_env["LD_LIBRARY_PATH"].split(":")[:2] == [
        str(openmm_dir / "lib"),
        str(openmm_dir / "lib/plugins"),
    ]

def test_byteff2_adapter_does_not_use_formal_run_md_as_demo_entry(tmp_path: Path):
    from workers.monomer_md_worker.app.byteff2_density_demo import _find_demo_entry

    formal_dir = tmp_path / "example" / "4_MD_simulations"
    formal_dir.mkdir(parents=True)
    (formal_dir / "run_md.py").write_text("print('formal')\n", encoding="utf-8")

    assert _find_demo_entry(tmp_path) is None


def test_sealed_byteff2_commit_marker_is_used_without_git_metadata(tmp_path: Path):
    from workers.monomer_md_worker.app.byteff2_formal_runner import resolve_byteff2_commit

    commit = "a" * 40
    (tmp_path / "BYTEFF2-COMMIT").write_text(commit + "\n", encoding="ascii")

    assert resolve_byteff2_commit(tmp_path) == commit


def test_byteff2_adapter_missing_configured_entry_fails(tmp_path: Path, monkeypatch):
    from workers.monomer_md_worker.app.byteff2_density_demo import _find_demo_entry

    monkeypatch.setenv("BYTEFF2_DENSITY_DEMO_ENTRY", "missing_demo.py")

    with pytest.raises(FileNotFoundError, match="BYTEFF2_DENSITY_DEMO_ENTRY"):
        _find_demo_entry(tmp_path)


def test_byteff2_adapter_series_reads_openmm_state_columns():
    import pandas as pd

    from workers.monomer_md_worker.app.byteff2_density_demo import _series_from_dataframe

    frame = pd.DataFrame(
        {
            '"Step"': [10, 20],
            "Time (ps)": [0.02, 0.04],
            "Density (g/mL)": [0.91, 0.92],
            "Total Energy (kJ/mole)": [-100.0, -120.0],
        }
    )

    density = _series_from_dataframe(frame, fields=("Density (g/mL)",))
    energy = _series_from_dataframe(frame, fields=("Total Energy (kJ/mole)",), value_multiplier=0.239005736)

    assert density == [
        {"value": 0.91, "step": 10, "time_ps": 0.02},
        {"value": 0.92, "step": 20, "time_ps": 0.04},
    ]
    assert round(energy[-1]["value"], 8) == -28.68068832


def test_byteff2_adapter_adds_missing_gro_box_line(tmp_path: Path):
    from workers.monomer_md_worker.app.byteff2_density_demo import _ensure_gro_box_lines

    gro_path = tmp_path / "MONOMER.gro"
    gro_path.write_text(
        "\n".join(
            [
                "A Gromacs structure file written by ASE ",
                "    2",
                "    1MONOMER    C    1  -0.089   0.017  -0.003  0.0000  0.0000  0.0000",
                "    1MONOMER    H    2  -0.085   0.112  -0.057  0.0000  0.0000  0.0000",
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    _ensure_gro_box_lines(tmp_path)

    lines = gro_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 5
    assert lines[2].startswith("    1MONOM    C    1")
    assert lines[4] == "  10.00000  10.00000  10.00000"


def test_real_demo_cancellation_terminates_process_group(tmp_path: Path):
    settings = _settings(tmp_path)
    object.__setattr__(settings, "mode", "real")
    settings.byteff2_root.mkdir()
    pid_path = tmp_path / "demo-child.pid"
    grandchild_pid_path = tmp_path / "demo-grandchild.pid"
    script = tmp_path / "slow_demo.py"
    script.write_text(
        "import os, pathlib, subprocess, sys, time\n"
        f"pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid()))\n"
        "grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"pathlib.Path({str(grandchild_pid_path)!r}).write_text(str(grandchild.pid))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    object.__setattr__(settings, "byteff2_demo_command", f"{sys.executable} {script}")
    runner = MonomerMdRunner(settings)
    request = JobRequest(job_id="cancel-demo", smiles="CCO", canonical_smiles="CCO", steps=300)

    async def scenario() -> tuple[int, int]:
        task = asyncio.create_task(runner.run(request, 300))
        for _ in range(100):
            if pid_path.exists() and grandchild_pid_path.exists():
                break
            await asyncio.sleep(0.02)
        assert pid_path.exists()
        pid = int(pid_path.read_text(encoding="utf-8"))
        assert grandchild_pid_path.exists()
        grandchild_pid = int(grandchild_pid_path.read_text(encoding="utf-8"))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return pid, grandchild_pid

    child_pid, grandchild_pid = asyncio.run(scenario())
    for pid in (child_pid, grandchild_pid):
        process_state = Path(f"/proc/{pid}/stat")
        if process_state.exists():
            assert process_state.read_text(encoding="utf-8").split()[2] == "Z"
