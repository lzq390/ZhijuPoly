from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.utils.exceptions import ModelArtifactError


REQUIRED_SECOND_MONOMER_TARGETS = ("polyimide", "polyester", "polyamide", "polyurethane")
OPTIONAL_SECOND_MONOMER_TARGETS = ("polyolefin", "polyether", "polyoxazolidone", "all")


def _settings(tmp_path: Path, *, smipoly_enabled: bool = True) -> Settings:
    return Settings(
        sqlite_db_path=str(tmp_path / "polyprop.db"),
        csv_source_path=str(tmp_path / "source.csv"),
        allowed_origins="http://localhost:5173",
        model_enabled=False,
        retro_model_enabled=False,
        smipoly_enabled=smipoly_enabled,
    )


def test_monomer_polymerization_status_reports_disabled_service(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path, smipoly_enabled=False)))

    response = client.get("/api/v1/monomer-polymerization/status")

    assert response.status_code == 200
    data = response.json()
    assert data["enabled"] is False
    assert data["available"] is False
    assert data["default_target_class"] == "polyimide"
    assert data["target_requirements"]["polyimide"]["monomer_b_required"] is True


def test_monomer_polymerization_post_reports_disabled_service(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path, smipoly_enabled=False)))

    response = client.post(
        "/api/v1/monomer-polymerization",
        json={"monomer_a_smiles": "CCO", "target_class": "polyimide"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "monomer polymerization service is disabled"


def test_monomer_polymerization_status_reports_missing_smipoly(tmp_path: Path, monkeypatch) -> None:
    def missing_runtime():
        raise ModelArtifactError("SMiPoly is not installed or cannot load its rule files")

    monkeypatch.setattr("app.services.monomer_polymerization._load_smipoly_runtime", missing_runtime)
    client = TestClient(create_app(_settings(tmp_path)))

    response = client.get("/api/v1/monomer-polymerization/status")

    assert response.status_code == 200
    data = response.json()
    assert data["enabled"] is True
    assert data["available"] is False
    assert "SMiPoly" in data["message"]


def test_monomer_polymerization_status_reports_available_service(tmp_path: Path, monkeypatch) -> None:
    runtime = SimpleNamespace(
        pd=pd,
        monc=SimpleNamespace(),
        polg=SimpleNamespace(Ps_classL={"polyimide": 8, "polyether": 12}),
        target_classes=("polyimide", "polyether"),
    )
    monkeypatch.setattr("app.services.monomer_polymerization._load_smipoly_runtime", lambda: runtime)
    client = TestClient(create_app(_settings(tmp_path)))

    response = client.get("/api/v1/monomer-polymerization/status")

    assert response.status_code == 200
    data = response.json()
    assert data["enabled"] is True
    assert data["available"] is True
    assert data["default_target_class"] == "polyimide"
    assert data["max_results_limit"] == 20
    assert data["available_target_classes"] == ["polyether", "polyimide", "all"]
    assert data["target_requirements"]["polyimide"]["min_monomers"] == 2
    assert data["target_requirements"]["polyimide"]["monomer_b_required"] is True
    assert data["target_requirements"]["polyether"]["min_monomers"] == 1
    assert data["target_requirements"]["polyether"]["monomer_b_required"] is False
    assert data["message"] == "SMiPoly rule polymerization service is available"


@pytest.mark.parametrize(
    ("target_class", "expected_min_monomers", "expected_required"),
    [
        *[(target_class, 2, True) for target_class in REQUIRED_SECOND_MONOMER_TARGETS],
        *[(target_class, 1, False) for target_class in OPTIONAL_SECOND_MONOMER_TARGETS],
    ],
)
def test_monomer_polymerization_status_exposes_target_requirement_matrix(
    tmp_path: Path,
    monkeypatch,
    target_class: str,
    expected_min_monomers: int,
    expected_required: bool,
) -> None:
    rule_classes = {
        "polyolefin": 1,
        "polyester": 2,
        "polyether": 3,
        "polyamide": 4,
        "polyimide": 5,
        "polyurethane": 6,
        "polyoxazolidone": 7,
    }
    runtime = SimpleNamespace(
        pd=pd,
        monc=SimpleNamespace(),
        polg=SimpleNamespace(Ps_classL=rule_classes),
        target_classes=tuple(rule_classes.keys()),
    )
    monkeypatch.setattr("app.services.monomer_polymerization._load_smipoly_runtime", lambda: runtime)
    client = TestClient(create_app(_settings(tmp_path)))

    response = client.get("/api/v1/monomer-polymerization/status")

    assert response.status_code == 200
    requirement = response.json()["target_requirements"][target_class]
    assert requirement["min_monomers"] == expected_min_monomers
    assert requirement["max_monomers"] == 2
    assert requirement["monomer_b_required"] is expected_required


def test_monomer_polymerization_post_reports_missing_smipoly_as_503(tmp_path: Path, monkeypatch) -> None:
    def missing_runtime():
        raise ModelArtifactError("SMiPoly is not installed or cannot load its rule files")

    monkeypatch.setattr("app.services.monomer_polymerization._load_smipoly_runtime", missing_runtime)
    client = TestClient(create_app(_settings(tmp_path)))

    response = client.post(
        "/api/v1/monomer-polymerization",
        json={"monomer_a_smiles": "CCO", "monomer_b_smiles": "CCN", "target_class": "polyimide"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "monomer polymerization service is unavailable"


@pytest.mark.parametrize("target_class", REQUIRED_SECOND_MONOMER_TARGETS)
def test_monomer_polymerization_rejects_missing_required_second_monomer_before_loading_smipoly(
    tmp_path: Path,
    monkeypatch,
    target_class: str,
) -> None:
    def fail_if_loaded():
        raise AssertionError("SMiPoly should not load when monomer B is required")

    monkeypatch.setattr("app.services.monomer_polymerization._load_smipoly_runtime", fail_if_loaded)
    client = TestClient(create_app(_settings(tmp_path)))

    response = client.post(
        "/api/v1/monomer-polymerization",
        json={"monomer_a_smiles": "CCO", "target_class": target_class},
    )

    assert response.status_code == 422
    assert "requires monomer_b_smiles" in response.json()["detail"]


@pytest.mark.parametrize(
    ("target_class", "expected_targ"),
    [
        ("polyolefin", ["polyolefin"]),
        ("polyether", ["polyether"]),
        ("polyoxazolidone", ["polyoxazolidone"]),
        ("all", ["all"]),
    ],
)
def test_monomer_polymerization_allows_single_monomer_for_optional_targets(
    tmp_path: Path,
    monkeypatch,
    target_class: str,
    expected_targ: list[str],
) -> None:
    captured: dict[str, object] = {}

    class FakeMonc:
        @staticmethod
        def moncls(df, smiColn: str, dsp_rsl: bool = False):
            assert smiColn == "SMILES"
            assert dsp_rsl is False
            captured["classified_smiles"] = df["SMILES"].tolist()
            return df.assign(smip_cand_mons=df["SMILES"])

    class FakePolg:
        Ps_classL = {"polyolefin": 1, "polyether": 2, "polyoxazolidone": 3}

        @staticmethod
        def biplym(df, targ, dsp_rsl: bool = False):
            assert dsp_rsl is False
            captured["generated_smiles"] = df["SMILES"].tolist()
            captured["targ"] = targ
            return pd.DataFrame(columns=["mon1", "mon2", "polym", "polymer_class", "Ps_rxnL", "reactset"])

    runtime = SimpleNamespace(
        pd=pd,
        monc=FakeMonc,
        polg=FakePolg,
        target_classes=("polyolefin", "polyether", "polyoxazolidone"),
    )
    monkeypatch.setattr("app.services.monomer_polymerization._load_smipoly_runtime", lambda: runtime)
    client = TestClient(create_app(_settings(tmp_path)))

    response = client.post(
        "/api/v1/monomer-polymerization",
        json={"monomer_a_smiles": "CCO", "target_class": target_class},
    )

    assert response.status_code == 200
    data = response.json()
    assert len(data["input_monomers"]) == 1
    assert captured["classified_smiles"] == ["CCO"]
    assert captured["generated_smiles"] == ["CCO"]
    assert captured["targ"] == expected_targ


def test_monomer_polymerization_rejects_invalid_smiles_before_loading_smipoly(tmp_path: Path, monkeypatch) -> None:
    def fail_if_loaded():
        raise AssertionError("SMiPoly should not load for invalid SMILES")

    monkeypatch.setattr("app.services.monomer_polymerization._load_smipoly_runtime", fail_if_loaded)
    client = TestClient(create_app(_settings(tmp_path)))

    response = client.post(
        "/api/v1/monomer-polymerization",
        json={"monomer_a_smiles": "not-a-smiles", "monomer_b_smiles": "CCN", "target_class": "polyimide"},
    )

    assert response.status_code == 422
    assert "invalid smiles" in response.json()["detail"]


def test_monomer_polymerization_rejects_dummy_atom_inputs(tmp_path: Path, monkeypatch) -> None:
    def fail_if_loaded():
        raise AssertionError("SMiPoly should not load for dummy atom input")

    monkeypatch.setattr("app.services.monomer_polymerization._load_smipoly_runtime", fail_if_loaded)
    client = TestClient(create_app(_settings(tmp_path)))

    response = client.post(
        "/api/v1/monomer-polymerization",
        json={"monomer_a_smiles": "*CC*", "monomer_b_smiles": "CCN", "target_class": "polyimide"},
    )

    assert response.status_code == 422
    assert "without attachment points" in response.json()["detail"]


def test_monomer_polymerization_returns_candidates(tmp_path: Path, monkeypatch) -> None:
    class FakeMonc:
        @staticmethod
        def moncls(df, smiColn: str, dsp_rsl: bool = False):
            assert smiColn == "SMILES"
            assert dsp_rsl is False
            return df.assign(smip_cand_mons=df["SMILES"])

    class FakePolg:
        Ps_classL = {"polyimide": 8, "polyether": 12}

        @staticmethod
        def biplym(df, targ, dsp_rsl: bool = False):
            assert targ == ["polyimide"]
            assert dsp_rsl is False
            return pd.DataFrame(
                [
                    {
                        "mon1": "CCO",
                        "mon2": "CCN",
                        "polym": "*CCOC(*)CN",
                        "polymer_class": "polyimide",
                        "Ps_rxnL": 110,
                        "reactset": ("CCO", "CCN"),
                    }
                ]
            )

    runtime = SimpleNamespace(
        pd=pd,
        monc=FakeMonc,
        polg=FakePolg,
        target_classes=("polyimide", "polyether"),
    )
    monkeypatch.setattr("app.services.monomer_polymerization._load_smipoly_runtime", lambda: runtime)
    monkeypatch.setattr("app.services.monomer_polymerization.generate_2d_svg", lambda smiles: "<svg />")
    client = TestClient(create_app(_settings(tmp_path)))

    response = client.post(
        "/api/v1/monomer-polymerization",
        json={
            "monomer_a_smiles": "CCO",
            "monomer_b_smiles": "CCN",
            "target_class": "polyimide",
            "max_results": 5,
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["target_class"] == "polyimide"
    assert data["total"] == 1
    assert data["results"][0]["polymer_smiles"] == "*CCOC(*)CN"
    assert data["results"][0]["reaction_id"] == 110
    assert data["results"][0]["structure_svg"] == "<svg />"


def test_monomer_polymerization_returns_empty_result_warning(tmp_path: Path, monkeypatch) -> None:
    class FakeMonc:
        @staticmethod
        def moncls(df, smiColn: str, dsp_rsl: bool = False):
            return df.assign(smip_cand_mons=df["SMILES"])

    class FakePolg:
        Ps_classL = {"polyimide": 8}

        @staticmethod
        def biplym(df, targ, dsp_rsl: bool = False):
            return pd.DataFrame(columns=["mon1", "mon2", "polym", "polymer_class", "Ps_rxnL", "reactset"])

    runtime = SimpleNamespace(
        pd=pd,
        monc=FakeMonc,
        polg=FakePolg,
        target_classes=("polyimide",),
    )
    monkeypatch.setattr("app.services.monomer_polymerization._load_smipoly_runtime", lambda: runtime)
    client = TestClient(create_app(_settings(tmp_path)))

    response = client.post(
        "/api/v1/monomer-polymerization",
        json={"monomer_a_smiles": "CCO", "target_class": "polyether"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 0
    assert data["results"] == []
    assert any("no polymer candidates" in warning for warning in data["warnings"])


def test_monomer_polymerization_filters_non_input_rows_and_truncates_results(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class FakeMonc:
        @staticmethod
        def moncls(df, smiColn: str, dsp_rsl: bool = False):
            return df.assign(smip_cand_mons=df["SMILES"])

    class FakePolg:
        Ps_classL = {"polyimide": 8}

        @staticmethod
        def biplym(df, targ, dsp_rsl: bool = False):
            return pd.DataFrame(
                [
                    {
                        "mon1": "CCO",
                        "mon2": "CCN",
                        "polym": "*CCOCCN*",
                        "polymer_class": "polyimide",
                        "Ps_rxnL": 110,
                        "reactset": ("CCO", "CCN"),
                    },
                    {
                        "mon1": "CCN",
                        "mon2": "CCO",
                        "polym": "*CCNCCO*",
                        "polymer_class": "polyimide",
                        "Ps_rxnL": 111,
                        "reactset": ("CCN", "CCO"),
                    },
                    {
                        "mon1": "CCO",
                        "mon2": "CCN",
                        "polym": "*CCOCCN-alt*",
                        "polymer_class": "polyimide",
                        "Ps_rxnL": 112,
                        "reactset": ("CCO", "CCN"),
                    },
                    {
                        "mon1": "CCO",
                        "mon2": "C=O",
                        "polym": "*CCOC=O*",
                        "polymer_class": "polyimide",
                        "Ps_rxnL": 113,
                        "reactset": ("CCO", "C=O"),
                    },
                ]
            )

    runtime = SimpleNamespace(
        pd=pd,
        monc=FakeMonc,
        polg=FakePolg,
        target_classes=("polyimide",),
    )
    monkeypatch.setattr("app.services.monomer_polymerization._load_smipoly_runtime", lambda: runtime)
    monkeypatch.setattr("app.services.monomer_polymerization.generate_2d_svg", lambda smiles: "<svg />")
    client = TestClient(create_app(_settings(tmp_path)))

    response = client.post(
        "/api/v1/monomer-polymerization",
        json={
            "monomer_a_smiles": "CCO",
            "monomer_b_smiles": "CCN",
            "target_class": "polyimide",
            "max_results": 2,
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 3
    assert len(data["results"]) == 2
    assert {result["reaction_id"] for result in data["results"]} == {110, 111}
    assert all(result["monomer_b_smiles"] != "C=O" for result in data["results"])
    assert any("Filtered SMiPoly rows" in warning for warning in data["warnings"])


def test_monomer_polymerization_optional_target_with_second_monomer_filters_to_both_inputs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class FakeMonc:
        @staticmethod
        def moncls(df, smiColn: str, dsp_rsl: bool = False):
            return df.assign(smip_cand_mons=df["SMILES"])

    class FakePolg:
        Ps_classL = {"polyether": 12}

        @staticmethod
        def biplym(df, targ, dsp_rsl: bool = False):
            assert targ == ["polyether"]
            return pd.DataFrame(
                [
                    {
                        "mon1": "CCO",
                        "mon2": "CCN",
                        "polym": "*CCOCCN*",
                        "polymer_class": "polyether",
                        "Ps_rxnL": 210,
                        "reactset": ("CCO", "CCN"),
                    },
                    {
                        "mon1": "CCO",
                        "mon2": "C=O",
                        "polym": "*CCOC=O*",
                        "polymer_class": "polyether",
                        "Ps_rxnL": 211,
                        "reactset": ("CCO", "C=O"),
                    },
                    {
                        "mon1": "CCO",
                        "mon2": None,
                        "polym": "*CCO*",
                        "polymer_class": "polyether",
                        "Ps_rxnL": 212,
                        "reactset": ("CCO",),
                    },
                ]
            )

    runtime = SimpleNamespace(
        pd=pd,
        monc=FakeMonc,
        polg=FakePolg,
        target_classes=("polyether",),
    )
    monkeypatch.setattr("app.services.monomer_polymerization._load_smipoly_runtime", lambda: runtime)
    monkeypatch.setattr("app.services.monomer_polymerization.generate_2d_svg", lambda smiles: "<svg />")
    client = TestClient(create_app(_settings(tmp_path)))

    response = client.post(
        "/api/v1/monomer-polymerization",
        json={"monomer_a_smiles": "CCO", "monomer_b_smiles": "CCN", "target_class": "polyether"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["results"][0]["reaction_id"] == 210
    assert data["results"][0]["monomer_a_smiles"] == "CCO"
    assert data["results"][0]["monomer_b_smiles"] == "CCN"
    assert any("Filtered SMiPoly rows" in warning for warning in data["warnings"])
