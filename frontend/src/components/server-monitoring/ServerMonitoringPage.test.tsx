// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { monitoringHistory, monitoringHostInfo, monitoringSnapshot } from "../../test/serverMonitoringFixture";
import type { ServerMonitoringControl } from "../../hooks/useServerMonitoring";
import { ServerMonitoringPage } from "./ServerMonitoringPage";
import { buildTrendSegments, monitoringClock } from "./MonitoringTrendChart";

afterEach(() => { cleanup(); vi.useRealTimers(); Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" }); });
const control = (changes: Partial<ServerMonitoringControl> = {}): ServerMonitoringControl => ({ key: "test", access: "allowed", snapshot: monitoringSnapshot(), history: monitoringHistory(), snapshotError: null, historyError: null, receivedAt: Date.now(), historyReceivedAt: Date.now(), refreshing: false, refresh: vi.fn(), login: vi.fn(), ...changes });

it("guards direct page access even if stale resource props are present", () => {
  render(<ServerMonitoringPage control={control({ access: "denied" })} />);
  expect(screen.getByText("当前账号暂无访问权限")).toBeTruthy();
  expect(screen.queryByText("compute-test")).toBeNull(); expect(screen.queryByText("Test GPU")).toBeNull();
});
it("displays actual GPU inventory and genuine zero utilization", () => {
  render(<ServerMonitoringPage control={control()} />);
  expect(screen.getAllByText("compute-test").length).toBeGreaterThan(0); expect(screen.getByText("Test GPU")).toBeTruthy();
  expect(screen.getByRole("progressbar", { name: "GPU 0 利用率" }).getAttribute("aria-valuenow")).toBe("0");
  expect(screen.queryByText("GPU 1")).toBeNull();
});
it("keeps unknown metrics distinct from zero and reports stale cached data", () => {
  render(<ServerMonitoringPage control={control({ snapshot: monitoringSnapshot({ cpu: null }), snapshotError: "最新采样暂时不可用，正在重试。" })} />);
  expect(screen.getByRole("progressbar", { name: "当前服务器 CPU" }).getAttribute("aria-valuenow")).toBeNull();
  expect(screen.getByText("数据已过期")).toBeTruthy(); expect(screen.getByText(/当前状态尚未确认/)).toBeTruthy();
  expect(screen.getByText(/最后更新/).querySelector("time")?.dateTime).toBe(monitoringSnapshot().captured_at);
});
it("shows a historical collection failure even before the first valid history arrives", () => {
  render(<ServerMonitoringPage control={control({ history: null, historyError: "历史数据暂时不可用，正在重试。" })} />);
  expect(screen.getByText("历史数据暂时不可用，正在等待采集服务恢复。")).toBeTruthy();
});
it("distinguishes the 15-second stale window from collection unavailable after 60 seconds", () => {
  const view = render(<ServerMonitoringPage control={control({ receivedAt: Date.now() - 16000 })} />);
  expect(screen.getByText("数据已过期")).toBeTruthy();
  view.rerender(<ServerMonitoringPage control={control({ receivedAt: Date.now() - 61000 })} />);
  expect(screen.getByText("采集不可用")).toBeTruthy();
  expect(screen.queryByText("服务器离线")).toBeNull();
});
it("advances freshness even without any network updates and pauses its display clock while hidden", () => {
  vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "Date"] });
  vi.setSystemTime(new Date("2026-11-11T01:00:00Z"));
  Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
  render(<ServerMonitoringPage control={control()} />);
  expect(within(screen.getByRole("region", { name: "服务器参数详情" })).getByText("在线")).toBeTruthy();
  act(() => vi.advanceTimersByTime(16000));
  expect(screen.getByText("数据已过期")).toBeTruthy();
  act(() => { Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" }); document.dispatchEvent(new Event("visibilitychange")); });
  expect(vi.getTimerCount()).toBe(0);
  act(() => vi.advanceTimersByTime(45000));
  act(() => { Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" }); document.dispatchEvent(new Event("visibilitychange")); });
  expect(screen.getByText("采集不可用")).toBeTruthy();
});
it("does not report zero GPUs when enumeration failed", () => {
  const snapshot = monitoringSnapshot({ gpus: [] });
  snapshot.components.gpus = { status: "unavailable", last_success_at: null, error_code: "gpu_query_failed" };
  const view = render(<ServerMonitoringPage control={control({ snapshot })} />);
  expect(screen.getByText("GPU 数量待采集")).toBeTruthy(); expect(screen.queryByText("0 张 GPU")).toBeNull();
  view.rerender(<ServerMonitoringPage control={control({ snapshot: monitoringSnapshot({ gpus: [] }) })} />);
  expect(screen.getByText("0 张 GPU")).toBeTruthy();
});
it("can compare all GPUs or select an individual GPU for utilization and memory trends", () => {
  const first = monitoringSnapshot().gpus[0];
  render(<ServerMonitoringPage control={control({ snapshot: monitoringSnapshot({ gpus: [first, { ...first, index: 2, uuid: "gpu-second", name: "Second GPU" }] }) })} />);
  fireEvent.click(screen.getByRole("button", { name: "GPU 利用率" }));
  expect(screen.getByRole("img", { name: /GPU 0、GPU 2/ })).toBeTruthy();
  fireEvent.click(screen.getByRole("combobox", { name: "选择趋势显卡" }));
  fireEvent.click(screen.getByRole("option", { name: /^GPU 2 / }));
  expect(screen.getByRole("img", { name: /最近一小时GPU 2趋势/ })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "GPU 显存" }));
  expect(screen.getByRole("img", { name: /最近一小时GPU 2趋势/ })).toBeTruthy();
});
it("uses wall-clock spacing and breaks lines at nulls and missed sampling intervals", () => {
  const history = monitoringHistory({ points: [
    { timestamp: "2026-11-11T00:00:00Z", cpu_percent: 0, memory_percent: null, gpus: [] },
    { timestamp: "2026-11-11T00:00:30Z", cpu_percent: 100, memory_percent: null, gpus: [] },
    { timestamp: "2026-11-11T00:01:00Z", cpu_percent: null, memory_percent: null, gpus: [] },
    { timestamp: "2026-11-11T00:01:30Z", cpu_percent: 25, memory_percent: null, gpus: [] },
    { timestamp: "2026-11-11T00:03:00Z", cpu_percent: 50, memory_percent: null, gpus: [] }
  ] });
  const segments = buildTrendSegments(history, { key: "cpu", label: "CPU", color: "blue", value: point => point.cpu_percent });
  expect(segments.map(segment => segment.length)).toEqual([2, 1, 1]);
  expect(segments[0][0]).toEqual({ x: 48, y: 194 }); expect(segments[0][1].y).toBe(30);
  expect(segments[2][0].x - segments[1][0].x).toBeCloseTo(836 * 90 / 3600);
});

it("keeps CPU and memory trends when GPU cards change the selected device", () => {
  const first = monitoringSnapshot().gpus[0];
  render(<ServerMonitoringPage control={control({ snapshot: monitoringSnapshot({ gpus: [first, { ...first, index: 3, uuid: "gpu-second" }] }) })} />);
  const server = screen.getByRole("button", { name: "查看当前服务器详情" });
  const gpu = screen.getByRole("button", { name: "查看GPU 0详情" });
  expect(server.getAttribute("aria-pressed")).toBe("true");
  expect(server.querySelectorAll('[role="progressbar"]')).toHaveLength(4);
  expect(gpu.querySelectorAll('[role="progressbar"]')).toHaveLength(4);
  fireEvent.click(gpu);
  expect(gpu.getAttribute("aria-pressed")).toBe("true");
  expect(server.getAttribute("aria-pressed")).toBe("true");
  expect(screen.getByRole("heading", { name: "服务器详情" })).toBeTruthy();
  expect(within(screen.getByRole("region", { name: "服务器参数详情" })).getAllByRole("article")).toHaveLength(6);
  expect(within(screen.getByRole("group", { name: "选择服务器" })).getAllByRole("button")).toHaveLength(1);
  expect(screen.queryByRole("heading", { name: "存储空间" })).toBeNull();
  expect(screen.queryByRole("heading", { name: "采样状态" })).toBeNull();
  expect(screen.getByRole("button", { name: "CPU / 内存" }).getAttribute("aria-pressed")).toBe("true");
  expect(screen.getByRole("img", { name: /最近一小时CPU、内存趋势/ })).toBeTruthy();
  const second = screen.getByRole("button", { name: "查看GPU 3详情" });
  fireEvent.click(second);
  expect(second.getAttribute("aria-pressed")).toBe("true");
  expect(gpu.getAttribute("aria-pressed")).toBe("false");
  expect(screen.getByRole("img", { name: /最近一小时CPU、内存趋势/ })).toBeTruthy();
  fireEvent.click(server);
  expect(server.getAttribute("aria-pressed")).toBe("true");
  expect(second.getAttribute("aria-pressed")).toBe("false");
  expect(screen.getByRole("img", { name: /最近一小时CPU、内存趋势/ })).toBeTruthy();
});

it.each([
  { tab: "GPU 显存", firstY: 153, secondY: 71 },
  { tab: "GPU 利用率", firstY: 177.6, secondY: 62.8 }
])("preserves $tab and displays the selected GPU's actual series", ({ tab, firstY, secondY }) => {
  const first = monitoringSnapshot().gpus[0];
  const snapshot = monitoringSnapshot({ gpus: [first, { ...first, index: 3, uuid: "gpu-second", memory_total_bytes: 48 * 2 ** 30 }] });
  const history = monitoringHistory();
  history.points = history.points.map(point => ({ ...point, gpus: [
    { uuid: first.uuid, utilization_percent: 10, memory_used_bytes: 6 * 2 ** 30 },
    { uuid: "gpu-second", utilization_percent: 80, memory_used_bytes: 36 * 2 ** 30 }
  ] }));
  render(<ServerMonitoringPage control={control({ snapshot, history })} />);
  fireEvent.click(screen.getByRole("button", { name: tab }));
  for (const [index, expectedY] of [[0, firstY], [3, secondY]] as const) {
    fireEvent.click(screen.getByRole("button", { name: `查看GPU ${index}详情` }));
    expect(screen.getByRole("button", { name: tab }).getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByRole("combobox", { name: "选择趋势显卡" }).querySelector("strong")?.textContent).toBe(`GPU ${index}`);
    const chart = screen.getByRole("img", { name: new RegExp(`最近一小时GPU ${index}趋势`) });
    const paths = chart.querySelectorAll("path");
    expect(paths).toHaveLength(1);
    const yValues = paths[0].getAttribute("d")!.split(/[ML]/).filter(Boolean).map(point => Number(point.trim().split(",")[1]));
    expect(yValues).toEqual([expectedY, expectedY, expectedY]);
  }
  fireEvent.click(screen.getByRole("button", { name: "查看当前服务器详情" }));
  expect(screen.getByRole("button", { name: "CPU / 内存" }).getAttribute("aria-pressed")).toBe("true");
  expect(document.querySelectorAll('.sm-gpu-grid [aria-pressed="true"]')).toHaveLength(0);
});

it("aggregates GPU memory by actual capacity instead of averaging percentages", () => {
  const first = monitoringSnapshot().gpus[0];
  const gpus = [
    { ...first, memory_used_bytes: 8 * 2 ** 30, memory_total_bytes: 8 * 2 ** 30 },
    { ...first, index: 3, uuid: "gpu-second", memory_used_bytes: 0, memory_total_bytes: 24 * 2 ** 30 }
  ];
  const view = render(<ServerMonitoringPage control={control({ snapshot: monitoringSnapshot({ gpus }) })} />);
  const aggregate = within(screen.getByRole("article", { name: "GPU 总显存占用" }));
  expect(aggregate.getByText("25.0%")).toBeTruthy();
  expect(aggregate.getByText("已用 8.0 GiB / 总量 32.0 GiB")).toBeTruthy();
  view.rerender(<ServerMonitoringPage control={control({ snapshot: monitoringSnapshot({ gpus: gpus.map(gpu => ({ ...gpu, memory_used_bytes: 0 })) }) })} />);
  expect(aggregate.getByText("0.0%")).toBeTruthy();
});

it("switches the GPU dropdown by keyboard without changing its trend and links tasks by physical UUID", () => {
  const first = monitoringSnapshot().gpus[0];
  const snapshot = monitoringSnapshot({ gpus: [first, { ...first, index: 3, uuid: "gpu-second", name: "Second GPU", memory_total_bytes: 48 * 2 ** 30 }] });
  const renderGpuTasks = vi.fn(({ gpuUuid }: { gpuUuid: string | null }) => <p>{gpuUuid ?? "全部任务内容"}</p>);
  render(<ServerMonitoringPage control={control({ snapshot })} renderGpuTasks={renderGpuTasks} />);
  fireEvent.click(screen.getByRole("button", { name: "GPU 显存" }));
  const picker = screen.getByRole("combobox", { name: "选择趋势显卡" });
  fireEvent.keyDown(picker, { key: "ArrowDown" });
  expect(screen.getByRole("option", { name: "GPU 3 Second GPU · 48.0 GiB" })).toBeTruthy();
  fireEvent.keyDown(picker, { key: "End" });
  fireEvent.keyDown(picker, { key: "Enter" });
  expect(picker.getAttribute("aria-expanded")).toBe("false");
  expect(picker.querySelector("strong")?.textContent).toBe("GPU 3");
  expect(screen.getByRole("button", { name: "GPU 显存" }).getAttribute("aria-pressed")).toBe("true");
  expect(screen.getByRole("button", { name: "查看GPU 3详情" }).getAttribute("aria-pressed")).toBe("true");
  expect(renderGpuTasks).toHaveBeenLastCalledWith(expect.objectContaining({ gpuUuid: "gpu-second" }));
  fireEvent.click(picker);
  expect(screen.getByRole("option", { name: /^GPU 3 / }).getAttribute("aria-selected")).toBe("true");
  fireEvent.keyDown(picker, { key: "Home" });
  fireEvent.keyDown(picker, { key: "Escape" });
  expect(picker.querySelector("strong")?.textContent).toBe("GPU 3");
  fireEvent.click(picker);
  fireEvent.keyDown(picker, { key: "Home" });
  fireEvent.keyDown(picker, { key: "Enter" });
  expect(picker.querySelector("strong")?.textContent).toBe("全部 GPU");
  expect(screen.getByRole("img", { name: /最近一小时GPU 0、GPU 3趋势/ })).toBeTruthy();
  expect(screen.getByText("全部任务内容")).toBeTruthy();
  expect(renderGpuTasks).toHaveBeenLastCalledWith(expect.objectContaining({ gpuUuid: null }));
});

it("updates an open GPU menu when inventory changes and removes it with lost access", () => {
  const first = monitoringSnapshot().gpus[0];
  const view = render(<ServerMonitoringPage control={control({ snapshot: monitoringSnapshot({ gpus: [first, { ...first, index: 3, uuid: "gpu-second" }] }) })} />);
  fireEvent.click(screen.getByRole("button", { name: "GPU 利用率" }));
  fireEvent.click(screen.getByRole("combobox", { name: "选择趋势显卡" }));
  fireEvent.click(screen.getByRole("option", { name: /^GPU 3 / }));
  fireEvent.click(screen.getByRole("combobox", { name: "选择趋势显卡" }));
  view.rerender(<ServerMonitoringPage control={control()} />);
  expect(screen.queryByRole("option", { name: /^GPU 3 / })).toBeNull();
  expect(screen.getByRole("combobox", { name: "选择趋势显卡" }).getAttribute("aria-expanded")).toBe("false");
  fireEvent.click(screen.getByRole("combobox", { name: "选择趋势显卡" }));
  expect(screen.getByRole("option", { name: "全部 GPU 1 张显卡" }).getAttribute("aria-selected")).toBe("true");
  expect(screen.getByRole("img", { name: /最近一小时GPU 0趋势/ })).toBeTruthy();
  view.rerender(<ServerMonitoringPage control={control({ access: "denied" })} />);
  expect(screen.queryByRole("combobox")).toBeNull();
  expect(screen.queryByRole("listbox")).toBeNull();
});

it.each(["used", "total", "zero-total", "empty"])("does not fabricate aggregate GPU memory when %s is missing or invalid", missing => {
  const first = monitoringSnapshot().gpus[0];
  const second: typeof first = { ...first, index: 1, uuid: "gpu-second", memory_used_bytes: 2 ** 30 };
  if (missing === "used") second.memory_used_bytes = null;
  if (missing === "total") second.memory_total_bytes = null;
  if (missing === "zero-total") second.memory_total_bytes = 0;
  render(<ServerMonitoringPage control={control({ snapshot: monitoringSnapshot({ gpus: missing === "empty" ? [] : [first, second] }) })} />);
  const aggregate = within(screen.getByRole("article", { name: "GPU 总显存占用" }));
  expect(aggregate.getByText("—")).toBeTruthy();
  expect(aggregate.getByText("等待全部显卡的有效显存采样")).toBeTruthy();
  expect(aggregate.queryByText(/已用/)).toBeNull();
});

it("keeps each disk's used and total capacity in one accessible note", () => {
  const snapshot = monitoringSnapshot();
  snapshot.disks.push({ mount_point: "/data", label: "数据盘", total_bytes: 100 * 2 ** 30, used_bytes: 75 * 2 ** 30, available_bytes: 25 * 2 ** 30, used_percent: 75, status: "ok" });
  render(<ServerMonitoringPage control={control({ snapshot })} />);
  const system = within(screen.getByRole("article", { name: "系统盘使用率" }));
  expect(system.getByText("已用 512.0 GiB / 总量 1.0 TiB").getAttribute("title")).toBe("已用 512.0 GiB / 总量 1.0 TiB");
  expect(system.queryByText(/可用/)).toBeNull();
  const data = within(screen.getByRole("article", { name: "数据盘使用率" }));
  expect(data.getByText("75.0%")).toBeTruthy();
  expect(data.getByText("已用 75.0 GiB / 总量 100.0 GiB").getAttribute("title")).toBe("已用 75.0 GiB / 总量 100.0 GiB");
  expect(data.queryByText(/可用|\/data/)).toBeNull();
});

it("does not substitute the system disk when the data mount has no observation", () => {
  render(<ServerMonitoringPage control={control()} />);
  const data = within(screen.getByRole("article", { name: "数据盘使用率" }));
  expect(data.getByText("—")).toBeTruthy();
  expect(data.getByText("已用 — / 总量 —")).toBeTruthy();
});

it("retains each failed component and its last success after removing the separate status panel", () => {
  const snapshot = monitoringSnapshot();
  snapshot.components.disks = { status: "unavailable", last_success_at: "2026-11-11T00:30:00Z", error_code: "disk_failed" };
  snapshot.components.gpus = { status: "unavailable", last_success_at: null, error_code: "gpu_failed" };
  render(<ServerMonitoringPage control={control({ snapshot })} />);
  const issue = screen.getByText(/部分指标暂未就绪/);
  expect(issue.textContent).toContain("磁盘（暂不可用；最后成功");
  expect(issue.textContent).toContain("GPU（暂不可用；最后成功 尚无有效采样）");
  expect(issue.textContent).toContain(monitoringClock("2026-11-11T00:30:00Z"));
});

it("labels valid observations online and shows the current host identity and available disks", () => {
  const snapshot = monitoringSnapshot({ host_info: monitoringHostInfo() });
  snapshot.disks.push({ mount_point: "/data", label: "数据盘", total_bytes: 100 * 2 ** 30, used_bytes: 75 * 2 ** 30, available_bytes: 25 * 2 ** 30, used_percent: 75, status: "ok" });
  const first = snapshot.gpus[0];
  snapshot.gpus = [{ ...first, memory_used_bytes: 6 * 2 ** 30 }, { ...first, index: 3, uuid: "gpu-second", memory_total_bytes: 48 * 2 ** 30, memory_used_bytes: 40 * 2 ** 30 }];
  render(<ServerMonitoringPage control={control({ snapshot })} />);
  expect(screen.getByRole("heading", { name: "服务器详情" })).toBeTruthy();
  expect(screen.getAllByText("在线")).toHaveLength(4);
  expect(screen.queryByText("已采集")).toBeNull();
  expect(screen.queryByText("采样正常")).toBeNull();
  expect(screen.queryByText("我的近期任务")).toBeNull();
  const host = screen.getByRole("complementary", { name: "主机信息" });
  expect(screen.queryByRole("complementary", { name: "资源余量" })).toBeNull();
  expect(within(host).getByText("CPU").nextElementSibling?.textContent).toBe("Test CPU 32-Core2 插槽 · 32 核 · 64 线程");
  expect(within(host).getByText("系统").nextElementSibling?.textContent).toBe("Test Linux 24.04 LTS6.8.0-test");
  expect(within(host).getByText("GPU 驱动").nextElementSibling?.textContent).toBe("550.54.14");
  expect(within(host).getByText("系统盘").nextElementSibling?.textContent).toBe("512.0 GiB");
  expect(within(host).getByText("数据盘").nextElementSibling?.textContent).toBe("25.0 GiB");
  expect(host.textContent).not.toContain("可调度");
});

it.each([undefined, null, monitoringHostInfo({ cpu_model: null, cpu_socket_count: null, cpu_physical_core_count: null, os_name: null, kernel_release: null, nvidia_driver_version: null })])("keeps missing or nullable host metadata unknown without inferring physical CPU counts", host_info => {
  render(<ServerMonitoringPage control={control({ snapshot: monitoringSnapshot({ host_info }) })} />);
  const host = screen.getByRole("complementary", { name: "主机信息" });
  expect(Array.from(host.querySelectorAll(".sm-host-info-value"), node => node.textContent)).toEqual(["--", "--", "--"]);
  expect(within(host).getByText("-- 插槽 · -- 核 · 64 线程")).toBeTruthy();
  expect(within(host).getByText("系统").nextElementSibling?.textContent).toBe("----");
  expect(within(host).getByText("系统盘").nextElementSibling?.textContent).toBe("512.0 GiB");
  expect(within(host).getByText("数据盘").nextElementSibling?.textContent).toBe("--");
});

it("keeps full metadata accessible when long host fields are abbreviated visually", () => {
  const host_info = monitoringHostInfo({ cpu_model: "Example CPU with an unusually long manufacturer and product name", os_name: "Example Linux Enterprise Server Edition with a long release label", kernel_release: "6.8.0-example-long-custom-kernel-build-20261112" });
  const view = render(<ServerMonitoringPage control={control({ snapshot: monitoringSnapshot({ host_info }) })} />);
  const host = screen.getByRole("complementary", { name: "主机信息" });
  for (const value of [host_info.cpu_model, host_info.os_name, host_info.kernel_release, host_info.nvidia_driver_version]) {
    expect(within(host).getByText(value!).getAttribute("title")).toBe(value);
  }
  view.rerender(<ServerMonitoringPage control={control({ snapshot: monitoringSnapshot({ logical_cpu_count: 96, host_info: monitoringHostInfo({ cpu_model: "Updated CPU", cpu_socket_count: 4, cpu_physical_core_count: 48 }) }) })} />);
  expect(within(host).queryByText(host_info.cpu_model!)).toBeNull();
  expect(within(host).getByText("Updated CPU")).toBeTruthy();
  expect(within(host).getByText("4 插槽 · 48 核 · 96 线程").getAttribute("title")).toBe("4 插槽 · 48 核 · 96 线程");
});

it("distinguishes unobserved disk capacity from observed zero and waits for the first host sample", () => {
  const snapshot = monitoringSnapshot({ memory: null, disks: [], gpus: [] });
  const view = render(<ServerMonitoringPage control={control({ snapshot })} />);
  const host = screen.getByRole("complementary", { name: "主机信息" });
  expect(Array.from(host.querySelectorAll(".sm-host-disk strong"), node => node.textContent)).toEqual(["--", "--"]);
  const observed = monitoringSnapshot();
  observed.disks[0].available_bytes = 0;
  observed.disks.push({ ...observed.disks[0], mount_point: "/data", label: "数据盘" });
  view.rerender(<ServerMonitoringPage control={control({ snapshot: observed })} />);
  expect(Array.from(host.querySelectorAll(".sm-host-disk strong"), node => node.textContent)).toEqual(["0.0 MiB", "0.0 MiB"]);
  view.rerender(<ServerMonitoringPage control={control({ snapshot: null })} />);
  expect(within(host).getByText("-- 插槽 · -- 核 · -- 线程")).toBeTruthy();
  expect(Array.from(host.querySelectorAll(".sm-host-disk strong"), node => node.textContent)).toEqual(["--", "--"]);
});

it("renders aggregate tasks initially, supports task-to-GPU navigation and restores all GPUs on server reset", () => {
  const first = monitoringSnapshot().gpus[0];
  const snapshot = monitoringSnapshot({ gpus: [first, { ...first, index: 3, uuid: "gpu-second" }] });
  const renderGpuTasks = vi.fn(({ gpuUuid, onSelectGpu }: { gpuUuid: string | null; onSelectGpu: (uuid: string) => void }) => <>
    <p data-testid="linked-gpu-tasks">{gpuUuid ?? "全部任务内容"}</p>
    {gpuUuid === null && <button onClick={() => onSelectGpu("gpu-second")}>查看汇总记录的 GPU 3</button>}
  </>);
  render(<ServerMonitoringPage control={control({ snapshot })} renderGpuTasks={renderGpuTasks} />);
  const tasks = screen.getByRole("complementary", { name: "GPU 任务" });
  expect(within(tasks).getByText("全部 GPU")).toBeTruthy();
  expect(within(tasks).getByTestId("linked-gpu-tasks").textContent).toBe("全部任务内容");
  expect(renderGpuTasks).toHaveBeenLastCalledWith(expect.objectContaining({ gpuUuid: null, gpus: snapshot.gpus }));
  fireEvent.click(screen.getByRole("button", { name: "查看GPU 0详情" }));
  expect(within(tasks).getByText("GPU 0")).toBeTruthy();
  expect(renderGpuTasks).toHaveBeenLastCalledWith(expect.objectContaining({ gpuUuid: first.uuid }));
  fireEvent.click(screen.getByRole("button", { name: "查看GPU 3详情" }));
  expect(within(tasks).getByText("GPU 3")).toBeTruthy();
  expect(within(tasks).getByTestId("linked-gpu-tasks").textContent).toBe("gpu-second");
  expect(renderGpuTasks).toHaveBeenLastCalledWith(expect.objectContaining({ gpuUuid: "gpu-second" }));
  renderGpuTasks.mockClear();
  fireEvent.click(screen.getByRole("button", { name: "查看当前服务器详情" }));
  expect(renderGpuTasks).toHaveBeenLastCalledWith(expect.objectContaining({ gpuUuid: null }));
  expect(within(tasks).getByTestId("linked-gpu-tasks").textContent).toBe("全部任务内容");
  fireEvent.click(screen.getByRole("button", { name: "GPU 显存" }));
  fireEvent.click(screen.getByRole("button", { name: "查看汇总记录的 GPU 3" }));
  expect(screen.getByRole("button", { name: "GPU 显存" }).getAttribute("aria-pressed")).toBe("true");
  expect(screen.getByRole("button", { name: "查看GPU 3详情" }).getAttribute("aria-pressed")).toBe("true");
  expect(screen.getByRole("combobox", { name: "选择趋势显卡" }).querySelector("strong")?.textContent).toBe("GPU 3");
  expect(within(tasks).getByTestId("linked-gpu-tasks").textContent).toBe("gpu-second");
  fireEvent.click(screen.getByRole("button", { name: "查看全部 GPU 任务" }));
  expect(screen.getByRole("button", { name: "GPU 显存" }).getAttribute("aria-pressed")).toBe("true");
  expect(screen.getByRole("combobox", { name: "选择趋势显卡" }).querySelector("strong")?.textContent).toBe("全部 GPU");
  expect(within(tasks).getByTestId("linked-gpu-tasks").textContent).toBe("全部任务内容");
  fireEvent.click(screen.getByRole("button", { name: "CPU / 内存" }));
  fireEvent.click(screen.getByRole("button", { name: "查看汇总记录的 GPU 3" }));
  fireEvent.click(screen.getByRole("button", { name: "查看全部 GPU 任务" }));
  expect(screen.getByRole("button", { name: "CPU / 内存" }).getAttribute("aria-pressed")).toBe("true");
  expect(within(tasks).getByText("全部 GPU")).toBeTruthy();
});

it("does not infer an empty task history and removes supplied task content when access is lost", () => {
  const view = render(<ServerMonitoringPage control={control()} />);
  fireEvent.click(screen.getByRole("button", { name: "查看GPU 0详情" }));
  const tasks = screen.getByRole("complementary", { name: "GPU 任务" });
  expect(tasks.textContent).toBe("GPU 任务GPU 0");
  const renderGpuTasks = vi.fn(() => <p>实际来源任务内容</p>);
  view.rerender(<ServerMonitoringPage control={control()} renderGpuTasks={renderGpuTasks} />);
  expect(within(tasks).getByText("实际来源任务内容")).toBeTruthy();
  renderGpuTasks.mockClear();
  view.rerender(<ServerMonitoringPage control={control({ access: "denied" })} renderGpuTasks={renderGpuTasks} />);
  expect(renderGpuTasks).not.toHaveBeenCalled();
  expect(screen.queryByText("实际来源任务内容")).toBeNull();
});

it("keeps the compact monitoring interface free of explanatory sampling copy", () => {
  render(<ServerMonitoringPage control={control()} />);
  expect(screen.queryByText(/每 30 秒记录|采样缺失处保留空缺|最近采样|快照 5 秒|本次采集起点|切换到后台时暂停|纵轴固定|切换显卡保留当前/)).toBeNull();
  expect(document.querySelector('.sm-sample-time, .sm-trend-footer, .sm-footer')).toBeNull();
  expect(screen.getByRole("button", { name: "刷新服务器监测" })).toBeTruthy();
  expect(within(screen.getByRole("region", { name: "服务器参数详情" })).getAllByRole("article")).toHaveLength(6);
  expect(screen.getByRole("group", { name: "趋势指标" })).toBeTruthy();
});
