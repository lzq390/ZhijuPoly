import { describe, expect, it } from "vitest";
import { formatMetric, recentUtcDates, utcPeriod } from "./format";
import { pathFromRoute, routeFromPath } from "../../routing";

describe("personal usage periods and units", () => {
  it("uses UTC calendar days and an exclusive next-day end including leap day", () => {
    expect(recentUtcDates(30, new Date("2024-03-01T00:15:00+08:00"))).toEqual({ startDate: "2024-01-31", endDate: "2024-02-29" });
    expect(utcPeriod("2024-02-01", "2024-02-29", "md")).toEqual({ start: "2024-02-01T00:00:00.000Z", end: "2024-03-01T00:00:00.000Z", module: "md" });
  });
  it("accepts exactly366 inclusive days and rejects invalid or oversized ranges", () => {
    expect(() => utcPeriod("2024-01-01", "2024-12-31")).not.toThrow();
    expect(() => utcPeriod("2024-01-01", "2025-01-01")).toThrow("366");
    expect(() => utcPeriod("2025-02-29", "2025-03-01")).toThrow("日期无效");
    expect(() => utcPeriod("2025-03-02", "2025-03-01")).toThrow("结束日期");
    expect(() => utcPeriod("", "2025-03-01")).toThrow("完整");
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
