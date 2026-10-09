export const USAGE_STATUSES = ["queued", "running", "completed", "failed", "cancelled", "timed_out", "interrupted", "rejected"] as const;
export type UsageStatus = typeof USAGE_STATUSES[number];
export type UsageMetric = { value: number | null; known_count: number; unknown_count: number };
export type UsageTotals = {
  tasks: number; attempts: number; child_calls: number;
  states: Partial<Record<UsageStatus, number>>;
  metrics: Record<string, UsageMetric>;
};
export type UsageCatalog = {
  enabled: boolean; mode: "statistics_only"; retention_days: number;
  modules: Array<{ id: string; label: string; category: string; coverage: "measured" | "partial" | "external"; note?: string }>;
  metrics: Array<{ id: string; label: string; unit: string }>;
};
export type UsagePeriod = { start: string; end: string; module?: string };
export type UsageSummary = {
  period: { start: string; end: string; timezone: "UTC"; basis: "task_submitted_at" };
  totals: UsageTotals;
  modules: Array<UsageTotals & { module: string; label: string }>;
  daily: Array<{ date: string; tasks: number; states: Partial<Record<UsageStatus, number>> }>;
  coverage: { uncovered: string[]; warnings: string[] };
};
export type UsageRecord = {
  module: string; task_id: string; attempt_id: string | null; kind: string; status: UsageStatus;
  submitted_at: string; started_at: string | null; finished_at: string | null;
  parent_task_id: string | null; root_task_id: string | null; source: string;
  metrics: Record<string, number | null>;
};
export type UsageRecords = { items: UsageRecord[]; page: number; page_size: number; total: number };
export type UsageRecordsQuery = UsagePeriod & {
  status?: UsageStatus; page: number; page_size: number; include_children: boolean;
};
