export type MatchMode = "structure" | "property";
export type WorkspaceMode = "query" | "predict";
export type ResultsTab = "query" | "predict";

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
  created_at: string;
  updated_at: string;
  error_message: string | null;
  result: OnlineKnowledgeSearchResponse | null;
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
  "Glass transition temperature": { label: "Glass transition temperature", unit: "°C" },
  "Melting temperature": { label: "Melting temperature", unit: "°C" },
  "Thermal decomposition temperature": { label: "Thermal decomposition temperature", unit: "°C" },
  "Thermal decomposition weight loss": { label: "Thermal decomposition weight loss", unit: "%" },
  "Elongation at break": { label: "Elongation at break", unit: "%" },
  "Tensile stress strength at break": { label: "Tensile stress strength at break", unit: "MPa" },
  "O2 Permeability Barrer": { label: "O2 permeability", unit: "Barrer" },
  "Co2 Permeability Barrer": { label: "CO2 permeability", unit: "Barrer" },
  "H2 Permeability Barrer": { label: "H2 permeability", unit: "Barrer" }
};
