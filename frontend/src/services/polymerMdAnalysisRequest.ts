export const POLYMER_MD_LOG_TIMEOUT_MS = 10_000;
export const POLYMER_MD_ANALYSIS_TIMEOUT_MS = 100_000;

export class PolymerMdAnalysisTimeout extends Error {
  constructor() { super("分析请求等待超时。"); this.name = "PolymerMdAnalysisTimeout"; }
}

/** Bound browser waiting even when a transport ignores cancellation. */
export async function boundedPolymerMdRead<T>(controller: AbortController, timeoutMs: number, read: (signal: AbortSignal) => Promise<T>): Promise<T> {
  let rejectAbort!: (error: unknown) => void;
  const interrupted = new Promise<never>((_, reject) => { rejectAbort = reject; });
  const abort = () => rejectAbort(controller.signal.reason ?? new DOMException("已停止等待。", "AbortError"));
  controller.signal.addEventListener("abort", abort, { once: true });
  const timer = setTimeout(() => controller.abort(new PolymerMdAnalysisTimeout()), timeoutMs);
  try {
    if (controller.signal.aborted) { abort(); return await interrupted; }
    return await Promise.race([interrupted, read(controller.signal)]);
  } finally {
    clearTimeout(timer);
    controller.signal.removeEventListener("abort", abort);
  }
}

export function polymerMdReadError(error: unknown, operation: "日志读取" | "轨迹读取" | "距离分析") {
  if (error instanceof PolymerMdAnalysisTimeout) return `${operation}等待已超时，请稍后重试；后台可能仍在处理。`;
  const code = error instanceof Error && "code" in error ? error.code : undefined;
  if (code === "analysis_busy") return "分析服务繁忙，请稍后重试。";
  if (code === "analysis_timeout") return `${operation}超过服务处理时限，请稍后重试。`;
  if (code === "analysis_storage_busy") return "分析临时存储空间不足，请稍后重试。";
  if (code === "analysis_storage_unavailable") return "分析产物暂不可读取，请稍后重试。";
  if (code === "analysis_failed") return `${operation}未能完成，请检查阶段产物或稍后重试。`;
  if (code === "invalid_analysis_request") return operation === "距离分析" ? "分析参数无效，请重新选择原子或轨迹范围。" : "读取参数无效，请刷新任务后重试。";
  if (code === "invalid_analysis_data") return "阶段分析数据不完整或不符合要求，请检查产物。";
  if (["artifact_identity_mismatch", "artifact_integrity_mismatch"].includes(String(code))) return "分析产物校验未通过，请刷新任务后重试。";
  if (["artifact_unavailable", "trajectory_not_found"].includes(String(code))) return "该阶段的分析产物不可用，请刷新任务；已加载的视图仍保留。";
  if (["analysis_unavailable", "worker_unavailable", "analysis_service_unavailable"].includes(String(code))) return `${operation}服务暂不可用，请稍后重试；模拟任务状态未因此改变。`;
  return error instanceof Error ? error.message : `${operation}失败，请稍后重试。`;
}
