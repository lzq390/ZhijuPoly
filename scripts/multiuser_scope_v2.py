"""File-only scope validation. Never imports or launches an acceptance runner.

PASS means the supplied, reviewed evidence package is internally complete. This
tool cannot authenticate a collector or reconstruct deleted observations.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re

SCOPE = "acceptance-scope-v2"
LEGACY = "fixed128-v1"
GPU1 = "GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771"
OLD_AC = "WF-DFT-active_child.A"
OLD_GMX = "WF-MD-formal_gromacs_child.C"
OPENMM = "WF-MD-formal_openmm_child.C"
PROVEN = "AC-v2.proven-recovery.A"
CONTAINED = "AC-v2.unproven-containment.A"
GATE = "AC-v2.managed-environment-recovery"
CPU_GATE = "UI-v2.fixture-environment-preserved"
MD_ENGINE_POLICY = {"dynamics": "byteff2_openmm", "preparation": "gromacs_box_tools",
                    "gromacs_mdrun": False, "formal_density": {
                        "protocol": "Density", "run_mode": "formal", "config_json": {
                            "protocol": "Density", "temperature": 298, "natoms": 2000,
                            "components": {"CCO": 1}, "smiles": {"CCO": "CCO"},
                            "params_dir": "managed_params", "output_dir": "managed_output",
                            "working_dir": "managed_working"}, "steps": 1500000, "timestep_fs": 2}}
COMMON = ("baseline.A", "baseline.B", "baseline.C", "active_child.A",
          "parent_restart.B", "lost_submit_ack.C", "terminal_commit_ack.A",
          "cleanup_release_ack.B", "artifact_unique.C", "stale_identity.A",
          "uncertain_cleanup.B", "reentry.C", "private_six_directions")
SCENARIOS = (("ocsr", "result"), ("retrosynthesis", "result"),
             ("conditional-generation", "result"), ("polytao", "result"),
             ("conditional-generation", "submission"), ("polytao", "submission"),
             ("formal-md", "result"), ("formal-md", "trajectory"),
             ("formal-dft", "result"), ("formal-dft", "bundle"),
             ("formal-dft", "trajectory"))
TERMINATION_STAGES = ("identity_revalidation", "mps_termination", "pre_freeze_audit",
                      "scope_freeze", "post_freeze_audit", "scope_kill", "scope_empty")
CLEANUP_CHECKS = ("requests_exited", "tasks_terminal", "attempts_released",
                  "processes_exited", "leases_zero", "waiters_zero", "scopes_empty",
                  "scratch_removed", "database_consumers_exited", "fixture_removed")
RESTORE_CHECKS = ("host_recovery_outside_coordinator_scope", "native_session_restored",
                  "resource_limits_verified", "gpu_only_preserved", "service_health_verified")
CPU_CLEANUP_CHECKS = ("requests_exited", "processes_exited", "database_consumers_exited", "fixture_removed",
                      "accounts_and_sessions_removed", "no_gpu_resources_started")
FAULT_POINTS = {"baseline": "normal_completion", "active_child": "active_execution_before_terminal",
                "parent_restart": "active_execution_parent_exit", "lost_submit_ack": "submit_accepted_before_ack",
                "terminal_commit_ack": "durable_terminal_commit_before_ack", "cleanup_release_ack": "cleanup_release_before_ack",
                "artifact_unique": "completed_artifact_replay", "stale_identity": "retired_attempt_replay",
                "uncertain_cleanup": "cleanup_release_unconfirmed", "reentry": "post_cleanup_readmission",
                "private_six_directions": "cross_owner_private_access", "queued_restart_order": "queued_journal_parent_restart",
                "formal_openmm_child": "formal_density_openmm_npt_active"}


class InvalidEvidence(ValueError):
    """An available package contradicts its required contract."""


class UnavailableEvidence(ValueError):
    """A required original or reviewed qualification is unavailable."""


def require(condition, message):
    if not condition:
        raise InvalidEvidence(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical_digest(value):
    return digest(json.dumps(value, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False).encode())


def sha(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def legacy_ids():
    md = ["WF-MD-" + name for name in COMMON] + [OLD_GMX, OPENMM]
    dft = ["WF-DFT-" + name for name in COMMON] + ["WF-DFT-queued_restart_order.ABC"]
    auth = [f"AT-GPU-NATIVE.{module}.{scenario}.{owner}.{phase}"
            for module, scenario in SCENARIOS for owner in "ABC"
            for phase in ("before", "headers", "body")]
    return md + dft + auth


def expected_cases():
    result = []
    for identifier in [i for i in legacy_ids() if i not in {OLD_AC, OLD_GMX}] + [PROVEN, CONTAINED]:
        group = "md" if identifier.startswith("WF-MD-") else "gpu_auth" if identifier.startswith("AT-") else "dft"
        artifact_ui = identifier.startswith(("AT-GPU-NATIVE.formal-md.", "AT-GPU-NATIVE.formal-dft."))
        layer = "native_ui_owned_gpu_artifact" if artifact_ui else "native_ui_real_gpu" if group == "gpu_auth" else "real_gpu_worker_fault"
        engine = "byteff2_openmm" if group == "md" or identifier.startswith("AT-GPU-NATIVE.formal-md.") else "aimnet" if group == "dft" or identifier.startswith("AT-GPU-NATIVE.formal-dft.") else "native_backend_model"
        if identifier in {PROVEN, CONTAINED}:
            point, required = "finite_result_before_ipc", {"smiles": "CCO", "calculation_type": "single_point"}
        elif group == "gpu_auth":
            _, module, scenario, owner, phase = identifier.split(".")
            point = "auth_context_retired_" + phase
            required = {"module": module, "scenario_id": scenario, "owner": owner, "phase": phase}
        else:
            point = FAULT_POINTS[identifier.split("-", 2)[2].split(".")[0]]
            if group == "dft" and identifier == "WF-DFT-parent_restart.B":
                point = "finite_result_before_ipc_parent_exit"
            required = {"smiles": "CCO", "calculation_type": "single_point"} if group == "dft" else {"smiles": "CCO", "protocol": "DensityDemo", "steps": 300}
            if identifier == OPENMM:
                required = MD_ENGINE_POLICY["formal_density"]
        result.append({"id": identifier, "group": group, "execution_layer": layer,
                       "engine": engine, "fault_point": point, "required_parameters": required,
                       "new_instance": identifier in {PROVEN, CONTAINED}})
    return result


def expected_relationships():
    edges = [{"from": [LEGACY, i], "to": [SCOPE, i], "type": "continues_requirement",
              "closes_failure": False} for i in legacy_ids() if i not in {OLD_AC, OLD_GMX}]
    edges += [{"from": [LEGACY, OLD_AC], "to": [SCOPE, i], "type": "contract_supersedes",
               "closes_failure": False} for i in (PROVEN, CONTAINED)]
    edges.append({"from": [LEGACY, OLD_GMX], "to": [SCOPE, OPENMM],
                  "type": "covered_by_existing_requirement", "closes_failure": False})
    edges += [{"from": [LEGACY, i], "to": [SCOPE, "$scope"],
               "type": "scope_removes_requirement", "closes_failure": False} for i in (OLD_AC, OLD_GMX)]
    edges += [{"from": [SCOPE, "$scope"], "to": [SCOPE, gate],
               "type": "requires_environment_gate", "closes_failure": False} for gate in (GATE, CPU_GATE)]
    return edges


def validate_contract(contract):
    require(contract.get("schema_version") == 1 and contract.get("scope_version") == SCOPE,
            "Unsupported scope/schema version")
    legacy = contract.get("legacy", {})
    require(legacy.get("scope_version") == LEGACY and legacy.get("case_ids") == legacy_ids(),
            "Legacy inventory changed")
    require(legacy.get("reported_counts") == {"PASS": 126, "FAIL": 1, "BLOCKED": 1, "NOT_RUN": 0}
            and legacy.get("exceptions") == {OLD_AC: "FAIL", OLD_GMX: "BLOCKED"}, "Legacy statuses changed")
    require(legacy.get("expected_report_sha256") == "15c0170336ad19d80959025ad49b0cba6fea3e2389fcbba8105ceea82a42f196"
            and legacy.get("original_availability") == "deleted_by_user"
            and legacy.get("replayed") is False, "Historical summary cannot impersonate an original")
    require(contract.get("cases") == expected_cases(), "Unknown, duplicate, missing or altered scope case")
    require(contract.get("counts") == {"md": 14, "dft": 15, "gpu_auth": 99, "total": 128}, "Incorrect denominator")
    require(contract.get("md_engine_policy") == MD_ENGINE_POLICY, "MD dynamics/preparation or formal protocol changed")
    require(contract.get("environment_gate") == {"id": GATE, "counted": False, "required_per_real_run": True,
            "applies_to_layers": ["real_gpu_worker_fault", "native_ui_real_gpu"]},
            "Managed recovery must be a required uncounted gate")
    require(contract.get("artifact_ui_environment_gate") == {"id": CPU_GATE, "counted": False,
            "required_layer": "native_ui_owned_gpu_artifact"}, "Artifact browser preservation gate changed")
    require(contract.get("removed_requirements") == [OLD_AC, OLD_GMX], "Incorrect scope removals")
    edges = contract.get("relationships", [])
    nodes = {(LEGACY, i) for i in legacy_ids()} | {(SCOPE, i["id"]) for i in expected_cases()}
    nodes |= {(SCOPE, "$scope"), (SCOPE, GATE), (SCOPE, CPU_GATE)}
    graph = {node: [] for node in nodes}
    for edge in edges:
        start, end = tuple(edge.get("from", [])), tuple(edge.get("to", []))
        require(start in nodes and end in nodes, "Relationship has an unknown versioned key")
        require(edge.get("closes_failure") is False, "Scope changes cannot close historical failures")
        graph[start].append(end)
    visiting, visited = set(), set()
    def visit(node):
        require(node not in visiting, "Relationship cycle")
        if node in visited:
            return
        visiting.add(node)
        for child in graph[node]:
            visit(child)
        visiting.remove(node)
        visited.add(node)
    for node in graph:
        visit(node)
    require(edges == expected_relationships(), "Relationship semantics changed")
    require(contract.get("scope_decision") == "approved" and contract.get("scientific_execution_authorized") is False,
            "Scope approval is not scientific execution authorization")
    require(contract.get("ac_policy") == {
        "phase": "finite_result_before_ipc", "normal_trigger": "native_timeout",
        "single_point_timeout_seconds": 600, "unproven_trigger": "exact_pidfd_sigkill",
        "quarantine_auto_clear": False, "historical_failure_closed": False,
        "future_science_requires_separate_budget": True}, "AC contract policy changed")
    require(contract.get("independent_requirements") == ["service-least-privilege", "SCI-MIX-v1"],
            "Independent requirements cannot disappear from the scope")
    return contract


def parse_json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "Duplicate JSON key")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(InvalidEvidence("Nonfinite JSON number")))


def read_json(path):
    return parse_json(path.read_bytes())


def read_reference_bytes(reference, root):
    if not isinstance(reference, dict) or not reference.get("path"):
        raise UnavailableEvidence("Required evidence reference absent")
    require(sha(reference.get("sha256")), "Invalid reference SHA256")
    path = root / reference["path"]
    try:
        raw = path.read_bytes()
    except OSError:
        raise UnavailableEvidence("Required original file unavailable") from None
    require(digest(raw) == reference["sha256"], "Evidence SHA256 mismatch")
    return raw


def read_reference(reference, root):
    raw = read_reference_bytes(reference, root)
    try:
        return parse_json(raw)
    except (UnicodeError, json.JSONDecodeError):
        raise InvalidEvidence("Evidence is not valid JSON") from None


def all_true(document, names):
    require(all(document.get(name) is True for name in names), "Required proof missing or false")


def finite_number(value):
    return type(value) in (int, float) and -1e100 < value < 1e100


def validate_ac(raw, proof_ids):
    boundary = raw.get("boundary", {})
    require(boundary.get("phase") == "finite_result_before_ipc", "Wrong AC lifecycle boundary")
    all_true(boundary, ("finite_result", "identity_captured_before_fault"))
    require(boundary.get("ipc_delivered") is False, "IPC result already delivered")
    require(raw.get("terminal_status") == "failed" and raw.get("uncommitted_result_published") is False
            and type(raw.get("fault_attempt_publication_count")) is int and raw["fault_attempt_publication_count"] == 0,
            "Faulted attempt must fail without publishing its undelivered result")
    all_true(raw, ("owner_and_committed_artifacts_unchanged", "no_duplicate_execution_or_publication",
                   "abc_baselines_passed", "six_direction_denials_passed"))
    identity = raw.get("target_identity", {})
    require(all(type(identity.get(k)) is int and identity[k] > 0 for k in ("pid", "start_ticks"))
            and all(isinstance(identity.get(k), str) and identity[k] for k in ("boot_id", "cgroup"))
            and identity.get("gpu_uuid") == GPU1, "Incomplete exact GPU1 process identity")
    require(raw.get("owner_label") == "A", "Wrong AC task owner")
    authority = raw.get("termination_authority", {})
    require(authority.get("kind") == "dft_residency" and authority.get("parent_lease_id") is None
            and isinstance(authority.get("lease_id"), str) and authority["lease_id"]
            and type(authority.get("fencing_token")) is int and authority["fencing_token"] > 0
            and isinstance(authority.get("broker_instance_id"), str) and authority["broker_instance_id"]
            and authority.get("target_identity") == identity, "Missing exact residency termination authority")
    if raw["case_id"] == PROVEN:
        require(boundary.get("trigger") == "native_timeout" and boundary.get("timeout_seconds") == 600,
                "Proven recovery requires unchanged native 600-second timeout")
        elapsed = boundary.get("elapsed_since_admission_seconds")
        require(finite_number(elapsed) and elapsed >= 600, "Timeout did not reach native deadline")
        attempts = raw.get("termination_attempts", [])
        require(isinstance(attempts, list) and attempts, "Missing fresh termination proof")
        for attempt in attempts:
            identifier = attempt.get("proof_id")
            require(isinstance(identifier, str) and identifier and identifier not in proof_ids,
                    "Termination evidence reused")
            proof_ids.add(identifier)
            require(attempt.get("target_identity") == identity, "Termination identity mismatch")
            require(attempt.get("termination_authority") == authority, "Termination lease authority mismatch")
            require(attempt.get("stages") == list(TERMINATION_STAGES)
                    and attempt.get("mps_response") == "CUDA_SUCCESS", "Unsafe termination order or MPS response")
            times = attempt.get("stage_times", [])
            require(len(times) == len(TERMINATION_STAGES) and all(finite_number(t) for t in times)
                    and times == sorted(times), "Invalid termination timeline")
            fault_time, release_time = raw.get("fault_time"), raw.get("capacity_release_time")
            require(finite_number(fault_time) and finite_number(release_time)
                    and fault_time <= times[0] <= times[-1] < release_time,
                    "Proof stale or capacity released before proof")
        require(raw.get("broker_instance_before") == authority["broker_instance_id"] == raw.get("broker_instance_after")
                and raw.get("mps_instance_before") and raw["mps_instance_before"] == raw.get("mps_instance_after"),
                "Recovery replaced the control plane")
        require(raw.get("reentry_succeeded_owners") == ["A", "B", "C"], "Missing real ABC reentry")
        require(raw.get("terminal_status") == "failed", "Timed-out result was published")
    else:
        require(boundary.get("trigger") == "exact_pidfd_sigkill", "Wrong unproven fault trigger")
        failure = raw.get("failure", {})
        require(failure.get("stage") == "workload_revalidation"
                and failure.get("code") == "workload_identity_mismatch", "Wrong unproven failure branch")
        require(failure.get("operations_after_failure") == [], "Termination continued after identity failure")
        all_true(raw, ("suspect_observed", "reservation_retained_at_failure", "quarantine_persisted",
                       "related_admission_denied", "external_identities_untouched"))
        snapshot = raw.get("failure_snapshot", {})
        require(snapshot.get("lease_id") == authority["lease_id"]
                and snapshot.get("fencing_token") == authority["fencing_token"]
                and snapshot.get("gpu_uuid") == GPU1 and snapshot.get("lease_status") == "suspect"
                and snapshot.get("reserved_mib") == 4096 and snapshot.get("quarantined") is True
                and snapshot.get("new_residency_admitted") is False, "Missing failure-time residency accounting")
        require(raw.get("automatic_recovery_claimed") is False, "Containment cannot imply automatic recovery")


def check_package(entry, case, root, proof_ids):
    if entry.get("evidence_kind") != "original_package":
        raise UnavailableEvidence("Summary cannot substitute for original evidence")
    qualification = read_reference(entry.get("qualification"), root)
    if qualification.get("review_status") != "APPROVED":
        raise UnavailableEvidence("Case qualification has not been reviewed")
    require(qualification.get("scope_version") == SCOPE and qualification.get("case_id") == case["id"],
            "Qualification version/case mismatch")
    require(isinstance(qualification.get("parameters"), dict) and qualification["parameters"], "Missing locked parameters")
    require(all(qualification["parameters"].get(k) == v for k, v in case["required_parameters"].items()),
            "Qualified parameters violate the locked case contract")
    if case["id"] == OPENMM:
        require(qualification["parameters"] == MD_ENGINE_POLICY["formal_density"],
                "Formal MD request must match the original complete parameter boundary")
    if case["id"] in {PROVEN, CONTAINED}:
        parameters = qualification["parameters"]
        require(parameters.get("smiles") == "CCO" and parameters.get("calculation_type") == "single_point",
                "AC requires the existing CCO single-point scientific input")
    for field in ("source_sha256", "model_sha256", "image_sha256"):
        require(sha(qualification.get(field)), "Missing qualified source/model/image identity")
    raw = read_reference(entry.get("raw"), root)
    require(raw.get("document_kind") == "original_case_result" and raw.get("simulated") is False,
            "Summary or CPU fixture is not a real case result")
    source_version = raw.get("scope_version")
    require(source_version in {SCOPE, LEGACY} and (source_version == SCOPE or not case["new_instance"]),
            "Wrong evidence version")
    require(raw.get("case_id") == case["id"], "Evidence belongs to another case")
    for field in ("engine", "execution_layer", "fault_point"):
        require(raw.get(field) == qualification.get(field) == case[field], "Engine or execution layer mismatch")
    for field in ("parameters", "source_sha256", "model_sha256", "image_sha256"):
        require(raw.get(field) == qualification[field], "Qualified parameters or implementation changed")
    require(raw.get("parameters_sha256") == canonical_digest(qualification["parameters"]), "Parameter hash mismatch")
    require(raw.get("status") == "PASS", "Original execution did not pass")
    run_id = raw.get("run_id")
    require(isinstance(run_id, str) and run_id, "Missing run identity")
    if case["execution_layer"] == "native_ui_owned_gpu_artifact":
        check_artifact_ui(entry, raw, qualification, root)
        return run_id, "verified_evidence_inheritance" if source_version == LEGACY else "new_execution", CPU_GATE
    require(raw.get("gpu_uuid") == GPU1, "Evidence is not restricted to GPU1")
    cleanup, restore = read_reference(entry.get("cleanup"), root), read_reference(entry.get("restore"), root)
    for receipt, kind in ((cleanup, "cleanup_receipt"), (restore, "managed_recovery_receipt")):
        require(receipt.get("document_kind") == kind and receipt.get("run_id") == run_id
                and receipt.get("status") == "PASS" and receipt.get("simulated") is False,
                "Missing or mismatched cleanup/recovery chain")
    all_true(cleanup, CLEANUP_CHECKS)
    all_true(restore, RESTORE_CHECKS)
    require(restore.get("gate_id") == GATE and restore.get("gpu_uuid") == GPU1
            and restore.get("fault_failure_closed") is False, "Invalid environment gate")
    if case["id"] in {PROVEN, CONTAINED}:
        validate_ac(raw, proof_ids)
        if case["id"] == CONTAINED:
            require(restore.get("old_quarantine_preserved") is True, "Quarantine evidence was discarded")
    return run_id, "verified_evidence_inheritance" if source_version == LEGACY else "new_execution", GATE


def check_artifact_ui(entry, raw, qualification, root):
    require(raw.get("gpu_services_started") is False and raw.get("scientific_submissions") == 0
            and raw.get("gpu_uuid") is None, "Artifact UI must not fabricate a current GPU run")
    source = read_reference(entry.get("scientific_source"), root)
    source_cleanup = read_reference(entry.get("scientific_source_cleanup"), root)
    require(source.get("document_kind") == "original_scientific_source" and source.get("simulated") is False
            and source.get("status") == "PASS" and source.get("gpu_uuid") == GPU1
            and source.get("engine") == qualification["engine"], "Missing actual GPU1 scientific source")
    locked_source = qualification.get("scientific_source_parameters", {})
    required_source = {"smiles": "CCO"}
    trajectory = qualification["parameters"]["scenario_id"] == "trajectory"
    if source["engine"] == "byteff2_openmm":
        if trajectory or locked_source.get("protocol") == "Density":
            required_source = MD_ENGINE_POLICY["formal_density"]
            require(locked_source == required_source, "Formal MD source request drifted from original full parameters")
        else:
            required_source.update(protocol="DensityDemo", steps=300)
    elif trajectory:
        required_source["calculation_type"] = "optimization"
    else:
        require(locked_source.get("calculation_type") in {"single_point", "optimization"},
                "DFT artifact needs an explicitly qualified calculation type")
    require(locked_source and all(locked_source.get(k) == v for k, v in required_source.items())
            and source.get("parameters") == locked_source, "Scientific artifact source parameters mismatch")
    for field in ("source_sha256", "model_sha256", "image_sha256"):
        require(sha(qualification.get("scientific_" + field))
                and source.get(field) == qualification["scientific_" + field], "Scientific source identity mismatch")
    require(source.get("run_id") and source["run_id"] != raw["run_id"]
            and source_cleanup.get("run_id") == source["run_id"]
            and source_cleanup.get("document_kind") == "cleanup_receipt"
            and source_cleanup.get("simulated") is False and source_cleanup.get("status") == "PASS",
            "Missing separate scientific source cleanup chain")
    all_true(source_cleanup, CLEANUP_CHECKS)
    artifact = read_reference_bytes(entry.get("artifact"), root)
    require(digest(artifact) == source.get("artifact_sha256") == qualification.get("artifact_sha256")
            == raw.get("observed_artifact_sha256"), "UI artifact is not the qualified scientific artifact")
    cleanup, preservation = read_reference(entry.get("cleanup"), root), read_reference(entry.get("restore"), root)
    for receipt, kind in ((cleanup, "cpu_fixture_cleanup_receipt"), (preservation, "environment_preservation_receipt")):
        require(receipt.get("document_kind") == kind and receipt.get("run_id") == raw["run_id"]
                and receipt.get("status") == "PASS" and receipt.get("simulated") is False,
                "Artifact browser cleanup/preservation chain mismatch")
    all_true(cleanup, CPU_CLEANUP_CHECKS)
    all_true(preservation, ("no_service_mutations", "gpu_only_preserved", "development_health_unchanged"))
    require(preservation.get("gate_id") == CPU_GATE and preservation.get("fault_failure_closed") is False,
            "Invalid CPU browser environment gate")


def adjudicate(contract, manifest=None, base_dir=Path(".")):
    validate_contract(contract)
    if manifest is None:
        manifest = {"schema_version": 1, "scope_version": SCOPE, "entries": []}
    require(manifest.get("schema_version") == 1 and manifest.get("scope_version") == SCOPE,
            "Evidence manifest version mismatch")
    require(isinstance(manifest.get("entries"), list), "Evidence entries must be an explicit list")
    entries, seen = {}, set()
    known = {c["id"] for c in contract["cases"]}
    for entry in manifest.get("entries", []):
        key = entry.get("case_id")
        require(key in known and key not in seen, "Unknown or duplicate evidence case")
        require(entry.get("scope_version") == SCOPE, "Evidence entry needs an exact versioned key")
        seen.add(key)
        entries[key] = entry
    rows, gates, proof_ids = [], {}, set()
    for case in contract["cases"]:
        identifier = case["id"]
        row = {"scope_version": SCOPE, "case_id": identifier,
               "reported_historical_status": None if case["new_instance"] else "PASS",
               "execution_status": "NOT_RUN", "current_status": "NOT_RUN" if case["new_instance"] else "BLOCKED",
               "evidence_status": "NOT_PRODUCED" if case["new_instance"] else "SUMMARY_ONLY_RAW_DELETED",
               "reason": "new_instance_not_executed" if case["new_instance"] else "original_evidence_unavailable"}
        if identifier in entries:
            try:
                run, relationship, gate_id = check_package(entries[identifier], case, Path(base_dir), proof_ids)
                row.update(current_status="PASS", evidence_status="VERIFIED_ORIGINAL", reason=None,
                           execution_status="PASS" if relationship == "new_execution" else "NOT_RUN",
                           relationship=relationship, source_run_id=run)
                gates[(run, gate_id)] = {"run_id": run, "id": gate_id, "status": "PASS", "counted": False}
            except UnavailableEvidence as error:
                row.update(current_status="BLOCKED", evidence_status="UNAVAILABLE", reason=str(error))
            except (InvalidEvidence, KeyError, TypeError, AttributeError) as error:
                row.update(current_status="FAIL", evidence_status="INVALID_PACKAGE", reason=str(error))
        rows.append(row)
    counts = {status: sum(r["current_status"] == status for r in rows)
              for status in ("PASS", "FAIL", "BLOCKED", "NOT_RUN")}
    return {"schema_version": 1, "scope_version": SCOPE, "contract_status": "VALID", "contract_valid": True,
            "legacy_summary": contract["legacy"],
            "counts": counts, "cases": rows, "environment_gates": list(gates.values()),
            "environment_gate_requirement": contract["environment_gate"],
            "artifact_ui_environment_gate_requirement": contract["artifact_ui_environment_gate"],
            "status": "FAIL" if counts["FAIL"] else "PASS" if counts["PASS"] == 128 else "BLOCKED",
            "overall_multiuser_status": "NOT_ASSESSED", "independent_requirements": contract["independent_requirements"],
            "scientific_executions_started_by_tool": 0, "contract_sha256": canonical_digest(contract)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, default=Path(__file__).resolve().parents[1] / "contracts/multiuser_scope_v2.json")
    parser.add_argument("--evidence", type=Path, help="Explicit file manifest; relative references resolve beside it")
    parser.add_argument("--output", type=Path, help="New output file; existing files are never overwritten")
    args = parser.parse_args(argv)
    try:
        report = adjudicate(read_json(args.contract), read_json(args.evidence) if args.evidence else None,
                            args.evidence.parent if args.evidence else Path("."))
        rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write(rendered)
        else:
            print(rendered, end="")
        return 0 if report["status"] == "PASS" else 1
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        print(json.dumps({"status": "INVALID", "error_type": type(error).__name__,
                          "scientific_executions_started_by_tool": 0}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
