export type MatchMode = "exact" | "similarity";

export type SmilesQueryRequest = {
  smiles: string;
  match_mode: MatchMode;
  similarity_threshold: number;
  top_k: number;
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
  properties: PropertyGroups;
};

export type SmilesQueryResponse = {
  match_type: MatchMode;
  query_time_ms: number;
  total: number;
  results: PolymerResult[];
};
