/** Formal polymer MD contracts; demo fixtures never satisfy these job types. */
export type PolymerMdQuality = "not_evaluated" | "passed" | "warning" | "not_converged" | "failed";
export type PolymerMdStatus = "pending" | "queued" | "running" | "completed" | "failed" | "cancel_requested" | "cancelled";
export type PolymerMdForcefield = "GAFF" | "GAFF2" | "GAFF2_mod" | "Dreiding" | "Dreiding_UT";
export type PolymerMdPostEquilibriumProperty = "tg" | "sp_total" | "sp_ced";
export type PolymerMdPostEquilibriumProtocols = {
  tg: { method: "step_wise"; minimum_temperature_k: number; maximum_temperature_k: number;
    interval_temperature_k: number; temperature_platforms: number; high_temperature_equilibration_ps: number;
    time_step_fs: number; temperature_interval_ps: number; temperature_hold_ps: number; cooling_ramp_ps: number;
    cooling_ramps: number; sampling_authority: "server"; requires_equilibrium: boolean };
  sp: { method: "molecular_interaction_rerun"; properties: ("sp_total" | "sp_ced")[];
    minimum_chain_count: number; decomposition: false; sampling_ps: number; burn_in_ps: number; sample_interval_ps: number;
    time_step_fs: number; sampling_authority: "server"; requires_equilibrium: boolean };
};
export type PolymerMdChainLength = { mode: "atoms" | "dp" | "molecular_weight"; value: number };
export type PolymerMdModelComponent = {
  id: string;
  smiles: string;
  n_chain?: number;
  chain_length?: PolymerMdChainLength;
};
export type PolymerMdBlock = { component_id: string; dp: number };
export type PolymerMdModelConfiguration = {
  version: 1;
  mode: "homopolymer" | "alternating" | "random" | "block" | "blend";
  components: PolymerMdModelComponent[];
  chain_length?: PolymerMdChainLength;
  ratios?: number[];
  blocks?: PolymerMdBlock[];
};
export type PolymerMdNativeConfiguration = {
  opt_method: "HF" | "wb97m-d3bj";
  opt_basis: string;
  charge: "RESP" | "ESP" | "Mulliken" | "Lowdin" | "gasteiger" | "zero";
  charge_method: "HF" | "wb97m-d3bj";
  charge_basis: string;
  sp_method: "HF" | "wb97m-d3bj";
  sp_basis: string;
  polar_method: "HF" | "wb97m-d3bj";
  polar_basis: string;
  properties: string[];
};
export type PolymerMdRequest = {
  smiles: string;
  temp: number;
  pressure: number;
  n_atom: number;
  n_chain: number;
  forcefield: PolymerMdForcefield;
  terminal_head: "CH3" | "H" | "OH";
  terminal_tail: "CH3" | "H" | "OH";
  tacticity: "atactic" | "isotactic" | "syndiotactic";
  calculation: "basic" | "basic_tc";
  eq_ns: number;
  additional_ns: number;
  max_additional: number;
  sample_interval_ps: number;
  tc_axis: "x" | "y" | "z";
  tc_ns: number;
  tc_rep: number;
  seed?: number | null;
  native_configuration?: PolymerMdNativeConfiguration;
  model_configuration?: PolymerMdModelConfiguration;
};
export type PolymerMdCatalogStatus = "connected" | "upstream_only" | "derived" | "diagnostic" | "fixed" | "experimental";
export type PolymerMdCatalogEntry = {
  id: string;
  label: string;
  status: PolymerMdCatalogStatus;
  description: string;
  requirements?: string[];
  selection?: { field: "native_configuration.properties"; properties: string[]; calculations: ("basic" | "basic_tc")[] };
  property_groups?: string[];
};
export type PolymerMdCapabilityCatalog = {
  catalog_version: number;
  source_sha: string;
  description: string;
  forcefields: PolymerMdCatalogEntry[];
  calculations: PolymerMdCatalogEntry[];
  property_groups: Array<{
    id: string;
    label: string;
    calculation: "basic" | "basic_tc" | null;
    status: PolymerMdCatalogStatus;
    properties: Array<{ key: string; label: string; unit: string; status: PolymerMdCatalogStatus; description: string; dependencies?: string[] }>;
  }>;
  parameters: Array<{
    key: string;
    label: string;
    group: "basic" | "advanced" | "protocol" | "upstream";
    status: PolymerMdCatalogStatus;
    description: string;
    default?: unknown;
    unit?: string;
    source?: string;
  }>;
};
export type PolymerMdAcceptedScope = {
  receipt_id: string; receipt_sha256: string; protocol_id: string; request_sha256: string;
  material_label: string; model_mode: PolymerMdModelConfiguration["mode"]; forcefield: PolymerMdForcefield;
  qm: Omit<PolymerMdNativeConfiguration, "properties">;
  conditions: { temperature_k: number; pressure_atm: number; eq_ns: number; additional_ns: number;
    max_additional: number; sample_interval_ps: number; tc_axis: "x" | "y" | "z"; tc_ns: number; tc_rep: number; seed: number };
  properties: string[]; reviewed_at: string; reviewer_ref: string; decision_sha256: string;
  evidence: Array<{ label: string; sha256: string; size_bytes: number }>;
  scope_policy: "exact_scientific_request_only";
};
export type PolymerMdAcceptanceSummary = {
  scientific_acceptance: string; adapter_scientific_validation: string;
  accepted_protocols: string[]; accepted_protocol_count: number;
  source?: "external_registry";
  registry_status?: "unconfigured" | "missing" | "verified" | "invalid" | "mismatch" | "revoked";
  binding?: { runtime_fingerprint: string | null; source_sha: string | null; patch_version: string | null;
    scientific_protocol_sha256: string | null } | null;
  accepted_scopes?: PolymerMdAcceptedScope[];
};
export type PolymerMdCapabilities = {
  enabled: boolean;
  submit_enabled: boolean;
  schema_ready: boolean;
  worker_ready: boolean;
  protocol_version: number;
  message?: string;
  reason?: string;
  admission_reason?: string | null;
  maintenance?: { enabled: boolean; updated_at: string | null; persisted: boolean } | null;
  resource_limits?: {
    cpu_count: number; memory_bytes: number; gpu_count: number; stage_timeout_seconds: number;
    job_timeout_seconds: number; max_job_bytes: number; max_total_atoms: number; max_replicated_atoms: number;
  } | null;
  acceptance_summary?: PolymerMdAcceptanceSummary;
  supported_forcefields: string[];
  supported_terminals: string[];
  supported_calculations: string[];
  supported_tacticities: string[];
  trajectory_available: boolean;
  distance_available: boolean;
  parameter_schema?: Record<string, unknown>;
  parameter_defaults?: Partial<PolymerMdRequest>;
  scientific_protocol?: Record<string, unknown>;
  native_configuration_schema?: Record<string, unknown>;
  model_configuration_spec?: Record<string, unknown>;
  post_equilibrium_protocols?: PolymerMdPostEquilibriumProtocols;
  supported_model_modes?: PolymerMdModelConfiguration["mode"][];
  user_configuration_spec?: Record<string, unknown>;
  capability_catalog?: PolymerMdCapabilityCatalog;
  runtime?: {
    source_sha?: string;
    patch_version?: string;
    runtime_fingerprint?: string;
    scientific_acceptance?: string;
    adapter_scientific_validation?: string;
    accepted_protocols?: string[];
  };
  limits: {
    per_owner_active: number;
    global_active: number;
    execution: number;
    max_total_atoms: number;
    max_rnemd_atoms: number;
  };
};
export type PolymerMdServiceStatus = PolymerMdCapabilities & {
  worker?: Record<string, unknown> | null;
};
export type PolymerMdStage = {
  stage_run_id: string;
  stage: string;
  attempt: number;
  status: string;
  quality_status: PolymerMdQuality;
  results: Record<string, unknown>;
  quality: Record<string, unknown>;
  errors: Array<Record<string, unknown>>;
  started_at?: string | null;
  finished_at?: string | null;
};
export type PolymerMdArtifact = {
  artifact_id: string;
  stage_run_id: string;
  kind: string;
  filename: string;
  media_type: string;
  size_bytes: number;
  sha256: string;
};
export type PolymerMdJob = {
  job_id: string;
  status: PolymerMdStatus;
  quality_status: PolymerMdQuality;
  scientific_request: PolymerMdRequest;
  stage: string;
  stage_runs: PolymerMdStage[];
  artifacts: PolymerMdArtifact[];
  results: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  artifact_state?: "available" | "delete_requested" | "deleted";
  error_code?: string | null;
  error_message?: string | null;
};
export type PolymerMdJobPage = {
  items: PolymerMdJob[];
  total: number;
  page: number;
  page_size: number;
};
export type PolymerMdAtom = {
  atom_id: number;
  chain_id: number;
  atom_type: string;
  x: number;
  y: number;
  z: number;
};
export type PolymerMdFrame = {
  frame_index: number;
  time_ps: number;
  points: PolymerMdAtom[];
  box: {
    lx: number;
    ly: number;
    lz: number;
  };
};
export type PolymerMdTrajectory = {
  job_id: string;
  stage_run_id: string;
  total_frames: number;
  frames: PolymerMdFrame[];
};
export type PolymerMdDistance = {
  job_id: string;
  stage_run_id: string;
  frames: number[];
  time_ps: number[];
  distance: number[];
  unit?: string;
  sampling?: {
    source_frame_count: number;
    start_frame: number;
    /** Exclusive upper bound, matching the worker's stop_frame contract. */
    stop_frame: number;
    stride: number;
    sampled_frames: number;
    first_frame: number;
    last_frame: number;
    start_ps: number;
    stop_ps: number;
  };
  atom_1?: {
    atom_id: number;
    chain_id: number;
    atom_type: string;
  };
  atom_2?: {
    atom_id: number;
    chain_id: number;
    atom_type: string;
  };
  stats?: {
    n_atoms: number;
    n_frames: number;
    source_n_frames: number;
    n_chains: number;
    use_pbc: boolean;
    min_distance: number;
    max_distance: number;
  };
};
