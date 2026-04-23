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
  "O2 Permeability Barrer": { label: "O₂ 渗透性", unit: "Barrer" },
  "Co2 Permeability Barrer": { label: "CO₂ 渗透性", unit: "Barrer" },
  "H2 Permeability Barrer": { label: "H₂ 渗透性", unit: "Barrer" }
};
