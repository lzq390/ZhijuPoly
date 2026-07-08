import type { RefObject } from "react";

export type MatchMode = "structure" | "property";
export type WorkspaceMode = "query" | "predict";
export type ResultsTab = "query" | "predict";
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

export type MonomerMdJobStatus = "pending" | "submitted" | "running" | "completed" | "failed" | "cancelled";
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

export type KnowledgeSearchRequest = {
  query: string;
  top_k: number;
  page?: number;
  page_size?: number;
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
  terms: string[];
  page: number;
  page_size: number;
  query_time_ms: number;
  total: number;
  results: KnowledgeDocumentResult[];
};

export type KnowledgeNavigationRequest = {
  query: string;
  terms?: string[];
};
export type AssistantChatRole = "user" | "assistant";
export type AssistantImageMode = "analysis" | "structure";

export type AssistantMessageAttachment = {
  type: "image";
  name: string;
  previewUrl: string;
  mode: AssistantImageMode;
  sizeBytes: number;
};

export type AssistantChatMessage = {
  role: AssistantChatRole;
  content: string;
  attachments?: AssistantMessageAttachment[];
};

export type AssistantModuleContext = {
  id: string;
  title: string;
  route: string;
  group: string;
  description: string;
};

export type AssistantChatContext = {
  active_module?: string | null;
  modules: AssistantModuleContext[];
};

export type AssistantChatStreamRequest = {
  messages: AssistantChatMessage[];
  context: AssistantChatContext;
};

export type AssistantSkillStatus = "running" | "completed" | "error";

export type AssistantPredictionSkillProperty = {
  name: PredictableProperty;
  label_zh: string;
  unit: string;
  value: number;
};

export type AssistantPredictionSkillResult = {
  type: "predict_polymer_properties";
  smiles: string;
  predictions: Partial<Record<PredictableProperty, number>>;
  properties: AssistantPredictionSkillProperty[];
  query_time_ms: number;
};

export type AssistantUnknownSkillResult = {
  type: string;
  [key: string]: unknown;
};

export type AssistantSkillResult = AssistantPredictionSkillResult | AssistantUnknownSkillResult;

export type AssistantSkillCall = {
  skill_call_id: string;
  skill_name: string;
  display_name?: string;
  arguments?: Record<string, unknown>;
  status: AssistantSkillStatus;
  result?: AssistantSkillResult;
  error?: string;
};

export type AssistantSkillStartEvent = {
  skill_call_id: string;
  skill_name: string;
  display_name?: string;
  arguments?: Record<string, unknown>;
};

export type AssistantSkillResultEvent = {
  skill_call_id: string;
  skill_name: string;
  display_name?: string;
  result: AssistantSkillResult;
};

export type AssistantSkillErrorEvent = {
  skill_call_id?: string;
  skill_name: string;
  detail: string;
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

export const PREDICTABLE_PROPERTIES: readonly PredictableProperty[] = [
  "Glass transition temperature",
  "Melting temperature",
  "Thermal decomposition temperature",
  "Thermal decomposition weight loss",
  "Elongation at break",
  "Tensile stress strength at break",
  "O2 Permeability Barrer",
  "Co2 Permeability Barrer",
  "H2 Permeability Barrer"
] as const;

export const PREDICT_PROPERTY_META: Record<
  PredictableProperty,
  { label: string; unit: string }
> = {
  "Glass transition temperature": { label: "玻璃化转变温度", unit: "°C" },
  "Melting temperature": { label: "熔融温度", unit: "°C" },
  "Thermal decomposition temperature": { label: "热分解温度", unit: "°C" },
  "Thermal decomposition weight loss": { label: "热分解失重率", unit: "%" },
  "Elongation at break": { label: "断裂伸长率", unit: "%" },
  "Tensile stress strength at break": { label: "断裂拉伸强度", unit: "MPa" },
  "O2 Permeability Barrer": { label: "O2 渗透率", unit: "Barrer" },
  "Co2 Permeability Barrer": { label: "CO2 渗透率", unit: "Barrer" },
  "H2 Permeability Barrer": { label: "H2 渗透率", unit: "Barrer" }
};
