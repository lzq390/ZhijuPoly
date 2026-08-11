export function databaseAnalysisErrorMessage(error: unknown, fallback: string) {
  if (
    error instanceof Error &&
    error.name === "ApiRequestError" &&
    "status" in error &&
    typeof error.status === "number"
  ) {
    return `${fallback}（HTTP ${error.status}）`;
  }
  if (error instanceof Error && /[\u3400-\u9fff]/.test(error.message)) return error.message;
  return fallback;
}
