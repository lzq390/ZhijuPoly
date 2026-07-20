from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
VALIDATOR_PATH = REPOSITORY_ROOT / "scripts/validate_monomer_dft_release_contract.py"
SPEC = importlib.util.spec_from_file_location(
    "validate_monomer_dft_release_contract",
    VALIDATOR_PATH,
)
assert SPEC is not None and SPEC.loader is not None
VALIDATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VALIDATOR)
PREFLIGHT_PATH = REPOSITORY_ROOT / "scripts/preflight_monomer_dft_env.py"
PREFLIGHT_SPEC = importlib.util.spec_from_file_location(
    "preflight_monomer_dft_env",
    PREFLIGHT_PATH,
)
assert PREFLIGHT_SPEC is not None and PREFLIGHT_SPEC.loader is not None
PREFLIGHT = importlib.util.module_from_spec(PREFLIGHT_SPEC)
PREFLIGHT_SPEC.loader.exec_module(PREFLIGHT)


class MonomerDftReleaseContractTests(unittest.TestCase):
    def test_current_tree_satisfies_release_contract(self) -> None:
        self.assertEqual(VALIDATOR.validate(REPOSITORY_ROOT), [])

    def test_current_development_delivery_satisfies_isolation_contract(self) -> None:
        failures: list[str] = []
        compose = (
            REPOSITORY_ROOT / "docker-compose.monomer-dft-dev.yml"
        ).read_text(encoding="utf-8")

        VALIDATOR.validate_development_compose(compose, failures)
        VALIDATOR.validate_development_delivery(REPOSITORY_ROOT, failures)

        self.assertEqual(failures, [])

    def test_development_gpu_contract_rejects_gpu0_and_gpu2(self) -> None:
        text = (
            REPOSITORY_ROOT / ".env.monomer-dft.dev.example"
        ).read_text(encoding="utf-8")
        mutations = (
            text.replace("NEXPOLY_DFT_GPU_DEVICE=1", "NEXPOLY_DFT_GPU_DEVICE=0"),
            text.replace(
                "NEXPOLY_DFT_OVERFLOW_GPU_DEVICES=3",
                "NEXPOLY_DFT_OVERFLOW_GPU_DEVICES=2",
            ),
        )
        for mutated in mutations:
            with self.subTest(mutated=mutated):
                failures: list[str] = []
                VALIDATOR.validate_development_gpu_contract(mutated, failures)
                self.assertTrue(
                    any("must never select physical GPU" in failure for failure in failures),
                    failures,
                )

    def test_development_gpu_contract_rejects_prod_mode(self) -> None:
        text = (
            REPOSITORY_ROOT / ".env.monomer-dft.dev.example"
        ).read_text(encoding="utf-8")
        failures: list[str] = []

        VALIDATOR.validate_development_gpu_contract(
            text.replace("MONOMER_DFT_DEPLOYMENT=dev", "MONOMER_DFT_DEPLOYMENT=prod"),
            failures,
        )

        self.assertTrue(
            any("MONOMER_DFT_DEPLOYMENT" in failure for failure in failures),
            failures,
        )

    def test_development_gpu_contract_rejects_production_state_path(self) -> None:
        text = (
            REPOSITORY_ROOT / ".env.monomer-dft.dev.example"
        ).read_text(encoding="utf-8")
        failures: list[str] = []

        VALIDATOR.validate_development_gpu_contract(
            text.replace(
                ".runtime/gpu-resource/broker.sock",
                "/data/lzq/gith/nexpoly/ops/state/gpu-resource/broker.sock",
            ),
            failures,
        )

        self.assertTrue(
            any("production state" in failure for failure in failures),
            failures,
        )

    def test_preflight_rejects_gpu0_and_gpu2_before_hardware_queries(self) -> None:
        self.assertEqual(set(PREFLIGHT.EXPECTED_GPU_UUIDS), {"1", "3"})
        for gpu_index in ("0", "2"):
            with self.subTest(gpu_index=gpu_index):
                with self.assertRaisesRegex(
                    PREFLIGHT.PreflightError,
                    "GPUs 0 and 2 are forbidden",
                ):
                    PREFLIGHT.validate_physical_gpu(gpu_index)


if __name__ == "__main__":
    unittest.main()
