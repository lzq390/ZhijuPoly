import { privateFetch } from "../auth/session";
import { confirmUsageOperation } from "../auth/usageAdmission";
import { UsageRequestError } from "./usage";
import type { Entitlements, UsageOperation, UsageOperationPage, UsageOperationResult } from "../types/entitlements";
const BASE = `${import.meta.env.VITE_API_BASE_URL ?? "/api/v1"}/usage`;
async function read<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await privateFetch(`${BASE}${path}`, { signal });
  if (!response.ok) {
    const value = await response.json().catch(() => null);
    const code = value?.detail?.code ?? value?.code;
    const message = code === "usage_result_unavailable" ? "操作回执仍保留，但已有结果不可恢复。此操作不会重新执行。"
      : code === "usage_operation_not_found" ? "未找到属于当前账号的操作。"
      : response.status === 404 ? "当前服务尚未提供操作回执查询。"
      : "暂时无法读取次数或操作回执，请重试读取。";
    throw new UsageRequestError(response.status, code, message);
  }
  return await response.json() as T;
}
export const fetchEntitlements = (signal?: AbortSignal) => read<Entitlements>("/entitlements", signal);
export const fetchUsageOperations = (offset = 0, module?: string, signal?: AbortSignal) => {
  const query = new URLSearchParams({ offset: String(offset), limit: "25" });
  if (module) query.set("module", module);
  return read<UsageOperationPage>(`/operations?${query}`, signal);
};
function confirm(operation: UsageOperation) {
  if (["succeeded", "released"].includes(operation.state)) confirmUsageOperation(operation.operation_id);
}
export async function fetchUsageOperation(id: string, signal?: AbortSignal) {
  const operation = await read<UsageOperation>(`/operations/${encodeURIComponent(id)}`, signal);
  confirm(operation); return operation;
}
export async function fetchUsageOperationResult(id: string, signal?: AbortSignal) {
  const result = await read<UsageOperationResult>(`/operations/${encodeURIComponent(id)}/result`, signal);
  confirm(result.operation); return result;
}
