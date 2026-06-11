from __future__ import annotations

from fastapi.testclient import TestClient


def test_md_demo_defaults_returns_fixture_metadata(test_app):
    client = TestClient(test_app)

    response = client.get("/api/v1/md-demo/defaults")

    assert response.status_code == 200
    data = response.json()
    assert data["default_request"]["temperature"] == 300.0
    assert data["default_request"]["pressure"] == 1.0
    assert data["default_request"]["forcefield"] == "GAFF2_mod"
    assert [stage["stage_id"] for stage in data["available_stages"]] == ["eq1", "eq2", "eq3"]
    assert data["summary"]["primary_stage"] == "eq3"
    assert data["fixture_metadata"]["fixture_version"] == 1


def test_md_demo_run_rejects_blank_smiles(test_app):
    client = TestClient(test_app)

    response = client.post(
        "/api/v1/md-demo/run",
        json={
            "smiles": "   ",
            "temperature": 300.0,
            "pressure": 1.0,
            "n_atom": 1000,
            "n_chain": 10,
            "forcefield": "GAFF2_mod",
        },
    )

    assert response.status_code == 422


def test_md_demo_run_returns_completed_fixture_result(test_app):
    client = TestClient(test_app)

    response = client.post(
        "/api/v1/md-demo/run",
        json={
            "smiles": "*C(=C(*)C(F)(F)F)c1ccc(CCCC)cc1",
            "temperature": 300.0,
            "pressure": 1.0,
            "n_atom": 1000,
            "n_chain": 10,
            "forcefield": "GAFF2_mod",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "completed"
    assert data["run_id"].startswith("md-demo-")
    assert data["input"]["smiles"] == "*C(=C(*)C(F)(F)F)c1ccc(CCCC)cc1"
    assert len(data["stages"]) == 3
    assert data["summary"]["n_atoms"] == 9940
    assert data["density_series"]["points"]
    assert len(data["thermo_series"]) >= 2
    assert data["trajectory_preview"]["sampled_points"] == 9940
    assert len(data["trajectory_preview"]["points"]) == 9940
    assert data["atom_distance_series"] is None


def test_md_demo_atom_distance_rejects_same_atom(test_app):
    client = TestClient(test_app)

    response = client.post(
        "/api/v1/md-demo/atom-distance",
        json={"atom_id_1": 1, "atom_id_2": 1, "use_pbc": True},
    )

    assert response.status_code == 422


def test_md_demo_atom_distance_rejects_missing_atom(test_app):
    client = TestClient(test_app)

    response = client.post(
        "/api/v1/md-demo/atom-distance",
        json={"atom_id_1": 1, "atom_id_2": 999999, "use_pbc": True},
    )

    assert response.status_code == 404


def test_md_demo_atom_distance_returns_series_for_selected_atoms(test_app):
    client = TestClient(test_app)

    response = client.post(
        "/api/v1/md-demo/atom-distance",
        json={"atom_id_1": 1, "atom_id_2": 65, "use_pbc": True},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["atom_1"]["atom_id"] == 1
    assert data["atom_2"]["atom_id"] == 65
    assert data["stats"]["n_atoms"] == 9940
    assert data["stats"]["n_frames"] == 200
    assert data["stats"]["source_n_frames"] == 5001
    assert len(data["frames"]) == len(data["time_ps"]) == len(data["distance"]) == 200
    assert len(data["series"]["points"]) == 200
    assert data["stats"]["min_distance"] <= min(data["distance"])
    assert data["stats"]["max_distance"] >= max(data["distance"])
