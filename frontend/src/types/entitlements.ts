export type OperationState = "reserved" | "dispatching" | "running" | "unknown" | "succeeded" | "released";
export type HeldPeriod = { pool_id: string; period_start: string; period_end: string; states: Partial<Record<OperationState,number>>; reserved: number; unknown: number };
export type SpecificationField = { type: "integer" | "number" | "enum" | "enum_list"; min?: number; max?: number; values?: string[]; min_items?: number; max_items?: number; exclusive_min?: boolean };
export type AllowancePolicy = {
  version: string; sha256: string; example: boolean; period: "day" | "week" | "month";
  request_rate_per_minute: number; active_operations_per_owner: number;
  pool_labels: Record<string, string>;
  tiers: Record<"free" | "member", { pools: Record<string, number | null> }>;
  rules: Record<string, boolean>;
  modules: Record<string, { pool_id: string | null; selector: string | null; variants: Record<string, { mode: "count" | "rate_only" | "external"; pool_id: string | null; fields: Record<string, SpecificationField> }> }>;
};
export type Entitlements = {
  enabled: boolean; mode: "example" | "configured" | "not_enabled"; policy: AllowancePolicy | null;
  tier?: "free" | "member"; policy_version?: string; period_start?: string; period_end?: string;
  period?: "day" | "week" | "month"; active_across_periods?: number; unknown_across_periods?: number;
  held_outside_current_period?: number; held_periods?: HeldPeriod[]; admission_blocked_by_period?: boolean; conflicting_periods?: HeldPeriod[];
  period_basis?: "admission_reservation"; diagnostics_only?: boolean; resource_measurements_are_diagnostic?: boolean; message?: string;
  pools: Array<{ pool_id: string; limit: number | null; success_used: number; reserved: number; unknown: number; remaining: number | null; states: Partial<Record<OperationState, number>>; held_outside_current_period?: number; admission_blocked_by_period?: boolean }>;
  operation_count?: number; execution_attempts?: number; request_observation_degraded?: boolean;
  request_observations?: { enabled: boolean; request_count: number; finished_count: number; by_status: Record<string, number>; start: string; end: string };
};
export type UsageOperation = {
  operation_id: string; module: string; native_task_id: string | null; policy_version: string;
  pool_id: string | null; spec_id: string; spec: Record<string, unknown>;
  period_start: string; period_end: string; generation: number; state: OperationState;
  outcome: string | null; qualified: boolean | null; created_at: string; updated_at: string; settled_at: string | null;
};
export type UsageOperationPage = { items: UsageOperation[]; offset: number; limit: number; has_more: boolean };
export type UsageOperationResult = { operation: UsageOperation; result: unknown; result_ref: unknown };
