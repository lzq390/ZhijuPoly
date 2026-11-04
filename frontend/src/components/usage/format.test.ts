import { describe, expect, it } from "vitest";
import { beijingTimestamp, formatMetric, recentBeijingDates, beijingPeriod } from "./format";
import { pathFromRoute, routeFromPath } from "../../routing";

describe("personal usage periods and units", () => {
  it("uses Beijing calendar days and an exclusive next-day end including leap day", () => {
    expect(recentBeijingDates(30, new Date("2024-03-01T00:15:00+08:00"))).toEqual({ startDate: "2024-02-01", endDate: "2024-03-01" });
    expect(beijingPeriod("2024-02-01", "2024-02-29", "md")).toEqual({ start: "2024-01-31T16:00:00.000Z", end: "2024-02-29T16:00:00.000Z", timezone: "Asia/Shanghai", module: "md" });
  });
  it("accepts exactly366 inclusive days and rejects invalid or oversized ranges", () => {
    expect(() => beijingPeriod("2024-01-01", "2024-12-31")).not.toThrow();
    expect(() => beijingPeriod("2024-01-01", "2025-01-01")).toThrow("366");
    expect(() => beijingPeriod("2025-02-29", "2025-03-01")).toThrow("日期无效");
    expect(() => beijingPeriod("2025-03-02", "2025-03-01")).toThrow("结束日期");
    expect(() => beijingPeriod("", "2025-03-01")).toThrow("完整");
  });
  it("uses the Beijing day at UTC16:00, including month and year rollover", () => {
    expect(recentBeijingDates(1, new Date("2026-12-31T15:59:59Z"))).toEqual({ startDate: "2026-12-31", endDate: "2026-12-31" });
    expect(recentBeijingDates(1, new Date("2026-12-31T16:00:00Z"))).toEqual({ startDate: "2027-01-01", endDate: "2027-01-01" });
    expect(beijingPeriod("2027-01-01", "2027-01-01")).toEqual({ start: "2026-12-31T16:00:00.000Z", end: "2027-01-01T16:00:00.000Z", timezone: "Asia/Shanghai" });
  });
  it("converts instants to Beijing time without changing server cycle boundaries", () => {
    expect(beijingTimestamp("2026-09-30T15:59:59Z")).toBe("2026-09-30 23:59:59");
    expect(beijingTimestamp("2026-09-30T16:00:00Z")).toBe("2026-10-01 00:00:00");
    expect(beijingTimestamp("2026-10-01T00:00:00Z")).toBe("2026-10-01 08:00:00");
    expect(beijingTimestamp("2026-10-01T00:00:00+08:00")).toBe("2026-10-01 00:00:00");
    expect(beijingTimestamp(null)).toBe("未记录");
    expect(beijingTimestamp("invalid")).toBe("未记录");
  });
  it("never turns missing/nonfinite metrics into zero or GPU hours", () => {
    expect(formatMetric("execution_ms", null)).toBe("未记录");
    expect(formatMetric("execution_ms", 0)).toBe("0 秒");
    expect(formatMetric("execution_ms", 3600000)).toBe("1 小时");
    expect(formatMetric("provider_total_tokens", undefined)).toBe("未记录");
    expect(formatMetric("provider_total_tokens", NaN)).toBe("未记录");
    expect(formatMetric("artifact_bytes", 0)).toBe("0 B");
    expect(formatMetric("artifact_bytes", 1024)).toBe("1 KiB");
  });
  it("round trips the dedicated account route", () => {
    expect(routeFromPath("/account/usage/")).toEqual({ module: "personalUsage", datasetKey: null });
    expect(pathFromRoute({ module: "personalUsage", datasetKey: null })).toBe("/account/usage");
  });
});
