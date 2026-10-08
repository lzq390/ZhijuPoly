import { privateFetch } from "../auth/session";
import type { UsageCatalog, UsagePeriod, UsageRecords, UsageRecordsQuery, UsageSummary } from "../types/usage";

const BASE = `${import.meta.env.VITE_API_BASE_URL ?? "/api/v1"}/usage`;
export class UsageRequestError extends Error {
  constructor(readonly status: number, readonly code: string | undefined, message: string) {
    super(message); this.name = "UsageRequestError";
  }
}
async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await privateFetch(`${BASE}${path}`, { signal });
  if (!response.ok) {
    const error = await response.json().catch(() => null);
    const code = error?.code ?? error?.detail?.code;
    throw new UsageRequestError(response.status, typeof code === "string" ? code : undefined,
      typeof error?.detail === "string" ? error.detail : "暂时无法读取用量，请重试。");
  }
  return await response.json() as T;
}
function periodParams(period: UsagePeriod) {
  // Deliberate allowlist: caller-supplied owner/user fields never reach the API.
  const query = new URLSearchParams({ start: period.start, end: period.end });
  if (period.module) query.set("module", period.module);
  return query;
}
export const fetchUsageCatalog = (signal?: AbortSignal) => get<UsageCatalog>("/catalog", signal);
export const fetchUsageSummary = (period: UsagePeriod, signal?: AbortSignal) =>
  get<UsageSummary>(`/summary?${periodParams(period)}`, signal);
export function fetchUsageRecords(filter: UsageRecordsQuery, signal?: AbortSignal) {
  const query = periodParams(filter);
  if (filter.status) query.set("status", filter.status);
  query.set("page", String(filter.page)); query.set("page_size", String(filter.page_size));
  query.set("include_children", String(filter.include_children));
  return get<UsageRecords>(`/records?${query}`, signal);
}
