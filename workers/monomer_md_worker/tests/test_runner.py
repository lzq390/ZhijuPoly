from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

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
        default_steps=1000,
        max_steps=1000,
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
    request = JobRequest(job_id="job-1", smiles="CCO", canonical_smiles="CCO", steps=1000)

    run_result = asyncio.run(runner.run(request, 1000))
    result = run_result.result

    assert result["job_id"] == "job-1"
    assert result["summary"]["n_steps"] == 1000
    assert result["summary"]["sample_count"] == 100
    assert result["summary"]["final_density_g_cm3"] > 0
    assert result["density_series"]["points"][-1]["step"] == 1000
    assert result["temperature_series"]["points"][-1]["value"] == 298.15
    assert result["energy_series"]["points"] == []
    assert result["trajectory_preview"] is None
    assert result["artifacts"]["npt_state_csv"]["path"] == "npt_state.csv"
    assert result["physical_density_estimate"] is False
    assert (run_result.output_dir / "density_demo_results.json").exists()
    assert (run_result.output_dir / "npt_state.csv").exists()
    assert (run_result.output_dir / "npt.dcd").exists()

def test_byteff2_adapter_does_not_use_formal_run_md_as_demo_entry(tmp_path: Path):
    from workers.monomer_md_worker.app.byteff2_density_demo import _find_demo_entry

    formal_dir = tmp_path / "example" / "4_MD_simulations"
    formal_dir.mkdir(parents=True)
    (formal_dir / "run_md.py").write_text("print('formal')\n", encoding="utf-8")

    assert _find_demo_entry(tmp_path) is None


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
