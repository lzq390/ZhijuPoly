import type { GpuActivityItem, GpuHistoricalTask, GpuTaskHistoryResponse, GpuTaskSource } from "../types/gpuTaskHistory";

export const gpuHistoricalTask = (changes: Partial<GpuHistoricalTask> = {}): GpuHistoricalTask => ({
  id: "0123456789abcdef0123", gpu_uuid: "gpu-first", module: "monomer_dft", environment: "development",
  calculation_type: "single_point", status: "completed", started_at: "2026-11-11T00:58:00Z", finished_at: "2026-11-11T00:59:00Z", duration_seconds: 60,
  ...changes
});
export const gpuActivityItem = (changes: Partial<GpuActivityItem> = {}): GpuActivityItem => ({
  id: "aaaaaaaaaaaaaaaaaaaa", gpu_uuid: "gpu-first", kind: "service", module: "monomer_dft", environment: "development",
  label: "DFT 工作进程", status: "resident", memory_used_bytes: 2 ** 30, started_at: "2026-11-11T00:00:00Z", ...changes
});
const sourceIds: GpuTaskSource["source_id"][] = ["development_dft", "production_dft", "development_md", "production_md", "development_polymer_md", "production_polymer_md"];
export const gpuTaskHistory = (changes: Partial<GpuTaskHistoryResponse> = {}): GpuTaskHistoryResponse => ({
  schema_version: 1, node_id: "local-server", collector_id: "task-collector-1", started_at: "2026-11-11T00:00:00Z", captured_at: "2026-11-11T01:00:00Z",
  history_captured_at: "2026-11-11T01:00:00Z", age_seconds: 0, freshness: "fresh", coverage: "retained_verified_gpu_records", status: "ready", gpu_uuids: ["gpu-first", "gpu-second"],
  sources: sourceIds.map(source_id => ({ source_id, status: "ready", matched_tasks: source_id === "development_dft" ? 1 : 0, excluded_tasks: 0, error_code: null })),
  tasks: [gpuHistoricalTask()], activity: { captured_at: "2026-11-11T01:00:00Z", status: "ready", error_code: null, items: [] }, ...changes
});
