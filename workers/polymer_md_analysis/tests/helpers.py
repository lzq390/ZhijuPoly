from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import UUID

from workers.polymer_md_analysis.config import AnalysisSettings, GIB
from workers.polymer_md_analysis.contracts import DistanceAnalysisRequest, TrajectoryAnalysisRequest

JOB = UUID("11111111-2222-4333-8444-555555555555")
TOKEN = "a" * 64


def fixture(root: Path, frames=205, triclinic=False):
    import mdtraj as md
    import numpy as np
    top = md.Topology()
    chain = top.add_chain()
    residue = top.add_residue("SYN", chain)
    top.add_atom("C1", md.element.carbon, residue)
    top.add_atom("C2", md.element.carbon, residue)
    xyz = np.zeros((frames, 2, 3), np.float32)
    xyz[:, 0, 0] = .05 + np.arange(frames) * .0001
    xyz[:, 1, 0] = .95 - np.arange(frames) * .0001
    tr = md.Trajectory(xyz, top, time=np.arange(frames, dtype=np.float32), unitcell_lengths=np.ones((frames, 3)),
                      unitcell_angles=np.tile([90., 90., 60. if triclinic else 90.], (frames, 1)))
    if triclinic:
        tr.xyz[:, 0, :] = np.array([.03, .03, .03]) @ tr.unitcell_vectors[0]
        tr.xyz[:, 1, :] = np.array([.97, .97, .03]) @ tr.unitcell_vectors[0]
    directory = root / str(JOB) / TOKEN / "science/eq1"
    directory.mkdir(parents=True)
    tr.save_xtc(str(directory / "trajectory.xtc"))
    tr[0].save_pdb(str(directory / "cell.pdb"))
    meta = {"xtc_file": "eq1/trajectory.xtc", "pdb_file": "eq1/cell.pdb", "frame_count": frames,
            "sample_interval_steps": 10, "time_schedule": [{"start_step": 0, "stop_step": 1000, "time_step_fs": .1, "start_time_ps": 0},
                {"start_step": 1000, "stop_step": 1000000, "time_step_fs": 1., "start_time_ps": .1}]}
    result = root / str(JOB) / TOKEN / "stages/eq1-1/result.json"
    result.parent.mkdir(parents=True)
    result.write_text(json.dumps({"stage": "eq1", "results": {"trajectory": meta}}))
    artifacts = []
    for index, name in enumerate(["trajectory.xtc", "cell.pdb"]):
        path = directory / name
        artifacts.append({"artifact_id": f"eq1-1-{index}", "stage_run_id": "eq1-1", "kind": "trajectory" if index == 0 else "analysis",
            "filename": name, "size_bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "storage_ref": f"{JOB}/{TOKEN}/science/eq1/{name}"})
    # Not every manifest entry is an analysis input. This one need not exist.
    artifacts.append({"artifact_id": "eq1-1-extra", "stage_run_id": "eq1-1", "kind": "analysis", "filename": "unused.json",
        "size_bytes": 100, "sha256": "0" * 64, "storage_ref": f"{JOB}/{TOKEN}/science/eq1/unused.json"})
    body = {"job_id": str(JOB), "attempt_token": TOKEN, "stage_run_id": "eq1-1", "stage": "eq1", "artifacts": artifacts}
    return body, directory, meta


def query(body, **overrides):
    return DistanceAnalysisRequest.model_validate({**body, "distance": {"atom_id_1": 1, "atom_id_2": 2, "start_frame": 3, "stop_frame": 204, "stride": 7, "use_pbc": True, **overrides}})


def settings(root, **options):
    return AnalysisSettings(job_root=root / "jobs", spool_root=root / "spool", min_free_bytes=0, free_fraction=0,
        probe_on_start=False, **options)
