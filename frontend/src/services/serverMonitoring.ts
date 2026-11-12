import { privateFetch } from "../auth/session";
import type { MonitoringHistory, MonitoringSnapshot } from "../types/serverMonitoring";

const BASE = `${import.meta.env.VITE_API_BASE_URL ?? "/api/v1"}/server-monitoring`;

export class MonitoringRequestError extends Error {
  constructor(public status: number, public code: string) {
    super(code === "authentication_unavailable" ? "暂时无法确认访问权限，请重试。"
      : status === 403 ? "当前账号没有服务器监测权限。"
      : "暂时无法读取服务器监测数据。");
    this.name = "MonitoringRequestError";
  }
}
export function isMonitoringAccessError(error: unknown): boolean {
  return error instanceof MonitoringRequestError &&
    ([401, 403, 409].includes(error.status) || error.code === "authentication_unavailable");
}

async function read<T>(path: string, signal: AbortSignal): Promise<T> {
  const controller = new AbortController();
  const abort = () => controller.abort();
  signal.addEventListener("abort", abort, { once: true });
  if (signal.aborted) controller.abort();
  let timedOut = false;
  const timer = setTimeout(() => { timedOut = true; controller.abort(); }, 10_000);
  try {
    const response = await privateFetch(`${BASE}/${path}`, { method: "GET", signal: controller.signal });
    const value = await response.json().catch(error => {
      if (controller.signal.aborted || error?.name === "AbortError") throw error;
      return null;
    });
    if (!response.ok) throw new MonitoringRequestError(response.status, typeof value?.code === "string" ? value.code : "monitoring_unavailable");
    if (!value || typeof value !== "object") throw new MonitoringRequestError(502, "invalid_response");
    return value as T;
  } catch (error) {
    if (timedOut && !signal.aborted) throw new MonitoringRequestError(0, "monitoring_timeout");
    // privateFetch may already turn an entitlement response into a typed error.
    // All 401/403/409 responses must still close the monitoring view.
    if (!(error instanceof MonitoringRequestError) && error && typeof error === "object" && "status" in error && typeof error.status === "number") {
      throw new MonitoringRequestError(error.status, "code" in error && typeof error.code === "string" ? error.code : "monitoring_unavailable");
    }
    throw error;
  } finally {
    clearTimeout(timer);
    signal.removeEventListener("abort", abort);
  }
}

export { read as readMonitoringJson };

export async function fetchMonitoringAccess(signal: AbortSignal): Promise<boolean> {
  const value = await read<{ can_read?: boolean }>("access", signal);
  return value.can_read === true;
}
export async function fetchMonitoringSnapshot(signal: AbortSignal): Promise<MonitoringSnapshot> {
  const value = await read<MonitoringSnapshot>("snapshot", signal);
  if (!validEnvelope(value) || typeof value.hostname !== "string" || !Array.isArray(value.gpus) || !Array.isArray(value.disks) || !value.components) {
    throw new MonitoringRequestError(502, "invalid_response");
  }
  return value;
}
export async function fetchMonitoringHistory(signal: AbortSignal): Promise<MonitoringHistory> {
  const value = await read<MonitoringHistory>("history", signal);
  if (!validEnvelope(value) || !Array.isArray(value.points) || !Number.isFinite(value.interval_seconds) || value.interval_seconds <= 0) {
    throw new MonitoringRequestError(502, "invalid_response");
  }
  return value;
}
function validEnvelope(value: MonitoringSnapshot | MonitoringHistory) {
  return value.schema_version === 1 && typeof value.node_id === "string" && typeof value.collector_id === "string"
    && Number.isFinite(Date.parse(value.started_at)) && Number.isFinite(Date.parse(value.captured_at))
    && Number.isFinite(value.age_seconds) && value.age_seconds >= 0 && ["fresh", "stale", "unavailable"].includes(value.freshness);
}
