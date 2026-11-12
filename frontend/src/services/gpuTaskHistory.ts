import { MonitoringRequestError, readMonitoringJson } from "./serverMonitoring";
import type { GpuTaskHistoryResponse } from "../types/gpuTaskHistory";

const record = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === "object" && !Array.isArray(value);
const date = (value: unknown): value is string => typeof value === "string" && Number.isFinite(Date.parse(value));
const count = (value: unknown) => typeof value === "number" && Number.isInteger(value) && value >= 0;
const status = (value: unknown) => value === "ready" || value === "partial" || value === "unavailable";
const errorCode = (value: unknown) => value === null || typeof value === "string";
const identifier = (value: unknown) => typeof value === "string" && /^[a-f0-9]{20}$/i.test(value);
const sourceIds = ["development_dft", "production_dft", "development_md", "production_md", "development_polymer_md", "production_polymer_md"];
const taskTypes: Record<string, string[]> = { monomer_dft: ["single_point", "optimization"], monomer_md: ["md_formal", "md_demo"], polymer_md: ["polymer_md"] };
const activityStatuses: Record<string, string> = { task: "running", service: "resident", process: "observed" };

export async function fetchGpuTaskHistory(signal: AbortSignal): Promise<GpuTaskHistoryResponse> {
  const value = await readMonitoringJson<unknown>("gpu-tasks", signal);
  if (!record(value) || value.schema_version !== 1 || value.node_id !== "local-server" || typeof value.collector_id !== "string" || !value.collector_id
    || !date(value.started_at) || !date(value.captured_at) || !date(value.history_captured_at)
    || typeof value.age_seconds !== "number" || !Number.isFinite(value.age_seconds) || value.age_seconds < 0
    || !["fresh", "stale", "unavailable"].includes(String(value.freshness)) || value.coverage !== "retained_verified_gpu_records" || !status(value.status)
    || !Array.isArray(value.gpu_uuids) || !value.gpu_uuids.every(uuid => typeof uuid === "string" && uuid.length > 0)
    || !Array.isArray(value.sources) || !value.sources.every(source => record(source) && sourceIds.includes(String(source.source_id))
      && status(source.status) && count(source.matched_tasks) && count(source.excluded_tasks) && errorCode(source.error_code))
    || !Array.isArray(value.tasks) || !value.tasks.every(task => record(task) && identifier(task.id)
      && typeof task.gpu_uuid === "string" && (value.gpu_uuids as string[]).includes(task.gpu_uuid)
      && ["development", "production"].includes(String(task.environment)) && Object.hasOwn(taskTypes, String(task.module)) && taskTypes[String(task.module)].includes(String(task.calculation_type))
      && task.status === "completed" && date(task.started_at) && date(task.finished_at) && Date.parse(task.finished_at) >= Date.parse(task.started_at)
      && typeof task.duration_seconds === "number" && Number.isFinite(task.duration_seconds) && task.duration_seconds >= 0)
    || !record(value.activity) || !date(value.activity.captured_at) || !status(value.activity.status) || !errorCode(value.activity.error_code)
    || !Array.isArray(value.activity.items) || !value.activity.items.every(item => record(item) && identifier(item.id)
      && typeof item.gpu_uuid === "string" && (value.gpu_uuids as string[]).includes(item.gpu_uuid)
      && ["monomer_dft", "monomer_md", "polymer_md", "platform", "nvidia_mps", "other"].includes(String(item.module))
      && ["development", "production", "unknown"].includes(String(item.environment))
      && Object.hasOwn(activityStatuses, String(item.kind)) && activityStatuses[String(item.kind)] === item.status
      && typeof item.label === "string" && item.label.trim().length > 0 && item.label.length <= 160 && !/[\x00-\x1f\x7f]/.test(item.label)
      && (item.memory_used_bytes === null || count(item.memory_used_bytes)) && (item.started_at === null || date(item.started_at)))) {
    throw new MonitoringRequestError(502, "invalid_response");
  }
  return value as GpuTaskHistoryResponse;
}
