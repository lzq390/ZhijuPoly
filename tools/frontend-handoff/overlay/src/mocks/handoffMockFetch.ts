import type { PolymerResult, MdDemoStageSummary, MdDemoSummary, MdDemoFixtureMetadata, MdDemoRunResponse, MonomerMdTrajectoryTimeline, MonomerMdJobResponse, MonomerDftResult, MonomerDftJobResponse, OnlineKnowledgeSearchResponse, ReverseDesignTgResponse, ConditionalGenerationTgResponse, PolytaoGenerationResponse, PropertyFilterOptionsResponse, DatasetSummaryResponse, DatabaseAnalyticsResponse } from "../types";
import { mockEventStream, type MockEvent } from "./handoffState";
import entitlementsFixture from "../../scripts/fixtures/entitlements.json";
import type { AuthSession } from "../auth/session";
import type { BatchImport, BatchImportPreview, BatchJob, BatchMapping, BatchStatistics } from "../types/polymerizationBatch";

/** Browser-only, synthetic fixtures. Never sends business requests to a server. */
export const HANDOFF_MOCK_SCENARIOS = ["success", "loading", "empty", "error"] as const;

export type HandoffMockScenario = (typeof HANDOFF_MOCK_SCENARIOS)[number];

type MockFetchOptions = {
  apiBaseUrl?: string;
  pageUrl?: string;
  nativeFetch?: typeof fetch;
};

const SYNTHETIC_NOW = "2030-01-01T00:00:00Z";
const JSON_HEADERS = { "Content-Type": "application/json; charset=utf-8" };
const SIMPLE_STRUCTURE_SVG = [
  '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 240 120">',
  '<rect width="240" height="120" rx="12" fill="#f8fafc"/>',
  '<path d="M42 62h48l24-34 24 34h48" fill="none" stroke="#334155" stroke-width="5"/>',
  '<circle cx="42" cy="62" r="9" fill="#2563eb"/>',
  '<circle cx="186" cy="62" r="9" fill="#16a34a"/>',
  '<text x="120" y="104" text-anchor="middle" font-size="14" fill="#64748b">SYNTHETIC</text>',
  "</svg>"
].join("");

const SIMPLE_MOLBLOCK = [
  "Synthetic handoff structure",
  "  NexPoly  2D",
  "",
  "  2  1  0  0  0  0            999 V2000",
  "    0.0000    0.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0",
  "    1.4000    0.0000    0.0000 O   0  0  0  0  0  0  0  0  0  0  0  0",
  "  1  2  1  0  0  0  0",
  "M  END"
].join("\n");

function jsonResponse(payload: unknown, status = 200, headers: HeadersInit = {}): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { ...JSON_HEADERS, ...headers }
  });
}

function noContentResponse(): Response {
  return new Response(null, { status: 204 });
}

function getScenario(pageUrl: string): HandoffMockScenario {
  const value = new URL(pageUrl).searchParams.get("mockScenario");
  return HANDOFF_MOCK_SCENARIOS.includes(value as HandoffMockScenario)
    ? value as HandoffMockScenario
    : "success";
}

function waitForAbort(signal?: AbortSignal | null): Promise<Response> {
  if (signal?.aborted) {
    return Promise.reject(new DOMException("The operation was aborted.", "AbortError"));
  }
  return new Promise((_resolve, reject) => {
    signal?.addEventListener(
      "abort",
      () => reject(new DOMException("The operation was aborted.", "AbortError")),
      { once: true }
    );
  });
}

function requestSignal(input: RequestInfo | URL, init?: RequestInit): AbortSignal | null | undefined {
  if (init?.signal) return init.signal;
  return typeof Request !== "undefined" && input instanceof Request ? input.signal : null;
}

async function requestJson(input: RequestInfo | URL, init?: RequestInit): Promise<Record<string, unknown>> {
  if (typeof init?.body === "string") {
    try {
      return JSON.parse(init.body) as Record<string, unknown>;
    } catch {
      return {};
    }
  }
  if (typeof Request !== "undefined" && input instanceof Request) {
    try {
      return await input.clone().json() as Record<string, unknown>;
    } catch {
      return {};
    }
  }
  return {};
}

function requestMethod(input: RequestInfo | URL, init?: RequestInit): string {
  if (init?.method) return init.method.toUpperCase();
  return typeof Request !== "undefined" && input instanceof Request ? input.method.toUpperCase() : "GET";
}

function polymerResult(): PolymerResult {
  return {
    polymer_id: "SYN-POLY-001",
    polymer_name: "合成示例聚合物",
    smiles: "*NCCNC(=O)CC(=O)*",
    canonical_smiles: "*NCCNC(=O)CC(=O)*",
    similarity_score: 0.92,
    structure_svg: SIMPLE_STRUCTURE_SVG,
    matched_property_name: "Glass transition temperature",
    matched_property_value: 218,
    matched_property_unit: "°C",
    matched_property_source: "synthetic",
    properties: {
      thermal: [{
        property_category: "thermal",
        property_name: "Glass transition temperature",
        property_value: "218",
        property_value_num: 218,
        property_unit: "°C",
        label_source: "synthetic"
      }],
      mechanical: [],
      electrical: [],
      chemical: [],
      optical: [],
      other: []
    }
  };
}

function mdStage(): MdDemoStageSummary {
  return {
    stage_id: "eq1",
    label: "合成平衡阶段",
    description: "仅用于页面布局的确定性模拟阶段。",
    n_atoms: 4,
    n_frames: 3,
    dt_ps: 0.5,
    n_chains: 1,
    data_file_size_bytes: 128,
    trajectory_file_size_bytes: 256,
    log_file_size_bytes: 64,
    box: { lx: 20, ly: 20, lz: 20 }
  };
}

function mdSummary(): MdDemoSummary {
  return {
    primary_stage: "eq1",
    elapsed_seconds: 1.2,
    n_atoms: 4,
    n_frames: 3,
    n_chains: 1,
    final_density_g_cm3: 1.2,
    mean_temperature_k: 300,
    mean_total_energy_kcal_mol: -12.5
  };
}

function mdFixtureMetadata(): MdDemoFixtureMetadata {
  return {
    fixture_version: 1,
    source: {
      label: "Synthetic UI fixture",
      data_root_hint: "none",
      generated_from: ["deterministic handoff mock"]
    }
  };
}

function mdRunResponse(): MdDemoRunResponse {
  const points = [
    { atom_id: 1, chain_id: 1, atom_type: "C", x: 0, y: 0, z: 0 },
    { atom_id: 2, chain_id: 1, atom_type: "O", x: 1.4, y: 0, z: 0 }
  ];
  return {
    input: { smiles: "CCO", temperature: 300, pressure: 1, n_atom: 4, n_chain: 1, forcefield: "synthetic" },
    run_id: "synthetic-md-demo",
    status: "completed",
    query_time_ms: 12,
    stages: [mdStage()],
    summary: mdSummary(),
    density_series: {
      key: "density",
      label: "Density",
      unit: "g/cm³",
      points: [{ time_ps: 0, value: 1.1 }, { time_ps: 1, value: 1.2 }]
    },
    thermo_series: [{
      key: "temperature",
      label: "Temperature",
      unit: "K",
      points: [{ time_ps: 0, value: 298 }, { time_ps: 1, value: 300 }]
    }],
    trajectory_preview: {
      stage_id: "eq1",
      frame_index: 0,
      time_ps: 0,
      sampled_points: points.length,
      points,
      box: { lx: 20, ly: 20, lz: 20 }
    },
    atom_distance_series: null,
    fixture_metadata: mdFixtureMetadata()
  };
}

function mdTimeline(): MonomerMdTrajectoryTimeline {
  return { schema_version: 1, source_frame_count: 2, sampled_frame_count: 2, total_atoms: 2, sampled_points: 2,
    coordinate_unit: "angstrom", coordinate_scale: 0.01, coordinate_encoding: "int16-delta-gzip-base64", coordinate_byte_order: "little", decoded_byte_length: 24,
    atoms: [{ atom_id: 1, chain_id: 1, atom_type: "C", element: "C" }, { atom_id: 2, chain_id: 1, atom_type: "O", element: "O" }],
    frames: [{ frame_index: 0, time_ps: 0 }, { frame_index: 1, time_ps: 1 }], coordinates: "H4sIAAAAAAACA2NgAIEeBgRgApMAoPSbXBgAAAA=" };
}

function monomerMdJob(status: "running" | "completed" | "cancelled" = "completed"): MonomerMdJobResponse {
  const completed = status === "completed";
  return {
    job_id: "a0000000000000000000000000000001",
    status,
    input_smiles: "CCO",
    canonical_smiles: "CCO",
    smiles: "CCO",
    protocol: "DensityDemo",
    run_mode: "demo",
    requested_steps: 100,
    completed_steps: completed ? 100 : 48,
    progress_percent: completed ? 100 : 48,
    progress_stage: completed ? "completed" : "simulation",
    progress_message: completed ? "合成任务已完成" : "合成任务运行中",
    queue_position: null,
    created_at: SYNTHETIC_NOW,
    updated_at: SYNTHETIC_NOW,
    started_at: SYNTHETIC_NOW,
    finished_at: completed ? SYNTHETIC_NOW : null,
    error_message: null,
    artifacts: completed ? [{ name: "synthetic-md.csv", url: "/handoff/downloads/result.csv", size_bytes: 128 }] : [],
    result: completed ? {
      protocol: "DensityDemo",
      run_mode: "demo",
      density_series: [{ step: 0, value: 1.1 }, { step: 100, value: 1.2 }],
      summary: { density_g_cm3: 1.2, synthetic: true },
      artifacts: [{ name: "synthetic-md.csv", url: "/handoff/downloads/result.csv", size_bytes: 128 }],
      trajectory_preview: mdRunResponse().trajectory_preview,
      visualization: { schema_version: 3, status: "complete", default_stage_id: "synthetic-stage", stages: [{ stage_id: "synthetic-stage", label: "合成演示轨迹",
        density_series: [{ time_ps: 0, value: 1.1 }, { time_ps: 1, value: 1.2 }], trajectory_preview: mdRunResponse().trajectory_preview,
        trajectory_timeline_available: true, trajectory_timeline_summary: mdTimeline() }] },
      warnings: ["UI-only synthetic result"]
    } : null
  };
}

function monomerDftResult(): MonomerDftResult {
  return {
    schema_version: 2,
    calculation_type: "single_point",
    engine: "aimnet2",
    model: "aimnet2",
    input: {
      input_type: "smiles",
      canonical_smiles: "O",
      net_charge: 0,
      input_formal_charge: 0,
      multiplicity: 1,
      electron_count: 10
    },
    atoms: {
      count: 3,
      atomic_numbers: [8, 1, 1],
      symbols: ["O", "H", "H"],
      isotope_mass_numbers: [0, 0, 0],
      atomic_masses_u: [15.999, 1.008, 1.008]
    },
    geometry: {
      initial_coordinates_angstrom: [[0, 0, 0], [0, 0, 1], [1, 0, 0]],
      final_coordinates_angstrom: [[0, 0, 0], [0, 0, 1], [1, 0, 0]],
      units: "angstrom"
    },
    rdkit: {
      seed: 1,
      force_field: "MMFF94",
      optimization_status: 0,
      optimization_performed: true,
      optimization_state: "converged"
    },
    properties: {
      energy: { value_eV: -1 },
      charges: { values_e: [0, 0, 0], sum_e: 0, conservation_error_e: 0, conserved: true },
      forces: {
        values_eV_per_A: [[0, 0, 0], [0, 0, 0], [0, 0, 0]],
        fmax_eV_per_A: 0
      }
    },
    optimization: null,
    scientific_status: {
      calculation_completed: true,
      geometry_status: "not_optimized",
      is_stationary: true,
      minimum_assessment: "unassessed",
      stationary_point: "not_evaluated",
      fmax_eV_per_A: 0
    },
    warnings: [{ code: "synthetic_fixture", message: "UI-only synthetic result." }],
    timings: { queue_wait_ms: 1, model_compute_ms: 4, total_ms: 8 },
    provenance: {
      rdkit_optimization_performed: true,
      rdkit_optimization_status: 0,
      rdkit_version: "synthetic",
      mass_source: "synthetic",
      execution_path: "primary",
      gpu_uuid: "synthetic-device",
      gpu_budget_mib: 0,
      broker_instance_id: "synthetic-broker",
      lease_id: "synthetic-lease",
      fencing_token: 1
    }
  };
}

function monomerDftJob(status: "running" | "completed" | "cancelled" = "completed"): MonomerDftJobResponse {
  const completed = status === "completed";
  return {
    job_id: "d0000000-0000-4000-8000-000000000001",
    calculation_type: "single_point",
    status,
    request: {
      input: { smiles: "O", net_charge: 0, multiplicity: 1, psmiles_mode: null },
      model: "aimnet2",
      conformer: { seed: 1, max_iterations: 100 },
      calculation_type: "single_point",
      single_point: { properties: ["energy", "charges", "forces"] }
    },
    request_sha256: "0".repeat(64),
    attempt: 1,
    queue_position: null,
    stage: completed ? "completed" : "single_point",
    progress_percent: completed ? 100 : 55,
    scientific_status: completed ? "completed" : null,
    warnings: ["UI-only synthetic job"],
    result: completed ? monomerDftResult() : null,
    timings: { queue_wait_ms: 1, total_ms: completed ? 8 : 2 },
    provenance: { gpu_name: "synthetic-device" },
    error: null,
    artifacts: completed ? [{
      artifact_id: "scientific_result",
      name: "synthetic_result.json",
      media_type: "application/json",
      size_bytes: 128,
      sha256: "0".repeat(64),
      available: true
    }] : [],
    artifacts_state: completed ? "available" : "none",
    artifacts_deleted: false,
    cancel_requested: false,
    created_at: SYNTHETIC_NOW,
    updated_at: SYNTHETIC_NOW,
    started_at: SYNTHETIC_NOW,
    finished_at: completed ? SYNTHETIC_NOW : null,
    idempotent_replay: false
  };
}

function onlineKnowledgeResult(): OnlineKnowledgeSearchResponse {
  return {
    material: "Synthetic Polymer A",
    mode: "property",
    query_time_ms: 24,
    totalPapers: 2,
    max_papers: 20,
    exampleUsed: true,
    stats: { synthetic: true },
    syntheses: [],
    propertyPoints: [{
      polymer_type: "synthetic",
      polymer_name: "Synthetic Polymer A",
      condition_name: "temperature",
      condition_value: "300 K",
      property_name: "tensile strength",
      property_value: "50 MPa",
      relationship: "illustrative",
      paper_title: "Synthetic Literature Record"
    }],
    temperatureDistribution: [{ label: "300 K", count: 1, percentage: 100 }],
    solventDistribution: [],
    catalystTable: [],
    tempLabels: [],
    conditionSummary: ["temperature: 300 K"],
    reactionTypeTable: [],
    propertyNameDistribution: [{ label: "tensile strength", count: 1, percentage: 100 }],
    conditionDistribution: [{ label: "temperature", count: 1, percentage: 100 }],
    polymerTypeDistribution: [{ label: "synthetic", count: 1, percentage: 100 }],
    relationshipDistribution: [{ label: "illustrative", count: 1, percentage: 100 }],
    dataframe: [{ source: "synthetic", value: 50 }]
  };
}

function reverseDesignResult(): ReverseDesignTgResponse {
  return {
    target_tg: 220,
    query_time_ms: 18,
    candidate_pool_size: 12,
    sampled_candidate_count: 3,
    total: 2,
    data_source: "pi_reverse_design",
    results: [1, 2].map((rank) => ({
      rank,
      pi_id: 900_000 + rank,
      polymer_smiles: rank === 1 ? "*NCCNC(=O)CC(=O)*" : "*NCCCNC(=O)CC(=O)*",
      canonical_polym: null,
      monomer_a_smiles: "NCCN",
      monomer_b_smiles: "O=C(O)CC(=O)O",
      monomer_a_iupac: `Synthetic monomer A${rank}`,
      monomer_b_iupac: `Synthetic monomer B${rank}`,
      monomer_a_structure_svg: SIMPLE_STRUCTURE_SVG,
      monomer_b_structure_svg: SIMPLE_STRUCTURE_SVG,
      tg_value: 218 + rank,
      tg_unit: "°C",
      tg_difference: rank - 1,
      similarity_score: 0.9 - rank * 0.05,
      structure_svg: SIMPLE_STRUCTURE_SVG,
      knowledge_available: true
    }))
  };
}

function conditionalGenerationResult(): ConditionalGenerationTgResponse {
  return {
    input_smiles: "*CC*",
    normalized_input_smiles: "*CC*",
    delta_tg: 30,
    query_time_ms: 16,
    requested_count: 2,
    returned_count: 2,
    attempts: 2,
    filter_counter: { accepted: 2 },
    results: [1, 2].map((rank) => ({
      rank,
      generated_smiles: rank === 1 ? "*CC*" : "*CCC*",
      structure_svg: SIMPLE_STRUCTURE_SVG,
      predicted_tg: 210 + rank * 5,
      tg_unit: "°C",
      tg_error: rank,
      similarity_score: 0.9 - rank * 0.05,
      sa_score: 2 + rank * 0.1
    }))
  };
}

function polytaoResult(): PolytaoGenerationResponse {
  return {
    prompt: "synthetic descriptor prompt",
    query_time_ms: 14,
    requested_count: 2,
    returned_count: 2,
    attempts: 2,
    filter_counter: { accepted: 2 },
    results: [1, 2].map((rank) => ({
      rank,
      generated_smiles: rank === 1 ? "*COC*" : "*COCC*",
      raw_smiles: rank === 1 ? "*COC*" : "*COCC*",
      structure_svg: SIMPLE_STRUCTURE_SVG,
      valid_smiles: true,
      sa_score: 2 + rank * 0.2,
      warnings: []
    }))
  };
}

function browseEnvelope(results: unknown[]) {
  return {
    query: "",
    page: 1,
    page_size: 20,
    query_time_ms: 4,
    total_records: results.length,
    matched_records: results.length,
    data_source: "synthetic",
    source_status: results.length ? "ready" : "empty",
    source_message: null,
    results
  };
}

function propertyFilterOptions(empty: boolean): PropertyFilterOptionsResponse {
  return {
    query_time_ms: 3,
    total_records: empty ? 0 : 24,
    mapped_records: empty ? 0 : 24,
    raw_records: 0,
    data_source: "synthetic",
    source_status: empty ? "empty" : "ready",
    source_message: null,
    options: empty ? [] : [{
      filter_type: "standardized",
      option_key: "standardized:tg:C",
      label: "玻璃化转变温度 (Tg)",
      property_key: "tg",
      property_name: null,
      property_unit_clean: null,
      canonical_unit: "°C",
      rows: 24,
      unique_smiles: 12,
      min_value: 120,
      p5_value: 130,
      median_value: 180,
      p95_value: 230,
      max_value: 240
    }]
  };
}

function databaseSummary(empty: boolean): DatasetSummaryResponse {
  const definitions = [
    ["process", "Synthetic Process Data"],
    ["property", "Synthetic Property Data"],
    ["structureEffect", "Synthetic Structure-Property Data"],
    ["dft", "Synthetic DFT Data"],
    ["formulation", "Synthetic Formulation Data"]
  ];
  return {
    query_time_ms: 2,
    backend: "synthetic",
    datasets: definitions.map(([key, title]) => ({
      key,
      title,
      total_records: empty ? 0 : 12,
      data_source: "synthetic",
      source_status: empty ? "empty" : "ready",
      source_message: null,
      latest_import_status: "synthetic",
      latest_import_finished_at: SYNTHETIC_NOW
    }))
  };
}

function databaseAnalytics(empty: boolean): DatabaseAnalyticsResponse {
  const range = { label: "Synthetic range", count: empty ? 0 : 12, min: 0, p5: 10, median: 50, p95: 90, max: 100 };
  return {
    query_time_ms: 3,
    backend: "synthetic",
    source: "handoff-mock",
    generated_at: SYNTHETIC_NOW,
    datasets: {
      process: {
        rows: empty ? 0 : 12,
        uniqueRecordIds: empty ? 0 : 12,
        uniquePolymers: empty ? 0 : 4,
        uniqueProducts: empty ? 0 : 4,
        avgProcessTextLength: empty ? 0 : 80,
        processSignalSummary: { extractedRows: empty ? 0 : 8, uniqueSnippets: empty ? 0 : 12, medianChars: empty ? 0 : 64 },
        processSignals: empty ? [] : [{ label: "synthetic process", value: 12, total: 12 }],
        topTerms: empty ? [] : [{ label: "synthetic", value: 12 }],
        topProducts: empty ? [] : [{ label: "Synthetic Polymer", value: 12 }],
        topMaterials: empty ? [] : [{ label: "Synthetic Monomer", value: 12 }]
      },
      property: {
        rows: empty ? 0 : 12,
        uniquePolymers: empty ? 0 : 4,
        uniqueProperties: empty ? 0 : 2,
        categories: empty ? [] : [{ label: "thermal", value: 12, color: "#3b82f6" }],
        topProperties: empty ? [] : [{ label: "Tg", value: 12 }],
        ranges: empty ? [] : [range],
        categoryTop: empty ? [] : [{ label: "thermal: Tg", value: 12 }]
      },
      structureEffect: {
        rows: empty ? 0 : 12,
        uniqueSmiles: empty ? 0 : 4,
        properties: empty ? [] : [{ label: "Tg", value: 12 }],
        units: empty ? [] : [{ label: "°C", value: 12, color: "#8b5cf6" }],
        sources: empty ? [] : [{ label: "synthetic", value: 12 }],
        sourceMatrix: empty ? [] : [{ label: "Tg", exp: 0, sim: 12, na: 0 }],
        ranges: empty ? [] : [range]
      },
      dft: {
        rows: empty ? 0 : 12,
        molCount: empty ? 0 : 3,
        energyRange: range,
        gapRange: range,
        orbitalDistributions: [],
        stepRange: range,
        atomRange: range,
        atomTotals: empty ? [] : [{ label: "C", value: 6 }],
        convergence: empty ? [] : [{ label: "converged", value: 3 }]
      },
      formulation: {
        files: empty ? 0 : 1,
        rows: empty ? 0 : 4,
        coverage: empty ? [] : [{ label: "synthetic", count: 4, pct: 100 }],
        componentCounts: empty ? [] : [{ label: "2", value: 4 }],
        topComponents: empty ? [] : [{ label: "Synthetic A", value: 4 }],
        polymerFamilies: empty ? [] : [{ label: "synthetic", value: 4, color: "#0f9f8f" }],
        ratioTypes: empty ? [] : [{ label: "ratio", value: 4 }],
        tempBands: empty ? [] : [{ label: "300 K", value: 4 }],
        timeUnits: empty ? [] : [{ label: "hours", value: 4 }],
        topCatalysts: [],
        topSolvents: [],
        examples: empty ? [] : [{ title: "SYN-1", polymer: "synthetic", formula: "A:B = 1:1", condition: "300 K · 1 h" }]
      }
    }
  };
}

type DemoJob = { job_id: string; status: string; [key: string]: unknown };
type Recording = { recording_id: string; status: string; started_at: string; ended_at: string; events: Record<string, unknown>[] };
type MockState = {
  nextId: number;
  fetchAsset: (path: string) => Promise<Response>;
  session: AuthSession;
  md: Map<string, DemoJob>;
  dft: Map<string, DemoJob>;
  imports: Map<string, BatchImport>;
  batches: Map<string, BatchJob>;
  idempotentBatches: Map<string, string>;
  recordings: Map<string, Recording>;
  measurements: Record<string, unknown>[];
  subscribers: Record<"md" | "dft", Set<(event: MockEvent) => void>>;
};
const guestSession = (): AuthSession => ({ authenticated: false, user: null, session_id: null, csrf_token: null, capabilities: {} });
function authenticatedSession(username = "handoff", generation = 1): AuthSession {
  return { authenticated: true, account_type: "member", user: { id: "synthetic-handoff-user", username, must_change_password: false },
    session_id: `synthetic-session-${generation}`, csrf_token: `synthetic-csrf-${generation}`, // Ordinary members receive openscience.use in backend/app/auth/policy.py.
    // The staged Vite configuration still fixes its remote embed URL to empty.
    capabilities: { dev_gpu_control: false, "openscience.use": true } };
}
function createState(pageUrl: string, fetchAsset: MockState["fetchAsset"]): MockState {
  const empty = getScenario(pageUrl) === "empty";
  const dft = monomerDftJob();
  const md = monomerMdJob();
  return {
    nextId: 100, fetchAsset, session: new URL(pageUrl).searchParams.get("mockAuth") === "guest" ? guestSession() : authenticatedSession(),
    md: new Map(empty ? [] : [[md.job_id, { ...md, run_mode: "formal", protocol: "Density" }], ["a0000000000000000000000000000002", { ...monomerMdJob("running"), job_id: "a0000000000000000000000000000002", run_mode: "formal", protocol: "Density" }]]),
    dft: new Map(empty ? [] : [[dft.job_id, dft], ["d0000000-0000-4000-8000-000000000002", { ...monomerDftJob("running"), job_id: "d0000000-0000-4000-8000-000000000002" }]]),
    measurements: empty ? [] : [{ id: 900001, sampleId: "SYN-SAMPLE-001", experimentProject: "Synthetic tensile test", instrumentId: "SYN-INSTRUMENT", operator: "SYNTHETIC", collectionTime: SYNTHETIC_NOW, temperature: 300, concentration: null, resultValue: 50, resultUnit: "MPa", remarks: "UI-only synthetic record" }],
    imports: new Map(), batches: new Map(), idempotentBatches: new Map(), recordings: new Map(), subscribers: { md: new Set(), dft: new Set() }
  };
}
function snapshot(state: MockState, module: "md" | "dft"): MockEvent {
  return { event: "snapshot", data: { module, jobs: [...state[module].values()].map(job => ({ job_id: job.job_id, status: job.status,
    queue_position: job.queue_position ?? null, attempt: job.attempt ?? 1, artifacts_state: job.artifacts_state ?? "none",
    artifacts_deleted: job.artifacts_deleted ?? false, artifact_deleted_at: job.artifact_deleted_at ?? null })) } };
}
function publish(state: MockState, module: "md" | "dft") {
  const event = snapshot(state, module);
  state.subscribers[module].forEach(push => push(event));
}
function requestHeaders(input: RequestInfo | URL, init?: RequestInit): Headers {
  return new Headers(init?.headers ?? (typeof Request !== "undefined" && input instanceof Request ? input.headers : undefined));
}
function missing(method: string, path: string) {
  return jsonResponse({ detail: `No handoff mock is defined for ${method} ${path}.`, code: "handoff_mock_missing" }, 501);
}
async function authResponse(path: string, method: string, input: RequestInfo | URL, init: RequestInit | undefined, state: MockState): Promise<Response> {
  const body = await requestJson(input, init);
  if (path === "/auth/session" && method === "GET") return jsonResponse(state.session);
  if (path === "/auth/login" && method === "POST") {
    if (!String(body.username ?? "").trim() || !String(body.password ?? "")) return jsonResponse({ detail: "请输入演示用户名和密码。" }, 400);
    state.session = authenticatedSession(String(body.username).trim(), ++state.nextId);
    return jsonResponse(state.session);
  }
  if (path === "/auth/logout" && method === "POST") {
    state.session = guestSession();
    return noContentResponse();
  }
  if (path === "/auth/password" && method === "POST") {
    if (!state.session.authenticated) return jsonResponse({ detail: "请先登录演示账号。" }, 401);
    if (!String(body.current_password ?? "") || String(body.new_password ?? "").length < 12) return jsonResponse({ detail: "演示新密码至少需要12个字符。" }, 400);
    // Match the real UI contract: password changes retire the session.
    // No entered secret is stored.
    state.session = guestSession();
    return noContentResponse();
  }
  return missing(method, path);
}
const batchMapping = (): BatchMapping => ({ sheet: null, encoding: "utf-8-sig", smiles_column: "smiles", id_column: "id", name_column: "name" });
function batchStatistics(): BatchStatistics {
  const table = { row_count: 2, valid_rows: 2, unique_count: 2, duplicate_rows: 0, invalid_rows: 0, blank_rows: 0 };
  return { raw_pairs: 4, valid_pairs: 4, unique_pairs: 4, tables: { a: { ...table }, b: { ...table } } };
}
function createBatchImport(id: string, input: RequestInfo | URL, init?: RequestInit): BatchImport {
  const form = init?.body instanceof FormData ? init.body : null;
  const table = (role: "a" | "b") => ({ headers: ["id", "name", "smiles"], sheets: [], sheet: null, row_count: 2, blank_rows: 0,
    sample: [1, 2].map(index => ({ id: `SYN-${role}-${index}`, name: `合成单体 ${role.toUpperCase()}${index}`, smiles: role === "a" ? "NCCN" : "O=C(O)CC(=O)O" })), mapping: batchMapping() });
  const file = (role: "a" | "b") => {
    const value = form?.get(`file_${role}`);
    return { filename: value && typeof value !== "string" ? value.name : `synthetic-${role}.csv`, format: "csv", size_bytes: value && typeof value !== "string" ? value.size : 64, sha256: "0".repeat(64) };
  };
  void input;
  return { import_id: id, expires_at: "2099-01-01T00:00:00Z", files: { a: file("a"), b: file("b") }, tables: { a: table("a"), b: table("b") } };
}
function createBatch(id: string, target: BatchJob["target_class"]): BatchJob {
  return { job_id: id, status: "completed", stage: "completed", target_class: target,
    summary: { ...batchStatistics(), processed_pairs: 4, computed_unique_pairs: 4, candidate_count: 4, pair_statuses: { success: 4 }, complete: true },
    artifacts: Object.fromEntries(["results.zip", "results.xlsx", "input_errors.csv"].map(name => [name, { name, url: `/api/v1/monomer-polymerization/batch/jobs/${id}/artifacts/${name}`, size_bytes: 80, sha256: "0".repeat(64), media_type: name.endsWith("zip") ? "application/zip" : name.endsWith("xlsx") ? "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" : "text/csv" }])),
    created_at: SYNTHETIC_NOW, updated_at: SYNTHETIC_NOW, finished_at: SYNTHETIC_NOW, expires_at: "2099-01-01T00:00:00Z", error_code: null, message: "仅用于布局的合成批量结果" };
}
function csvDownload(name: string, content: string): Response {
  return new Response(`\uFEFF${content}`, { headers: { "Content-Type": "text/csv; charset=utf-8", "Content-Disposition": `attachment; filename="${name}"` } });
}
async function currentApiResponse(path: string, url: URL, method: string, body: Record<string, unknown>, input: RequestInfo | URL, init: RequestInit | undefined, empty: boolean, state: MockState): Promise<Response | null> {
  const signal = requestSignal(input, init);
  if (["/knowledge/search", "/database-browser/property-filter/search"].includes(path)) {
    const recording = state.recordings.get(String(body.recording_id));
    if (recording) recording.events.push({ sequence: recording.events.length + 1, time: SYNTHETIC_NOW, event: path === "/knowledge/search" ? "search.completed" : "property_filter.search_completed", query: body.query ?? "synthetic", total: empty ? 0 : 1 });
  }
  if (path === "/task-events" && method === "GET") {
    const module = url.searchParams.get("module");
    if (module !== "md" && module !== "dft") return jsonResponse({ detail: "Unknown synthetic task module." }, 400);
    return mockEventStream([snapshot(state, module)], signal, push => {
      state.subscribers[module].add(push);
      return () => state.subscribers[module].delete(push);
    });
  }
  const task = /^\/monomer-(md|dft)\/jobs(?:\/([^/]+))?(?:\/(.+))?$/.exec(path);
  if (task) {
    const module = task[1] as "md" | "dft";
    const id = task[2] ? decodeURIComponent(task[2]) : undefined;
    const suffix = task[3];
    const jobs = state[module];
    if (!id && method === "POST") {
      const base = module === "md" ? monomerMdJob() : monomerDftJob();
      const job: DemoJob = { ...base, job_id: module === "md" ? `a${(++state.nextId).toString(16).padStart(31, "0")}` : `d0000000-0000-4000-8000-${(++state.nextId).toString(16).padStart(12, "0")}` };
      if (module === "md") {
        Object.assign(job, { protocol: body.protocol ?? "DensityDemo", run_mode: body.run_mode ?? "demo", smiles: body.smiles ?? "CCO", input_smiles: body.smiles ?? "CCO", config_json: body.config_json });
      } else {
        const result = monomerDftResult();
        if (body.calculation_type === "optimization") {
          result.calculation_type = "optimization";
          result.optimization = { converged: true, steps: 2, fmax_threshold_eV_per_A: 0.01, max_steps: 50,
            trajectory_artifact_id: "trajectory", trace: [{ step: 0, energy_eV: -0.9, fmax_eV_per_A: 0.1 }, { step: 2, energy_eV: -1, fmax_eV_per_A: 0.001 }] };
          result.scientific_status = { ...result.scientific_status, geometry_status: "converged", fmax_eV_per_A: 0.001 };
          (job.artifacts as MonomerDftJobResponse["artifacts"]).push({ artifact_id: "trajectory", name: "synthetic-trajectory.json", media_type: "application/json", size_bytes: 512, sha256: "0".repeat(64), available: true });
        }
        Object.assign(job, { request: Object.keys(body).length ? body : monomerDftJob().request, calculation_type: result.calculation_type, result });
      }
      jobs.set(job.job_id, job); publish(state, module);
      return jsonResponse(job, 201);
    }
    if (!id && method === "GET") {
      let items = [...jobs.values()];
      for (const field of ["status", "run_mode", "protocol", "calculation_type"]) {
        const filter = url.searchParams.get(field);
        if (filter) items = items.filter(job => job[field] === filter);
      }
      if (url.searchParams.get("active_only") === "true") items = items.filter(job => ["pending", "submitted", "queued", "running", "cancel_requested"].includes(job.status));
      const page = Math.max(1, Number(url.searchParams.get("page")) || 1);
      const pageSize = Math.max(1, Number(url.searchParams.get("page_size")) || 20);
      return jsonResponse({ items: items.slice((page - 1) * pageSize, page * pageSize), total: items.length, page, page_size: pageSize });
    }
    const job = id ? jobs.get(id) : undefined;
    if (!job) return jsonResponse({ detail: "合成任务不存在。", code: "not_found" }, 404);
    if (suffix === "cancel" && method === "POST") {
      Object.assign(job, { status: "cancelled", stage: "cancelled", progress_stage: "cancelled", result: null, cancel_requested: true, finished_at: SYNTHETIC_NOW });
      publish(state, module); return jsonResponse(job);
    }
    if (suffix === "artifacts" && method === "DELETE") {
      Object.assign(job, { artifacts: [], artifacts_state: "deleted", artifacts_deleted: true, artifact_deleted_at: SYNTHETIC_NOW, result: null });
      publish(state, module);
      return jsonResponse(module === "dft" ? { job_id: id, deleted: true, deleted_artifacts: 1, artifacts_state: "deleted", message: "合成文件已清理" } : job);
    }
    if (suffix?.startsWith("artifacts/") && method === "GET") {
      if (job.artifacts_deleted) return jsonResponse({ detail: "合成文件已清理。" }, 410);
      if (suffix === "artifacts/trajectory" && module === "dft") {
        const result = job.result as MonomerDftResult | null;
        if (!result?.optimization) return jsonResponse({ detail: "此任务无优化轨迹。" }, 404);
        return jsonResponse({ units: { energy: "eV", fmax: "eV/angstrom", coordinates: "angstrom" }, frames: result.optimization.trace.map(step => ({ ...step, coordinates_angstrom: result.geometry.final_coordinates_angstrom })) });
      }
      if (suffix !== "artifacts/scientific_result") return jsonResponse({ detail: "合成文件不存在。" }, 404);
      return jsonResponse(job.result ?? monomerDftResult());
    }
    if (module === "md" && suffix === "visualization/stages/synthetic-stage/trajectory" && method === "GET") return jsonResponse(mdTimeline());
    if (suffix === "bundle" && method === "GET") {
      if (job.artifacts_deleted) return jsonResponse({ detail: "合成文件已清理。" }, 410);
      // A real ZIP is generated from public synthetic JSON by the packager.
      return new Response(await (await state.fetchAsset("/handoff/downloads/dft-bundle.zip")).arrayBuffer(), { headers: { "Content-Type": "application/zip", "Content-Disposition": "attachment; filename=synthetic-dft-bundle.zip" } });
    }
    if (!suffix && method === "DELETE") { jobs.delete(job.job_id); publish(state, module); return noContentResponse(); }
    if (!suffix && method === "GET") return jsonResponse(job);
    return missing(method, path);
  }
  const batchBase = "/monomer-polymerization/batch";
  if (path === `${batchBase}/imports` && method === "POST") {
    const item = createBatchImport(`synthetic-import-${++state.nextId}`, input, init);
    state.imports.set(item.import_id, item); return jsonResponse(item, 201);
  }
  const preview = new RegExp(`^${batchBase}/imports/([^/]+)/preview$`).exec(path);
  if (preview && method === "POST") {
    const item = state.imports.get(decodeURIComponent(preview[1]));
    if (!item) return jsonResponse({ detail: "请先上传两份演示表格。" }, 404);
    for (const role of ["a", "b"] as const) if (body[role]) item.tables[role].mapping = body[role] as BatchMapping;
    const result: BatchImportPreview = { ...item, preview_revision: `synthetic-revision-${state.nextId}`, can_submit: true, statistics: batchStatistics(), input_errors: [], input_error_count: 0 };
    return jsonResponse(result);
  }
  if (path === `${batchBase}/jobs` && method === "POST") {
    if (!state.imports.has(String(body.import_id))) return jsonResponse({ detail: "演示导入不存在。" }, 404);
    const key = requestHeaders(input, init).get("Idempotency-Key");
    const existing = key && state.idempotentBatches.get(key);
    if (existing) return jsonResponse(state.batches.get(existing));
    const job = createBatch(`b${(++state.nextId).toString(16).padStart(31, "0")}`, (body.target_class ?? "all") as BatchJob["target_class"]);
    state.batches.set(job.job_id, job);
    if (key) state.idempotentBatches.set(key, job.job_id);
    return jsonResponse(job, 201);
  }
  if (path === `${batchBase}/jobs` && method === "GET") {
    const items = [...state.batches.values()]; const offset = Number(url.searchParams.get("offset")) || 0;
    return jsonResponse({ items: items.slice(offset, offset + 20), total: items.length, next_offset: items.length > offset + 20 ? offset + 20 : null });
  }
  const batch = new RegExp(`^${batchBase}/jobs/([^/]+)(?:/(.+))?$`).exec(path);
  if (batch) {
    const job = state.batches.get(decodeURIComponent(batch[1]));
    if (!job) return jsonResponse({ detail: "合成批量任务不存在。" }, 404);
    if (batch[2] === "cancel" && method === "POST") { job.status = "cancelled"; job.stage = "cancelled"; return jsonResponse(job); }
    if (batch[2] === "results" && method === "GET") {
      const items = empty || job.status === "cancelled" ? [] : [1, 2, 3, 4].map(index => ({ pair_id: `SYN-${index}`, a_row: Math.ceil(index / 2), b_row: index % 2 + 1,
        a_id: `SYN-a-${Math.ceil(index / 2)}`, b_id: `SYN-b-${index % 2 + 1}`, a_name: "合成二胺", b_name: "合成二酸", a_input_smiles: "NCCN", b_input_smiles: "O=C(O)CC(=O)O",
        a_canonical_smiles: "NCCN", b_canonical_smiles: "O=C(O)CC(=O)O", candidate_index: 1, polymer_smiles: "*NCCNC(=O)CC(=O)*", polymer_class: "polyamide", reaction_id: null,
        engine_mon1_smiles: "NCCN", engine_mon2_smiles: "O=C(O)CC(=O)O", reactset: ["NCCN", "O=C(O)CC(=O)O"] }));
      const offset = Number(url.searchParams.get("offset")) || 0;
      return jsonResponse({ items: items.slice(offset, offset + 50), total: items.length, next_offset: null });
    }
    if (batch[2]?.startsWith("artifacts/") && method === "GET") {
      const name = batch[2].slice("artifacts/".length);
      if (!job.artifacts[name]) return jsonResponse({ detail: "合成文件不存在。" }, 404);
      const asset = await state.fetchAsset(`/handoff/downloads/${name}`);
      return new Response(await asset.arrayBuffer(), { status: asset.status, headers: { "Content-Type": job.artifacts[name].media_type, "Content-Disposition": `attachment; filename="${name}"` } });
    }
    if (batch[2] === "artifacts/candidates.csv" && method === "GET") return csvDownload("candidates.csv", "pair_id,polymer_smiles,source\nSYN-1,*NCCNC(=O)CC(=O)*,synthetic\n");
    if (!batch[2] && method === "GET") return jsonResponse(job);
    return missing(method, path);
  }
  if (/^\/monomer-polymerization\/batch\/templates\/[ab]\.csv$/.test(path)) return csvDownload("synthetic-template.csv", "id,name,smiles\nSYN-1,Synthetic,NCCN\n");
  if (path === "/knowledge/recordings" && method === "POST") {
    const id = String(body.recording_id ?? `synthetic-recording-${++state.nextId}`);
    state.recordings.set(id, { recording_id: id, status: "recording", started_at: SYNTHETIC_NOW, ended_at: SYNTHETIC_NOW, events: [] });
    return jsonResponse({ recording_id: id, status: "recording" });
  }
  if (path === "/knowledge/observations" || path === "/database-browser/property-filter/observations") {
    const recording = state.recordings.get(String(body.recording_id));
    const event = path === "/knowledge/observations" ? (body.source === "reaction_tab" ? "article.reaction_viewed" : "article.opened") : "property_filter.measurements_viewed";
    if (recording) recording.events.push({ sequence: recording.events.length + 1, time: SYNTHETIC_NOW, query: "synthetic", ...body, event });
    return jsonResponse({ ...body, event });
  }
  const recordingRoute = /^\/knowledge\/recordings\/([^/]+)\/(stop|summary)$/.exec(path);
  if (recordingRoute && method === "POST") {
    const id = decodeURIComponent(recordingRoute[1]);
    const recording = state.recordings.get(id);
    if (!recording) return jsonResponse({ detail: "合成浏览记录不存在。" }, 404);
    if (recordingRoute[2] === "stop") { recording.status = "stopped"; return jsonResponse(recording); }
    const summary = { recording_id: id, summary: `本次演示记录了 ${recording.events.length} 次浏览操作。全部内容均为本地合成数据，不代表真实科研结论。`, generated: true };
    if (!requestHeaders(input, init).get("accept")?.includes("text/event-stream")) return jsonResponse(summary);
    return mockEventStream([{ event: "delta", data: { text: summary.summary } }, { event: "done", data: summary }], signal);
  }
  if (path.startsWith("/lab-data/sample-measurements")) {
    if (path === "/lab-data/sample-measurements" && method === "POST") {
      const measurement = { ...body, id: ++state.nextId };
      state.measurements.unshift(measurement);
      return jsonResponse(measurement, 201);
    }
    const grouped = Object.entries(state.measurements.reduce<Record<string, number>>((counts, item) => {
      const project = String(item.experimentProject); counts[project] = (counts[project] ?? 0) + 1; return counts;
    }, {})).map(([experimentProject, count]) => ({ experimentProject, count }));
    if (path === "/lab-data/sample-measurements/count") return jsonResponse({ count: state.measurements.length });
    if (path === "/lab-data/sample-measurements/stats/by-project") return jsonResponse(grouped);
    if (path === "/lab-data/sample-measurements/stats/summary") return jsonResponse({ totalCount: state.measurements.length, byProject: grouped });
    if (path === "/lab-data/sample-measurements" && method === "GET") {
      let items = state.measurements;
      for (const field of ["sampleId", "experimentProject"]) {
        const value = url.searchParams.get(field); if (value) items = items.filter(item => String(item[field]).includes(value));
      }
      const page = Math.max(1, Number(url.searchParams.get("page")) || 1); const pageSize = Math.max(1, Number(url.searchParams.get("pageSize")) || 20);
      return jsonResponse({ items: items.slice((page - 1) * pageSize, page * pageSize), total: items.length, page, pageSize });
    }
  }
  if (path === "/assistant/tg/status") return jsonResponse({ enabled: true, configured: true, image: { supported: true, max_files: 2, max_canvas_snapshots: 1, max_user_upload_files: 1, max_bytes: 10485760, max_total_bytes: 20971520, accepted_mime_types: ["image/png", "image/jpeg", "image/webp"] } });
  if (path === "/assistant/tg/guide") return jsonResponse({ module: "reverseDesign", version: 3, language: "zh-CN", defaults: { target_tg: 220, top_k: 10, similarity_threshold: 0.7 }, sections: [{ id: "handoff", title: "本地演示说明", content: ["参数、结构与结果均为合成演示；可安全修改页面以统一界面。"] }] });
  if (/^\/assistant\/tg\/chat\/(image-stream|stream)$/.test(path) && method === "POST") return mockEventStream([
    { event: "meta", data: { request_id: `synthetic-chat-${++state.nextId}`, context_attached: true, context_trimmed: [] } },
    { event: "token", data: { content: "这是本地合成助手回复。" } },
    { event: "token", data: { content: "可调整目标 Tg 和相似度，查看界面中的演示候选。此内容不构成科研结论。" } },
    { event: "done", data: {} }
  ], signal);
  return null;
}

async function apiResponse(
  path: string,
  url: URL,
  method: string,
  input: RequestInfo | URL,
  init: RequestInit | undefined,
  empty: boolean,
  state: MockState
): Promise<Response> {
  const body = await requestJson(input, init);
  const current = await currentApiResponse(path, url, method, body, input, init, empty, state);
  if (current) return current;

  if (path === "/query/smiles") {
    const results = empty ? [] : [polymerResult()];
    return jsonResponse({
      match_type: body.match_mode ?? "structure",
      query_time_ms: 8,
      total: results.length,
      predicted_property_name: null,
      predicted_property_value: null,
      predicted_property_unit: null,
      results
    });
  }
  if (path === "/predict") {
    const properties = Array.isArray(body.properties) ? body.properties : ["Glass transition temperature"];
    const values: Record<string, number> = {
      "Glass transition temperature": 218, "Melting temperature": 305,
      "Thermal decomposition temperature": 390, "Thermal decomposition weight loss": 5,
      "Elongation at break": 24, "Tensile stress strength at break": 65,
      "O2 Permeability Barrer": 2.4, "Co2 Permeability Barrer": 9.2, "H2 Permeability Barrer": 12
    };
    return jsonResponse({ predictions: empty ? {} : Object.fromEntries(properties.filter(key => typeof key === "string" && key in values).map(key => [key, values[String(key)]])), query_time_ms: 6 });
  }
  if (path === "/dev-gpu-session/status" || path === "/dev-gpu-session/recover") {
    return jsonResponse({
      schema_version: 1,
      operator_available: false,
      phase: "ready",
      controller_status: "synthetic",
      can_recover: false,
      operation_id: null,
      message: "Handoff mock is ready.",
      source_sha: null,
      source_tree: null,
      updated_at: SYNTHETIC_NOW
    });
  }
  if (path === "/database-browser/smiles-lookup") {
    const results = empty ? [] : [{
      record_id: "SYN-LOOKUP-001",
      source_column: "synthetic",
      smiles: String(body.smiles ?? "CCO"),
      canonical_smiles: "CCO",
      structure_svg: SIMPLE_STRUCTURE_SVG,
      summary: "Synthetic lookup result",
      fields: { source: "handoff-mock" }
    }];
    return jsonResponse({
      query_smiles: body.smiles ?? "CCO",
      canonical_smiles: "CCO",
      table: body.table ?? "polymers",
      exists: results.length > 0,
      total: results.length,
      query_time_ms: 5,
      results
    });
  }
  if (path === "/md-demo/defaults") {
    return jsonResponse({
      default_request: { smiles: "CCO", temperature: 300, pressure: 1, n_atom: 4, n_chain: 1, forcefield: "synthetic" },
      available_stages: empty ? [] : [mdStage()],
      summary: mdSummary(),
      fixture_metadata: mdFixtureMetadata()
    });
  }
  if (path === "/md-demo/run") return jsonResponse(mdRunResponse());
  if (path === "/md-demo/atom-distance") {
    return jsonResponse({
      atom_1: { atom_id: 1, chain_id: 1, atom_type: "C" },
      atom_2: { atom_id: 2, chain_id: 1, atom_type: "O" },
      frames: [0, 1],
      time_ps: [0, 1],
      distance: [1.4, 1.42],
      series: { key: "distance", label: "Distance", unit: "Å", points: [{ time_ps: 0, value: 1.4 }, { time_ps: 1, value: 1.42 }] },
      stats: { n_atoms: 2, n_frames: 2, source_n_frames: 2, n_chains: 1, use_pbc: false, min_distance: 1.4, max_distance: 1.42 }
    });
  }
  if (path === "/monomer-dft/status") {
    return jsonResponse({ enabled: true, available: true, schema_ready: true, worker_status: "synthetic", runtime_ready: true, draining: false, active_jobs: 0, max_active_jobs: 2, message: "Handoff mock is ready." });
  }
  if (path === "/monomer-dft/capabilities") {
    return jsonResponse({
      enabled: true,
      available: true,
      schema_ready: true,
      calculation_types: ["single_point", "optimization"],
      properties: ["energy", "charges", "forces", "hessian", "frequencies"],
      default_model: "aimnet2",
      models: ["aimnet2", "aimnet2-2025", "aimnet2-b973c", "aimnet2-nse", "aimnet2-pd", "aimnet2-rxn"].map(id => ({ id, label: `Synthetic ${id}`, available: true, is_default: id === "aimnet2", supported_calculation_types: ["single_point", "optimization"], supported_properties: ["energy", "charges", "forces", "hessian", "frequencies"], supported_elements: ["H", "C", "N", "O"], supports_spin: id === "aimnet2-nse" })),
      defaults: { conformer: { seed: 1, max_iterations: 100 }, single_point: { properties: ["energy", "charges", "forces"] }, optimization: { fmax_eV_per_A: 0.01, max_steps: 50, post_optimization_properties: [] } },
      limits: { max_atoms: 100, max_optimization_steps: 50, min_optimization_steps: 10, max_concurrent_jobs: 1, max_queued_jobs: 2, max_active_jobs: 3 },
      worker: { mode: "synthetic" },
      message: "Handoff mock capability catalog."
    });
  }

  if (path === "/monomer-md/status") {
    return jsonResponse({ enabled: true, available: true, accepting_jobs: true, can_submit: true, worker_status: "synthetic", worker_base_url_configured: false, message: "Handoff mock is ready." });
  }
  if (path === "/monomer-md/protocols") {
    return jsonResponse({ enabled: true, available: true, protocols: ["DensityDemo", "Density", "Transport", "HVap", "Dielectric", "Compressibility"].map(protocol => ({ protocol, run_mode: protocol === "DensityDemo" ? "demo" : "formal", supported: true, runtime_ready: true, default_config: { steps: 100 } })), message: "Synthetic protocol catalog." });
  }

  if (path === "/knowledge/search") {
    const results = empty ? [] : [{
      knowledge_id: 900001,
      source_file: "synthetic-knowledge.jsonl",
      source_row_number: 1,
      source_sequence: "SYN-001",
      title_zh: "合成知识记录",
      title_en: "Synthetic Knowledge Record",
      abstract: "This deterministic abstract is provided only for interface layout.",
      abstract_snippet: "Deterministic synthetic abstract for layout.",
      claim: "Synthetic claim.",
      analysis: "Synthetic analysis.",
      is_polymer_synthesis: "yes",
      judgement_reason: "Synthetic UI fixture.",
      polymer_iupac: "synthetic polymer",
      formulation: "synthetic monomer A + synthetic monomer B",
      catalyst: null,
      temperature: "300 K",
      reaction_time: "1 h",
      solvent: "synthetic solvent",
      matched_terms: [String(body.query ?? "synthetic")],
      matched_fields: ["Title"]
    }];
    return jsonResponse({ search_id: `synthetic-search-${++state.nextId}`, groups: body.groups ?? [{ terms: [body.query ?? "synthetic"] }], query: body.query ?? "", terms: body.terms ?? [body.query ?? "synthetic"], page: body.page ?? 1, page_size: body.page_size ?? 20, query_time_ms: 5, total: results.length, results });
  }
  if (path === "/online-knowledge/default-config") return jsonResponse({ base_url: "https://example.invalid/v1", model: "synthetic-model", max_papers: 20, has_server_api_key: true });
  if (path === "/online-knowledge/search") return jsonResponse(empty ? { ...onlineKnowledgeResult(), totalPapers: 0, propertyPoints: [], dataframe: [] } : onlineKnowledgeResult());
  if (path === "/online-knowledge/jobs" && method === "POST") return jsonResponse({ job_id: "synthetic-online-job", status: "pending" }, 201);
  if (/^\/online-knowledge\/jobs\/[^/]+$/.test(path)) return jsonResponse({ job_id: "synthetic-online-job", status: "completed", material: "Synthetic Polymer A", mode: "property", max_papers: 20, progress_stage: "completed", progress_message: "Synthetic search complete", processed_papers: empty ? 0 : 2, total_papers: empty ? 0 : 2, created_at: SYNTHETIC_NOW, updated_at: SYNTHETIC_NOW, error_message: null, result: empty ? { ...onlineKnowledgeResult(), totalPapers: 0, propertyPoints: [], dataframe: [] } : onlineKnowledgeResult() });
  if (path === "/online-knowledge/history" && method === "GET") return jsonResponse({ history: [] });
  if (path === "/online-knowledge/history/clear") return jsonResponse({ success: true });
  if (/^\/online-knowledge\/history\/\d+$/.test(path) && method === "DELETE") return jsonResponse({ success: true });
  if (path === "/online-knowledge/export-csv") return jsonResponse({ success: true, csv_content: "source,value\nsynthetic,1\n", filename: "synthetic-results.csv" });

  if (path === "/reverse-design/tg") return jsonResponse(empty ? { ...reverseDesignResult(), total: 0, results: [] } : reverseDesignResult());
  if (path === "/reverse-design/tg/jobs" && method === "POST") return jsonResponse({ job_id: "synthetic-reverse-job", status: "pending" }, 201);
  if (/^\/reverse-design\/tg\/jobs\/[^/]+$/.test(path)) return jsonResponse({ job_id: "synthetic-reverse-job", status: "found_enough", target_tg: 220, similarity_threshold: 0.7, created_at: SYNTHETIC_NOW, updated_at: SYNTHETIC_NOW, started_at: SYNTHETIC_NOW, finished_at: SYNTHETIC_NOW, scanned_rows: 12, matched_count: empty ? 0 : 2, current_tg_radius: 5, best_similarity_score: empty ? null : 0.85, message: "Synthetic search complete", error: null, result: empty ? { ...reverseDesignResult(), total: 0, results: [] } : reverseDesignResult() });

  if (path === "/conditional-generation/tg/status") return jsonResponse({ enabled: true, available: true, model_dir: "synthetic", missing_artifacts: [], message: "Handoff mock is ready." });
  if (path === "/conditional-generation/tg/jobs" && method === "POST") return jsonResponse({ job_id: "synthetic-generation-job", status: "pending" }, 201);
  if (/^\/conditional-generation\/tg\/jobs\/[^/]+$/.test(path)) return jsonResponse({ job_id: "synthetic-generation-job", status: "completed", delta_tg: 30, created_at: SYNTHETIC_NOW, updated_at: SYNTHETIC_NOW, started_at: SYNTHETIC_NOW, finished_at: SYNTHETIC_NOW, attempts: 2, accepted_count: empty ? 0 : 2, message: "Synthetic generation complete", error: null, result: empty ? { ...conditionalGenerationResult(), returned_count: 0, results: [] } : conditionalGenerationResult() });
  if (path === "/conditional-generation/polytao/status") return jsonResponse({ enabled: true, available: true, worker_base_url_configured: false, worker_status: "ready", worker_mode: "handoff", db_configured: true, db_ready: true, db_error: null, runtime_ready: true, runtime_error: null, active_jobs: 0, model_id: "synthetic-model", model_revision: null, default_params: {}, worker_version: "synthetic", message: "Handoff mock is ready." });
  if (path === "/conditional-generation/polytao/descriptors") return jsonResponse({ input_smiles: body.smiles ?? "*CC*", canonical_smiles: body.smiles ?? "*CC*", descriptors: [{ name: "MolWt", value: 100 }, { name: "HeavyAtomCount", value: 8 }], prompt: "synthetic descriptor prompt", query_time_ms: 4 });
  if (path === "/conditional-generation/polytao/jobs" && method === "POST") return jsonResponse({ job_id: "synthetic-polytao-job", status: "pending" }, 201);
  if (/^\/conditional-generation\/polytao\/jobs\/[^/]+$/.test(path)) return jsonResponse({ job_id: "synthetic-polytao-job", status: "completed", input_smiles: "*CC*", canonical_smiles: "*CC*", prompt: "synthetic descriptor prompt", requested_count: 2, returned_count: empty ? 0 : 2, attempts: 2, progress_percent: 100, progress_stage: "completed", progress_message: "Synthetic generation complete", created_at: SYNTHETIC_NOW, updated_at: SYNTHETIC_NOW, started_at: SYNTHETIC_NOW, finished_at: SYNTHETIC_NOW, worker_id: null, worker_job_id: null, worker_version: "synthetic", engine: "handoff-mock", error_message: null, result: empty ? { ...polytaoResult(), returned_count: 0, results: [] } : polytaoResult() });

  if (path === "/structure/3d") return jsonResponse({ molblock: SIMPLE_MOLBLOCK, capped_smiles: body.smiles ?? "CCO", format: "mol" });
  if (path === "/structure/2d") return jsonResponse({ structure_svg: SIMPLE_STRUCTURE_SVG });
  if (path === "/structure/standardize-smiles") return jsonResponse({ input_smiles: body.smiles ?? "CCO", standardized_smiles: body.smiles ?? "CCO", changed: false, query_time_ms: 2 });
  if (path === "/structure/recognize-image") return jsonResponse({ smiles: "CCO", molfile: SIMPLE_MOLBLOCK, confidence: 0.95, warnings: ["Synthetic recognition result"], query_time_ms: 3 });

  if (path === "/monomer-retrosynthesis") {
    const candidates = empty ? [] : [{ rank: 1, raw_output: "NCCN.O=C(O)CC(=O)O", reactants_smiles: "NCCN.O=C(O)CC(=O)O", canonical_reactants_smiles: "NCCN.O=C(O)CC(=O)O", reactants: [{ input_smiles: "NCCN", canonical_smiles: "NCCN", valid_smiles: true, heavy_atom_count: 4 }, { input_smiles: "O=C(O)CC(=O)O", canonical_smiles: "O=C(O)CC(=O)O", valid_smiles: true, heavy_atom_count: 8 }], valid_smiles: true, all_reactants_smaller_than_target: true, reaction_hint: "Synthetic condensation example" }];
    return jsonResponse({ input_smiles: body.smiles ?? "*CC*", canonical_smiles: body.smiles ?? "*CC*", target_role: body.target_role ?? "auto", inferred_target_role: "other", query_time_ms: 5, total: candidates.length, candidates });
  }
  if (path === "/monomer-polymerization/status") return jsonResponse({ enabled: true, available: true,
    default_target_class: "all", available_target_classes: ["all", "polyolefin", "polyester", "polyether", "polyamide", "polyimide", "polyurethane", "polyoxazolidone"],
    target_requirements: { polyolefin: { min_monomers: 1, max_monomers: 1, monomer_b_required: false, note: "合成单体示例" } },
    max_results_limit: 10, batch: { enabled: true, available: true, message: "仅处理本地合成演示数据", formats: ["csv", "xlsx"], limits: { file_bytes: 10485760, request_bytes: 20971520, max_rows: 500, max_pairs: 250000, retention_days: 7 } }, message: "Handoff mock is ready." });
  if (path === "/monomer-polymerization") {
    const a = String(body.monomer_a_smiles ?? "NCCN");
    const b = body.monomer_b_smiles == null ? null : String(body.monomer_b_smiles);
    const results = empty ? [] : [{ rank: 1, monomer_a_smiles: a, monomer_b_smiles: b, polymer_smiles: b ? "*NCCNC(=O)CC(=O)*" : "*CC*", polymer_class: b ? "polyamide" : "polyolefin", reaction_id: null, reaction_name: "Synthetic polymerization", reactset: b ? [a, b] : [a], structure_svg: SIMPLE_STRUCTURE_SVG }];
    return jsonResponse({ input_monomers: [{ role: "monomer_a", input_smiles: a, canonical_smiles: a }, ...(b ? [{ role: "monomer_b", input_smiles: b, canonical_smiles: b }] : [])], target_class: body.target_class ?? "all", query_time_ms: 6, total: results.length, results, warnings: ["UI-only synthetic result; not a chemistry prediction"] });
  }

  if (path === "/dft/pca-sample") {
    const results = empty ? [] : [{ mol_id: "SYN-DFT-001", x: 0, y: 0, z: 0, n_atoms: 2, final_step: 2, homo_ev: -5, lumo_ev: -2, gap_ev: 3, dipole_moment: 1 }];
    return jsonResponse({ query_time_ms: 3, total: results.length, results });
  }
  if (/^\/dft\/molecule\/[^/]+$/.test(path)) return jsonResponse({ mol_id: "SYN-DFT-001", range_group: "synthetic", final_step: 2, n_atoms: 2, coordinates: [[6, 0, 0, 0], [8, 1.4, 0, 0]], scf_energy: -10, zero_point_energy: 0.1, thermal_enthalpy: 0.2, gibbs_free_energy: -9.8, lowest_freq: 100, dipole_moment: 1, homo_ev: -5, lumo_ev: -2, gap_ev: 3, is_converged: "converged", trace: [{ step: 0, scf_energy: -9, homo_ev: -5, lumo_ev: -2, gap_ev: 3 }, { step: 2, scf_energy: -10, homo_ev: -5, lumo_ev: -2, gap_ev: 3 }] });

  if (path === "/database-browser/property-filter/options") return jsonResponse(propertyFilterOptions(empty), 200, { ETag: 'W/"synthetic-options-v1"' });
  if (path === "/database-browser/property-filter/histogram") return jsonResponse({ query_time_ms: 2, option_key: url.searchParams.get("option_key") ?? "standardized:tg:C", data_source: "synthetic", source_status: empty ? "empty" : "ready", source_message: null, histogram: { domain_min: 130, domain_max: 230, domain_kind: "p5_p95", bin_count: 6, counts: empty ? [0, 0, 0, 0, 0, 0] : [1, 3, 6, 7, 5, 2], underflow_count: 0, overflow_count: 0, total_count: empty ? 0 : 24 } }, 200, { ETag: 'W/"synthetic-histogram-v1"' });
  if (path === "/database-browser/property-filter/search") {
    const results = empty ? [] : [{ smiles: "*NCCNC(=O)CC(=O)*", canonical_smiles: "*NCCNC(=O)CC(=O)*", polymer_name: "Synthetic Polymer A", matched_filters: 1, records: [{ filter_record_id: 900001, source_row_number: 1, polymer_name: "Synthetic Polymer A", smiles: "*NCCNC(=O)CC(=O)*", canonical_smiles: "*NCCNC(=O)CC(=O)*", property_category: "thermal", property_name: "Tg", property_value: "218", property_value_num: 218, property_unit_raw: "C", property_unit_clean: "C", property_key: "tg", property_label: "玻璃化转变温度", canonical_value: 218, canonical_unit: "°C", unit_conversion_status: "already_standard", value_origin: "synthetic", label_source: "synthetic", reliable_score: 1, soft_quality_flags: "", duplicate_flag: "", filter_index: 0 }] }];
    return jsonResponse({ ...browseEnvelope(results), total_records: empty ? 0 : 24 });
  }
  if (path === "/database-browser/datasets/summary") return jsonResponse(databaseSummary(empty));
  if (path === "/database-browser/datasets/analytics") return jsonResponse(databaseAnalytics(empty));
  if (path === "/database-browser/structure-property") {
    const results = empty ? [] : [{ property_id: 900001, polymer_id: 900001, smiles: "*NCCNC(=O)CC(=O)*", canonical_smiles: "*NCCNC(=O)CC(=O)*", property_category: "thermal", property_name: "Tg", property_value: "218", property_value_num: 218, property_unit: "°C", label_source: "synthetic" }];
    return jsonResponse(browseEnvelope(results));
  }
  if (path === "/database-browser/dft/molecules") {
    const results = empty ? [] : [{ mol_id: "SYN-DFT-001", range_group: "synthetic", final_step: 2, n_atoms: 2, trace_points: 2, scf_energy: -10, zero_point_energy: 0.1, thermal_enthalpy: 0.2, gibbs_free_energy: -9.8, lowest_freq: 100, dipole_moment: 1, homo_ev: -5, lumo_ev: -2, gap_ev: 3, is_converged: "converged" }];
    return jsonResponse({ ...browseEnvelope(results), total_step_records: results.length * 2, average_steps: results.length ? 2 : 0, max_steps: results.length ? 2 : 0 });
  }
  if (path === "/database-browser/dft/steps") {
    const results = empty ? [] : [{ mol_id: "SYN-DFT-001", step: 0, scf_energy: -9, homo_ev: -5, lumo_ev: -2, gap_ev: 3 }, { mol_id: "SYN-DFT-001", step: 2, scf_energy: -10, homo_ev: -5, lumo_ev: -2, gap_ev: 3 }];
    return jsonResponse(browseEnvelope(results));
  }
  if (path === "/database-browser/experimental-process") {
    const results = empty ? [] : [{ source_file: "synthetic-process.csv", source_row_number: 1, polymer_id: "SYN-POLY-001", polymer_name: "Synthetic Polymer A", product_name: "Synthetic Film", process_flow_original_text: "Synthetic process at 300 K", material_original_text: "Synthetic monomers A and B" }];
    return jsonResponse(browseEnvelope(results));
  }
  if (path === "/database-browser/experimental-property") {
    const results = empty ? [] : [{ source_file: "synthetic-property.csv", source_row_number: 1, polymer_id: "SYN-POLY-001", polymer_name: "Synthetic Polymer A", property_category: "thermal", property_name_en: "Glass transition temperature", value: "218 °C" }];
    return jsonResponse(browseEnvelope(results));
  }
  if (path === "/database-browser/formulation") {
    const results = empty ? [] : [{ formulation_id: 900001, knowledge_id: 900001, source_file: "synthetic-formulation.csv", source_row_number: 1, polymer_iupac: "synthetic polymer", formulation: "A:B = 1:1", catalyst: null, temperature: "300 K", reaction_time: "1 h", solvent: "synthetic solvent" }];
    return jsonResponse(browseEnvelope(results));
  }

  if (path === "/lab-data/test-projects") return jsonResponse(empty ? [] : [{ id: 900001, projectName: "Synthetic tensile test", resultUnit: "MPa" }]);


  return jsonResponse({
    detail: `No handoff mock is defined for ${method} ${path}.`,
    code: "handoff_mock_missing"
  }, 501);
}

function isKnownPath(path: string): boolean {
  return new Set(["/usage/entitlements", "/assistant/tg/guide", "/assistant/tg/status", "/conditional-generation/polytao/descriptors", "/conditional-generation/polytao/jobs", "/conditional-generation/polytao/status", "/conditional-generation/tg/jobs", "/conditional-generation/tg/status", "/database-browser/datasets/analytics", "/database-browser/datasets/summary", "/database-browser/dft/molecules", "/database-browser/dft/steps", "/database-browser/experimental-process", "/database-browser/experimental-property", "/database-browser/formulation", "/database-browser/property-filter/histogram", "/database-browser/property-filter/observations", "/database-browser/property-filter/options", "/database-browser/property-filter/search", "/database-browser/smiles-lookup", "/database-browser/structure-property", "/dev-gpu-session/recover", "/dev-gpu-session/status", "/dft/pca-sample", "/knowledge/observations", "/knowledge/recordings", "/knowledge/search", "/lab-data/sample-measurements", "/lab-data/sample-measurements/count", "/lab-data/sample-measurements/stats/by-project", "/lab-data/sample-measurements/stats/summary", "/lab-data/test-projects", "/md-demo/atom-distance", "/md-demo/defaults", "/md-demo/run", "/monomer-dft/capabilities", "/monomer-dft/status", "/monomer-md/protocols", "/monomer-md/status", "/monomer-polymerization", "/monomer-polymerization/status", "/monomer-retrosynthesis", "/online-knowledge/default-config", "/online-knowledge/export-csv", "/online-knowledge/history", "/online-knowledge/history/clear", "/online-knowledge/jobs", "/online-knowledge/search", "/predict", "/query/smiles", "/reverse-design/tg", "/reverse-design/tg/jobs", "/structure/2d", "/structure/3d", "/structure/recognize-image", "/structure/standardize-smiles", "/task-events"]).has(path) || [
    /^\/monomer-(md|dft)\/jobs(?:\/[^/]+(?:\/(?:cancel|bundle|artifacts(?:\/[^/]+)?|visualization\/stages\/[^/]+\/trajectory))?)?$/,
    /^\/monomer-polymerization\/batch\/(?:imports(?:\/[^/]+\/preview)?|jobs(?:\/[^/]+(?:\/(?:cancel|results|artifacts\/[^/]+))?)?|templates\/[ab]\.(?:csv|xlsx))$/,
    /^\/knowledge\/recordings\/[^/]+\/(?:stop|summary)$/,
    /^\/assistant\/tg\/chat\/(?:image-stream|stream)$/,
    /^\/online-knowledge\/(?:jobs\/[^/]+|history\/\d+)$/,
    /^\/reverse-design\/tg\/jobs\/[^/]+$/,
    /^\/conditional-generation\/(?:tg|polytao)\/jobs\/[^/]+$/,
    /^\/dft\/molecule\/[^/]+$/
  ].some(pattern => pattern.test(path));
}

export function createHandoffMockFetch(options: MockFetchOptions = {}): typeof fetch {
  const pageUrl = options.pageUrl ?? (typeof window !== "undefined" ? window.location.href : "http://127.0.0.1/");
  const pageOrigin = new URL(pageUrl).origin;
  const apiBaseUrl = new URL(options.apiBaseUrl ?? "/api/v1", pageUrl);
  const apiPathPrefix = apiBaseUrl.pathname.replace(/\/$/, "");
  const nativeFetch = options.nativeFetch ?? globalThis.fetch?.bind(globalThis);
  const state = createState(pageUrl, path => nativeFetch ? nativeFetch(new URL(path, pageUrl)) : Promise.resolve(jsonResponse({ detail: "Bundled file fetch unavailable." }, 500)));

  return async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const rawUrl = typeof input === "string" || input instanceof URL ? String(input) : input.url;
    const url = new URL(rawUrl, pageUrl);
    const method = requestMethod(input, init);
    const isApiRequest = url.origin === apiBaseUrl.origin &&
      (url.pathname === apiPathPrefix || url.pathname.startsWith(`${apiPathPrefix}/`));

    if (isApiRequest) {
      const signal = requestSignal(input, init);
      if (signal?.aborted) throw new DOMException("The operation was aborted.", "AbortError");
      const path = url.pathname.slice(apiPathPrefix.length) || "/";
      // Authentication must stay usable in loading/error demos.
      if (path.startsWith("/auth/")) return authResponse(path, method, input, init, state);
      if (!isKnownPath(path)) return missing(method, path);
      if (!state.session.authenticated) return jsonResponse({ detail: "请登录演示账号。" }, 401);
      // Control metadata stays readable in every business scenario, after auth.
      // Preserve the full canonical example policy and quota snapshot.
      if (path === "/usage/entitlements") {
        if (method !== "GET") return missing(method, path);
        return jsonResponse(entitlementsFixture.entitlements, 200, { "Cache-Control": "no-store" });
      }
      const scenario = getScenario(pageUrl);
      if (scenario === "loading") return waitForAbort(signal);
      if (scenario === "error") return jsonResponse({ detail: "交接 Mock 已切换到错误场景。", code: "handoff_mock_error", retryable: true }, 503);
      return apiResponse(path, url, method, input, init, scenario === "empty", state);
    }

    if (url.origin !== pageOrigin && !["blob:", "data:"].includes(url.protocol)) {
      return jsonResponse({
        detail: "交接模式已阻止外部网络请求。",
        code: "handoff_external_request_blocked"
      }, 451);
    }

    if (!nativeFetch) {
      return jsonResponse({ detail: "No native fetch implementation is available." }, 500);
    }
    return nativeFetch(input, init);
  };
}

let restoreInstalledFetch: (() => void) | null = null;

export function installHandoffMockFetch(options: Omit<MockFetchOptions, "nativeFetch"> = {}): () => void {
  if (restoreInstalledFetch) return restoreInstalledFetch;
  const nativeFetch = globalThis.fetch.bind(globalThis);
  globalThis.fetch = createHandoffMockFetch({ ...options, nativeFetch });
  restoreInstalledFetch = () => {
    globalThis.fetch = nativeFetch;
    restoreInstalledFetch = null;
  };
  return restoreInstalledFetch;
}
