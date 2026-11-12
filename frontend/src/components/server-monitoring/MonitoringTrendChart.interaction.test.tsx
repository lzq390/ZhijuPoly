// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { monitoringHistory } from "../../test/serverMonitoringFixture";
import type { MonitoringHistoryPoint } from "../../types/serverMonitoring";
import { buildTrendSegments, MonitoringTrendChart, type TrendSeries } from "./MonitoringTrendChart";

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
const end = Date.parse("2026-11-11T01:00:00Z");
const stamp = (seconds: number) => new Date(end - seconds * 1000).toISOString();
const point = (seconds: number, cpu: number | null, memory = 30): MonitoringHistoryPoint => ({ timestamp: stamp(seconds), cpu_percent: cpu, memory_percent: memory,
  gpus: [{ uuid: "GPU-first", utilization_percent: cpu, memory_used_bytes: 20 }, { uuid: "GPU-second", utilization_percent: memory, memory_used_bytes: 80 }] });
const cpu: TrendSeries = { key: "cpu", label: "CPU", color: "var(--np-theme-blue)", value: sample => sample.cpu_percent };
const memory: TrendSeries = { key: "memory", label: "内存", color: "var(--np-theme-cyan)", value: sample => sample.memory_percent };
const history = (points: MonitoringHistoryPoint[]) => monitoringHistory({ captured_at: new Date(end).toISOString(), points });

function mockBounds(width = 920, height = 238) {
  const svg = screen.getByRole("img");
  vi.spyOn(svg, "getBoundingClientRect").mockReturnValue({ x: 10, y: 20, left: 10, top: 20, width, height, right: width + 10, bottom: height + 20, toJSON() {} });
  return svg;
}
function pointer(svg: Element, type: string, x: number, y = 100, pointerType = "mouse") {
  const event = new MouseEvent(type, { bubbles: true, cancelable: true, clientX: x + 10, clientY: y + 20 });
  Object.defineProperties(event, { pointerType: { value: pointerType }, pointerId: { value: 1 } });
  fireEvent(svg, event);
  return event;
}
const at = (seconds: number) => 48 + (3600 - seconds) / 3600 * 836;
const shownTime = () => screen.getByRole("tooltip").querySelector("time")?.getAttribute("datetime");

it("snaps hover readouts and crosshair to a real sample, displaying zero and missing values distinctly", () => {
  const data = history([point(1800, 0), point(1770, null), point(1740, 42.125)]);
  render(<MonitoringTrendChart history={data} series={[cpu, memory]} />);
  const svg = mockBounds();
  expect(screen.queryByRole("tooltip")).toBeNull();
  pointer(svg, "pointermove", at(1795));
  expect(shownTime()).toBe(stamp(1800));
  expect(within(screen.getByRole("tooltip")).getByText("0%")).toBeTruthy();
  expect(Number(svg.querySelector(".sm-trend-crosshair")?.getAttribute("x1"))).toBeCloseTo(at(1800));
  pointer(svg, "pointermove", at(1770));
  expect(shownTime()).toBe(stamp(1770));
  expect(within(screen.getByRole("tooltip")).getByText("--")).toBeTruthy();
  expect(svg.querySelectorAll(".sm-trend-marker")).toHaveLength(1);
  pointer(svg, "pointermove", at(1740));
  expect(within(screen.getByRole("tooltip")).getByText("42.13%")).toBeTruthy();
  fireEvent.pointerLeave(svg);
  expect(screen.queryByRole("tooltip")).toBeNull();
});

it("uses the responsive SVG's actual letterboxed plotting coordinates", () => {
  render(<MonitoringTrendChart history={history([point(3600, 10), point(0, 90)])} series={[cpu]} />);
  const svg = mockBounds(1100, 238); // 90px horizontal space on either side of the viewBox.
  pointer(svg, "pointermove", 90 + at(3600));
  expect(shownTime()).toBe(stamp(3600));
  pointer(svg, "pointermove", 90 + at(0));
  expect(shownTime()).toBe(stamp(0));
  pointer(svg, "pointermove", 30);
  expect(screen.queryByRole("tooltip")).toBeNull();
});

it("observes the plot width and keeps mobile axes, paths and pointer readings in the same full-height geometry", () => {
  let resize: ResizeObserverCallback | undefined;
  const observe = vi.fn(), disconnect = vi.fn();
  vi.stubGlobal("ResizeObserver", class {
    constructor(callback: ResizeObserverCallback) { resize = callback; }
    observe = observe;
    disconnect = disconnect;
  });
  const data = history([point(3600, 0), point(1800, 50), point(0, 100)]);
  const view = render(<MonitoringTrendChart history={data} series={[cpu]} />);
  const group = screen.getByRole("group", { name: "趋势采样读数" });
  expect(observe).toHaveBeenCalledWith(group);
  const measured = (width: number) => act(() => resize!([{
    target: group, contentRect: new DOMRect(0, 0, width, 238),
    borderBoxSize: [], contentBoxSize: [], devicePixelContentBoxSize: [],
  }], {} as ResizeObserver));
  measured(317);
  const svg = mockBounds(317, 238);
  expect(svg.getAttribute("viewBox")).toBe("0 0 317 238");
  expect(svg.querySelectorAll(".sm-chart-grid-vertical")).toHaveLength(5);
  expect(svg.querySelectorAll(".sm-chart-tick[y='222']")).toHaveLength(5);
  expect(svg.querySelector(".sm-chart-grid-vertical:last-of-type")?.getAttribute("x1")).toBe("281");
  const geometry = buildTrendSegments(data, cpu, 317);
  expect(geometry.map(segment => segment[0])).toEqual([{ x: 48, y: 194 }, { x: 164.5, y: 112 }, { x: 281, y: 30 }]);
  pointer(svg, "pointermove", 164.5);
  expect(shownTime()).toBe(stamp(1800));
  expect(within(screen.getByRole("tooltip")).getByText("50%")).toBeTruthy();
  expect(svg.querySelector(".sm-trend-crosshair")?.getAttribute("x1")).toBe("164.5");
  expect(svg.querySelector(".sm-trend-marker")?.getAttribute("cy")).toBe("112");
  measured(640);
  expect(svg.getAttribute("viewBox")).toBe("0 0 640 238");
  expect(shownTime()).toBe(stamp(1800));
  expect(svg.querySelector(".sm-trend-crosshair")?.getAttribute("x1")).toBe("326");
  expect(svg.querySelector(".sm-trend-marker")?.getAttribute("cy")).toBe("112");
  vi.mocked(svg.getBoundingClientRect).mockReturnValue({ x: 10, y: 20, left: 10, top: 20, width: 640, height: 238, right: 650, bottom: 258, toJSON() {} });
  pointer(svg, "pointermove", 604);
  expect(shownTime()).toBe(stamp(0));
  expect(within(screen.getByRole("tooltip")).getByText("100%")).toBeTruthy();
  measured(0);
  expect(svg.getAttribute("viewBox")).toBe("0 0 640 238");
  measured(220);
  expect(svg.getAttribute("viewBox")).toBe("0 0 280 238");
  view.unmount();
  expect(disconnect).toHaveBeenCalledOnce();
});

it("measures its own container before paint and remeasures without ResizeObserver support", () => {
  vi.stubGlobal("ResizeObserver", undefined);
  let containerWidth = 317;
  const original = HTMLElement.prototype.getBoundingClientRect;
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    if (!this.classList.contains("sm-trend-plot")) return original.call(this);
    return { x: 0, y: 0, left: 0, top: 0, width: containerWidth, height: 238, right: containerWidth, bottom: 238, toJSON() {} };
  });
  render(<MonitoringTrendChart history={history([point(0, 100)])} series={[cpu]} />);
  expect(screen.getByRole("img").getAttribute("viewBox")).toBe("0 0 317 238");
  containerWidth = 512;
  fireEvent(window, new Event("resize"));
  expect(screen.getByRole("img").getAttribute("viewBox")).toBe("0 0 512 238");
});

it("supports focus, adjacent actual samples, Home, End and Escape without inserting gap values", () => {
  const data = history([point(1800, 0), point(1770, null), point(1650, 100)]);
  render(<MonitoringTrendChart history={data} series={[cpu]} />);
  const group = screen.getByRole("group", { name: "趋势采样读数" });
  fireEvent.focus(group);
  expect(shownTime()).toBe(stamp(1650));
  fireEvent.keyDown(group, { key: "ArrowLeft" });
  expect(shownTime()).toBe(stamp(1770));
  expect(within(screen.getByRole("tooltip")).getByText("--")).toBeTruthy();
  fireEvent.keyDown(group, { key: "ArrowLeft" });
  expect(shownTime()).toBe(stamp(1800));
  fireEvent.keyDown(group, { key: "ArrowRight" });
  expect(shownTime()).toBe(stamp(1770));
  fireEvent.keyDown(group, { key: "Home" });
  expect(shownTime()).toBe(stamp(1800));
  fireEvent.keyDown(group, { key: "End" });
  expect(shownTime()).toBe(stamp(1650));
  expect(buildTrendSegments(data, cpu).map(segment => segment.length)).toEqual([1, 1]);
  expect(screen.getByRole("img").getAttribute("aria-label")).toContain("纵轴 0 至 100%");
  fireEvent.keyDown(group, { key: "Escape" });
  expect(screen.queryByRole("tooltip")).toBeNull();
  expect(group.getAttribute("tabindex")).toBe("0");
});

it("selects a touch tap but does not prevent scrolling or select a dragged/cancelled gesture", () => {
  render(<MonitoringTrendChart history={history([point(1800, 10), point(0, 90)])} series={[cpu]} />);
  const svg = mockBounds();
  expect(pointer(svg, "pointerdown", at(1800), 90, "touch").defaultPrevented).toBe(false);
  pointer(svg, "pointermove", at(1800), 140, "touch");
  expect(pointer(svg, "pointerup", at(1800), 140, "touch").defaultPrevented).toBe(false);
  expect(screen.queryByRole("tooltip")).toBeNull();
  pointer(svg, "pointerdown", at(1800), 90, "touch");
  pointer(svg, "pointerup", at(1800), 92, "touch");
  expect(shownTime()).toBe(stamp(1800));
  fireEvent.pointerLeave(svg);
  expect(shownTime()).toBe(stamp(1800));
  pointer(svg, "pointerdown", at(0), 90, "touch");
  fireEvent.pointerCancel(svg);
  pointer(svg, "pointerup", at(0), 90, "touch");
  expect(shownTime()).toBe(stamp(1800));
});

it("immediately derives the selected timestamp from the current GPU and metric series", () => {
  const data = history([point(30, 12, 67)]);
  const gpu = (uuid: string, label: string, metric: "utilization_percent" | "memory_used_bytes"): TrendSeries => ({ key: uuid, label, color: "var(--np-theme-blue)", value: sample => sample.gpus.find(item => item.uuid === uuid)?.[metric] ?? null });
  const view = render(<MonitoringTrendChart history={data} series={[gpu("GPU-first", "GPU 0", "utilization_percent")]} />);
  fireEvent.focus(screen.getByRole("group", { name: "趋势采样读数" }));
  expect(within(screen.getByRole("tooltip")).getByText("12%")).toBeTruthy();
  view.rerender(<MonitoringTrendChart history={data} series={[gpu("GPU-second", "GPU 2", "utilization_percent")]} />);
  expect(shownTime()).toBe(stamp(30));
  expect(within(screen.getByRole("tooltip")).getByText("GPU 2")).toBeTruthy();
  expect(within(screen.getByRole("tooltip")).getByText("67%")).toBeTruthy();
  expect(within(screen.getByRole("tooltip")).queryByText("12%")).toBeNull();
  view.rerender(<MonitoringTrendChart history={data} series={[gpu("GPU-second", "GPU 2", "memory_used_bytes")]} />);
  expect(within(screen.getByRole("tooltip")).getByText("80%")).toBeTruthy();
});

it("clears removed samples, empty history and collector changes without reviving prior readouts", () => {
  const data = history([point(30, 12)]);
  const view = render(<MonitoringTrendChart history={data} series={[cpu]} />);
  fireEvent.focus(screen.getByRole("group", { name: "趋势采样读数" }));
  expect(shownTime()).toBe(stamp(30));
  view.rerender(<MonitoringTrendChart history={history([point(0, 20)])} series={[cpu]} />);
  expect(screen.queryByRole("tooltip")).toBeNull();
  view.rerender(<MonitoringTrendChart history={null} series={[cpu]} />);
  expect(screen.getByText("等待历史采样")).toBeTruthy();
  view.rerender(<MonitoringTrendChart history={data} series={[cpu]} />);
  expect(screen.queryByRole("tooltip")).toBeNull();
  fireEvent.focus(screen.getByRole("group", { name: "趋势采样读数" }));
  view.rerender(<MonitoringTrendChart history={{ ...data, collector_id: "new-collector" }} series={[cpu]} />);
  expect(screen.queryByRole("tooltip")).toBeNull();
});

it.each([NaN, Infinity, -1, 101])("never presents invalid percentage %s as a valid reading or marker", value => {
  render(<MonitoringTrendChart history={history([point(0, value)])} series={[cpu]} />);
  fireEvent.focus(screen.getByRole("group", { name: "趋势采样读数" }));
  expect(within(screen.getByRole("tooltip")).getByText("--")).toBeTruthy();
  expect(screen.getByRole("img").querySelectorAll(".sm-trend-marker")).toHaveLength(0);
});
