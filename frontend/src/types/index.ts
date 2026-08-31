import type { RefObject } from "react";

export type MatchMode = "structure" | "property";
export type SmilesLookupTable = "polymers" | "properties" | "pi_candidates";
export type MonomerRetrosynthesisTargetRole = "auto" | "diamine" | "dianhydride" | "other";
export type MonomerPolymerizationTargetClass =
  | "polyolefin"
  | "polyester"
  | "polyether"
  | "polyamide"
  | "polyimide"
  | "polyurethane"
  | "polyoxazolidone"
  | "all";

export type StructureWorkspaceContext = {
  smiles: string;
  setSmiles: (value: string) => void;
  iframeRef: RefObject<HTMLIFrameElement | null>;
  setIsReady: (ready: boolean) => void;
  getCurrentSmiles: () => Promise<string>;
};

export type SmilesQueryRequest = {
  smiles: string;
  match_mode: MatchMode;
  similarity_threshold: number;
  top_k: number;
  property_name: PredictableProperty | null;
};

export type PredictableProperty =
  | "Glass transition temperature"
  | "Melting temperature"
  | "Thermal decomposition temperature"
  | "Thermal decomposition weight loss"
  | "Elongation at break"
  | "Tensile stress strength at break"
  | "O2 Permeability Barrer"
  | "Co2 Permeability Barrer"
  | "H2 Permeability Barrer";

export type PredictRequest = {
  smiles: string;
  properties: PredictableProperty[];
};

export type PropertyItem = {
  property_category: string;
  property_name: string;
  property_value: string;
  property_value_num: number | null;
  property_unit: string | null;
  label_source: string | null;
};

export type PropertyGroups = {
  thermal: PropertyItem[];
  mechanical: PropertyItem[];
  electrical: PropertyItem[];
  chemical: PropertyItem[];
  optical: PropertyItem[];
  other: PropertyItem[];
};

export type PolymerResult = {
  polymer_id: string;
  polymer_name: string;
  smiles: string;
  canonical_smiles: string | null;
  similarity_score: number | null;
  structure_svg: string | null;
  matched_property_name: string | null;
  matched_property_value: number | null;
  matched_property_unit: string | null;
  matched_property_source: string | null;
  properties: PropertyGroups;
};

export type SmilesQueryResponse = {
  match_type: MatchMode;
  query_time_ms: number;
  total: number;
  predicted_property_name: PredictableProperty | null;
  predicted_property_value: number | null;
  predicted_property_unit: string | null;
  results: PolymerResult[];
};

export type PredictResponse = {
  predictions: Partial<Record<PredictableProperty, number>>;
  query_time_ms: number;
};

export type MdDemoRunRequest = {
  smiles: string;
  temperature: number;
  pressure: number;
  n_atom: number;
  n_chain: number;
  forcefield: string;
};

export type MdDemoStageSummary = {
  stage_id: "eq1" | "eq2" | "eq3" | string;
  label: string;
  description: string;
  n_atoms: number;
  n_frames: number;
  dt_ps: number;
  n_chains: number;
  data_file_size_bytes: number;
  trajectory_file_size_bytes: number;
  log_file_size_bytes: number | null;
  box: {
    lx: number;
    ly: number;
    lz: number;
  };
};

export type MdDemoSeriesPoint = {
  step?: number;
  frame?: number;
  time_ps: number;
  value: number;
};

export type MdDemoSeries = {
  key: string;
  label: string;
  unit: string;
  points: MdDemoSeriesPoint[];
};

export type MdDemoTrajectoryPoint = {
  atom_id: number;
  chain_id: number;
  atom_type: string;
  x: number;
  y: number;
  z: number;
};

export type MdDemoTrajectoryPreview = {
  stage_id: string;
  frame_index: number;
  time_ps: number;
  sampled_points: number;
  points: MdDemoTrajectoryPoint[];
  box: {
    lx: number;
    ly: number;
    lz: number;
  };
};

export type MdDemoAtomSelection = Pick<MdDemoTrajectoryPoint, "atom_id" | "chain_id" | "atom_type">;

export type MdDemoAtomDistanceRequest = {
  atom_id_1: number;
  atom_id_2: number;
  use_pbc: boolean;
};

export type MdDemoAtomDistanceResponse = {
  atom_1: MdDemoAtomSelection;
  atom_2: MdDemoAtomSelection;
  frames: number[];
  time_ps: number[];
  distance: number[];
  series: MdDemoSeries;
  stats: {
    n_atoms: number;
    n_frames: number;
    source_n_frames: number;
    n_chains: number;
    use_pbc: boolean;
    min_distance: number;
    max_distance: number;
  };
};

export type MdDemoSummary = {
  primary_stage: string;
  elapsed_seconds: number;
  n_atoms: number;
  n_frames: number;
  n_chains: number;
  final_density_g_cm3: number;
  mean_temperature_k: number;
  mean_total_energy_kcal_mol: number;
};

export type MdDemoFixtureMetadata = {
  fixture_version: number;
  source: {
    label: string;
    data_root_hint: string;
    generated_from: string[];
  };
};

export type MdDemoDefaultsResponse = {
  default_request: MdDemoRunRequest;
  available_stages: MdDemoStageSummary[];
  summary: MdDemoSummary;
  fixture_metadata: MdDemoFixtureMetadata;
};

export type MdDemoRunResponse = {
  input: MdDemoRunRequest;
  run_id: string;
  status: "completed";
  query_time_ms: number;
  stages: MdDemoStageSummary[];
  summary: MdDemoSummary;
  density_series: MdDemoSeries;
  thermo_series: MdDemoSeries[];
  trajectory_preview: MdDemoTrajectoryPreview;
  atom_distance_series: MdDemoAtomDistanceResponse | null;
  fixture_metadata: MdDemoFixtureMetadata;
};

export type MonomerMdJobStatus = "pending" | "submitted" | "running" | "cancel_requested" | "completed" | "failed" | "cancelled";
export type MonomerMdProtocol = "DensityDemo" | "Density" | "Transport" | "HVap" | "Dielectric" | "Compressibility";
export type MonomerMdRunMode = "demo" | "formal";

export type MonomerMdServiceStatusResponse = {
  enabled?: boolean;
  available?: boolean;
  default_steps?: number;
  worker_base_url_configured?: boolean;
  status?: string;
  worker_status?: string | null;
  worker_mode?: string | null;
  active_jobs?: number | null;
  database_active_jobs?: number | null;
  oldest_active_heartbeat_age_seconds?: number | null;
  max_active_jobs?: number | null;
  accepting_jobs?: boolean | null;
  draining?: boolean;
  busy?: boolean;
  can_submit?: boolean;
  formal_running_jobs?: number;
  formal_queued_jobs?: number;
  formal_max_running_jobs?: number;
  formal_max_queued_jobs?: number;
  formal_can_submit?: boolean;
  job_retention_enabled?: boolean;
  job_retention_days?: number;
  job_retention_status?: "disabled" | "standby" | "ready" | "degraded";
  job_retention_last_sweep_at?: string | null;
  message?: string | null;
  queue_depth?: number | null;
  running_jobs?: number | null;
  worker?: string | null;
  protocols?: Record<string, MonomerMdProtocolInfo>;
  [key: string]: string | number | boolean | null | undefined | Record<string, MonomerMdProtocolInfo>;
};

export type MonomerMdProtocolInfo = {
  protocol: MonomerMdProtocol;
  run_mode: MonomerMdRunMode;
  supported?: boolean;
  runtime_ready?: boolean;
  runtime_error?: string | null;
  default_config?: Record<string, unknown>;
  required_result_file?: string;
  [key: string]: unknown;
};

export type MonomerMdProtocolCatalogResponse = {
  enabled: boolean;
  available: boolean;
  protocols: MonomerMdProtocolInfo[];
  message: string;
};

export type MonomerMdJobCreateRequest = {
  smiles?: string;
  protocol?: MonomerMdProtocol;
  run_mode?: MonomerMdRunMode;
  config_json?: Record<string, unknown>;
};
export type MonomerMdJobCreateResponse = { job_id: string; status: MonomerMdJobStatus };
export type MonomerMdSeriesPoint = { step?: number; frame?: number; time_ps?: number; time_ns?: number; value?: number; [key: string]: string | number | null | undefined };
export type MonomerMdSeries = { key?: string; label?: string; unit?: string; points: MonomerMdSeriesPoint[] } | MonomerMdSeriesPoint[];
export type MonomerMdTrajectoryPoint = { atom_id?: number; chain_id?: number; atom_type?: string; element?: string; x: number; y: number; z: number };
export type MonomerMdTrajectoryPreview = { stage_id?: string; frame_index?: number; time_ps?: number; sampled_points?: number; points?: MonomerMdTrajectoryPoint[]; atoms?: MonomerMdTrajectoryPoint[]; box?: { lx?: number; ly?: number; lz?: number }; preview_url?: string | null; format?: string | null; content?: string | null; [key: string]: unknown };
export type MonomerMdArtifact = { name?: string; label?: string; kind?: string; url?: string | null; path?: string | null; size_bytes?: number | null; [key: string]: string | number | boolean | null | undefined };
export type MonomerMdSimulationResult = {
  protocol?: MonomerMdProtocol;
  run_mode?: MonomerMdRunMode;
  density_series?: MonomerMdSeries;
  temperature_series?: MonomerMdSeries;
  energy_series?: MonomerMdSeries;
  trajectory_preview?: MonomerMdTrajectoryPreview | null;
  summary: Record<string, string | number | boolean | null>;
  metrics?: Record<string, unknown>;
  artifact_manifest?: Record<string, unknown>;
  artifacts: MonomerMdArtifact[] | Record<string, MonomerMdArtifact | string | number | boolean | null>;
  warnings?: string[];
  not_equilibrated?: boolean;
  physical_density_estimate?: boolean;
  physical_result?: boolean;
};
export type MonomerMdJobResponse = {
  job_id: string;
  status: MonomerMdJobStatus;
  input_smiles?: string;
  canonical_smiles?: string;
  protocol?: MonomerMdProtocol;
  run_mode?: MonomerMdRunMode;
  config_json?: Record<string, unknown>;
  components?: Record<string, unknown>;
  requested_steps?: number;
  completed_steps?: number;
  progress_percent?: number | null;
  progress_stage?: string | null;
  progress_message?: string | null;
  queue_position?: number | null;
  cancel_requested_at?: string | null;
  error_message?: string | null;
  worker_id?: string | null;
  worker_job_id?: string | null;
  worker_version?: string | null;
  engine?: string | null;
  artifact_root?: string | null;
  artifact_manifest?: Record<string, unknown>;
  artifact_deleted_at?: string | null;
  artifact_delete_message?: string | null;
  result_summary?: Record<string, string | number | boolean | null>;
  byteff2_git_sha?: string | null;
  gpu_device?: string | null;
  error_category?: string | null;
  smiles?: string;
  created_at?: string;
  updated_at?: string;
  started_at?: string | null;
  finished_at?: string | null;
  message?: string | null;
  error?: string | null;
  progress?: number | null;
  result?: MonomerMdSimulationResult | null;
  density_series?: MonomerMdSeries;
  temperature_series?: MonomerMdSeries;
  energy_series?: MonomerMdSeries;
  trajectory_preview?: MonomerMdTrajectoryPreview | null;
  summary?: Record<string, string | number | boolean | null>;
  artifacts?: MonomerMdArtifact[] | Record<string, MonomerMdArtifact | string | number | boolean | null>;
};

export type MonomerMdJobPageResponse = {
  items: MonomerMdJobResponse[];
  total: number;
  page: number;
  page_size: number;
};

export type MonomerMdJobListQuery = {
  run_mode?: MonomerMdRunMode;
  active_only?: boolean;
  protocol?: MonomerMdProtocol | "";
  status?: MonomerMdJobStatus | "";
  page?: number;
  page_size?: number;
};

export type MonomerDftCalculationType = "single_point" | "optimization";
export type MonomerDftModelName =
  | "aimnet2"
  | "aimnet2-2025"
  | "aimnet2-b973c"
  | "aimnet2-nse"
  | "aimnet2-pd"
  | "aimnet2-rxn";
export type MonomerDftJobStatus =
  | "pending"
  | "queued"
  | "running"
  | "completed"
  | "failed"
  | "cancel_requested"
  | "cancelled";
export type MonomerDftProperty = "energy" | "forces" | "charges" | "hessian" | "frequencies";
export type MonomerDftProgressStage =
  | "pending"
  | "queued"
  | "validating"
  | "conformer"
  | "single_point"
  | "optimization"
  | "hessian"
  | "frequency"
  | "artifacts"
  | "running"
  | "dispatch_retry"
  | "dispatch_failed"
  | "cancel_requested"
  | "worker_failed"
  | "completed"
  | "failed"
  | "cancelled";

export type MonomerDftInput = {
  smiles: string;
  net_charge: number | null;
  multiplicity: number;
  psmiles_mode: "close" | "cap" | null;
};

export type MonomerDftConformerOptions = {
  seed: number;
  max_iterations: number;
};

export type MonomerDftSinglePointOptions = {
  properties: MonomerDftProperty[];
};

export type MonomerDftPostOptimizationProperty = "hessian" | "frequencies";

export type MonomerDftOptimizationOptions = {
  fmax_eV_per_A: number;
  max_steps: number;
  post_optimization_properties: MonomerDftPostOptimizationProperty[];
};

type MonomerDftJobCreateRequestBase = {
  input: MonomerDftInput;
  model: MonomerDftModelName;
  conformer: MonomerDftConformerOptions;
};

export type MonomerDftSinglePointRequest = MonomerDftJobCreateRequestBase & {
  calculation_type: "single_point";
  single_point: MonomerDftSinglePointOptions;
  optimization?: never;
};

export type MonomerDftOptimizationRequest = MonomerDftJobCreateRequestBase & {
  calculation_type: "optimization";
  optimization: MonomerDftOptimizationOptions;
  single_point?: never;
};

export type MonomerDftJobCreateRequest = MonomerDftSinglePointRequest | MonomerDftOptimizationRequest;

export type MonomerDftModelCapability = {
  id: MonomerDftModelName;
  label: string;
  description?: string | null;
  available: boolean;
  is_default?: boolean;
  deprecated?: boolean;
  deprecation_message?: string | null;
  supported_calculation_types: MonomerDftCalculationType[];
  supported_properties: MonomerDftProperty[];
  supported_elements: string[];
  supports_spin: boolean;
  charge_min?: number;
  charge_max?: number;
};

export type MonomerDftCapabilitiesResponse = {
  enabled: boolean;
  available: boolean;
  schema_ready: boolean;
  calculation_types: MonomerDftCalculationType[];
  properties: MonomerDftProperty[];
  default_model: MonomerDftModelName;
  models: MonomerDftModelCapability[];
  defaults: {
    conformer: MonomerDftConformerOptions;
    single_point: MonomerDftSinglePointOptions;
    optimization: MonomerDftOptimizationOptions;
  };
  limits: {
    max_atoms?: number;
    max_heavy_atoms?: number;
    max_hessian_atoms?: number;
    max_optimization_steps: number;
    min_optimization_steps?: number;
    max_concurrent_jobs: number;
    max_queued_jobs: number;
    max_active_jobs: number;
    [key: string]: number | undefined;
  };
  worker?: Record<string, unknown>;
  message?: string | null;
};

export type MonomerDftServiceStatusResponse = {
  enabled: boolean;
  available: boolean;
  schema_ready: boolean;
  worker_status: string;
  runtime_ready: boolean | null;
  draining: boolean | null;
  active_jobs: number;
  max_active_jobs: number;
  job_retention_enabled?: boolean;
  job_retention_days?: number;
  job_retention_status?: "disabled" | "standby" | "ready" | "degraded";
  job_retention_last_sweep_at?: string | null;
  message: string;
};

export type MonomerDftVector3 = [number, number, number];

export type MonomerDftAtom = {
  index: number;
  atomic_number: number;
  element: string;
  isotope_mass_number?: number | null;
  atomic_mass_u?: number | null;
  position_angstrom: MonomerDftVector3;
  charge_e?: number | null;
  force_ev_per_angstrom?: MonomerDftVector3 | null;
};

export type MonomerDftOptimizationStep = {
  step: number;
  energy_eV: number;
  fmax_eV_per_A: number;
};

export type MonomerDftFrequencyResult = {
  artifact_id: string;
  values_cm_1: number[];
  mode_count: number;
  removed_rigid_modes: number;
  expected_rigid_modes: number;
  linear_molecule: boolean;
  imaginary_threshold_cm_1: number;
  imaginary_mode_count: number;
  imaginary_values_cm_1: number[];
  near_zero_mode_count: number;
};

export type MonomerDftHessianSummary = {
  shape: [number, number];
  symmetry_max_abs_eV_per_A2: number;
  symmetry_relative_error: number;
  symmetric_within_tolerance: boolean;
  artifact_id: string;
  units: string;
};

export type MonomerDftTiming = Record<string, number>;

type MonomerDftProvenanceBase = Record<string, string | number | boolean | null | undefined> & {
  aimnet_commit?: string | null;
  aimnet_wheel_sha256?: string | null;
  model_id?: string | null;
  model_sha256?: string | null;
  torch_version?: string | null;
  cuda_version?: string | null;
  warp_version?: string | null;
  gpu_name?: string | null;
  gpu_uuid?: string | null;
  gpu_physical_device?: string | null;
  gpu_logical_device?: string | null;
  gpu_budget_mib?: number | null;
  gpu_active_thread_percentage?: number | null;
  gpu_preferred?: boolean | null;
  execution_path?: "primary" | "overflow" | null;
  broker_instance_id?: string | null;
  lease_id?: string | null;
  fencing_token?: number | null;
  conformer_seed?: number | null;
  rdkit_version?: string | null;
  rdkit_force_field?: string | null;
  mass_source?: string | null;
};

export type MonomerDftProvenance = MonomerDftProvenanceBase & {
  rdkit_optimization_performed?: boolean | null;
  rdkit_optimization_status?: number | null;
};

type MonomerDftProvenanceV1 = MonomerDftProvenance;

type MonomerDftProvenanceV2 = MonomerDftProvenanceBase & {
  rdkit_optimization_performed: boolean;
  rdkit_optimization_status: number;
  rdkit_version: string;
  mass_source: string;
  execution_path: "primary" | "overflow";
  gpu_uuid: string;
  gpu_budget_mib: number;
  broker_instance_id: string;
  lease_id: string;
  fencing_token: number;
};

export type MonomerDftArtifact = {
  artifact_id: string;
  name: string;
  media_type: string;
  size_bytes: number;
  sha256: string;
  available: boolean;
};

export type MonomerDftResultWarning = { code: string; message: string };

type MonomerDftResultAtomsV1 = {
  count: number;
  atomic_numbers: number[];
  symbols: string[];
};

type MonomerDftResultAtomsV2 = MonomerDftResultAtomsV1 & {
  /** 0 means natural abundance; positive values identify an explicit isotope. */
  isotope_mass_numbers: number[];
  atomic_masses_u: number[];
};

type MonomerDftResultRdkitV1 = {
  seed: number;
  force_field: string;
  optimization_status: number;
  optimization_performed?: boolean | null;
  optimization_state?: "not_performed" | "converged" | "not_converged" | null;
};

type MonomerDftResultRdkitV2 = {
  seed: number;
  force_field: string;
  optimization_status: number;
  optimization_performed: boolean;
  optimization_state: "not_performed" | "converged" | "not_converged";
};

type MonomerDftResultBase = {
  calculation_type: MonomerDftCalculationType;
  engine: "aimnet2" | string;
  model: MonomerDftModelName;
  input: {
    input_type: "smiles" | "psmiles" | string;
    canonical_smiles: string;
    net_charge: number;
    input_formal_charge: number;
    multiplicity: number;
    electron_count: number;
  };
  geometry: {
    initial_coordinates_angstrom: MonomerDftVector3[];
    final_coordinates_angstrom: MonomerDftVector3[];
    units: "angstrom" | string;
  };
  properties: {
    energy: { value_eV: number };
    charges?: {
      values_e: number[];
      sum_e: number;
      conservation_error_e: number;
      conserved: boolean;
    };
    spin_charges?: { values_e: number[] };
    forces?: {
      values_eV_per_A: MonomerDftVector3[];
      fmax_eV_per_A: number;
    };
    hessian?: MonomerDftHessianSummary;
    frequencies?: MonomerDftFrequencyResult;
  };
  optimization: {
    converged: boolean;
    steps: number;
    fmax_threshold_eV_per_A: number;
    max_steps: number;
    trajectory_artifact_id: string;
    trace: MonomerDftOptimizationStep[];
  } | null;
  scientific_status: {
    calculation_completed: boolean;
    geometry_status: "converged" | "max_steps_reached" | "not_optimized" | string;
    is_stationary: boolean;
    minimum_assessment: "unassessed" | "not_converged" | "confirmed_minimum" | "nonminimum_or_saddle";
    stationary_point?: "minimum" | "first_order_saddle" | "higher_order_saddle" | "not_evaluated" | "not_stationary" | string;
    fmax_eV_per_A: number | null;
  };
  warnings: MonomerDftResultWarning[];
  timings: MonomerDftTiming;
};

export type MonomerDftResult = MonomerDftResultBase & (
  | {
    schema_version: 1;
    atoms: MonomerDftResultAtomsV1;
    rdkit: MonomerDftResultRdkitV1;
    provenance: MonomerDftProvenanceV1;
  }
  | {
    schema_version: 2;
    atoms: MonomerDftResultAtomsV2;
    rdkit: MonomerDftResultRdkitV2;
    provenance: MonomerDftProvenanceV2;
  }
);

export type MonomerDftError = {
  code: string;
  message: string;
  retryable: boolean;
  details?: Record<string, unknown> | null;
};

export type MonomerDftArtifactsState = "available" | "delete_requested" | "deleted" | "none";

export type MonomerDftJobResponse = {
  job_id: string;
  calculation_type: MonomerDftCalculationType;
  status: MonomerDftJobStatus;
  request: MonomerDftJobCreateRequest;
  request_sha256: string;
  attempt: number;
  queue_position: number | null;
  stage: MonomerDftProgressStage;
  progress_percent: number;
  scientific_status: string | null;
  warnings: string[];
  result?: MonomerDftResult | null;
  timings: MonomerDftTiming;
  provenance: MonomerDftProvenance;
  error?: MonomerDftError | null;
  artifacts: MonomerDftArtifact[];
  artifacts_state: MonomerDftArtifactsState;
  artifacts_deleted: boolean;
  cancel_requested: boolean;
  created_at: string;
  updated_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  idempotent_replay: boolean;
};

export type MonomerDftJobCreateResponse = MonomerDftJobResponse;

export type MonomerDftJobListResponse = {
  items: MonomerDftJobResponse[];
  page: number;
  page_size: number;
  total: number;
};

export type MonomerDftJobListQuery = {
  page: number;
  page_size: number;
  status?: MonomerDftJobStatus | "";
  calculation_type?: MonomerDftCalculationType | "";
};

export type MonomerDftArtifactDeleteResponse = {
  job_id: string;
  deleted: boolean;
  deleted_artifacts: number;
  artifacts_state: MonomerDftArtifactsState;
  message: string;
};

export type MonomerDftTrajectoryArtifact = {
  units: { energy: string; fmax: string; coordinates: string; charges?: string };
  frames: Array<MonomerDftOptimizationStep & { coordinates_angstrom: MonomerDftVector3[] }>;
};

export type StructureImageRecognitionResponse = {
  smiles: string;
  molfile: string | null;
  confidence: number | null;
  warnings: string[];
  query_time_ms: number;
};

export type MonomerRetrosynthesisRequest = {
  smiles: string;
  target_role: MonomerRetrosynthesisTargetRole;
  num_beams: number;
  num_return_sequences: number;
  max_new_tokens?: number;
};

export type RetrosynthesisReactant = {
  input_smiles: string;
  canonical_smiles: string | null;
  valid_smiles: boolean;
  heavy_atom_count: number | null;
};

export type MonomerRetrosynthesisCandidate = {
  rank: number;
  raw_output: string;
  reactants_smiles: string;
  canonical_reactants_smiles: string | null;
  reactants: RetrosynthesisReactant[];
  valid_smiles: boolean;
  all_reactants_smaller_than_target: boolean | null;
  reaction_hint: string;
};

export type MonomerRetrosynthesisResponse = {
  input_smiles: string;
  canonical_smiles: string;
  target_role: MonomerRetrosynthesisTargetRole;
  inferred_target_role: Exclude<MonomerRetrosynthesisTargetRole, "auto">;
  query_time_ms: number;
  total: number;
  candidates: MonomerRetrosynthesisCandidate[];
};

export type MonomerPolymerizationRequest = {
  monomer_a_smiles: string;
  monomer_b_smiles?: string | null;
  target_class: MonomerPolymerizationTargetClass;
  max_results: number;
};

export type MonomerPolymerizationInput = {
  role: "monomer_a" | "monomer_b";
  input_smiles: string;
  canonical_smiles: string;
};

export type MonomerPolymerizationCandidate = {
  rank: number;
  monomer_a_smiles: string;
  monomer_b_smiles: string | null;
  polymer_smiles: string;
  polymer_class: string;
  reaction_id: number | null;
  reaction_name: string | null;
  reactset: string[];
  structure_svg: string | null;
};

export type MonomerPolymerizationResponse = {
  input_monomers: MonomerPolymerizationInput[];
  target_class: MonomerPolymerizationTargetClass;
  query_time_ms: number;
  total: number;
  results: MonomerPolymerizationCandidate[];
  warnings: string[];
};

export type MonomerPolymerizationStatusResponse = {
  enabled: boolean;
  available: boolean;
  default_target_class: MonomerPolymerizationTargetClass;
  available_target_classes: MonomerPolymerizationTargetClass[];
  target_requirements?: Partial<Record<MonomerPolymerizationTargetClass, {
    min_monomers: number;
    max_monomers: number;
    monomer_b_required: boolean;
    note: string;
  }>>;
  max_results_limit: number;
  message: string;
};

export type SmilesStandardizeRequest = {
  smiles: string;
};

export type Structure2DResponse = {
  structure_svg: string;
};

export type SmilesStandardizeResponse = {
  input_smiles: string;
  standardized_smiles: string;
  changed: boolean;
  query_time_ms: number;
};

export type SmilesLookupRequest = {
  smiles: string;
  table: SmilesLookupTable;
};

export type SmilesLookupResult = {
  record_id: string;
  source_column: string;
  smiles: string;
  canonical_smiles: string | null;
  structure_svg: string | null;
  summary: string;
  fields: Record<string, string | number | boolean | null>;
};

export type SmilesLookupResponse = {
  query_smiles: string;
  canonical_smiles: string;
  table: SmilesLookupTable;
  exists: boolean;
  total: number;
  query_time_ms: number;
  results: SmilesLookupResult[];
};

export type KnowledgeSearchGroup = {
  terms: string[];
};

export type KnowledgeSearchRequest = {
  query: string;
  top_k: number;
  page?: number;
  page_size?: number;
  groups?: KnowledgeSearchGroup[];
  terms?: string[];
};

export type KnowledgeDocumentResult = {
  knowledge_id: number;
  source_file: string;
  source_row_number: number;
  source_sequence: string | null;
  title_zh: string | null;
  title_en: string | null;
  abstract: string;
  abstract_snippet: string;
  claim: string | null;
  analysis: string | null;
  is_polymer_synthesis: string | null;
  judgement_reason: string | null;
  polymer_iupac: string | null;
  formulation: string | null;
  catalyst: string | null;
  temperature: string | null;
  reaction_time: string | null;
  solvent: string | null;
  matched_terms: string[];
  matched_fields: string[];
};

export type KnowledgeSearchResponse = {
  query: string;
  groups: KnowledgeSearchGroup[];
  terms: string[];
  page: number;
  page_size: number;
  query_time_ms: number;
  total: number;
  results: KnowledgeDocumentResult[];
};

export type KnowledgeNavigationRequest = {
  query: string;
  groups?: KnowledgeSearchGroup[];
  terms?: string[];
};

export type OnlineKnowledgeMode = "synthesis" | "property";

export type OnlineKnowledgeSearchRequest = {
  material: string;
  api_key?: string | null;
  base_url: string;
  model: string;
  mode: OnlineKnowledgeMode;
  max_papers: number;
  extraction_delay_seconds: number;
  use_server_default?: boolean;
};

export type OnlineKnowledgeDefaultConfigResponse = {
  base_url: string;
  model: string;
  max_papers: number;
  has_server_api_key: boolean;
};

export type OnlineKnowledgeCountItem = {
  label: string;
  count: number;
  percentage: number;
};

export type OnlineKnowledgeSynthesis = {
  method: string;
  reaction_type: string;
  product_name: string;
  product_abbreviation: string;
  temperature: string;
  catalyst: string;
  solvent: string;
  time: string;
  atmosphere: string;
  pressure: string;
  initiator: string;
  reactants: string;
  properties: string;
};

export type OnlineKnowledgePropertyPoint = {
  polymer_type: string;
  polymer_name: string;
  condition_name: string;
  condition_value: string;
  property_name: string;
  property_value: string;
  relationship: string;
  paper_title: string;
};

export type OnlineKnowledgeSearchResponse = {
  material: string;
  mode: OnlineKnowledgeMode;
  query_time_ms: number;
  totalPapers: number;
  max_papers: number;
  exampleUsed: boolean;
  stats: Record<string, unknown>;
  syntheses: OnlineKnowledgeSynthesis[];
  propertyPoints: OnlineKnowledgePropertyPoint[];
  temperatureDistribution: OnlineKnowledgeCountItem[];
  solventDistribution: OnlineKnowledgeCountItem[];
  catalystTable: OnlineKnowledgeCountItem[];
  tempLabels: OnlineKnowledgeCountItem[];
  conditionSummary: string[];
  reactionTypeTable: OnlineKnowledgeCountItem[];
  propertyNameDistribution: OnlineKnowledgeCountItem[];
  conditionDistribution: OnlineKnowledgeCountItem[];
  polymerTypeDistribution: OnlineKnowledgeCountItem[];
  relationshipDistribution: OnlineKnowledgeCountItem[];
  dataframe: Record<string, unknown>[];
};

export type OnlineKnowledgeHistoryItem = {
  history_id: number;
  material: string;
  mode: OnlineKnowledgeMode;
  timestamp: string;
  papers_found: number;
  reactions_extracted: number;
  max_papers: number;
  result_data: OnlineKnowledgeSearchResponse;
};

export type OnlineKnowledgeHistoryResponse = {
  history: OnlineKnowledgeHistoryItem[];
};

export type OnlineKnowledgeJobStatus = "pending" | "running" | "completed" | "failed";

export type OnlineKnowledgeJobCreateResponse = {
  job_id: string;
  status: OnlineKnowledgeJobStatus;
};

export type OnlineKnowledgeJobResponse = {
  job_id: string;
  status: OnlineKnowledgeJobStatus;
  material: string;
  mode: OnlineKnowledgeMode;
  max_papers: number;
  progress_stage: string;
  progress_message: string;
  processed_papers: number;
  total_papers: number;
  created_at: string;
  updated_at: string;
  error_message: string | null;
  result: OnlineKnowledgeSearchResponse | null;
};

export type LabDataTestProject = {
  id: number;
  projectName: string;
  resultUnit: string;
};

export type LabDataSampleMeasurementPayload = {
  sampleId: string;
  experimentProject: string;
  instrumentId: string;
  operator: string;
  collectionTime: string;
  temperature: number | null;
  concentration: number | null;
  resultValue: number;
  resultUnit: string;
  remarks: string | null;
};

export type LabDataSampleMeasurement = LabDataSampleMeasurementPayload & {
  id: number;
};

export type LabDataSampleMeasurementPage = {
  items: LabDataSampleMeasurement[];
  total: number;
  page: number;
  pageSize: number;
};

export type LabDataProjectStats = {
  experimentProject: string;
  count: number;
};

export type LabDataSummary = {
  totalCount: number;
  byProject: LabDataProjectStats[];
};

export type OnlineKnowledgeExportResponse = {
  success: boolean;
  csv_content: string;
  filename: string;
};

export type ReverseDesignTgRequest = {
  target_tg: number | null;
  smiles: string;
  similarity_threshold: number;
  candidate_size: number;
};

export type ReverseDesignTgCandidate = {
  rank: number;
  pi_id: number;
  polymer_smiles: string;
  canonical_polym: string | null;
  monomer_a_smiles: string;
  monomer_b_smiles: string;
  monomer_a_iupac: string | null;
  monomer_b_iupac: string | null;
  monomer_a_structure_svg: string | null;
  monomer_b_structure_svg: string | null;
  tg_value: number;
  tg_unit: "°C";
  tg_difference: number;
  similarity_score: number;
  structure_svg: string | null;
  knowledge_available: boolean;
};

export type ReverseDesignTgResponse = {
  target_tg: number;
  query_time_ms: number;
  candidate_pool_size: number;
  sampled_candidate_count: number;
  total: number;
  data_source: "pi_reverse_design";
  results: ReverseDesignTgCandidate[];
};

export type ReverseDesignJobStatus =
  | "pending"
  | "running"
  | "found_enough"
  | "exhausted"
  | "failed"
  | "cancelled";

export type ReverseDesignTgJobCreateResponse = {
  job_id: string;
  status: ReverseDesignJobStatus;
};

export type ReverseDesignTgJobStatusResponse = {
  job_id: string;
  status: ReverseDesignJobStatus;
  target_tg: number;
  similarity_threshold: number;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  finished_at: string | null;
  scanned_rows: number;
  matched_count: number;
  current_tg_radius: number | null;
  best_similarity_score: number | null;
  message: string | null;
  error: string | null;
  result: ReverseDesignTgResponse | null;
};

export type TgAssistantStatusResponse = {
  enabled: boolean;
  configured: boolean;
  image: {
    supported: true;
    max_files: 2;
    max_canvas_snapshots: 1;
    max_user_upload_files: 1;
    max_bytes: number;
    max_total_bytes: number;
    accepted_mime_types: Array<"image/png" | "image/jpeg" | "image/webp">;
  };
};

export type TgAssistantGuideSection = {
  id: string;
  title: string;
  content: string[];
};

export type TgAssistantGuideResponse = {
  module: "reverseDesign";
  version: 3;
  language: "zh-CN";
  defaults: Record<string, number>;
  sections: TgAssistantGuideSection[];
};

export type TgAssistantCandidateContext = {
  rank: number;
  polymer_smiles: string | null;
  monomer_a_smiles: string | null;
  monomer_b_smiles: string | null;
  monomer_a_iupac: string | null;
  monomer_b_iupac: string | null;
  tg_value: number;
  tg_difference: number;
  similarity_score: number;
};

export type TgAssistantPageContext = {
  type: "tg_reverse_design";
  version: 1;
  captured_at: string;
  action_context_revision: string;
  structure: {
    smiles: string | null;
    canvas_dirty: boolean;
    editor_ready: boolean;
    view_mode: "2d" | "3d";
    busy: boolean;
  };
  draft_parameters: {
    target_tg: number | null;
    similarity_threshold: number | null;
    candidate_size: number | null;
  };
  submitted_request: {
    smiles: string;
    target_tg: number;
    similarity_threshold: number;
    candidate_size: number;
  } | null;
  parameters_dirty: boolean;
  validation_error: {
    field: "target_tg" | "similarity_threshold" | "candidate_size" | "structure";
    message: string;
  } | null;
  job: {
    status: ReverseDesignJobStatus;
    scanned_rows: number;
    matched_count: number;
    current_tg_radius: number | null;
    best_similarity_score: number | null;
    message: string | null;
  } | null;
  result_view: {
    total: number;
    page: number;
    page_size: 5;
    drawer_open: boolean;
    visible_candidates: TgAssistantCandidateContext[];
  } | null;
  error: string | null;
};

export type TgAssistantSetParametersOperation = {
  type: "set_parameters";
  parameters: Partial<{
    target_tg: number;
    similarity_threshold: number;
    candidate_size: number;
  }>;
};

export type TgAssistantRunSearchOperation = { type: "run_search" };
export type TgAssistantSetStructureOperation = { type: "set_structure"; smiles: string };
export type TgAssistantOperation =
  | TgAssistantSetParametersOperation
  | TgAssistantRunSearchOperation
  | TgAssistantSetStructureOperation;

export type TgAssistantStreamRequest = {
  messages: Array<{ role: "user" | "assistant"; content: string }>;
  page_context?: TgAssistantPageContext;
};

export type ConditionalGenerationTgRequest = {
  smiles: string;
  delta_tg: number;
  candidate_count: number;
  top_k: number;
  temperature: number;
};

export type ConditionalGenerationCandidate = {
  rank: number;
  generated_smiles: string;
  structure_svg: string | null;
  predicted_tg: number | null;
  tg_unit: "°C";
  tg_error: number | null;
  similarity_score: number | null;
  sa_score: number | null;
};

export type ConditionalGenerationTgResponse = {
  input_smiles: string;
  normalized_input_smiles: string;
  delta_tg: number;
  query_time_ms: number;
  requested_count: number;
  returned_count: number;
  attempts: number;
  filter_counter: Record<string, number>;
  results: ConditionalGenerationCandidate[];
};

export type ConditionalGenerationTgStatusResponse = {
  enabled: boolean;
  available: boolean;
  model_dir: string;
  missing_artifacts: string[];
  message: string;
};

export type ConditionalGenerationJobStatus = "pending" | "running" | "completed" | "failed" | "cancelled";

export type ConditionalGenerationJobCreateResponse = {
  job_id: string;
  status: ConditionalGenerationJobStatus;
};

export type ConditionalGenerationJobStatusResponse = {
  job_id: string;
  status: ConditionalGenerationJobStatus;
  delta_tg: number;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  finished_at: string | null;
  attempts: number;
  accepted_count: number;
  message: string | null;
  error: string | null;
  result: ConditionalGenerationTgResponse | null;
};

export const POLYTAO_DESCRIPTOR_NAMES = [
  "MolWt",
  "HeavyAtomCount",
  "NHOHCount",
  "NOCount",
  "NumAliphaticCarbocycles",
  "NumAliphaticHeterocycles",
  "NumAliphaticRings",
  "NumAromaticCarbocycles",
  "NumAromaticHeterocycles",
  "NumAromaticRings",
  "NumHAcceptors",
  "NumHDonors",
  "NumHeteroatoms",
  "NumRotatableBonds",
  "RingCount"
] as const;

export type PolytaoDescriptorName = (typeof POLYTAO_DESCRIPTOR_NAMES)[number];
export type PolytaoDescriptorMap = Record<PolytaoDescriptorName, number>;

export type PolytaoDescriptorRequest = {
  smiles: string;
};

export type PolytaoDescriptorValue = {
  name: PolytaoDescriptorName;
  value: number;
};

export type PolytaoDescriptorResponse = {
  input_smiles: string;
  canonical_smiles: string;
  descriptors: PolytaoDescriptorValue[];
  prompt: string;
  query_time_ms: number;
};

export type PolytaoGenerationRequest = {
  descriptors: PolytaoDescriptorMap;
  input_smiles?: string | null;
  candidate_count: number;
  temperature: number;
  top_k: number;
  top_p: number;
  max_length: number;
};

export type PolytaoCandidate = {
  rank: number;
  generated_smiles: string;
  raw_smiles: string;
  structure_svg: string | null;
  valid_smiles: boolean;
  sa_score: number | null;
  warnings: string[];
};

export type PolytaoGenerationResponse = {
  prompt: string;
  query_time_ms: number;
  requested_count: number;
  returned_count: number;
  attempts: number;
  filter_counter: Record<string, number>;
  results: PolytaoCandidate[];
};

export type PolytaoStatusResponse = {
  enabled: boolean;
  available: boolean;
  worker_base_url_configured: boolean;
  worker_status: string | null;
  worker_mode: string | null;
  db_configured: boolean | null;
  db_ready: boolean | null;
  db_error: string | null;
  runtime_ready: boolean | null;
  runtime_error: string | null;
  active_jobs: number | null;
  model_id: string | null;
  model_revision: string | null;
  default_params: Record<string, string | number | boolean | null>;
  worker_version: string | null;
  message: string;
};

export type PolytaoJobStatus = "pending" | "submitted" | "running" | "completed" | "failed" | "cancelled";

export type PolytaoJobCreateResponse = {
  job_id: string;
  status: PolytaoJobStatus;
};

export type PolytaoJobStatusResponse = {
  job_id: string;
  status: PolytaoJobStatus;
  input_smiles: string | null;
  canonical_smiles: string | null;
  prompt: string;
  requested_count: number;
  returned_count: number;
  attempts: number;
  progress_percent: number;
  progress_stage: string;
  progress_message: string;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  finished_at: string | null;
  worker_id: string | null;
  worker_job_id: string | null;
  worker_version: string | null;
  engine: string;
  error_message: string | null;
  result: PolytaoGenerationResponse | null;
};

export type StructurePropertyRecord = {
  property_id: number;
  polymer_id: number;
  smiles: string;
  canonical_smiles: string | null;
  property_category: string;
  property_name: string;
  property_value: string;
  property_value_num: number | null;
  property_unit: string | null;
  label_source: string | null;
};

export type StructurePropertyBrowseResponse = {
  query: string;
  page: number;
  page_size: number;
  query_time_ms: number;
  total_records: number;
  matched_records: number;
  data_source: string;
  source_status: string;
  source_message: string | null;
  results: StructurePropertyRecord[];
};

export type PropertyFilterType = "standardized" | "raw";

export type PropertyFilterHistogram = {
  domain_min: number;
  domain_max: number;
  domain_kind: "p5_p95" | "full_range";
  bin_count: number;
  counts: number[];
  underflow_count: number;
  overflow_count: number;
  total_count: number;
};

export type PropertyFilterOption = {
  filter_type: PropertyFilterType;
  option_key: string;
  label: string;
  property_key: string | null;
  property_name: string | null;
  property_unit_clean: string | null;
  canonical_unit: string | null;
  rows: number;
  unique_smiles: number;
  min_value: number | null;
  p5_value: number | null;
  median_value: number | null;
  p95_value: number | null;
  max_value: number | null;
  histogram?: PropertyFilterHistogram | null;
};

export type PropertyFilterOptionsResponse = {
  query_time_ms: number;
  total_records: number;
  mapped_records: number;
  raw_records: number;
  data_source: string;
  source_status: string;
  source_message: string | null;
  options: PropertyFilterOption[];
};

export type PropertyFilterHistogramResponse = {
  query_time_ms: number;
  option_key: string;
  data_source: string;
  source_status: string;
  source_message: string | null;
  histogram: PropertyFilterHistogram;
};

export type PropertyFilterCondition = {
  filter_type: PropertyFilterType;
  property_key?: string | null;
  canonical_unit?: string | null;
  property_name?: string | null;
  property_unit_clean?: string | null;
  min_value?: number | null;
  max_value?: number | null;
};

export type PropertyFilterSearchRequest = {
  filters: PropertyFilterCondition[];
  q?: string;
  page?: number;
  page_size?: number;
};

export type PropertyFilterRecord = {
  filter_record_id: number;
  source_row_number: number;
  polymer_name: string | null;
  smiles: string | null;
  canonical_smiles: string | null;
  property_category: string;
  property_name: string;
  property_value: string;
  property_value_num: number | null;
  property_unit_raw: string | null;
  property_unit_clean: string | null;
  property_key: string | null;
  property_label: string | null;
  canonical_value: number | null;
  canonical_unit: string | null;
  unit_conversion_status: string | null;
  value_origin: string | null;
  label_source: string | null;
  reliable_score: number | null;
  soft_quality_flags: string | null;
  duplicate_flag: string | null;
  filter_index: number;
};

export type PropertyFilterSearchResult = {
  smiles: string | null;
  canonical_smiles: string | null;
  polymer_name: string | null;
  matched_filters: number;
  records: PropertyFilterRecord[];
};

export type PropertyFilterSearchResponse = {
  query: string;
  page: number;
  page_size: number;
  query_time_ms: number;
  total_records: number;
  matched_records: number;
  data_source: string;
  source_status: string;
  source_message: string | null;
  results: PropertyFilterSearchResult[];
};

export type DftMoleculeBrowserRecord = {
  mol_id: string;
  range_group: string;
  final_step: number;
  n_atoms: number;
  trace_points: number;
  scf_energy: number | null;
  zero_point_energy: number | null;
  thermal_enthalpy: number | null;
  gibbs_free_energy: number | null;
  lowest_freq: number | null;
  dipole_moment: number | null;
  homo_ev: number | null;
  lumo_ev: number | null;
  gap_ev: number | null;
  is_converged: string | null;
};

export type DftMoleculeBrowseResponse = {
  query: string;
  page: number;
  page_size: number;
  query_time_ms: number;
  total_records: number;
  matched_records: number;
  total_step_records: number;
  average_steps: number;
  max_steps: number;
  data_source: string;
  source_status: string;
  source_message: string | null;
  results: DftMoleculeBrowserRecord[];
};

export type DftEnergyStepRecord = {
  mol_id: string;
  step: number;
  scf_energy: number | null;
  homo_ev: number | null;
  lumo_ev: number | null;
  gap_ev: number | null;
};

export type DftEnergyStepBrowseResponse = {
  query: string;
  page: number;
  page_size: number;
  query_time_ms: number;
  total_records: number;
  matched_records: number;
  data_source: string;
  source_status: string;
  source_message: string | null;
  results: DftEnergyStepRecord[];
};

export type ExperimentalProcessRecord = {
  source_file: string;
  source_row_number: number;
  polymer_id: string;
  polymer_name: string;
  product_name: string;
  process_flow_original_text: string;
  material_original_text: string;
};

export type ExperimentalProcessBrowseResponse = {
  query: string;
  page: number;
  page_size: number;
  query_time_ms: number;
  total_records: number;
  matched_records: number;
  data_source: string;
  source_status: string;
  source_message: string | null;
  results: ExperimentalProcessRecord[];
};

export type ExperimentalPropertyRecord = {
  source_file: string;
  source_row_number: number;
  polymer_id: string;
  polymer_name: string;
  property_category: string | null;
  property_name_en: string;
  value: string;
};

export type ExperimentalPropertyBrowseResponse = {
  query: string;
  page: number;
  page_size: number;
  query_time_ms: number;
  total_records: number;
  matched_records: number;
  data_source: string;
  source_status: string;
  source_message: string | null;
  results: ExperimentalPropertyRecord[];
};

export type FormulationRecord = {
  formulation_id: number;
  knowledge_id: number;
  source_file: string;
  source_row_number: number;
  polymer_iupac: string | null;
  formulation: string | null;
  catalyst: string | null;
  temperature: string | null;
  reaction_time: string | null;
  solvent: string | null;
};

export type FormulationBrowseResponse = {
  query: string;
  page: number;
  page_size: number;
  query_time_ms: number;
  total_records: number;
  matched_records: number;
  data_source: string;
  source_status: string;
  source_message: string | null;
  results: FormulationRecord[];
};

export type DatasetSummaryItem = {
  key: string;
  title: string;
  total_records: number;
  data_source: string;
  source_status: string;
  source_message: string | null;
  latest_import_status: string | null;
  latest_import_finished_at: string | null;
};

export type DatasetSummaryResponse = {
  query_time_ms: number;
  backend: string;
  datasets: DatasetSummaryItem[];
};

export type DatabaseAnalyticsResponse = {
  query_time_ms: number;
  backend: string;
  source: string;
  refresh_status?: "unchanged" | "recomputed" | null;
  generated_at: string | null;
  datasets: Record<string, unknown>;
};

export type DftPcaPoint = {
  mol_id: string;
  x: number;
  y: number;
  z: number;
  n_atoms: number;
  final_step: number;
  homo_ev: number | null;
  lumo_ev: number | null;
  gap_ev: number | null;
  dipole_moment: number | null;
};

export type DftPcaSampleResponse = {
  query_time_ms: number;
  total: number;
  results: DftPcaPoint[];
};

export type DftEnergyPoint = {
  step: number;
  scf_energy: number | null;
  homo_ev: number | null;
  lumo_ev: number | null;
  gap_ev: number | null;
};

export type DftMoleculeDetail = {
  mol_id: string;
  range_group: string;
  final_step: number;
  n_atoms: number;
  coordinates: [number, number, number, number][];
  scf_energy: number | null;
  zero_point_energy: number | null;
  thermal_enthalpy: number | null;
  gibbs_free_energy: number | null;
  lowest_freq: number | null;
  dipole_moment: number | null;
  homo_ev: number | null;
  lumo_ev: number | null;
  gap_ev: number | null;
  is_converged: string | null;
  trace: DftEnergyPoint[];
};

export type DevGpuSessionPhase =
  | "stopped"
  | "recovering"
  | "queued"
  | "starting"
  | "ready"
  | "failed"
  | "unavailable";

export type DevGpuSessionStatusResponse = {
  schema_version: number;
  operator_available: boolean;
  phase: DevGpuSessionPhase;
  controller_status: string;
  can_recover: boolean;
  operation_id: string | null;
  message: string;
  source_sha: string | null;
  source_tree: string | null;
  updated_at: string | null;
};
