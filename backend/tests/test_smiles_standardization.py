from __future__ import annotations

import pytest

from test_api import api_database, auth_database, test_app


def test_standardize_smiles_returns_rdkit_canonical_smiles(test_app) -> None:
    client = test_app.state.test_client

    response = client.post("/api/v1/structure/standardize-smiles", json={"smiles": "C(C)O"})

    assert response.status_code == 200
    data = response.json()
    assert data["input_smiles"] == "C(C)O"
    assert data["standardized_smiles"] == "CCO"
    assert data["changed"] is True
    assert data["query_time_ms"] >= 0


def test_standardize_smiles_preserves_polymer_dummy_atoms(test_app) -> None:
    client = test_app.state.test_client

    response = client.post("/api/v1/structure/standardize-smiles", json={"smiles": "*CC*"})

    assert response.status_code == 200
    data = response.json()
    assert data["standardized_smiles"] == "*CC*"
    assert data["changed"] is False


def test_standardize_smiles_rejects_invalid_smiles(test_app) -> None:
    client = test_app.state.test_client

    response = client.post("/api/v1/structure/standardize-smiles", json={"smiles": "not-a-smiles"})

    assert response.status_code == 422
    assert response.json()["detail"] == "invalid smiles: not-a-smiles"


def test_render_structure_2d_returns_rdkit_svg(test_app) -> None:
    client = test_app.state.test_client

    response = client.post("/api/v1/structure/2d", json={"smiles": "*CC*"})

    assert response.status_code == 200
    structure_svg = response.json()["structure_svg"]
    assert structure_svg.startswith("<?xml")
    assert "<svg" in structure_svg


def test_render_structure_2d_rejects_invalid_smiles(test_app) -> None:
    client = test_app.state.test_client

    response = client.post("/api/v1/structure/2d", json={"smiles": "not-a-smiles"})

    assert response.status_code == 422
    assert response.json()["detail"] == "invalid smiles: not-a-smiles"


def test_structure_descriptors_describe_the_repeat_unit_without_connection_points(test_app) -> None:
    client = test_app.state.test_client

    response = client.post(
        "/api/v1/structure/descriptors",
        json={"smiles": "*CC(*)c1ccccc1 |$star_e;;;star_e;;;;;;$|"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["canonical_smiles"] == "*CC(*)c1ccccc1"
    assert data["formula"] == "C8H8"
    assert data["molecular_weight"] == pytest.approx(104.15, abs=0.01)
    assert data["heavy_atom_count"] == 8
    assert data["attachment_point_count"] == 2
    assert data["aromatic_ring_count"] == 1
    assert data["ring_count"] == 1
    assert data["heteroatom_count"] == 0
    assert data["tpsa"] == 0
    assert 0 <= data["fraction_csp3"] <= 1


def test_structure_descriptors_count_polar_groups(test_app) -> None:
    client = test_app.state.test_client

    response = client.post("/api/v1/structure/descriptors", json={"smiles": "*CC(C)(*)C(=O)OC"})

    assert response.status_code == 200
    data = response.json()
    assert data["formula"] == "C5H8O2"
    assert data["heteroatom_count"] == 2
    assert data["hbond_acceptor_count"] == 2
    assert data["hbond_donor_count"] == 0
    assert data["tpsa"] == pytest.approx(26.3, abs=0.1)


def test_structure_descriptors_reject_invalid_smiles(test_app) -> None:
    client = test_app.state.test_client

    response = client.post("/api/v1/structure/descriptors", json={"smiles": "not-a-smiles"})

    assert response.status_code == 422
    assert response.json()["detail"] == "invalid smiles: not-a-smiles"
