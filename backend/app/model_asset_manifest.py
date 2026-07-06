from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ModelAssetSpec:
    path: str
    asset_type: str
    logical_name: str | None = None
    notes: str | None = None

    @property
    def resolved_logical_name(self) -> str:
        return self.logical_name or Path(self.path).name


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

MODEL_DIRECTORY_ASSETS: tuple[ModelAssetSpec, ...] = (
    ModelAssetSpec(
        "model/reactiont5-retrosynthesis",
        "model-directory",
        logical_name="reactiont5-retrosynthesis_dir",
        notes="registered as filesystem asset; model files remain outside Postgres",
    ),
)


def iter_model_asset_specs(include_directories: bool = True) -> tuple[ModelAssetSpec, ...]:
    if include_directories:
        return REQUIRED_MODEL_FILE_ASSETS + MODEL_DIRECTORY_ASSETS
    return REQUIRED_MODEL_FILE_ASSETS


def main() -> None:
    parser = argparse.ArgumentParser(description="Print Nexpoly model asset manifest entries.")
    parser.add_argument("--format", choices=["paths"], default="paths")
    args = parser.parse_args()
    if args.format == "paths":
        for spec in REQUIRED_MODEL_FILE_ASSETS:
            print(spec.path)


if __name__ == "__main__":
    main()
