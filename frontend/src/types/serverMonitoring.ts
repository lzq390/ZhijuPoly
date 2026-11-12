export type MonitoringFreshness = "fresh" | "stale" | "unavailable";
export type MonitoringComponent = {
  status: "ok" | "warming" | "unavailable";
  last_success_at: string | null;
  error_code: string | null;
};
export type MonitoringGpu = {
  index: number;
  uuid: string;
  name: string;
  utilization_percent: number | null;
  memory_total_bytes: number | null;
  memory_used_bytes: number | null;
  temperature_celsius: number | null;
  power_watts: number | null;
};
export type MonitoringDisk = {
  mount_point: string;
  label: string;
  total_bytes: number | null;
  used_bytes: number | null;
  available_bytes: number | null;
  used_percent: number | null;
  status: "ok" | "unavailable";
};
export type MonitoringHostInfo = {
  cpu_model: string | null;
  cpu_socket_count: number | null;
  cpu_physical_core_count: number | null;
  os_name: string | null;
  kernel_release: string | null;
  nvidia_driver_version: string | null;
};
type MonitoringEnvelope = {
  schema_version: number;
  node_id: string;
  collector_id: string;
  started_at: string;
  captured_at: string;
  age_seconds: number;
  freshness: MonitoringFreshness;
};
export type MonitoringSnapshot = MonitoringEnvelope & {
  hostname: string;
  host_info?: MonitoringHostInfo | null;
  sequence: number;
  logical_cpu_count: number;
  uptime_seconds: number | null;
  load_average: [number, number, number] | null;
  cpu: { utilization_percent: number | null } | null;
  memory: { total_bytes: number; available_bytes: number; used_bytes: number; used_percent: number } | null;
  disks: MonitoringDisk[];
  gpus: MonitoringGpu[];
  components: Record<"cpu" | "memory" | "disks" | "gpus", MonitoringComponent>;
};
export type MonitoringHistoryPoint = {
  timestamp: string;
  cpu_percent: number | null;
  memory_percent: number | null;
  gpus: Array<{ uuid: string; utilization_percent: number | null; memory_used_bytes: number | null }>;
};
export type MonitoringHistory = MonitoringEnvelope & {
  interval_seconds: number;
  points: MonitoringHistoryPoint[];
};
