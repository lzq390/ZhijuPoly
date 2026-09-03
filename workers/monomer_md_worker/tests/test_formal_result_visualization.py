from __future__ import annotations

import base64
import gzip
from pathlib import Path

import numpy as np
import pytest
from MDAnalysis.lib.formats.libdcd import DCDFile

from workers.monomer_md_worker.app.formal_result_visualization import (
    KJ_TO_KCAL,
    PROTOCOL_DEFAULT_STAGE,
    PROTOCOL_STAGE_SPECS,
    build_formal_visualization,
    deterministic_minmax_downsample,
    deterministic_trajectory_frame_indices,
    parse_openmm_state_csv,
)


def _write_state(path: Path, *, density: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    density_header = ',"Density (g/mL)"' if density else ""
    density_first = ",1.0" if density else ""
    density_last = ",1.1" if density else ""
    path.write_text(
        '#"Step","Time (ps)","Potential Energy (kJ/mole)",'
        '"Kinetic Energy (kJ/mole)","Total Energy (kJ/mole)","Temperature (K)"'
        + density_header
        + "\n"
        + "10,0.02,-140,40,-100,298"
        + density_first
        + "\n"
        + "20,0.04,-170,50,-120,299"
        + density_last
        + "\n",
        encoding="utf-8",
    )


def _write_gro(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "fixture\n"
        "4\n"
        "    1SOL      C    1   0.100   0.200   0.300\n"
        "    1SOL      H    2   0.200   0.200   0.300\n"
        "    2ION     Li    3   0.300   0.400   0.500\n"
        "    2ION      F    4   0.400   0.400   0.500\n"
        "   2.00000   2.00000   2.00000\n",
        encoding="utf-8",
    )


def _write_dcd(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    first = np.array(
        [[1, 2, 3], [2, 2, 3], [3, 4, 5], [4, 4, 5]],
        dtype=np.float32,
    )
    final = np.array(
        [[19.5, 2, 3], [0.5, 2, 3], [-7, 4, 5], [-6, 4, 5]],
        dtype=np.float32,
    )
    box = np.array([20, 90, 20, 90, 90, 20], dtype=np.float64)
    with DCDFile(str(path), "w") as trajectory:
        trajectory.write_header("fixture", 4, 0, 1, 1.0, True)
        trajectory.write(first, box)
        trajectory.write(final, box)


def _write_protocol_sources(root: Path, protocol: str) -> None:
    for spec in PROTOCOL_STAGE_SPECS[protocol]:
        _write_state(root / spec.state_csv, density=not spec.density_optional)
        _write_gro(root / spec.topology_gro)
        _write_dcd(root / spec.trajectory_dcd)


@pytest.mark.parametrize(
    ("protocol", "stage_ids"),
    [
        ("Density", ["npt"]),
        ("Transport", ["npt", "nvt"]),
        ("HVap", ["liquid_npt", "gas_nvt"]),
        ("Dielectric", ["npt", "nvt"]),
        ("Compressibility", ["npt"]),
    ],
)
def test_formal_visualization_maps_protocol_stages_and_real_dcd(
    tmp_path: Path,
    protocol: str,
    stage_ids: list[str],
) -> None:
    _write_protocol_sources(tmp_path, protocol)

    visualization = build_formal_visualization(protocol, tmp_path)

    assert visualization["schema_version"] == 3
    assert visualization["status"] == "complete"
    assert visualization["default_stage_id"] == PROTOCOL_DEFAULT_STAGE[protocol]
    assert [stage["stage_id"] for stage in visualization["stages"]] == stage_ids
    for stage in visualization["stages"]:
        assert stage["temperature_series"]["points"][-1] == {
            "step": 20,
            "time_ps": 0.04,
            "value": 299.0,
        }
        assert stage["energy_series"]["points"][-1]["value"] == round(
            -120 * KJ_TO_KCAL,
            8,
        )
        assert stage["potential_energy_series"]["points"][-1]["value"] == round(
            -170 * KJ_TO_KCAL,
            8,
        )
        assert stage["kinetic_energy_series"]["points"][-1]["value"] == round(
            50 * KJ_TO_KCAL,
            8,
        )
        preview = stage["trajectory_preview"]
        timeline = stage["trajectory_timeline"]
        assert preview["frame_index"] == 1
        assert preview["time_ps"] == 0.04
        assert preview["total_atoms"] == 4
        assert preview["sampled_points"] == 4
        assert preview["coordinate_unit"] == "angstrom"
        assert timeline["schema_version"] == 1
        assert timeline["source_frame_count"] == 2
        assert timeline["sampled_frame_count"] == 2
        assert timeline["sampled_points"] == 4
        assert timeline["coordinate_encoding"] == "int16-delta-gzip-base64"
        assert [frame["frame_index"] for frame in timeline["frames"]] == [0, 1]
        raw = gzip.decompress(base64.b64decode(timeline["coordinates"]))
        assert len(raw) == timeline["decoded_byte_length"] == 2 * 4 * 3 * 2
        encoded = np.frombuffer(raw, dtype="<i2").astype(np.int64).reshape(2, 4, 3)
        encoded[1] += encoded[0]
        final_coordinates = encoded[1] * timeline["coordinate_scale"]
        assert final_coordinates[0, 0] == pytest.approx(preview["points"][0]["x"], abs=0.01)
        if preview["box"] is not None:
            assert abs(preview["points"][0]["x"] - preview["points"][1]["x"]) == 1.0
    if protocol == "HVap":
        assert visualization["stages"][0]["trajectory_preview"]["box"]["lx"] == 20.0
        assert visualization["stages"][1]["trajectory_preview"]["box"] is None
        assert "density_series" not in visualization["stages"][1]


def test_state_parser_downsamples_deterministically_and_preserves_extrema(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.csv"
    path.write_text(
        '#"Step","Time (ps)","Potential Energy (kJ/mol)","Kinetic Energy (kJ/mol)",'
        '"Total Energy (kJ/mol)","Temperature (K)","Density (g/mL)"\n'
        + "\n".join(
            f"{index},{index / 10},{-index - 10},10,{-index},{298 + (index % 5)},"
            f"{1000 if index == 777 else index / 1000}"
            for index in range(1, 2_006)
        )
        + "\n",
        encoding="utf-8",
    )

    first = parse_openmm_state_csv(path)
    second = parse_openmm_state_csv(path)

    assert first == second
    density = first.density_series
    assert density is not None
    assert density["source_point_count"] == 2_005
    assert len(density["points"]) <= 1_000
    assert density["points"][0]["step"] == 1
    assert density["points"][-1]["step"] == 2_005
    assert any(point["value"] == 1000 for point in density["points"])


def test_partial_visualization_keeps_curves_when_trajectory_is_missing(
    tmp_path: Path,
) -> None:
    _write_state(tmp_path / "outputs/npt_state.csv")

    visualization = build_formal_visualization("Density", tmp_path)

    assert visualization["status"] == "partial"
    stage = visualization["stages"][0]
    assert stage["density_series"]["points"]
    assert stage["trajectory_preview"] is None
    assert stage["warnings"] == [
        "TRAJECTORY_TOPOLOGY_MISSING",
        "TRAJECTORY_DCD_MISSING",
    ]


def test_corrupt_trajectory_is_partial_and_does_not_discard_curves(
    tmp_path: Path,
) -> None:
    _write_state(tmp_path / "outputs/npt_state.csv")
    _write_gro(tmp_path / "params/solvent_salt.gro")
    trajectory = tmp_path / "outputs/npt.dcd"
    trajectory.parent.mkdir(parents=True, exist_ok=True)
    trajectory.write_bytes(b"not a DCD")

    visualization = build_formal_visualization("Density", tmp_path)

    assert visualization["status"] == "partial"
    stage = visualization["stages"][0]
    assert stage["density_series"]["points"]
    assert stage["trajectory_preview"] is None
    assert stage["warnings"] == ["TRAJECTORY_DCD_UNREADABLE"]


def test_multistage_time_axes_are_kept_independent(tmp_path: Path) -> None:
    _write_state(tmp_path / "outputs/npt_state.csv")
    nvt_state = tmp_path / "outputs/nvt_state.csv"
    nvt_state.write_text(
        '#"Step","Time (ps)","Potential Energy (kJ/mole)","Kinetic Energy (kJ/mole)",'
        '"Total Energy (kJ/mole)","Temperature (K)","Density (g/mL)"\n'
        "5,9.5,-130,40,-90,301,0.9\n",
        encoding="utf-8",
    )

    visualization = build_formal_visualization("Transport", tmp_path)

    npt, nvt = visualization["stages"]
    assert npt["temperature_series"]["points"][-1]["time_ps"] == 0.04
    assert nvt["temperature_series"]["points"] == [
        {"step": 5, "time_ps": 9.5, "value": 301.0}
    ]


def test_hvap_gas_stage_requires_the_gas_topology(tmp_path: Path) -> None:
    _write_protocol_sources(tmp_path, "HVap")
    (tmp_path / "params/solvent_salt_gas.gro").unlink()

    visualization = build_formal_visualization("HVap", tmp_path)

    liquid, gas = visualization["stages"]
    assert liquid["trajectory_preview"] is not None
    assert gas["trajectory_preview"] is None
    assert gas["warnings"] == ["TRAJECTORY_TOPOLOGY_MISSING"]


def test_minmax_downsample_rejects_an_invalid_limit() -> None:
    with pytest.raises(ValueError, match="at least 2"):
        deterministic_minmax_downsample([{"value": 1}, {"value": 2}], 1)


def test_trajectory_frame_selection_keeps_60_real_frames_and_salient_change() -> None:
    rows = [
        {
            "time_ps": float(index + 1),
            "density": 1.0 if index != 777 else 2.0,
            "temperature": 300.0,
            "energy": -100.0,
        }
        for index in range(3_000)
    ]

    first = deterministic_trajectory_frame_indices(3_000, 60, rows)
    second = deterministic_trajectory_frame_indices(3_000, 60, rows)

    assert first == second
    assert len(first) == 60
    assert first == sorted(set(first))
    assert first[0] == 0
    assert first[-1] == 2_999
    assert set(range(10)).issubset(first)
    assert 777 in first
