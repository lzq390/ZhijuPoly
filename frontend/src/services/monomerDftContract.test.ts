import { describe, expect, it } from "vitest";
import contractJson from "../../../contracts/monomer_dft_api_contract_v1.json";
import type {
  MonomerDftArtifactDeleteResponse,
  MonomerDftArtifactsState,
  MonomerDftResult
} from "../types";

type Contract = {
  schema_version: number;
  public_api_prefix: string;
  worker_http_protocol_version: number;
  database_schema_gate: {
    migration_version: string;
    migration_checksum_sha256: string;
    readiness_field: string;
    safe_without_schema: string[];
    guarded_resource_prefixes: string[];
    not_ready_error: {
      http_status: number;
      code: string;
      retry_after_seconds: number;
    };
  };
  scientific_results: {
    readable_schema_versions: number[];
    produced_schema_version: number;
    v1_optional_rdkit_fields: string[];
    v1_optional_provenance_fields: string[];
    v2_required_atom_fields: string[];
    v2_required_provenance_fields: string[];
    v2_required_timing_fields: string[];
  };
  artifacts_states: string[];
  delete_examples: Array<{
    http_status: number;
    retry_after: string | null;
    body: Record<string, unknown>;
  }>;
  stable_error_codes: string[];
  error_example: Record<string, unknown>;
};

const contract = contractJson as Contract;

const artifactStates: MonomerDftArtifactsState[] = [
  "none",
  "available",
  "delete_requested",
  "deleted"
];
type V2Result = MonomerDftResult & { schema_version: 2 };
const v2AtomFields: Array<keyof V2Result["atoms"]> = [
  "isotope_mass_numbers",
  "atomic_masses_u"
];
const stableErrorCodes = [
  "submission_disabled",
  "schema_not_ready",
  "worker_socket_not_configured",
  "worker_unavailable",
  "gpu_capacity_unavailable",
  "gpu_lease_lost",
  "gpu_runtime_unhealthy",
  "charge_out_of_range",
  "unsupported_isotope",
  "artifact_integrity_mismatch",
  "artifact_bundle_invalid",
  "artifact_size_out_of_contract",
  "download_capacity_full",
  "artifact_deletion_pending",
  "journal_upgrade_missing_enqueue_sequence"
];

describe("monomer DFT shared public API contract", () => {
  it("keeps the frontend artifact state and scientific-result versions aligned", () => {
    expect(contract.schema_version).toBe(1);
    expect(contract.public_api_prefix).toBe("/api/v1/monomer-dft");
    expect(contract.scientific_results.readable_schema_versions).toEqual([1, 2]);
    expect(contract.scientific_results.produced_schema_version).toBe(2);
    expect(contract.artifacts_states).toEqual(artifactStates);
    expect(contract.scientific_results.v1_optional_rdkit_fields).toEqual([
      "optimization_performed",
      "optimization_state"
    ]);
    expect(contract.scientific_results.v1_optional_provenance_fields).toEqual([
      "rdkit_optimization_performed",
      "rdkit_optimization_status"
    ]);
    expect(contract.scientific_results.v2_required_atom_fields).toEqual(v2AtomFields);
    expect(contract.scientific_results.v2_required_provenance_fields).toEqual([
      "rdkit_optimization_performed",
      "rdkit_optimization_status",
      "rdkit_version",
      "mass_source",
      "execution_path",
      "gpu_uuid",
      "gpu_budget_mib",
      "broker_instance_id",
      "lease_id",
      "fencing_token"
    ]);
    expect(contract.scientific_results.v2_required_timing_fields).toEqual([
      "queue_wait_ms",
      "gpu_wait_ms",
      "model_load_ms",
      "structure_prepare_ms",
      "model_compute_ms",
      "optimization_ms",
      "hessian_ms",
      "frequency_ms",
      "artifact_ms",
      "total_ms"
    ]);
  });

  it("pins the 0013 schema-readiness boundary", () => {
    expect(contract.database_schema_gate).toEqual({
      migration_version: "0013_monomer_dft_jobs",
      migration_checksum_sha256:
        "ab633a6253887dad45103c288d54a0d02d4d69ce1f9a14c1271338d448f9acbc",
      readiness_field: "schema_ready",
      safe_without_schema: ["/status", "/capabilities"],
      guarded_resource_prefixes: ["/jobs"],
      not_ready_error: {
        http_status: 503,
        code: "schema_not_ready",
        retry_after_seconds: 5
      }
    });
  });

  it("validates immediate and pending artifact deletion examples", () => {
    expect(contract.delete_examples.map((example) => example.http_status)).toEqual([200, 202]);
    expect(contract.delete_examples.map((example) => example.retry_after)).toEqual([null, "5"]);
    const bodies = contract.delete_examples.map((example) => example.body as MonomerDftArtifactDeleteResponse);
    expect(bodies.map((body) => body.artifacts_state)).toEqual(["deleted", "delete_requested"]);
    for (const body of bodies) {
      expect(typeof body.job_id).toBe("string");
      expect(typeof body.deleted).toBe("boolean");
      expect(Number.isInteger(body.deleted_artifacts)).toBe(true);
      expect(artifactStates).toContain(body.artifacts_state);
      expect(typeof body.message).toBe("string");
    }
  });

  it("pins stable public error codes and the structured error shape", () => {
    expect(contract.stable_error_codes).toEqual(stableErrorCodes);
    expect(contract.error_example).toEqual({
      code: "unsupported_isotope",
      message: "The requested isotope is not supported.",
      retryable: false,
      details: {}
    });
  });
});
