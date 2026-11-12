import type { MonitoringFreshness } from "./serverMonitoring";

export type GpuTaskSource = {
  source_id: "development_dft" | "production_dft" | "development_md" | "production_md" | "development_polymer_md" | "production_polymer_md";
  status: "ready" | "partial" | "unavailable";
  matched_tasks: number;
  excluded_tasks: number;
  error_code: string | null;
};
export type GpuHistoricalTask = {
  id: string;
  gpu_uuid: string;
  module: "monomer_dft" | "monomer_md" | "polymer_md";
  environment: "development" | "production";
  calculation_type: "single_point" | "optimization" | "md_formal" | "md_demo" | "polymer_md";
  status: "completed";
  started_at: string;
  finished_at: string;
  duration_seconds: number;
};
export type GpuActivityItem = {
  id: string;
  gpu_uuid: string;
  kind: "task" | "service" | "process";
  module: "monomer_dft" | "monomer_md" | "polymer_md" | "platform" | "nvidia_mps" | "other";
  environment: "development" | "production" | "unknown";
  label: string;
  status: "running" | "resident" | "observed";
  memory_used_bytes: number | null;
  started_at: string | null;
};
export type GpuTaskHistoryResponse = {
  schema_version: 1;
  node_id: "local-server";
  collector_id: string;
  started_at: string;
  captured_at: string;
  history_captured_at: string;
  age_seconds: number;
  freshness: MonitoringFreshness;
  coverage: "retained_verified_gpu_records";
  status: "ready" | "partial" | "unavailable";
  gpu_uuids: string[];
  sources: GpuTaskSource[];
  tasks: GpuHistoricalTask[];
  activity: {
    captured_at: string;
    status: "ready" | "partial" | "unavailable";
    error_code: string | null;
    items: GpuActivityItem[];
  };
};
