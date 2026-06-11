from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Self
from uuid import uuid4

import numpy as np
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator


FIXTURE_PATH = Path(__file__).resolve().parents[1] / "data" / "md_demo_fixture.json"
TRAJECTORY_FIXTURE_PATH = Path(__file__).resolve().parents[1] / "data" / "md_demo_trajectory.npz"

router = APIRouter(prefix="/api/v1/md-demo", tags=["md-demo"])


@dataclass(frozen=True)
class MdDemoTrajectoryFixture:
    atom_ids: np.ndarray
    chain_ids: np.ndarray
    atom_types: np.ndarray
    frame_indices: np.ndarray
    time_ps: np.ndarray
    coords: np.ndarray
    box_lengths: np.ndarray
    source_frame_count: int


class MdDemoRunRequest(BaseModel):
    smiles: str = Field(..., min_length=1, max_length=4000)
    temperature: float = Field(300.0, gt=0, le=5000)
    pressure: float = Field(1.0, gt=0, le=100000)
    n_atom: int = Field(1000, ge=100, le=500000)
    n_chain: int = Field(10, ge=1, le=10000)
    forcefield: str = Field("GAFF2_mod", min_length=1, max_length=64)

    @field_validator("smiles", "forcefield")
    @classmethod
    def _strip_required_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty")
        return stripped


class MdDemoAtomDistanceRequest(BaseModel):
    atom_id_1: int = Field(..., ge=1)
    atom_id_2: int = Field(..., ge=1)
    use_pbc: bool = True

    @model_validator(mode="after")
    def _distinct_atoms(self) -> Self:
        if self.atom_id_1 == self.atom_id_2:
            raise ValueError("Two distinct atom_ids are required")
        return self


class MdDemoDefaultsResponse(BaseModel):
    default_request: dict[str, object]
    available_stages: list[dict[str, object]]
    summary: dict[str, object]
    fixture_metadata: dict[str, object]


class MdDemoRunResponse(BaseModel):
    input: MdDemoRunRequest
    run_id: str
    status: str
    query_time_ms: float
    stages: list[dict[str, object]]
    summary: dict[str, object]
    density_series: dict[str, object]
    thermo_series: list[dict[str, object]]
    trajectory_preview: dict[str, object]
    atom_distance_series: dict[str, object] | None
    fixture_metadata: dict[str, object]


class MdDemoAtomDistanceResponse(BaseModel):
    atom_1: dict[str, object]
    atom_2: dict[str, object]
    frames: list[int]
    time_ps: list[float]
    distance: list[float]
    series: dict[str, object]
    stats: dict[str, object]


def _load_fixture() -> dict[str, object]:
    with FIXTURE_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _load_trajectory_fixture() -> MdDemoTrajectoryFixture:
    with np.load(TRAJECTORY_FIXTURE_PATH) as payload:
        return MdDemoTrajectoryFixture(
            atom_ids=payload["atom_ids"].astype(np.int32, copy=True),
            chain_ids=payload["chain_ids"].astype(np.int32, copy=True),
            atom_types=payload["atom_types"].astype(str, copy=True),
            frame_indices=payload["frame_indices"].astype(np.int32, copy=True),
            time_ps=payload["time_ps"].astype(np.float64, copy=True),
            coords=payload["coords"].astype(np.float32, copy=True),
            box_lengths=payload["box_lengths"].astype(np.float32, copy=True),
            source_frame_count=int(payload["source_frame_count"][0]),
        )


def _fixture_metadata(fixture: dict[str, object]) -> dict[str, object]:
    return {
        "fixture_version": fixture["fixture_version"],
        "source": fixture["source"],
    }


def _atom_metadata(trajectory: MdDemoTrajectoryFixture, atom_index: int) -> dict[str, object]:
    return {
        "atom_id": int(trajectory.atom_ids[atom_index]),
        "chain_id": int(trajectory.chain_ids[atom_index]),
        "atom_type": str(trajectory.atom_types[atom_index]),
    }


def _atom_index_by_id(trajectory: MdDemoTrajectoryFixture, atom_id: int) -> int:
    matches = np.where(trajectory.atom_ids == atom_id)[0]
    if matches.size == 0:
        raise HTTPException(status_code=404, detail=f"Atom id {atom_id} was not found in the demo trajectory")
    return int(matches[0])


def _trajectory_preview(trajectory: MdDemoTrajectoryFixture) -> dict[str, object]:
    final_coords = trajectory.coords[-1]
    points = [
        {
            "atom_id": int(atom_id),
            "chain_id": int(chain_id),
            "atom_type": str(atom_type),
            "x": round(float(position[0]), 2),
            "y": round(float(position[1]), 2),
            "z": round(float(position[2]), 2),
        }
        for atom_id, chain_id, atom_type, position in zip(
            trajectory.atom_ids,
            trajectory.chain_ids,
            trajectory.atom_types,
            final_coords,
            strict=True,
        )
    ]
    return {
        "stage_id": "eq3",
        "frame_index": int(trajectory.frame_indices[-1]),
        "time_ps": round(float(trajectory.time_ps[-1]), 4),
        "sampled_points": len(points),
        "points": points,
        "box": {
            "lx": round(float(trajectory.box_lengths[-1, 0]), 4),
            "ly": round(float(trajectory.box_lengths[-1, 1]), 4),
            "lz": round(float(trajectory.box_lengths[-1, 2]), 4),
        },
    }


def _atom_distance_series(
    trajectory: MdDemoTrajectoryFixture,
    atom_id_1: int,
    atom_id_2: int,
    use_pbc: bool,
) -> dict[str, object]:
    atom_index_1 = _atom_index_by_id(trajectory, atom_id_1)
    atom_index_2 = _atom_index_by_id(trajectory, atom_id_2)

    delta = trajectory.coords[:, atom_index_1, :] - trajectory.coords[:, atom_index_2, :]
    if use_pbc:
        box_lengths = trajectory.box_lengths
        valid_box = box_lengths > 0
        safe_box = np.where(valid_box, box_lengths, 1.0)
        delta = np.where(valid_box, delta - safe_box * np.round(delta / safe_box), delta)

    distances = np.linalg.norm(delta, axis=1).astype(np.float64)
    points = [
        {
            "frame": int(frame_index),
            "time_ps": round(float(time_ps), 4),
            "value": round(float(distance), 6),
        }
        for frame_index, time_ps, distance in zip(
            trajectory.frame_indices,
            trajectory.time_ps,
            distances,
            strict=True,
        )
    ]
    return {
        "atom_1": _atom_metadata(trajectory, atom_index_1),
        "atom_2": _atom_metadata(trajectory, atom_index_2),
        "frames": [int(value) for value in trajectory.frame_indices],
        "time_ps": [round(float(value), 4) for value in trajectory.time_ps],
        "distance": [round(float(value), 6) for value in distances],
        "series": {
            "key": "atom_distance",
            "label": "Atom pair distance",
            "unit": "A",
            "points": points,
        },
        "stats": {
            "n_atoms": int(trajectory.atom_ids.size),
            "n_frames": int(trajectory.frame_indices.size),
            "source_n_frames": trajectory.source_frame_count,
            "n_chains": int(np.unique(trajectory.chain_ids).size),
            "use_pbc": use_pbc,
            "min_distance": round(float(np.min(distances)), 6),
            "max_distance": round(float(np.max(distances)), 6),
        },
    }


@router.get("/defaults", response_model=MdDemoDefaultsResponse)
async def get_md_demo_defaults() -> MdDemoDefaultsResponse:
    fixture = _load_fixture()
    return MdDemoDefaultsResponse(
        default_request=fixture["defaults"],
        available_stages=fixture["stages"],
        summary=fixture["summary"],
        fixture_metadata=_fixture_metadata(fixture),
    )


@router.post("/run", response_model=MdDemoRunResponse)
async def run_md_demo(request_body: MdDemoRunRequest) -> MdDemoRunResponse:
    started_at = perf_counter()
    fixture = _load_fixture()
    trajectory = _load_trajectory_fixture()
    return MdDemoRunResponse(
        input=request_body,
        run_id=f"md-demo-{uuid4().hex[:12]}",
        status="completed",
        query_time_ms=(perf_counter() - started_at) * 1000,
        stages=fixture["stages"],
        summary=fixture["summary"],
        density_series=fixture["density_series"],
        thermo_series=fixture["thermo_series"],
        trajectory_preview=_trajectory_preview(trajectory),
        atom_distance_series=None,
        fixture_metadata=_fixture_metadata(fixture),
    )


@router.post("/atom-distance", response_model=MdDemoAtomDistanceResponse)
async def calculate_atom_distance(payload: MdDemoAtomDistanceRequest) -> MdDemoAtomDistanceResponse:
    trajectory = _load_trajectory_fixture()
    return MdDemoAtomDistanceResponse(
        **_atom_distance_series(
            trajectory=trajectory,
            atom_id_1=payload.atom_id_1,
            atom_id_2=payload.atom_id_2,
            use_pbc=payload.use_pbc,
        )
    )
