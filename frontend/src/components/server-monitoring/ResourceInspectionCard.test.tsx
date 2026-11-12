// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { ResourceInspectionCard, type InspectionMetric } from "./ResourceInspectionCard";

afterEach(cleanup);

function card(metric: InspectionMetric) {
  render(<ResourceInspectionCard title="测试服务器" subtitle="主机" icon={null} kind="server"
    selected={false} onSelect={() => undefined} status="采样正常" metrics={[metric]} />);
  return screen.getByRole("progressbar", { name: `测试服务器 ${metric.label}` });
}

it.each([
  [0, "low"], [59.99, "low"], [60, "medium"], [84.99, "medium"], [85, "high"], [100, "high"],
] as const)("uses shared occupancy thresholds at %s percent, regardless of caller color", (amount, level) => {
  const bar = card({ label: "CPU", value: `${amount}%`, amount, color: "purple" });
  expect(bar.classList.contains(`is-${level}`)).toBe(true);
  expect(bar.style.getPropertyValue("--sm-inspection-fill")).toBe("");
  expect(bar.getAttribute("aria-valuetext")).toContain("刻度占比");
});

it.each([null, Number.NaN, Number.POSITIVE_INFINITY])("keeps an unavailable value gray instead of showing zero", amount => {
  const bar = card({ label: "CPU", value: "--", amount });
  expect(bar.classList.contains("is-unknown")).toBe(true);
  expect(bar.getAttribute("aria-valuenow")).toBeNull();
  expect(bar.getAttribute("aria-valuetext")).toContain("数据不可用");
});

it.each([
  { label: "负载", value: "96", amount: 96, max: 128, level: "medium", description: "按逻辑 CPU 数归一化" },
  { label: "温度", value: "85 °C", amount: 85, max: 100, level: "high", description: "展示刻度 0–100 °C，不代表设备温度上限" },
  { label: "功耗", value: "250 W", amount: 250, max: 500, level: "low", description: "展示刻度 0–500 W，不代表设备功率上限" },
])("normalizes $label to its display scale and retains its explanation", metric => {
  const bar = card(metric);
  expect(bar.classList.contains(`is-${metric.level}`)).toBe(true);
  const description = document.getElementById(bar.getAttribute("aria-describedby")!);
  expect(description?.textContent).toBe(metric.description);
  expect(bar.closest(".sm-inspection-metric")?.getAttribute("title")).toContain(metric.description);
});
