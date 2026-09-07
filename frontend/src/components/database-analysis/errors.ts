const TECHNICAL_DETAIL = /(?:postgres(?:ql)?|\bbackend\b|\bschema\b|\bsnapshot\b|\bruntime\b|\bpayload\b|\btable\b|\bmissing\b|not reachable|environment variable|structured_data_backend|后端|统计快照|数据库表|数据表|环境变量|[a-z][\w]*\.[a-z][\w]*)/i;

function apiStatusMessage(status: unknown, fallback: string) {
  if (status === 401 || status === 403) return "当前账号暂无访问权限。";
  if (status === 404) return "未找到请求的数据。";
  if (status === 409) return "数据状态已发生变化，请重新加载。";
  if (status === 422) return "请求条件不符合要求，请调整后重试。";
  if (typeof status === "number" && status >= 500) return "数据服务暂不可用，请稍后重试。";
  return fallback;
}

function isUserFacingChinese(message: string) {
  return /[\u3400-\u9fff]/.test(message) && !TECHNICAL_DETAIL.test(message);
}

export function databaseAnalysisErrorMessage(error: unknown, fallback: string) {
  if (!(error instanceof Error)) return fallback;
  const message = error.message.trim();
  if (error.name === "AbortError") return "请求已取消";
  if (error.name === "ApiRequestError") {
    if (isUserFacingChinese(message)) return message;
    return apiStatusMessage((error as Error & { status?: number }).status, fallback);
  }
  if (
    !message
    || /^request(?: validation)? failed with status(?: code)? \d+$/i.test(message)
    || /^http(?: error)? \d+/i.test(message)
    || /^(failed to fetch|network error|load failed)$/i.test(message)
  ) return fallback;
  if (isUserFacingChinese(message)) return message;
  return fallback;
}

export function databaseAnalysisSourceMessage(message: string | null | undefined, fallback = "数据正在准备中，请稍后再试。") {
  const normalized = message?.trim();
  if (!normalized) return fallback;
  if (isUserFacingChinese(normalized)) return normalized;
  if (/derived from knowledge\.documents/i.test(normalized)) return "数据来源于知识文档。";
  return fallback;
}
