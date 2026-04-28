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
};

export type KnowledgeSearchResponse = {
  query: string;
  query_time_ms: number;
  total: number;
  results: KnowledgeDocumentResult[];
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
