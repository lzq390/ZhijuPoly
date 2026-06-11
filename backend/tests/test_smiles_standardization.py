from __future__ import annotations

from fastapi.testclient import TestClient


def test_standardize_smiles_returns_rdkit_canonical_smiles(test_app) -> None:
    client = TestClient(test_app)

    response = client.post("/api/v1/structure/standardize-smiles", json={"smiles": "C(C)O"})

    assert response.status_code == 200
    data = response.json()
    assert data["input_smiles"] == "C(C)O"
    assert data["standardized_smiles"] == "CCO"
    assert data["changed"] is True
    assert data["query_time_ms"] >= 0


def test_standardize_smiles_preserves_polymer_dummy_atoms(test_app) -> None:
    client = TestClient(test_app)

    response = client.post("/api/v1/structure/standardize-smiles", json={"smiles": "*CC*"})

    assert response.status_code == 200
    data = response.json()
    assert data["standardized_smiles"] == "*CC*"
    assert data["changed"] is False


def test_standardize_smiles_rejects_invalid_smiles(test_app) -> None:
    client = TestClient(test_app)

    response = client.post("/api/v1/structure/standardize-smiles", json={"smiles": "not-a-smiles"})

    assert response.status_code == 422
    assert response.json()["detail"] == "invalid smiles: not-a-smiles"
