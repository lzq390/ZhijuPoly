import type { MonitoringHistory, MonitoringHostInfo, MonitoringSnapshot } from "../types/serverMonitoring";

export const monitoringHostInfo = (changes: Partial<MonitoringHostInfo> = {}): MonitoringHostInfo => ({
  cpu_model: "Test CPU 32-Core", cpu_socket_count: 2, cpu_physical_core_count: 32,
  os_name: "Test Linux 24.04 LTS", kernel_release: "6.8.0-test", nvidia_driver_version: "550.54.14", ...changes
});

export const monitoringSnapshot = (changes: Partial<MonitoringSnapshot> = {}): MonitoringSnapshot => ({
  schema_version: 1, node_id: "local-host", hostname: "compute-test", collector_id: "collector-1",
  started_at: "2026-11-11T00:00:00Z", captured_at: "2026-11-11T01:00:00Z", sequence: 1,
  logical_cpu_count: 64, uptime_seconds: 86410, load_average: [1.2, 2.3, 3.4],
  cpu: { utilization_percent: 0 }, memory: { total_bytes: 128 * 2 ** 30, available_bytes: 64 * 2 ** 30, used_bytes: 64 * 2 ** 30, used_percent: 50 },
  disks: [{ mount_point: "/", label: "系统盘", total_bytes: 2 ** 40, used_bytes: 2 ** 39, available_bytes: 2 ** 39, used_percent: 50, status: "ok" }],
  gpus: [{ index: 0, uuid: "gpu-test", name: "Test GPU", utilization_percent: 0, memory_total_bytes: 24 * 2 ** 30, memory_used_bytes: 0, temperature_celsius: 31, power_watts: 50 }],
  components: Object.fromEntries(["cpu", "memory", "disks", "gpus"].map(key => [key, { status: "ok", last_success_at: "2026-11-11T01:00:00Z", error_code: null }])) as MonitoringSnapshot["components"],
  age_seconds: 0, freshness: "fresh", ...changes
});
export const monitoringHistory = (changes: Partial<MonitoringHistory> = {}): MonitoringHistory => ({
  schema_version: 1, node_id: "local-host", collector_id: "collector-1", started_at: "2026-11-11T00:00:00Z", captured_at: "2026-11-11T01:00:00Z",
  interval_seconds: 30, age_seconds: 0, freshness: "fresh",
  points: [0, 1, 2].map(index => ({ timestamp: new Date(Date.parse("2026-11-11T00:59:00Z") + index * 30000).toISOString(), cpu_percent: index * 10, memory_percent: 50, gpus: [{ uuid: "gpu-test", utilization_percent: index * 5, memory_used_bytes: 0 }] })),
  ...changes
});
