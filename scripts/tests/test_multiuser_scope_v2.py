"""Synthetic file packages only: these tests do not execute science or services."""
from __future__ import annotations

import ast
from copy import deepcopy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("scope_v2_under_test", ROOT / "scripts/multiuser_scope_v2.py")
SCOPE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SCOPE)
CONTRACT_PATH = ROOT / "contracts/multiuser_scope_v2.json"
DEFINITIONS_PATH = ROOT / "scripts/tests/fixtures/multiuser_scope_v2_definitions.json"


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.contract = SCOPE.read_json(CONTRACT_PATH)
        self.definitions = SCOPE.read_json(DEFINITIONS_PATH)
        self.serial = 0

    def reference(self, value):
        self.serial += 1
        filename = f"synthetic-{self.serial}.json"
        raw = json.dumps(value, ensure_ascii=False).encode()
        (self.directory / filename).write_bytes(raw)
        return {"path": filename, "sha256": SCOPE.digest(raw)}

    def manifest(self, entries):
        return {"schema_version": 1, "scope_version": SCOPE.SCOPE, "entries": entries}

    def package(self, identifier=SCOPE.PROVEN, *, run="synthetic-run", source_version=None):
        case = next(row for row in self.contract["cases"] if row["id"] == identifier)
        qualification = {"scope_version": SCOPE.SCOPE, "case_id": identifier,
                         "review_status": "APPROVED", "parameters": deepcopy(case["required_parameters"]),
                         "fault_point": case["fault_point"],
                         "engine": case["engine"], "execution_layer": case["execution_layer"],
                         "source_sha256": "1" * 64, "model_sha256": "2" * 64, "image_sha256": "3" * 64}
        raw = {**qualification, "document_kind": "original_case_result", "simulated": False,
               "scope_version": source_version or SCOPE.SCOPE, "status": "PASS", "run_id": run,
               "parameters_sha256": SCOPE.canonical_digest(qualification["parameters"]), "gpu_uuid": SCOPE.GPU1}
        identity = {"pid": 101, "start_ticks": 123, "boot_id": "synthetic-boot",
                    "cgroup": "/synthetic/lease.scope", "gpu_uuid": SCOPE.GPU1}
        authority = {"kind": "dft_residency", "parent_lease_id": None, "lease_id": "synthetic-residency",
                     "fencing_token": 123, "broker_instance_id": "synthetic-broker", "target_identity": identity}
        raw.update(boundary={"phase": "finite_result_before_ipc", "finite_result": True,
                             "identity_captured_before_fault": True, "ipc_delivered": False,
                             "trigger": "native_timeout", "timeout_seconds": 600,
                             "elapsed_since_admission_seconds": 600}, target_identity=identity,
                   owner_and_committed_artifacts_unchanged=True, no_duplicate_execution_or_publication=True,
                   abc_baselines_passed=True, six_direction_denials_passed=True,
                   fault_time=1000, capacity_release_time=1010, broker_instance_before="synthetic-broker",
                   broker_instance_after="synthetic-broker", reentry_succeeded_owners=["A", "B", "C"],
                   owner_label="A", termination_authority=authority,
                   mps_instance_before="synthetic-mps", mps_instance_after="synthetic-mps",
                   terminal_status="failed", uncommitted_result_published=False, fault_attempt_publication_count=0,
                   termination_attempts=[{
                       "proof_id": run + "-proof", "target_identity": identity,
                       "termination_authority": authority,
                       "stages": list(SCOPE.TERMINATION_STAGES), "stage_times": list(range(1001, 1008)),
                       "mps_response": "CUDA_SUCCESS"}])
        if identifier == SCOPE.CONTAINED:
            raw["boundary"]["trigger"] = "exact_pidfd_sigkill"
            raw.update(failure={"stage": "workload_revalidation", "code": "workload_identity_mismatch",
                                "operations_after_failure": []}, suspect_observed=True,
                       reservation_retained_at_failure=True, quarantine_persisted=True,
                       related_admission_denied=True, external_identities_untouched=True,
                       automatic_recovery_claimed=False, failure_snapshot={"lease_id": authority["lease_id"],
                           "fencing_token": authority["fencing_token"], "gpu_uuid": SCOPE.GPU1,
                           "lease_status": "suspect", "reserved_mib": 4096, "quarantined": True,
                           "new_residency_admitted": False})
        cleanup = {"document_kind": "cleanup_receipt", "run_id": run, "status": "PASS", "simulated": False,
                   **{key: True for key in SCOPE.CLEANUP_CHECKS}}
        restore = {"document_kind": "managed_recovery_receipt", "run_id": run, "status": "PASS", "simulated": False,
                   "gate_id": SCOPE.GATE, "gpu_uuid": SCOPE.GPU1, "fault_failure_closed": False,
                   "old_quarantine_preserved": True, **{key: True for key in SCOPE.RESTORE_CHECKS}}
        return {"qualification": qualification, "raw": raw, "cleanup": cleanup, "restore": restore}

    def entry(self, documents):
        return {"scope_version": SCOPE.SCOPE, "case_id": documents["qualification"]["case_id"],
                "evidence_kind": "original_package", **{key: self.reference(value) for key, value in documents.items()}}

    def row(self, documents=None, *, entry=None):
        entry = entry if entry is not None else self.entry(documents)
        report = SCOPE.adjudicate(self.contract, self.manifest([entry]), self.directory)
        return next(row for row in report["cases"] if row["case_id"] == entry["case_id"])

    def test_inventory_has_exact_versioned_denominators_and_unmodified_history(self):
        SCOPE.validate_contract(self.contract)
        report = SCOPE.adjudicate(self.contract)
        self.assertEqual(report["counts"], {"PASS": 0, "FAIL": 0, "BLOCKED": 126, "NOT_RUN": 2})
        self.assertEqual(report["legacy_summary"]["reported_counts"], {"PASS": 126, "FAIL": 1, "BLOCKED": 1, "NOT_RUN": 0})
        self.assertEqual(len({r["case_id"] for r in report["cases"]}), 128)
        self.assertEqual(sum(r["id"] == SCOPE.OPENMM for r in self.contract["cases"]), 1)
        self.assertNotIn(SCOPE.GATE, {r["id"] for r in self.contract["cases"]})
        self.assertFalse(report["legacy_summary"]["replayed"])

    def test_frozen_definition_excerpts_are_not_execution_evidence(self):
        self.assertEqual(self.definitions["document_kind"], "definition_only_fixture")
        self.assertIs(self.definitions["contains_execution_evidence"], False)
        original_sources = {row["path"]: row["sha256"] for row in self.contract["inventory_origin"]["sources"]}
        for excerpt in self.definitions["definitions"].values():
            self.assertEqual(excerpt["reviewed_original_file_sha256"], original_sources[excerpt["original_path"]])
            self.assertEqual(SCOPE.digest(excerpt["literal_expression"].encode()), excerpt["literal_expression_sha256"])
            self.assertNotEqual(excerpt["literal_expression_sha256"], excerpt["reviewed_original_file_sha256"])
            ast.literal_eval(excerpt["literal_expression"])
        self.assertEqual(tuple(self.literal_definition("common_worker_names")), SCOPE.COMMON)
        self.assertEqual(tuple((module, scenario) for module, scenario, _ in self.literal_definition("gpu_auth_scenarios")), SCOPE.SCENARIOS)
        self.assertEqual("WF-MD-" + self.literal_definition("existing_openmm_case_name"), SCOPE.OPENMM)
        self.assertEqual(len(SCOPE.SCENARIOS) * 3 * 3, 99)

    def literal_definition(self, name):
        return ast.literal_eval(self.definitions["definitions"][name]["literal_expression"])

    def test_unknown_duplicate_missing_and_renamed_cases_are_rejected(self):
        variants = []
        for change in ("duplicate", "missing", "rename", "gromacs", "environment"):
            value = deepcopy(self.contract)
            if change == "duplicate": value["cases"][1] = value["cases"][0]
            elif change == "missing": value["cases"].pop()
            elif change == "rename": value["cases"][0]["id"] = "invented-case"
            elif change == "gromacs": value["cases"][0]["engine"] = "gromacs"
            else: value["environment_gate"]["counted"] = True
            variants.append(value)
        for value in variants:
            with self.subTest(value=value["cases"][0]):
                with self.assertRaises(SCOPE.InvalidEvidence): SCOPE.validate_contract(value)

    def test_scope_changes_cannot_rewrite_history_or_close_failures(self):
        for change in ("counts", "status", "sha", "closure", "cycle", "version", "science", "engine", "preparation", "steps", "fault_point"):
            value = deepcopy(self.contract)
            if change == "counts": value["legacy"]["reported_counts"]["PASS"] = 128
            elif change == "status": value["legacy"]["exceptions"][SCOPE.OLD_AC] = "PASS"
            elif change == "sha": value["legacy"]["expected_report_sha256"] = "0" * 64
            elif change == "closure": value["relationships"][-1]["closes_failure"] = True
            elif change == "cycle":
                edge = deepcopy(value["relationships"][0]); edge["from"], edge["to"] = edge["to"], edge["from"]
                value["relationships"].append(edge)
            elif change == "version": value["relationships"][0]["from"][0] = SCOPE.SCOPE
            elif change == "engine": value["md_engine_policy"]["dynamics"] = "gromacs"
            elif change == "preparation": value["md_engine_policy"]["gromacs_mdrun"] = True
            elif change == "steps": value["md_engine_policy"]["formal_density"]["steps"] = 300
            elif change == "fault_point": value["cases"][0]["fault_point"] = "invented_boundary"
            else: value["scientific_execution_authorized"] = True
            with self.subTest(change=change), self.assertRaises(SCOPE.InvalidEvidence): SCOPE.validate_contract(value)

    def test_summary_and_missing_original_block_inheritance(self):
        documents = self.package("WF-DFT-baseline.A", source_version=SCOPE.LEGACY)
        entry = self.entry(documents)
        entry["evidence_kind"] = "summary"
        self.assertEqual(self.row(entry=entry)["current_status"], "BLOCKED")
        entry["evidence_kind"] = "original_package"
        (self.directory / entry["raw"]["path"]).unlink()
        row = self.row(entry=entry)
        self.assertEqual((row["current_status"], row["reported_historical_status"]), ("BLOCKED", "PASS"))

    def test_sha_mismatch_and_duplicate_json_key_are_invalid(self):
        entry = self.entry(self.package())
        (self.directory / entry["raw"]["path"]).write_text("{}")
        self.assertEqual(self.row(entry=entry)["current_status"], "FAIL")
        with self.assertRaises(SCOPE.InvalidEvidence): SCOPE.parse_json('{"status":"FAIL","status":"PASS"}')
        with self.assertRaises(SCOPE.InvalidEvidence): SCOPE.parse_json('{"time":NaN}')

    def test_valid_synthetic_package_tests_validator_without_claiming_real_execution(self):
        documents = self.package()
        report = SCOPE.adjudicate(self.contract, self.manifest([self.entry(documents)]), self.directory)
        self.assertEqual(report["counts"]["PASS"], 1)
        self.assertEqual(report["status"], "BLOCKED")
        self.assertEqual(report["scientific_executions_started_by_tool"], 0)
        self.assertEqual(len(report["environment_gates"]), 1)
        self.assertFalse(report["environment_gates"][0]["counted"])
        inherited = self.row(self.package("WF-DFT-baseline.A", source_version=SCOPE.LEGACY))
        self.assertEqual(inherited["relationship"], "verified_evidence_inheritance")
        self.assertEqual(inherited["execution_status"], "NOT_RUN")

    def test_cpu_engine_parameters_source_owner_and_scope_mismatches_fail(self):
        changes = (("simulated", True), ("execution_layer", "cpu_contract"), ("engine", "gromacs"),
                   ("source_sha256", "f" * 64), ("model_sha256", "f" * 64), ("image_sha256", "f" * 64),
                   ("parameters", {"smiles": "CCC"}), ("parameters_sha256", "f" * 64),
                   ("case_id", "AC-v2.proven-recovery.B"), ("scope_version", SCOPE.LEGACY),
                   ("gpu_uuid", "GPU-other"), ("status", "FAIL"), ("document_kind", "summary"))
        for key, value in changes:
            documents = self.package(); documents["raw"][key] = value
            with self.subTest(field=key): self.assertEqual(self.row(documents)["current_status"], "FAIL")

    def test_unreviewed_qualification_blocks_and_missing_parameters_fail(self):
        documents = self.package(); documents["qualification"]["review_status"] = "DRAFT"
        self.assertEqual(self.row(documents)["current_status"], "BLOCKED")
        documents = self.package(); documents["qualification"]["parameters"] = {}
        self.assertEqual(self.row(documents)["current_status"], "FAIL")

    def test_non_ac_fault_point_is_locked_even_when_qualification_agrees(self):
        for identifier in ("WF-DFT-parent_restart.B", SCOPE.OPENMM, "AT-GPU-NATIVE.ocsr.result.A.headers"):
            for mutate_qualification in (False, True):
                documents = self.package(identifier)
                documents["raw"]["fault_point"] = "wrong_fault_boundary"
                if mutate_qualification: documents["qualification"]["fault_point"] = "wrong_fault_boundary"
                with self.subTest(identifier=identifier, both=mutate_qualification):
                    self.assertEqual(self.row(documents)["current_status"], "FAIL")

    def test_formal_openmm_parameters_cannot_be_shortened_or_change_timestep(self):
        self.assertEqual(self.row(self.package(SCOPE.OPENMM))["current_status"], "PASS")
        for field, value in (("steps", 300), ("timestep_fs", 1), ("protocol", "DensityDemo")):
            documents = self.package(SCOPE.OPENMM)
            documents["raw"]["parameters"][field] = value
            documents["qualification"]["parameters"][field] = value
            documents["raw"]["parameters_sha256"] = SCOPE.canonical_digest(documents["raw"]["parameters"])
            with self.subTest(field=field): self.assertEqual(self.row(documents)["current_status"], "FAIL")

    def test_formal_md_request_matches_original_literal_and_rejects_joint_drift(self):
        original_request = self.literal_definition("formal_md_request_body")
        policy_request = {k: v for k, v in SCOPE.MD_ENGINE_POLICY["formal_density"].items() if k not in {"steps", "timestep_fs"}}
        self.assertEqual(policy_request, original_request)
        for field, value in (("temperature", 999), ("natoms", 1), ("components", {"CCO": 2}),
                             ("smiles", "CCO"), ("smiles", {"CCO": "CCC"})):
            documents = self.package(SCOPE.OPENMM)
            documents["qualification"]["parameters"]["config_json"][field] = value
            documents["raw"]["parameters"]["config_json"][field] = value
            documents["raw"]["parameters_sha256"] = SCOPE.canonical_digest(documents["raw"]["parameters"])
            with self.subTest(kind="fault", field=field, value=value):
                self.assertEqual(self.row(documents)["current_status"], "FAIL")
            documents = self.artifact_package()
            documents["qualification"]["scientific_source_parameters"]["config_json"][field] = value
            documents["scientific_source"]["parameters"]["config_json"][field] = value
            with self.subTest(kind="artifact", field=field, value=value):
                self.assertEqual(self.row(documents)["current_status"], "FAIL")

    def artifact_package(self):
        documents = self.package("AT-GPU-NATIVE.formal-md.trajectory.A.body")
        raw, qualification = documents["raw"], documents["qualification"]
        raw.pop("gpu_uuid")
        raw.update(gpu_services_started=False, scientific_submissions=0)
        source_parameters = deepcopy(SCOPE.MD_ENGINE_POLICY["formal_density"])
        artifact = {"synthetic_fixture_only": True}
        artifact_sha = SCOPE.digest(json.dumps(artifact, ensure_ascii=False).encode())
        qualification.update(scientific_source_parameters=source_parameters, scientific_source_sha256="4" * 64,
                             scientific_model_sha256="5" * 64, scientific_image_sha256="6" * 64,
                             artifact_sha256=artifact_sha)
        raw["observed_artifact_sha256"] = artifact_sha
        documents["artifact"] = artifact
        documents["scientific_source"] = {"document_kind": "original_scientific_source", "simulated": False,
            "status": "PASS", "run_id": "synthetic-earlier-science", "gpu_uuid": SCOPE.GPU1,
            "engine": "byteff2_openmm", "parameters": source_parameters, "source_sha256": "4" * 64,
            "model_sha256": "5" * 64, "image_sha256": "6" * 64, "artifact_sha256": artifact_sha}
        documents["scientific_source_cleanup"] = {**documents["cleanup"], "run_id": "synthetic-earlier-science"}
        documents["cleanup"] = {"document_kind": "cpu_fixture_cleanup_receipt", "run_id": raw["run_id"],
            "status": "PASS", "simulated": False, **{key: True for key in SCOPE.CPU_CLEANUP_CHECKS}}
        documents["restore"] = {"document_kind": "environment_preservation_receipt", "run_id": raw["run_id"],
            "status": "PASS", "simulated": False, "no_service_mutations": True, "gpu_only_preserved": True,
            "development_health_unchanged": True, "gate_id": SCOPE.CPU_GATE, "fault_failure_closed": False}
        return documents

    def test_cpu_artifact_browser_uses_prior_science_and_current_cpu_cleanup(self):
        documents = self.artifact_package()
        report = SCOPE.adjudicate(self.contract, self.manifest([self.entry(documents)]), self.directory)
        self.assertEqual(report["counts"]["PASS"], 1)
        self.assertEqual(report["environment_gates"][0]["id"], SCOPE.CPU_GATE)
        self.assertTrue(report["contract_valid"])
        self.assertEqual(report["contract_status"], "VALID")
        self.assertEqual(report["status"], "BLOCKED")
        for change in ("source_missing", "current_gpu", "new_science", "source_gpu", "source_steps", "source_sha", "artifact", "source_cleanup", "cpu_cleanup", "preservation"):
            documents = self.artifact_package()
            if change == "source_missing": documents.pop("scientific_source")
            elif change == "current_gpu": documents["raw"]["gpu_uuid"] = SCOPE.GPU1
            elif change == "new_science": documents["raw"]["scientific_submissions"] = 1
            elif change == "source_gpu": documents["scientific_source"]["gpu_uuid"] = "GPU-other"
            elif change == "source_steps": documents["scientific_source"]["parameters"]["steps"] = 300
            elif change == "source_sha": documents["scientific_source"]["source_sha256"] = "a" * 64
            elif change == "artifact": documents["raw"]["observed_artifact_sha256"] = "a" * 64
            elif change == "source_cleanup": documents["scientific_source_cleanup"]["leases_zero"] = False
            elif change == "cpu_cleanup": documents["cleanup"]["database_consumers_exited"] = False
            else: documents["restore"]["no_service_mutations"] = False
            with self.subTest(change=change):
                self.assertEqual(self.row(documents)["current_status"], "BLOCKED" if change == "source_missing" else "FAIL")

    def test_cleanup_and_recovery_are_mandatory_and_bound_to_same_run(self):
        for section, fields in (("cleanup", SCOPE.CLEANUP_CHECKS), ("restore", SCOPE.RESTORE_CHECKS)):
            for field in (*fields, "status", "run_id", "simulated"):
                documents = self.package()
                documents[section][field] = True if field == "simulated" else "another" if field == "run_id" else False
                with self.subTest(section=section, field=field): self.assertEqual(self.row(documents)["current_status"], "FAIL")
        documents = self.package(); entry = self.entry(documents); entry.pop("cleanup")
        self.assertEqual(self.row(entry=entry)["current_status"], "BLOCKED")

    def test_proven_recovery_boundary_timeout_and_fresh_ordered_identity_are_required(self):
        for change in ("phase", "ipc", "timeout", "early", "trigger", "identity", "authority", "owner", "order", "mps", "stale", "release", "reuse", "broker", "mps_restart", "reentry", "completed"):
            documents = self.package(); raw = documents["raw"]; attempt = raw["termination_attempts"][0]
            if change == "phase": raw["boundary"]["phase"] = "cuda_kernel_running"
            elif change == "ipc": raw["boundary"]["ipc_delivered"] = True
            elif change == "timeout": raw["boundary"]["timeout_seconds"] = 3
            elif change == "early": raw["boundary"]["elapsed_since_admission_seconds"] = 599
            elif change == "trigger": raw["boundary"]["trigger"] = "cooperative_cancel"
            elif change == "identity": attempt["target_identity"] = {**attempt["target_identity"], "start_ticks": 999}
            elif change == "authority": raw["termination_authority"]["kind"] = "parented_execution"
            elif change == "owner": raw["owner_label"] = "B"
            elif change == "order": attempt["stages"][0], attempt["stages"][1] = attempt["stages"][1], attempt["stages"][0]
            elif change == "mps": attempt["mps_response"] = "UNKNOWN"
            elif change == "stale": raw["fault_time"] = 1002
            elif change == "release": raw["capacity_release_time"] = 1005
            elif change == "reuse": raw["termination_attempts"].append(deepcopy(attempt))
            elif change == "broker": raw["broker_instance_after"] = "replacement"
            elif change == "mps_restart": raw["mps_instance_after"] = "replacement"
            elif change == "reentry": raw["reentry_succeeded_owners"] = ["A"]
            else: raw["terminal_status"] = "completed"
            with self.subTest(change=change): self.assertEqual(self.row(documents)["current_status"], "FAIL")

    def test_containment_requires_stopping_termination_and_preserving_quarantine(self):
        self.assertEqual(self.row(self.package(SCOPE.CONTAINED))["current_status"], "PASS")
        for change in ("kill", "failure", "reservation", "snapshot", "quarantine", "admission", "external", "automatic", "restore"):
            documents = self.package(SCOPE.CONTAINED); raw = documents["raw"]
            if change == "kill": raw["failure"]["operations_after_failure"] = ["scope_kill"]
            elif change == "failure": raw["failure"]["stage"] = "mps_termination"
            elif change == "reservation": raw["reservation_retained_at_failure"] = False
            elif change == "snapshot": raw["failure_snapshot"]["reserved_mib"] = 0
            elif change == "quarantine": raw["quarantine_persisted"] = False
            elif change == "admission": raw["related_admission_denied"] = False
            elif change == "external": raw["external_identities_untouched"] = False
            elif change == "automatic": raw["automatic_recovery_claimed"] = True
            else: documents["restore"]["old_quarantine_preserved"] = False
            with self.subTest(change=change): self.assertEqual(self.row(documents)["current_status"], "FAIL")

    def test_both_ac_branches_require_failed_terminal_and_zero_invalid_publication(self):
        for identifier in (SCOPE.PROVEN, SCOPE.CONTAINED):
            for field, value in (("terminal_status", "completed"), ("terminal_status", None),
                                 ("uncommitted_result_published", True), ("uncommitted_result_published", None),
                                 ("fault_attempt_publication_count", 1), ("fault_attempt_publication_count", False)):
                documents = self.package(identifier); documents["raw"][field] = value
                with self.subTest(case=identifier, field=field, value=value):
                    self.assertEqual(self.row(documents)["current_status"], "FAIL")

    def test_manifest_keys_cannot_double_count_cases_or_cross_versions(self):
        entry = self.entry(self.package())
        for entries in ([entry, entry], [{**entry, "scope_version": SCOPE.LEGACY}], [{**entry, "case_id": SCOPE.OLD_AC}]):
            with self.assertRaises(SCOPE.InvalidEvidence): SCOPE.adjudicate(self.contract, self.manifest(entries), self.directory)
        for invalid in ({}, {"schema_version": 1, "scope_version": SCOPE.SCOPE}):
            with self.assertRaises(SCOPE.InvalidEvidence): SCOPE.adjudicate(self.contract, invalid, self.directory)

    def test_cli_does_not_overwrite_and_returns_nonzero_for_incomplete_report(self):
        output = self.directory / "new-report.json"
        self.assertEqual(SCOPE.main(["--output", str(output)]), 1)
        before = output.read_bytes()
        with mock.patch("sys.stdout", io.StringIO()): self.assertEqual(SCOPE.main(["--output", str(output)]), 2)
        self.assertEqual(output.read_bytes(), before)

    def test_import_surface_cannot_launch_services_or_science(self):
        tree = ast.parse((ROOT / "scripts/multiuser_scope_v2.py").read_text())
        imported = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        imported |= {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        self.assertLessEqual(imported, {"__future__", "argparse", "hashlib", "json", "pathlib", "re"})
        self.assertFalse(any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"eval", "exec", "__import__"} for node in ast.walk(tree)))


if __name__ == "__main__":
    unittest.main()
