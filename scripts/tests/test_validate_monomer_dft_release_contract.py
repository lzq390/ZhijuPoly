from __future__ import annotations

import importlib.util
from pathlib import Path
import re
import unittest
from unittest import mock


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

    def test_schema_contract_pins_both_exact_acl_fingerprints(self) -> None:
        self.assertEqual(
            VALIDATOR.CATALOG_FINGERPRINT,
            "6dc2e6ca7e1bb052836afec2bbdd46c6aa0928e97efdbbc6669b9b220f9bf6f8",
        )
        self.assertEqual(
            VALIDATOR.GOVERNED_MUTABLE_AUDIT_CATALOG_FINGERPRINT,
            "8972b5de85d2beb43f0e0023c7a842c602e237be8a7902831f32a9ce7eb401e2",
        )
        self.assertEqual(
            VALIDATOR.EXPLICIT_EMPTY_CATALOG_ACL_SENTINEL,
            "<explicit-empty-catalog-acl-array>",
        )

        failures: list[str] = []
        VALIDATOR.validate_database_schema_state_contract(
            REPOSITORY_ROOT,
            failures,
        )

        self.assertEqual(failures, [])

    def test_schema_contract_rejects_missing_or_normalized_governed_acl(
        self,
    ) -> None:
        schema_relative = "backend/app/services/monomer_dft_schema.py"
        schema = (REPOSITORY_ROOT / schema_relative).read_text(encoding="utf-8")
        governed_reason = "exact_0013_with_governed_mutable_audit_acl"
        mutations = {
            "fingerprint": schema.replace(
                VALIDATOR.GOVERNED_MUTABLE_AUDIT_CATALOG_FINGERPRINT,
                "0" * 64,
                1,
            ),
            "reason": schema.replace(governed_reason, "governed_acl", 1),
            "acl-normalizer": (
                schema + "\ndef _normalize_catalog_access_control():\n    pass\n"
            ),
        }
        original_read_text = VALIDATOR._read_text

        for mutation_name, mutated_schema in mutations.items():
            with self.subTest(mutation=mutation_name):
                def read_text(root, relative, failures):  # type: ignore[no-untyped-def]
                    if relative == schema_relative:
                        return mutated_schema
                    return original_read_text(root, relative, failures)

                failures: list[str] = []
                with mock.patch.object(
                    VALIDATOR,
                    "_read_text",
                    side_effect=read_text,
                ):
                    VALIDATOR.validate_database_schema_state_contract(
                        REPOSITORY_ROOT,
                        failures,
                    )
                if mutation_name == "acl-normalizer":
                    self.assertTrue(
                        any("raw ACLs" in failure for failure in failures),
                        failures,
                    )
                else:
                    self.assertTrue(
                        any(
                            "schema probe is missing" in failure
                            for failure in failures
                        ),
                        failures,
                    )

    def test_schema_contract_rejects_acl_null_empty_collapse(self) -> None:
        schema_relative = "backend/app/services/monomer_dft_schema.py"
        schema = (REPOSITORY_ROOT / schema_relative).read_text(encoding="utf-8")
        original_read_text = VALIDATOR._read_text
        acl_projections = (
            "n.nspacl",
            "c.relacl",
            "a.attacl",
            "t.typacl",
            "p.proacl",
        )

        for acl_projection in acl_projections:
            escaped_projection = re.escape(acl_projection)
            exact_projection = re.compile(
                rf"CASE\s+WHEN {escaped_projection} IS NULL THEN ''\s+"
                rf"WHEN pg_catalog\.cardinality\({escaped_projection}\) = 0\s+"
                r"THEN '<explicit-empty-catalog-acl-array>'\s+"
                rf"ELSE pg_catalog\.array_to_string\({escaped_projection}, ','\)\s+"
                r"END AS access_control"
            )
            self.assertEqual(len(exact_projection.findall(schema)), 1)

            mutations = {
                "missing-explicit-empty-branch": schema.replace(
                    f"WHEN pg_catalog.cardinality({acl_projection}) = 0",
                    f"WHEN pg_catalog.cardinality({acl_projection}) = -1",
                    1,
                ),
                "coalesce-null-and-empty": exact_projection.sub(
                    "COALESCE(pg_catalog.array_to_string("
                    f"{acl_projection}, ','), '') AS access_control",
                    schema,
                    count=1,
                ),
            }
            for mutation_name, mutated_schema in mutations.items():
                with self.subTest(
                    acl_projection=acl_projection,
                    mutation=mutation_name,
                ):
                    self.assertNotEqual(mutated_schema, schema)

                    def read_text(  # type: ignore[no-untyped-def]
                        root,
                        relative,
                        failures,
                    ):
                        if relative == schema_relative:
                            return mutated_schema
                        return original_read_text(root, relative, failures)

                    failures: list[str] = []
                    with mock.patch.object(
                        VALIDATOR,
                        "_read_text",
                        side_effect=read_text,
                    ):
                        VALIDATOR.validate_database_schema_state_contract(
                            REPOSITORY_ROOT,
                            failures,
                        )
                    self.assertTrue(
                        any(
                            "explicit empty ACL arrays for "
                            f"{acl_projection}" in failure
                            for failure in failures
                        ),
                        failures,
                    )
                    if mutation_name == "coalesce-null-and-empty":
                        self.assertTrue(
                            any(
                                "with COALESCE" in failure
                                for failure in failures
                            ),
                            failures,
                        )

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
