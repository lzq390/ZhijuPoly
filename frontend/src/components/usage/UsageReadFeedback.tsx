export const usageErrorMessage = (error: unknown) => error instanceof Error ? error.message : "暂时无法读取用量，请重试。";

export function UsageReadFeedback({ loading, error, hasData, label, retry }: {
  loading: boolean; error?: unknown; hasData: boolean; label: string; retry: () => void;
}) {
  if (loading) return hasData
    ? <p className="np-usage-read-feedback" role="status">{label}更新中…</p>
    : <div className="np-usage-skeleton" role="status" aria-label={`正在读取${label}`}><span /><span /><span /><p>正在读取{label}…</p></div>;
  if (error) return <div className="np-usage-read-feedback np-usage-error">
    <p role="alert">{hasData && "更新失败，当前显示上次成功读取的数据。"}{usageErrorMessage(error)}</p>
    <button type="button" onClick={retry}>重试</button>
  </div>;
  // Keep the refresh line allocated so an in-flight refresh does not move data.
  return hasData ? <div className="np-usage-read-feedback" aria-hidden="true" /> : null;
}
