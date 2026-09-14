import type { MonomerPolymerizationTargetClass } from "./index";

export type BatchCapability = {
  enabled: boolean;
  available: boolean;
  message: string;
  formats: string[];
  limits: { file_bytes: number; request_bytes: number; max_rows: number; max_pairs: number; retention_days: number };
};
export type BatchMapping = {
  sheet: string | null;
  encoding: "utf-8-sig" | "gb18030";
  smiles_column: string | null;
  id_column: string | null;
  name_column: string | null;
};
export type BatchTablePreview = {
  headers: string[];
  sheets: string[];
  sheet: string | null;
  row_count: number;
  blank_rows: number;
  sample: Record<string, string>[];
  mapping: BatchMapping;
  error?: string | null;
};
export type BatchImport = {
  import_id: string;
  expires_at: string;
  files: Record<"a" | "b", { filename: string; format: string; size_bytes: number; sha256: string }>;
  tables: Record<"a" | "b", BatchTablePreview>;
};
export type BatchStatistics = {
  raw_pairs: number;
  valid_pairs: number;
  unique_pairs: number;
  tables: Record<"a" | "b", { row_count: number; valid_rows: number; unique_count: number; duplicate_rows: number; invalid_rows: number; blank_rows: number }>;
};
export type BatchImportPreview = BatchImport & {
  preview_revision: string | null;
  can_submit: boolean;
  statistics: BatchStatistics | null;
  input_errors: Array<{ role: string; row_number: number; id: string; input_smiles: string; error_code: string; message: string }>;
  input_error_count: number;
};
export type BatchPairStatus = "success" | "no_match" | "invalid_input" | "error" | "not_processed";
export type BatchArtifact = { name: string; url: string; size_bytes: number; sha256: string; media_type: string };
export type BatchJob = {
  job_id: string;
  status: "queued" | "running" | "cancelling" | "completed" | "completed_with_errors" | "failed" | "cancelled" | "expired";
  stage: string;
  target_class: MonomerPolymerizationTargetClass;
  summary: BatchStatistics & {
    processed_pairs: number;
    computed_unique_pairs: number;
    candidate_count: number;
    pair_errors?: number;
    classification_errors?: number;
    classified_unique_monomers?: number;
    pair_statuses?: Partial<Record<BatchPairStatus, number>>;
    complete?: boolean;
  };
  artifacts: Record<string, BatchArtifact>;
  created_at: string;
  updated_at: string;
  finished_at: string | null;
  expires_at: string | null;
  error_code: string | null;
  message: string | null;
};
export type BatchCandidate = {
  pair_id: string;
  a_row: number;
  b_row: number;
  a_id: string;
  b_id: string;
  a_name: string;
  b_name: string;
  a_input_smiles: string;
  b_input_smiles: string;
  a_canonical_smiles: string;
  b_canonical_smiles: string;
  candidate_index: number;
  polymer_smiles: string;
  polymer_class: string;
  reaction_id: number | null;
  engine_mon1_smiles: string;
  engine_mon2_smiles: string;
  reactset: string[];
};
export type BatchResults = { items: BatchCandidate[]; total: number; next_offset: number | null };
