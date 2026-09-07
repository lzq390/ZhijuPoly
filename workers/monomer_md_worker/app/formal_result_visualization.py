from __future__ import annotations

import base64
import csv
from dataclasses import dataclass
import gzip
import math
from pathlib import Path
import re
import stat
from typing import Any, Iterable, Sequence


VISUALIZATION_SCHEMA_VERSION = 3
DEFAULT_CURVE_POINT_LIMIT = 1_000
DEFAULT_TRAJECTORY_ATOM_LIMIT = 2_000
DEFAULT_TRAJECTORY_FRAME_LIMIT = 60
TRAJECTORY_COORDINATE_SCALE = 0.01
KJ_TO_KCAL = 0.239005736


@dataclass(frozen=True)
class StageSpec:
    stage_id: str
    label: str
    state_csv: str
    trajectory_dcd: str
    topology_gro: str
    phase: str | None = None
    periodic: bool = True
    density_optional: bool = False


@dataclass(frozen=True)
class ParsedState:
    density_series: dict[str, Any] | None
    temperature_series: dict[str, Any] | None
    potential_energy_series: dict[str, Any] | None
    kinetic_energy_series: dict[str, Any] | None
    energy_series: dict[str, Any] | None
    last_time_ps: float | None
    timeline_rows: tuple[dict[str, float], ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class GroAtom:
    atom_id: int
    residue_id: int
    residue_key: int
    residue_name: str
    atom_name: str


PROTOCOL_STAGE_SPECS: dict[str, tuple[StageSpec, ...]] = {
    "Density": (
        StageSpec(
            "npt",
            "NPT 平衡与采样",
            "outputs/npt_state.csv",
            "outputs/npt.dcd",
            "params/solvent_salt.gro",
            phase="liquid",
        ),
    ),
    "Transport": (
        StageSpec(
            "npt",
            "NPT 平衡阶段",
            "outputs/npt_state.csv",
            "outputs/npt.dcd",
            "params/solvent_salt.gro",
            phase="liquid",
        ),
        StageSpec(
            "nvt",
            "NVT 生产阶段",
            "outputs/nvt_state.csv",
            "outputs/nvt.dcd",
            "params/solvent_salt.gro",
            phase="liquid",
        ),
    ),
    "HVap": (
        StageSpec(
            "liquid_npt",
            "NPT 液相",
            "outputs/npt_state.csv",
            "outputs/npt.dcd",
            "params/solvent_salt.gro",
            phase="liquid",
        ),
        StageSpec(
            "gas_nvt",
            "NVT 气相",
            "outputs/nvt_state.csv",
            "outputs/nvt.dcd",
            "params/solvent_salt_gas.gro",
            phase="gas",
            periodic=False,
            density_optional=True,
        ),
    ),
    "Dielectric": (
        StageSpec(
            "npt",
            "NPT 平衡阶段",
            "outputs/npt_state.csv",
            "outputs/npt.dcd",
            "params/solvent_salt.gro",
            phase="liquid",
        ),
        StageSpec(
            "nvt",
            "NVT 偶极采样阶段",
            "outputs/nvt_state.csv",
            "outputs/nvt.dcd",
            "params/solvent_salt.gro",
            phase="liquid",
        ),
    ),
    "Compressibility": (
        StageSpec(
            "npt",
            "NPT 生产阶段",
            "outputs/npt_state.csv",
            "outputs/npt.dcd",
            "params/solvent_salt.gro",
            phase="liquid",
        ),
    ),
}

PROTOCOL_DEFAULT_STAGE = {
    "Density": "npt",
    "Transport": "nvt",
    "HVap": "liquid_npt",
    "Dielectric": "nvt",
    "Compressibility": "npt",
}

_HEADER_ALIASES = {
    "step": ("step",),
    "time_ps": ("timeps", "timepicoseconds"),
    "density": ("densitygml", "densitygcm3", "density"),
    "temperature": ("temperaturek", "temperature", "tempk"),
    "potential_energy": (
        "potentialenergykjmole",
        "potentialenergykjmol",
        "potentialenergy",
    ),
    "kinetic_energy": (
        "kineticenergykjmole",
        "kineticenergykjmol",
        "kineticenergy",
    ),
    "energy": (
        "totalenergykjmole",
        "totalenergykjmol",
        "totalenergy",
    ),
}

_ELEMENT_SYMBOLS = frozenset(
    {
        "H",
        "He",
        "Li",
        "Be",
        "B",
        "C",
        "N",
        "O",
        "F",
        "Ne",
        "Na",
        "Mg",
        "Al",
        "Si",
        "P",
        "S",
        "Cl",
        "Ar",
        "K",
        "Ca",
        "Sc",
        "Ti",
        "V",
        "Cr",
        "Mn",
        "Fe",
        "Co",
        "Ni",
        "Cu",
        "Zn",
        "Br",
        "I",
    }
)


def build_formal_visualization(
    protocol: str,
    job_root: Path,
    *,
    curve_point_limit: int = DEFAULT_CURVE_POINT_LIMIT,
    trajectory_atom_limit: int = DEFAULT_TRAJECTORY_ATOM_LIMIT,
    trajectory_frame_limit: int = DEFAULT_TRAJECTORY_FRAME_LIMIT,
) -> dict[str, Any]:
    """Materialize bounded display data from a completed ByteFF2 formal run.

    Missing or corrupt optional display artifacts are represented as warnings;
    they never invalidate the protocol's already-produced scientific result.
    """

    try:
        specs = PROTOCOL_STAGE_SPECS[protocol]
        default_stage_id = PROTOCOL_DEFAULT_STAGE[protocol]
    except KeyError as exc:
        raise ValueError(f"unsupported formal visualization protocol: {protocol}") from exc
    if curve_point_limit < 2:
        raise ValueError("curve_point_limit must be at least 2")
    if trajectory_atom_limit < 1:
        raise ValueError("trajectory_atom_limit must be positive")
    if trajectory_frame_limit < 1:
        raise ValueError("trajectory_frame_limit must be positive")

    stages: list[dict[str, Any]] = []
    global_warnings: list[str] = []
    has_any_data = False
    has_any_warning = False
    for spec in specs:
        state = parse_openmm_state_csv(
            job_root / spec.state_csv,
            point_limit=curve_point_limit,
            density_optional=spec.density_optional,
        )
        preview, timeline, trajectory_warnings = build_trajectory_visualization(
            job_root / spec.topology_gro,
            job_root / spec.trajectory_dcd,
            state_rows=state.timeline_rows,
            periodic=spec.periodic,
            atom_limit=trajectory_atom_limit,
            frame_limit=trajectory_frame_limit,
        )
        warnings = [*state.warnings, *trajectory_warnings]
        stage: dict[str, Any] = {
            "stage_id": spec.stage_id,
            "label": spec.label,
            "warnings": warnings,
        }
        if spec.phase is not None:
            stage["phase"] = spec.phase
        for field, series in (
            ("density_series", state.density_series),
            ("temperature_series", state.temperature_series),
            ("potential_energy_series", state.potential_energy_series),
            ("kinetic_energy_series", state.kinetic_energy_series),
            ("energy_series", state.energy_series),
        ):
            if series is not None:
                stage[field] = series
                has_any_data = has_any_data or bool(series["points"])
        stage["trajectory_preview"] = preview
        if timeline is not None:
            stage["trajectory_timeline"] = timeline
        has_any_data = has_any_data or preview is not None or timeline is not None
        has_any_warning = has_any_warning or bool(warnings)
        global_warnings.extend(f"{spec.stage_id}:{warning}" for warning in warnings)
        stages.append(stage)

    status = (
        "unavailable"
        if not has_any_data
        else "partial"
        if has_any_warning
        else "complete"
    )
    return {
        "schema_version": VISUALIZATION_SCHEMA_VERSION,
        "status": status,
        "default_stage_id": default_stage_id,
        "stages": stages,
        "warnings": list(dict.fromkeys(global_warnings)),
    }


def unavailable_visualization(protocol: str, warning: str) -> dict[str, Any]:
    specs = PROTOCOL_STAGE_SPECS.get(protocol, ())
    default_stage_id = PROTOCOL_DEFAULT_STAGE.get(
        protocol,
        specs[0].stage_id if specs else "unknown",
    )
    return {
        "schema_version": VISUALIZATION_SCHEMA_VERSION,
        "status": "unavailable",
        "default_stage_id": default_stage_id,
        "stages": [
            {
                "stage_id": spec.stage_id,
                "label": spec.label,
                **({"phase": spec.phase} if spec.phase is not None else {}),
                "trajectory_preview": None,
                "warnings": [warning],
            }
            for spec in specs
        ],
        "warnings": [warning],
    }


def parse_openmm_state_csv(
    path: Path,
    *,
    point_limit: int = DEFAULT_CURVE_POINT_LIMIT,
    density_optional: bool = False,
) -> ParsedState:
    file_state = _regular_file_state(path)
    if file_state != "ok":
        warning = "STATE_CSV_UNSAFE" if file_state == "unsafe" else "STATE_CSV_MISSING"
        return ParsedState(None, None, None, None, None, None, (), (warning,))

    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            header = next(reader)
            normalized = [_normalize_header(item) for item in header]
            indices = {
                key: _first_header_index(normalized, aliases)
                for key, aliases in _HEADER_ALIASES.items()
            }
            density_points: list[dict[str, Any]] = []
            temperature_points: list[dict[str, Any]] = []
            potential_energy_points: list[dict[str, Any]] = []
            kinetic_energy_points: list[dict[str, Any]] = []
            energy_points: list[dict[str, Any]] = []
            timeline_rows: list[dict[str, float]] = []
            last_time_ps: float | None = None
            for row in reader:
                if not row or not any(cell.strip() for cell in row):
                    continue
                step_value = _cell_float(row, indices["step"])
                time_value = _cell_float(row, indices["time_ps"])
                if time_value is not None:
                    last_time_ps = time_value
                base: dict[str, Any] = {}
                if step_value is not None:
                    base["step"] = (
                        int(step_value) if step_value.is_integer() else _compact(step_value)
                    )
                if time_value is not None:
                    base["time_ps"] = _compact(time_value)
                density = _cell_float(row, indices["density"])
                temperature = _cell_float(row, indices["temperature"])
                potential_energy = _cell_float(row, indices["potential_energy"])
                kinetic_energy = _cell_float(row, indices["kinetic_energy"])
                energy = _cell_float(row, indices["energy"])
                timeline_row: dict[str, float] = {}
                for key, value in (
                    ("time_ps", time_value),
                    ("density", density),
                    ("temperature", temperature),
                    ("energy", energy),
                ):
                    if value is not None:
                        timeline_row[key] = _compact(value)
                timeline_rows.append(timeline_row)
                if density is not None:
                    density_points.append({**base, "value": _compact(density)})
                if temperature is not None:
                    temperature_points.append({**base, "value": _compact(temperature)})
                if potential_energy is not None:
                    potential_energy_points.append(
                        {**base, "value": _compact(potential_energy * KJ_TO_KCAL)}
                    )
                if kinetic_energy is not None:
                    kinetic_energy_points.append(
                        {**base, "value": _compact(kinetic_energy * KJ_TO_KCAL)}
                    )
                if energy is not None:
                    energy_points.append(
                        {**base, "value": _compact(energy * KJ_TO_KCAL)}
                    )
    except (OSError, UnicodeError, csv.Error, StopIteration, ValueError):
        return ParsedState(
            None,
            None,
            None,
            None,
            None,
            None,
            (),
            ("STATE_CSV_UNREADABLE",),
        )

    warnings: list[str] = []
    if not density_points and not density_optional:
        warnings.append("DENSITY_SERIES_UNAVAILABLE")
    if not temperature_points:
        warnings.append("TEMPERATURE_SERIES_UNAVAILABLE")
    if not potential_energy_points:
        warnings.append("POTENTIAL_ENERGY_SERIES_UNAVAILABLE")
    if not kinetic_energy_points:
        warnings.append("KINETIC_ENERGY_SERIES_UNAVAILABLE")
    if not energy_points:
        warnings.append("ENERGY_SERIES_UNAVAILABLE")
    if not any(
        (
            density_points,
            temperature_points,
            potential_energy_points,
            kinetic_energy_points,
            energy_points,
        )
    ):
        warnings.append("STATE_CSV_EMPTY")

    return ParsedState(
        _series(
            "density",
            "Density",
            "g/cm³",
            density_points,
            point_limit,
        )
        if density_points
        else None,
        _series(
            "temperature",
            "Temperature",
            "K",
            temperature_points,
            point_limit,
        )
        if temperature_points
        else None,
        _series(
            "potential_energy",
            "Potential energy",
            "kcal/mol",
            potential_energy_points,
            point_limit,
        )
        if potential_energy_points
        else None,
        _series(
            "kinetic_energy",
            "Kinetic energy",
            "kcal/mol",
            kinetic_energy_points,
            point_limit,
        )
        if kinetic_energy_points
        else None,
        _series(
            "energy",
            "Total energy",
            "kcal/mol",
            energy_points,
            point_limit,
        )
        if energy_points
        else None,
        _compact(last_time_ps) if last_time_ps is not None else None,
        tuple(timeline_rows),
        tuple(warnings),
    )


def build_trajectory_visualization(
    topology_path: Path,
    trajectory_path: Path,
    *,
    state_rows: Sequence[dict[str, float]],
    periodic: bool,
    atom_limit: int = DEFAULT_TRAJECTORY_ATOM_LIMIT,
    frame_limit: int = DEFAULT_TRAJECTORY_FRAME_LIMIT,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, tuple[str, ...]]:
    topology_state = _regular_file_state(topology_path)
    trajectory_state = _regular_file_state(trajectory_path)
    warnings: list[str] = []
    if topology_state != "ok":
        warnings.append(
            "TRAJECTORY_TOPOLOGY_UNSAFE"
            if topology_state == "unsafe"
            else "TRAJECTORY_TOPOLOGY_MISSING"
        )
    if trajectory_state != "ok":
        warnings.append(
            "TRAJECTORY_DCD_UNSAFE"
            if trajectory_state == "unsafe"
            else "TRAJECTORY_DCD_MISSING"
        )
    if warnings:
        return None, None, tuple(warnings)

    try:
        atoms = _read_gro_atoms(topology_path)
    except Exception:
        return None, None, ("TRAJECTORY_TOPOLOGY_UNREADABLE",)
    try:
        import numpy as np
        from MDAnalysis.lib.formats.libdcd import DCDFile
    except ImportError:
        return None, None, ("TRAJECTORY_READER_UNAVAILABLE",)

    try:
        with DCDFile(str(trajectory_path)) as trajectory:
            frame_count = len(trajectory)
            atom_count = int(trajectory.header["natoms"])
            if frame_count < 1:
                return None, None, ("TRAJECTORY_EMPTY",)
            if atom_count != len(atoms):
                return None, None, ("TRAJECTORY_ATOM_COUNT_MISMATCH",)

            frame_indices = deterministic_trajectory_frame_indices(
                frame_count,
                frame_limit,
                state_rows,
            )
            sampled_indices, sampling_strategy = _whole_residue_stratified_indices(
                atoms,
                atom_limit,
            )
            aligned_times = len(state_rows) == frame_count
            if not aligned_times:
                warnings.append("TRAJECTORY_TIME_ALIGNMENT_UNAVAILABLE")

            coordinate_frames: list[Any] = []
            frame_metadata: list[dict[str, Any]] = []
            for frame_index in frame_indices:
                trajectory.seek(frame_index)
                frame = trajectory.read()
                coordinates = frame.xyz.copy()
                if coordinates.shape != (len(atoms), 3):
                    return None, None, ("TRAJECTORY_ATOM_COUNT_MISMATCH",)
                if not bool(_all_finite(coordinates)):
                    return None, None, ("TRAJECTORY_COORDINATES_NONFINITE",)

                box = _orthorhombic_box(frame.unitcell) if periodic else None
                if periodic and box is None:
                    warnings.append("TRAJECTORY_BOX_UNAVAILABLE")
                if box is not None:
                    _wrap_residues_in_place(coordinates, atoms, box)

                coordinate_frames.append(coordinates[sampled_indices].copy())
                metadata: dict[str, Any] = {"frame_index": frame_index}
                if aligned_times:
                    time_ps = state_rows[frame_index].get("time_ps")
                    if time_ps is not None and math.isfinite(time_ps):
                        metadata["time_ps"] = _compact(time_ps)
                metadata["box"] = _box_payload(box)
                frame_metadata.append(metadata)
    except Exception:
        return None, None, ("TRAJECTORY_DCD_UNREADABLE",)

    atom_metadata = [_trajectory_atom_payload(atoms[index]) for index in sampled_indices]
    final_coordinates = coordinate_frames[-1]
    final_metadata = frame_metadata[-1]
    points = [
        {
            **metadata,
            "x": round(float(coordinates[0]), 4),
            "y": round(float(coordinates[1]), 4),
            "z": round(float(coordinates[2]), 4),
        }
        for metadata, coordinates in zip(atom_metadata, final_coordinates, strict=True)
    ]
    preview: dict[str, Any] = {
        "frame_index": final_metadata["frame_index"],
        "coordinate_unit": "angstrom",
        "total_atoms": len(atoms),
        "sampled_points": len(points),
        "points": points,
        "box": final_metadata["box"],
    }
    if "time_ps" in final_metadata:
        preview["time_ps"] = final_metadata["time_ps"]

    try:
        encoded, decoded_bytes, compressed_bytes = _encode_trajectory_coordinates(
            np.asarray(coordinate_frames),
            scale=TRAJECTORY_COORDINATE_SCALE,
        )
    except (OverflowError, ValueError):
        warnings.append("TRAJECTORY_TIMELINE_ENCODING_UNAVAILABLE")
        return preview, None, tuple(dict.fromkeys(warnings))

    timeline = {
        "schema_version": 1,
        "source_frame_count": frame_count,
        "sampled_frame_count": len(frame_indices),
        "total_atoms": len(atoms),
        "sampled_points": len(sampled_indices),
        "coordinate_unit": "angstrom",
        "coordinate_scale": TRAJECTORY_COORDINATE_SCALE,
        "coordinate_encoding": "int16-delta-gzip-base64",
        "coordinate_byte_order": "little",
        "decoded_byte_length": decoded_bytes,
        "compressed_byte_length": compressed_bytes,
        "sampling_strategy": sampling_strategy,
        "atoms": atom_metadata,
        "frames": frame_metadata,
        "coordinates": encoded,
    }
    return preview, timeline, tuple(dict.fromkeys(warnings))


def build_final_frame_preview(
    topology_path: Path,
    trajectory_path: Path,
    *,
    time_ps: float | None,
    periodic: bool,
    atom_limit: int = DEFAULT_TRAJECTORY_ATOM_LIMIT,
) -> tuple[dict[str, Any] | None, tuple[str, ...]]:
    """Compatibility wrapper for callers that only need the final frame."""

    preview, _, warnings = build_trajectory_visualization(
        topology_path,
        trajectory_path,
        state_rows=(),
        periodic=periodic,
        atom_limit=atom_limit,
        frame_limit=1,
    )
    if preview is not None and time_ps is not None:
        preview["time_ps"] = _compact(time_ps)
    return preview, tuple(
        warning
        for warning in warnings
        if warning != "TRAJECTORY_TIME_ALIGNMENT_UNAVAILABLE"
    )


def deterministic_minmax_downsample(
    points: Sequence[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    if limit < 2:
        raise ValueError("limit must be at least 2")
    if len(points) <= limit:
        return list(points)
    interior = points[1:-1]
    budget = limit - 2
    bucket_count = max(1, budget // 2)
    selected_indices: set[int] = {0, len(points) - 1}
    for bucket in range(bucket_count):
        start = 1 + (len(interior) * bucket) // bucket_count
        stop = 1 + (len(interior) * (bucket + 1)) // bucket_count
        if stop <= start:
            continue
        candidates = range(start, stop)
        minimum = min(candidates, key=lambda index: (float(points[index]["value"]), index))
        maximum = max(candidates, key=lambda index: (float(points[index]["value"]), -index))
        selected_indices.add(minimum)
        selected_indices.add(maximum)
    selected = sorted(selected_indices)
    if len(selected) > limit:
        interior_selected = selected[1:-1]
        keep = _evenly_spaced_values(interior_selected, budget)
        selected = [0, *keep, len(points) - 1]
    return [points[index] for index in selected]


def deterministic_trajectory_frame_indices(
    frame_count: int,
    limit: int = DEFAULT_TRAJECTORY_FRAME_LIMIT,
    state_rows: Sequence[dict[str, float]] = (),
) -> list[int]:
    """Choose real trajectory frames with early, event, and uniform coverage."""

    if frame_count < 1:
        raise ValueError("frame_count must be positive")
    if limit < 1:
        raise ValueError("limit must be positive")
    if frame_count <= limit:
        return list(range(frame_count))
    if limit == 1:
        return [frame_count - 1]

    selected: list[int] = []
    selected_set: set[int] = set()

    def add(index: int) -> None:
        if 0 <= index < frame_count and index not in selected_set and len(selected) < limit:
            selected.append(index)
            selected_set.add(index)

    add(0)
    add(frame_count - 1)
    for index in range(min(10, frame_count)):
        add(index)

    signal_keys = ("density", "temperature", "energy")
    if len(state_rows) == frame_count:
        spans: dict[str, float] = {}
        for key in signal_keys:
            values = [row[key] for row in state_rows if key in row and math.isfinite(row[key])]
            if values:
                minimum = min(values)
                maximum = max(values)
                spans[key] = maximum - minimum or 1.0
                add(next(index for index, row in enumerate(state_rows) if row.get(key) == minimum))
                add(next(index for index, row in enumerate(state_rows) if row.get(key) == maximum))

        changes: list[tuple[float, int]] = []
        for index in range(1, frame_count):
            score = 0.0
            for key, span in spans.items():
                previous = state_rows[index - 1].get(key)
                current = state_rows[index].get(key)
                if previous is not None and current is not None:
                    score += abs(current - previous) / span
            if score > 0:
                changes.append((score, index))
        event_limit = min(12, max(0, limit - len(selected)))
        for _, index in sorted(changes, key=lambda item: (-item[0], item[1]))[:event_limit]:
            add(index)

    for index in _evenly_spaced_values(list(range(frame_count)), limit):
        add(index)

    while len(selected) < limit:
        remaining = (index for index in range(frame_count) if index not in selected_set)
        candidate = max(
            remaining,
            key=lambda index: (min(abs(index - chosen) for chosen in selected_set), -index),
        )
        add(candidate)
    return sorted(selected)


def _series(
    key: str,
    label: str,
    unit: str,
    points: list[dict[str, Any]],
    point_limit: int,
) -> dict[str, Any]:
    sampled = deterministic_minmax_downsample(points, point_limit)
    return {
        "key": key,
        "label": label,
        "unit": unit,
        "source_point_count": len(points),
        "sampled_point_count": len(sampled),
        "points": sampled,
    }


def _normalize_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _first_header_index(headers: Sequence[str], aliases: Iterable[str]) -> int | None:
    alias_set = set(aliases)
    return next((index for index, value in enumerate(headers) if value in alias_set), None)


def _cell_float(row: Sequence[str], index: int | None) -> float | None:
    if index is None or index >= len(row):
        return None
    try:
        value = float(row[index])
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _compact(value: float) -> float:
    return round(float(value), 8)


def _regular_file_state(path: Path) -> str:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unsafe"
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        return "unsafe"
    return "ok"


def _read_gro_atoms(path: Path) -> list[GroAtom]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) < 3:
        raise ValueError("GRO file is incomplete")
    try:
        atom_count = int(lines[1].strip())
    except ValueError as exc:
        raise ValueError("GRO atom count is invalid") from exc
    if atom_count < 1 or len(lines) < atom_count + 3:
        raise ValueError("GRO atom records are incomplete")
    atoms: list[GroAtom] = []
    last_residue: tuple[int, str] | None = None
    residue_key = 0
    for line in lines[2 : atom_count + 2]:
        if len(line) < 20:
            raise ValueError("GRO atom line is too short")
        try:
            residue_id = int(line[0:5])
            atom_id = int(line[15:20])
        except ValueError as exc:
            raise ValueError("GRO atom identity is invalid") from exc
        residue_name = line[5:10].strip() or "UNK"
        atom_name = line[10:15].strip() or "X"
        residue = (residue_id, residue_name)
        if residue != last_residue:
            residue_key += 1
            last_residue = residue
        atoms.append(
            GroAtom(
                atom_id=atom_id,
                residue_id=residue_id,
                residue_key=residue_key,
                residue_name=residue_name,
                atom_name=atom_name,
            )
        )
    return atoms


def _orthorhombic_box(raw_box: Any) -> tuple[float, float, float] | None:
    try:
        values = (float(raw_box[0]), float(raw_box[2]), float(raw_box[5]))
    except (TypeError, ValueError, IndexError):
        return None
    if not all(math.isfinite(value) and value > 0 for value in values):
        return None
    return values


def _all_finite(values: Any) -> bool:
    try:
        import numpy as np
    except ImportError:
        return all(math.isfinite(float(value)) for row in values for value in row)
    return bool(np.isfinite(values).all())


def _wrap_residues_in_place(
    coordinates: Any,
    atoms: Sequence[GroAtom],
    box: tuple[float, float, float],
) -> None:
    start = 0
    while start < len(atoms):
        residue_key = atoms[start].residue_key
        stop = start + 1
        while stop < len(atoms) and atoms[stop].residue_key == residue_key:
            stop += 1
        for axis, length in enumerate(box):
            anchor = float(coordinates[start, axis])
            for atom_index in range(start + 1, stop):
                delta = float(coordinates[atom_index, axis]) - anchor
                image = math.floor(delta / length + 0.5)
                coordinates[atom_index, axis] -= image * length
            center = float(coordinates[start:stop, axis].mean())
            shift = math.floor(center / length) * length
            coordinates[start:stop, axis] -= shift
        start = stop


def _box_payload(box: tuple[float, float, float] | None) -> dict[str, Any] | None:
    if box is None:
        return None
    return {
        "lx": _compact(box[0]),
        "ly": _compact(box[1]),
        "lz": _compact(box[2]),
        "unit": "angstrom",
    }


def _trajectory_atom_payload(atom: GroAtom) -> dict[str, Any]:
    return {
        "atom_id": atom.atom_id,
        "chain_id": atom.residue_key,
        "atom_type": atom.atom_name,
        "residue_name": atom.residue_name,
        "element": _element_from_atom_name(atom.atom_name),
    }


def _encode_trajectory_coordinates(
    coordinates: Any,
    *,
    scale: float,
) -> tuple[str, int, int]:
    import numpy as np

    values = np.asarray(coordinates, dtype=np.float64)
    if values.ndim != 3 or values.shape[0] < 1 or values.shape[1] < 1 or values.shape[2] != 3:
        raise ValueError("trajectory coordinate shape is invalid")
    if not math.isfinite(scale) or scale <= 0 or not bool(np.isfinite(values).all()):
        raise ValueError("trajectory coordinates are invalid")

    quantized = np.rint(values / scale).astype(np.int64)
    encoded = np.empty(quantized.shape, dtype="<i2")
    first = quantized[0]
    if bool(np.any(first < -32768)) or bool(np.any(first > 32767)):
        raise OverflowError("first trajectory frame exceeds int16 range")
    encoded[0] = first.astype("<i2")
    if len(quantized) > 1:
        deltas = np.diff(quantized, axis=0)
        if bool(np.any(deltas < -32768)) or bool(np.any(deltas > 32767)):
            raise OverflowError("trajectory frame delta exceeds int16 range")
        encoded[1:] = deltas.astype("<i2")

    raw = encoded.tobytes(order="C")
    compressed = gzip.compress(raw, compresslevel=6, mtime=0)
    return base64.b64encode(compressed).decode("ascii"), len(raw), len(compressed)


def _whole_residue_stratified_indices(
    atoms: Sequence[GroAtom],
    limit: int,
) -> tuple[list[int], str]:
    if len(atoms) <= limit:
        return list(range(len(atoms))), "all_atoms"

    residues: list[tuple[str, list[int]]] = []
    start = 0
    while start < len(atoms):
        residue_key = atoms[start].residue_key
        stop = start + 1
        while stop < len(atoms) and atoms[stop].residue_key == residue_key:
            stop += 1
        residues.append((atoms[start].residue_name, list(range(start, stop))))
        start = stop

    if not residues or min(len(indices) for _, indices in residues) > limit:
        return _stratified_atom_indices(atoms, limit), "atom_type_stratified_fallback"

    components: dict[str, list[list[int]]] = {}
    for residue_name, indices in residues:
        components.setdefault(residue_name, []).append(indices)
    total_atoms = len(atoms)
    target_atoms = {
        name: limit * sum(len(indices) for indices in component_residues) / total_atoms
        for name, component_residues in components.items()
    }
    selected_residues: dict[str, list[list[int]]] = {name: [] for name in components}
    selected_atoms = {name: 0 for name in components}
    component_offsets = {name: 0 for name in components}
    used = 0

    while True:
        candidates: list[tuple[float, int, str, list[int]]] = []
        for order, (name, component_residues) in enumerate(components.items()):
            offset = component_offsets[name]
            if offset >= len(component_residues):
                continue
            residue = component_residues[offset]
            if used + len(residue) > limit:
                continue
            coverage = selected_atoms[name] / max(target_atoms[name], 1.0)
            candidates.append((coverage, order, name, residue))
        if not candidates:
            break
        _, _, name, residue = min(candidates, key=lambda item: (item[0], item[1]))
        selected_residues[name].append(residue)
        selected_atoms[name] += len(residue)
        component_offsets[name] += 1
        used += len(residue)

    selected = sorted(
        index
        for component_residues in selected_residues.values()
        for residue in component_residues
        for index in residue
    )
    if not selected:
        return _stratified_atom_indices(atoms, limit), "atom_type_stratified_fallback"
    return selected, "whole_residue_component_stratified"


def _stratified_atom_indices(atoms: Sequence[GroAtom], limit: int) -> list[int]:
    if len(atoms) <= limit:
        return list(range(len(atoms)))
    groups: dict[tuple[str, str], list[int]] = {}
    for index, atom in enumerate(atoms):
        groups.setdefault((atom.residue_name, atom.atom_name), []).append(index)
    names = list(groups)
    if len(names) >= limit:
        return sorted(groups[name][0] for name in names[:limit])

    quotas = {name: 1 for name in names}
    remaining = limit - len(names)
    capacity = sum(max(0, len(groups[name]) - 1) for name in names)
    if capacity:
        fractions: list[tuple[float, tuple[str, str]]] = []
        assigned = 0
        for name in names:
            available = len(groups[name]) - 1
            exact = remaining * available / capacity
            extra = min(available, int(math.floor(exact)))
            quotas[name] += extra
            assigned += extra
            fractions.append((exact - extra, name))
        for _, name in sorted(fractions, key=lambda item: (-item[0], names.index(item[1]))):
            if assigned >= remaining:
                break
            if quotas[name] < len(groups[name]):
                quotas[name] += 1
                assigned += 1

    selected: set[int] = set()
    for name in names:
        selected.update(_evenly_spaced_values(groups[name], quotas[name]))
    if len(selected) < limit:
        unselected = [index for index in range(len(atoms)) if index not in selected]
        selected.update(_evenly_spaced_values(unselected, limit - len(selected)))
    return sorted(selected)[:limit]


def _evenly_spaced_values(values: Sequence[int], count: int) -> list[int]:
    if count <= 0 or not values:
        return []
    if count >= len(values):
        return list(values)
    if count == 1:
        return [values[len(values) // 2]]
    chosen = {
        values[round(position * (len(values) - 1) / (count - 1))]
        for position in range(count)
    }
    if len(chosen) < count:
        chosen.update(value for value in values if value not in chosen)
    return sorted(chosen)[:count]


def _element_from_atom_name(atom_name: str) -> str:
    letters = re.sub(r"[^A-Za-z]", "", atom_name)
    if not letters:
        return "X"
    first = letters[0].upper()
    two = first + letters[1:2].lower()
    if len(letters) > 1 and two in _ELEMENT_SYMBOLS:
        return two
    return first
