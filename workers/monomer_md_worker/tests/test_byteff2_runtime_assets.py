from __future__ import annotations

import os
from pathlib import Path

import pytest

from workers.monomer_md_worker.app import byteff2_runtime_assets as runtime_assets


def _create_sparse_runtime_assets(root: Path) -> None:
    for asset in runtime_assets.BYTEFF2_RUNTIME_ASSETS:
        path = root / asset.relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as stream:
            stream.truncate(asset.size)


def test_runtime_asset_contract_pins_the_audited_delivery() -> None:
    assert [
        (asset.relative_path.as_posix(), asset.size, asset.sha256)
        for asset in runtime_assets.BYTEFF2_RUNTIME_ASSETS
    ] == [
        (
            "submodules/bytemol/bytemol/toolkit/infer_molecule/"
            "bond_length_ref.csv",
            802,
            "caa78ff02c7e65fb0c8bcf240382fa8d90b0dfea85a4d9888c96eab04cc4a40e",
        ),
        (
            "byteff2/trained_models/fftrainer_config_in_use.yaml",
            986,
            "8245a5c6ad9b4aa9d180c8bb24d6f05c210f1724ffae93aec0ef4f88e5fd7ea3",
        ),
        (
            "byteff2/trained_models/optimal.pt",
            111_892_932,
            "ae47a6e6860b563908a2e0a83d4a3f6adc1c36b48f544e2241d24066d43d539c",
        ),
    ]


def test_runtime_asset_validation_accepts_exact_regular_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _create_sparse_runtime_assets(tmp_path)
    digests = {
        asset.relative_path.name: asset.sha256
        for asset in runtime_assets.BYTEFF2_RUNTIME_ASSETS
    }
    monkeypatch.setattr(
        runtime_assets,
        "_hash_runtime_asset",
        lambda path, _metadata, *, deadline: digests[path.name],
    )

    assert runtime_assets.validate_byteff2_runtime_assets(tmp_path) is None


def test_runtime_asset_validation_rejects_missing_unsafe_size_and_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _create_sparse_runtime_assets(tmp_path)
    first = runtime_assets.BYTEFF2_RUNTIME_ASSETS[0]
    first_path = tmp_path / first.relative_path

    first_path.unlink()
    assert "missing" in (
        runtime_assets.validate_byteff2_runtime_assets(tmp_path) or ""
    )

    first_path.symlink_to("missing-target")
    assert "unsafe" in (
        runtime_assets.validate_byteff2_runtime_assets(tmp_path) or ""
    )

    first_path.unlink()
    first_path.write_bytes(b"wrong-size")
    assert "size mismatch" in (
        runtime_assets.validate_byteff2_runtime_assets(tmp_path) or ""
    )

    with first_path.open("wb") as stream:
        stream.truncate(first.size)
    monkeypatch.setattr(
        runtime_assets,
        "_hash_runtime_asset",
        lambda *_args, **_kwargs: "0" * 64,
    )
    assert "digest mismatch" in (
        runtime_assets.validate_byteff2_runtime_assets(tmp_path) or ""
    )


def test_runtime_asset_validation_honors_the_probe_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _create_sparse_runtime_assets(tmp_path)
    monkeypatch.setattr(runtime_assets, "monotonic", lambda: 2.0)

    with pytest.raises(TimeoutError):
        runtime_assets.validate_byteff2_runtime_assets(tmp_path, deadline=1.0)


def test_runtime_asset_validation_does_not_expose_host_paths(
    tmp_path: Path,
) -> None:
    private_root = tmp_path / "private" / "asset-root"
    private_root.mkdir(parents=True)

    error = runtime_assets.validate_byteff2_runtime_assets(private_root)

    assert error is not None
    assert str(private_root) not in error
    assert os.fspath(tmp_path) not in error
