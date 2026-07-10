from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Literal


@dataclass(frozen=True, slots=True)
class ModelAssetSpec:
    path: str
    asset_type: str
    logical_name: str | None = None
    notes: str | None = None

    @property
    def resolved_logical_name(self) -> str:
        return self.logical_name or Path(self.path).name


@dataclass(frozen=True, slots=True)
class ReleaseAssetSpec:
    path: str
    kind: Literal["file", "tree"]
    category: Literal["required-model", "reactiont5", "polytao"]


REQUIRED_MODEL_FILE_ASSETS: tuple[ModelAssetSpec, ...] = (
    ModelAssetSpec("model/rf_Glass transition temperature_exp.pkl", "sklearn-pickle"),
    ModelAssetSpec("model/rf_Melting temperature_exp.pkl", "sklearn-pickle"),
    ModelAssetSpec("model/rf_Thermal decomposition temperature_exp.pkl", "sklearn-pickle"),
    ModelAssetSpec("model/rf_Thermal decomposition weight loss_exp.pkl", "sklearn-pickle"),
    ModelAssetSpec("model/rf_Elongation at break_exp.pkl", "sklearn-pickle"),
    ModelAssetSpec("model/rf_Tensile stress strength at break_exp.pkl", "sklearn-pickle"),
    ModelAssetSpec("model/rf_O2 Permeability Barrer_exp.pkl", "sklearn-pickle"),
    ModelAssetSpec("model/rf_Co2 Permeability Barrer_exp.pkl", "sklearn-pickle"),
    ModelAssetSpec("model/rf_H2 Permeability Barrer_exp.pkl", "sklearn-pickle"),
    ModelAssetSpec("model/ocsr/swin_base_char_aux_1m.pth", "pytorch-checkpoint"),
    ModelAssetSpec("model/conditional_generation/generator_best_40.pth", "pytorch-checkpoint"),
    ModelAssetSpec("model/conditional_generation/best_chemberta_tg.pth", "pytorch-checkpoint"),
    ModelAssetSpec("model/conditional_generation/top10_desc_names.pkl", "pickle"),
    ModelAssetSpec("model/conditional_generation/tg_scaler.pkl", "pickle"),
    ModelAssetSpec("model/conditional_generation/ChemBerta/config.json", "transformers-config"),
    ModelAssetSpec("model/conditional_generation/ChemBerta/model.safetensors", "safetensors"),
    ModelAssetSpec("model/conditional_generation/ChemBerta/tokenizer.json", "tokenizer"),
    ModelAssetSpec("model/conditional_generation/ChemBerta/tokenizer_config.json", "tokenizer-config"),
    ModelAssetSpec("model/conditional_generation/ChemBerta/special_tokens_map.json", "tokenizer-config"),
    ModelAssetSpec("model/conditional_generation/ChemBerta/vocab.json", "tokenizer-vocab"),
    ModelAssetSpec("model/conditional_generation/ChemBerta/merges.txt", "tokenizer-vocab"),
)

POLYTAO_MODEL_FILE_ASSETS: tuple[ModelAssetSpec, ...] = (
    ModelAssetSpec("model/polytao/config.json", "transformers-config", logical_name="polytao_config_json"),
    ModelAssetSpec("model/polytao/pytorch_model.bin", "pytorch-checkpoint", logical_name="polytao_pytorch_model_bin"),
    ModelAssetSpec("model/polytao/tokenizer.json", "tokenizer", logical_name="polytao_tokenizer_json"),
    ModelAssetSpec("model/polytao/spiece.model", "sentencepiece-model", logical_name="polytao_spiece_model"),
)

MODEL_DIRECTORY_ASSETS: tuple[ModelAssetSpec, ...] = (
    ModelAssetSpec(
        "model/reactiont5-retrosynthesis",
        "model-directory",
        logical_name="reactiont5-retrosynthesis_dir",
        notes="registered as filesystem asset; model files remain outside Postgres",
    ),
    ModelAssetSpec(
        "model/polytao",
        "model-directory",
        logical_name="polytao_dir",
        notes="PolyTAO backend runtime model directory; missing files should make PolyTAO unavailable, not ready",
    ),
)


RELEASE_MODEL_ASSETS: tuple[ReleaseAssetSpec, ...] = (
    *(ReleaseAssetSpec(spec.path, "file", "required-model") for spec in REQUIRED_MODEL_FILE_ASSETS),
    ReleaseAssetSpec("model/reactiont5-retrosynthesis", "tree", "reactiont5"),
    *(ReleaseAssetSpec(spec.path, "file", "polytao") for spec in POLYTAO_MODEL_FILE_ASSETS),
)


def iter_model_asset_specs(include_directories: bool = True, include_optional: bool = True) -> tuple[ModelAssetSpec, ...]:
    specs = REQUIRED_MODEL_FILE_ASSETS
    if include_optional:
        specs += POLYTAO_MODEL_FILE_ASSETS
    if include_directories:
        return specs + MODEL_DIRECTORY_ASSETS
    return specs


def main() -> None:
    parser = argparse.ArgumentParser(description="Print Nexpoly model asset manifest entries.")
    parser.add_argument("--profile", choices=["runtime", "release"], default="runtime")
    parser.add_argument("--format", choices=["paths", "json"], default="paths")
    args = parser.parse_args()

    if args.profile == "runtime" and args.format == "paths":
        for spec in REQUIRED_MODEL_FILE_ASSETS:
            print(spec.path)
        return

    if args.profile == "release" and args.format == "paths":
        for spec in RELEASE_MODEL_ASSETS:
            print(spec.path)
        return

    if args.profile != "release":
        parser.error("--format json requires --profile release")

    print(
        json.dumps(
            {
                "schema_version": 1,
                "profile": "release",
                "assets": [
                    {"path": spec.path, "kind": spec.kind, "category": spec.category}
                    for spec in RELEASE_MODEL_ASSETS
                ],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
