import type { UsagePeriod, UsageStatus } from "../../types/usage";
const DAY = 86_400_000;
export const statusLabels: Record<UsageStatus, string> = {
  queued: "排队中", running: "执行中", completed: "已完成", failed: "失败",
  cancelled: "已取消", timed_out: "超时", interrupted: "已中断", rejected: "已拒绝"
};
export function recentUtcDates(days = 30, now = new Date()) {
  const end = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate());
  return { startDate: new Date(end - (days - 1) * DAY).toISOString().slice(0, 10), endDate: new Date(end).toISOString().slice(0, 10) };
}
export function utcPeriod(startDate: string, endDate: string, module?: string): UsagePeriod {
  function parse(value: string) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) throw new Error("请选择完整的起止日期。");
    const timestamp = Date.parse(value + "T00:00:00.000Z");
    if (!Number.isFinite(timestamp) || new Date(timestamp).toISOString().slice(0, 10) !== value) throw new Error("日期无效。");
    return timestamp;
  }
  const start = parse(startDate), last = parse(endDate);
  if (last < start) throw new Error("结束日期不能早于开始日期。");
  if ((last - start) / DAY + 1 > 366) throw new Error("一次最多查询 366 天。");
  return { start: new Date(start).toISOString(), end: new Date(last + DAY).toISOString(), ...(module ? { module } : {}) };
}
export function formatMetric(id: string, value: number | null | undefined, unit?: string) {
  if (value == null || !Number.isFinite(value)) return "未记录";
  const number = (n: number) => n.toLocaleString("zh-CN", { maximumFractionDigits: 2 });
  if (id.endsWith("_ms") || unit === "ms") {
    if (value >= 3_600_000) return `${number(value / 3_600_000)} 小时`;
    if (value >= 60_000) return `${number(value / 60_000)} 分钟`;
    return `${number(value / 1000)} 秒`;
  }
  if (id === "artifact_bytes" || unit === "bytes") {
    if (value >= 1024 ** 3) return `${number(value / 1024 ** 3)} GiB`;
    if (value >= 1024 ** 2) return `${number(value / 1024 ** 2)} MiB`;
    if (value >= 1024) return `${number(value / 1024)} KiB`;
    return `${number(value)} B`;
  }
  return number(value);
}
export function utcTimestamp(value: string | null | undefined) {
  if (!value) return "未记录";
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date.toISOString().replace("T", " ").slice(0, 19) : "未记录";
}
export const metricLabel = (metric: { id: string; label: string }) =>
  metric.id === "artifact_bytes" ? "累计已观测产物体积" : metric.label;
