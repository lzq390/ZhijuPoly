// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { gpuActivityItem, gpuHistoricalTask, gpuTaskHistory } from "../../test/gpuTaskHistoryFixture";
import { GpuTaskHistory } from "./GpuTaskHistory";

let fetcher: ReturnType<typeof vi.fn>;
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
const flush = async () => { await act(async () => { await Promise.resolve(); await Promise.resolve(); }); };
const advance = async (ms: number) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); };
const runningTask = (changes: Parameters<typeof gpuActivityItem>[0] = {}) => gpuActivityItem({ kind: "task", status: "running", label: "DFT任务", ...changes });
beforeEach(() => {
  vi.stubEnv("VITE_SERVER_MONITORING_ENABLED", "true");
  Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
  fetcher = vi.fn(async () => json(gpuTaskHistory())); vi.stubGlobal("fetch", fetcher);
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); vi.unstubAllEnvs(); });

it("switches UUIDs immediately within the cached response without mixing cards or refetching", async () => {
  fetcher.mockImplementation(async () => json(gpuTaskHistory({ tasks: [gpuHistoricalTask(), gpuHistoricalTask({ id: "1234567890abcdef1234", gpu_uuid: "gpu-second", environment: "production", calculation_type: "optimization" })] })));
  const view = render(<GpuTaskHistory gpuUuid="gpu-first" />); await flush();
  expect(screen.getByText("DFT 单点能")).toBeTruthy(); expect(screen.queryByText("DFT 结构优化")).toBeNull(); expect(screen.getByText("开发")).toBeTruthy();
  view.rerender(<GpuTaskHistory gpuUuid="gpu-second" />);
  expect(screen.getByText("DFT 结构优化")).toBeTruthy(); expect(screen.queryByText("DFT 单点能")).toBeNull(); expect(screen.getByText("生产")).toBeTruthy();
  expect(fetcher).toHaveBeenCalledTimes(1);
  view.rerender(<GpuTaskHistory gpuUuid="unknown-gpu" />);
  expect(screen.getByText("暂无可关联的历史记录")).toBeTruthy(); expect(screen.queryByRole("listitem")).toBeNull();
});

it("shows the two newest verified completed records with timestamps and duration", async () => {
  const tasks = Array.from({ length: 7 }, (_, index) => gpuHistoricalTask({ id: index.toString(16).padStart(20, "0"), started_at: "2026-11-11T00:00:00Z", finished_at: `2026-11-11T00:${53 + index}:00Z`, duration_seconds: (53 + index) * 60 }));
  fetcher.mockImplementation(async () => json(gpuTaskHistory({ tasks })));
  render(<GpuTaskHistory gpuUuid="gpu-first" />); await flush();
  const rows = screen.getAllByRole("listitem"); expect(rows).toHaveLength(2);
  expect(rows.map(row => row.querySelector("time")?.getAttribute("datetime"))).toEqual([59, 58].map(minute => `2026-11-11T00:${minute}:00Z`));
  expect(within(rows[0]).getByText("耗时 59 分 0 秒")).toBeTruthy();
  expect(screen.queryByText(/最近观测|可核验 GPU 归属|历史扫描|活动采样/)).toBeNull();
});

it("labels an empty verified source as no associated records rather than no server activity", async () => {
  fetcher.mockImplementation(async () => json(gpuTaskHistory({ tasks: [] })));
  render(<GpuTaskHistory gpuUuid="gpu-first" />); await flush();
  expect(screen.getByText("暂无可关联的历史记录")).toBeTruthy();
  expect(screen.queryByText("没有任务")).toBeNull();
});

it.each(["partial", "unavailable"] as const)("does not turn %s sources into an empty-history claim", async status => {
  fetcher.mockImplementation(async () => json(gpuTaskHistory({ status, tasks: [], sources: [{ source_id: "development_dft", status, matched_tasks: 0, excluded_tasks: 0, error_code: "source_failed" }] })));
  render(<GpuTaskHistory gpuUuid="gpu-first" />); await flush();
  expect(screen.queryByText("暂无可关联的历史记录")).toBeNull();
  expect(within(screen.getByRole("region", { name: "近期完成任务" })).getByRole("status").textContent).toBe("--");
  expect(screen.queryByText(/暂不可用|读取失败|仅展示已核验/)).toBeNull();
  expect(screen.queryByRole("button", { name: "重新读取" })).toBeNull();
});

it("shows verified records quietly when another source fails", async () => {
  const data = gpuTaskHistory({ status: "partial" });
  data.sources[1].status = "unavailable";
  fetcher.mockImplementation(async () => json(data));
  render(<GpuTaskHistory gpuUuid="gpu-first" />); await flush();
  expect(screen.getByText("DFT 单点能")).toBeTruthy();
  expect(screen.queryByText(/部分历史任务来源|仅展示已核验/)).toBeNull();
  expect(screen.queryByRole("button", { name: "重新读取" })).toBeNull();
});

it("uses neutral placeholders during transport failures without inventing an empty result", async () => {
  fetcher.mockImplementation(async () => json({ code: "monitoring_unavailable" }, 503));
  render(<GpuTaskHistory gpuUuid="gpu-first" />); await flush();
  expect(screen.getAllByText("--")).toHaveLength(2);
  expect(screen.queryByText(/GPU 任务暂时读取失败|暂无可关联的历史记录|暂无正在进行的任务/)).toBeNull();
  expect(screen.queryByRole("button", { name: "重新读取" })).toBeNull();
});

it("offers a working retry after an isolated authentication outage", async () => {
  fetcher.mockResolvedValueOnce(json({ code: "authentication_unavailable" }, 503));
  render(<GpuTaskHistory gpuUuid="gpu-first" />); await flush();
  expect(screen.getByText(/暂时无法确认访问权限/)).toBeTruthy(); expect(screen.queryByRole("listitem")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "重新读取" })); await flush();
  expect(fetcher).toHaveBeenCalledTimes(2); expect(screen.getByText("DFT 单点能")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "重新读取" })).toBeNull();
});

it("shows only verified running tasks and hides resident services and observed GPU processes", async () => {
  const data = gpuTaskHistory();
  data.activity.items = [
    gpuActivityItem({ id: "11111111111111111111", kind: "task", status: "running", module: "monomer_md", label: "单体 MD 正式任务", memory_used_bytes: null }),
    gpuActivityItem({ id: "22222222222222222222", module: "nvidia_mps", environment: "unknown", label: "NVIDIA MPS" }),
    gpuActivityItem({ id: "33333333333333333333", kind: "process", status: "observed", module: "other", environment: "unknown", label: "GPU 计算进程", started_at: null })
  ];
  fetcher.mockImplementation(async () => json(data));
  render(<GpuTaskHistory gpuUuid="gpu-first" />); await flush();
  const current = within(screen.getByRole("region", { name: "当前正在进行" }));
  expect(current.getAllByRole("listitem")).toHaveLength(1);
  expect(current.getByText("任务运行中")).toBeTruthy(); expect(current.queryByText("常驻服务")).toBeNull(); expect(current.queryByText("GPU 进程")).toBeNull();
  expect(current.getByText("显存 —")).toBeTruthy(); expect(current.queryByText("显存 1.0 GiB")).toBeNull();
  expect(current.queryByText("NVIDIA MPS")).toBeNull(); expect(current.queryByText("GPU 计算进程")).toBeNull();
});

it("shows an empty task state when a GPU has only resident services", async () => {
  const data = gpuTaskHistory(); data.activity.items = [gpuActivityItem(), gpuActivityItem({ id: "eeeeeeeeeeeeeeeeeeee", module: "platform", label: "平台服务" })];
  fetcher.mockImplementation(async () => json(data));
  render(<GpuTaskHistory gpuUuid="gpu-first" />); await flush();
  const current = within(screen.getByRole("region", { name: "当前正在进行" }));
  expect(current.getByText("暂无正在进行的任务")).toBeTruthy(); expect(current.queryByRole("listitem")).toBeNull();
  expect(screen.queryByText("平台服务")).toBeNull(); expect(screen.queryByText("DFT 工作进程")).toBeNull();
});

it("filters current activity and completed history immediately when the GPU UUID changes", async () => {
  const data = gpuTaskHistory({ tasks: [gpuHistoricalTask(), gpuHistoricalTask({ id: "bbbbbbbbbbbbbbbbbbbb", gpu_uuid: "gpu-second", module: "monomer_md", calculation_type: "md_formal" })] });
  data.activity.items = [runningTask(), runningTask({ id: "cccccccccccccccccccc", gpu_uuid: "gpu-second", label: "MD任务", module: "monomer_md", environment: "production" })];
  fetcher.mockImplementation(async () => json(data));
  const view = render(<GpuTaskHistory gpuUuid="gpu-first" />); await flush();
  expect(screen.getByText("DFT任务")).toBeTruthy(); expect(screen.getByText("DFT 单点能")).toBeTruthy();
  view.rerender(<GpuTaskHistory gpuUuid="gpu-second" />);
  expect(screen.getByText("MD任务")).toBeTruthy(); expect(screen.getByText("单体 MD · 正式")).toBeTruthy();
  expect(screen.queryByText("DFT任务")).toBeNull(); expect(screen.queryByText("DFT 单点能")).toBeNull(); expect(fetcher).toHaveBeenCalledTimes(1);
});

it.each([
  ["monomer_dft", "single_point", "DFT 单点能"], ["monomer_dft", "optimization", "DFT 结构优化"],
  ["monomer_md", "md_formal", "单体 MD · 正式"], ["monomer_md", "md_demo", "单体 MD · 演示"],
  ["polymer_md", "polymer_md", "聚合物 MD"]
] as const)("labels a verified %s/%s record without inventing coverage or current activity", async (module, calculation_type, label) => {
  const data = gpuTaskHistory({ tasks: [gpuHistoricalTask({ module, calculation_type })] });
  data.sources[4].excluded_tasks = 42;
  fetcher.mockImplementation(async () => json(data));
  render(<GpuTaskHistory gpuUuid="gpu-first" />); await flush();
  expect(within(screen.getByRole("list", { name: "历史任务记录" })).getByText(label)).toBeTruthy();
  expect(screen.getByText("暂无正在进行的任务")).toBeTruthy();
  expect(screen.queryByText(/部分来源或记录/)).toBeNull(); expect(screen.queryByText("没有任务")).toBeNull();
});

it("keeps fresh activity usable when completed-history sources fail", async () => {
  const data = gpuTaskHistory({ status: "unavailable", tasks: [] });
  data.activity.items = [runningTask()];
  fetcher.mockImplementation(async () => json(data));
  render(<GpuTaskHistory gpuUuid="gpu-first" />); await flush();
  const current = within(screen.getByRole("region", { name: "当前正在进行" }));
  expect(current.getByText("DFT任务")).toBeTruthy(); expect(current.getByText("任务运行中")).toBeTruthy(); expect(current.queryByRole("status")).toBeNull();
  expect(within(screen.getByRole("region", { name: "近期完成任务" })).getByText("--")).toBeTruthy();
  expect(screen.queryByText("暂无可关联的历史记录")).toBeNull();
});

it("keeps completed records usable when current GPU activity cannot be read", async () => {
  const data = gpuTaskHistory(); data.activity.status = "unavailable"; data.activity.error_code = "process_query_failed";
  fetcher.mockImplementation(async () => json(data));
  render(<GpuTaskHistory gpuUuid="gpu-first" />); await flush();
  expect(within(screen.getByRole("region", { name: "当前正在进行" })).getByText("--")).toBeTruthy(); expect(screen.getByText("DFT 单点能")).toBeTruthy();
  expect(screen.queryByText(/读取失败/)).toBeNull();
  expect(screen.queryByText("暂无正在进行的任务")).toBeNull();
});

it("leaves current activity visible and uses a neutral placeholder for omitted history sources", async () => {
  const data = gpuTaskHistory({ tasks: [] }); data.sources.pop(); data.activity.items = [runningTask()];
  fetcher.mockImplementation(async () => json(data));
  render(<GpuTaskHistory gpuUuid="gpu-first" />); await flush();
  expect(within(screen.getByRole("region", { name: "近期完成任务" })).getByText("--")).toBeTruthy(); expect(screen.getByText("DFT任务")).toBeTruthy();
  expect(screen.queryByText(/部分来源或记录暂不可用/)).toBeNull();
  expect(screen.queryByText("暂无可关联的历史记录")).toBeNull();
});

it("does not claim a cached running task is still running after its activity sample expires", async () => {
  const data = gpuTaskHistory({ age_seconds: 61, freshness: "unavailable" });
  data.activity.items = [gpuActivityItem({ kind: "task", status: "running" })];
  fetcher.mockImplementation(async () => json(data));
  render(<GpuTaskHistory gpuUuid="gpu-first" />); await flush();
  expect(screen.getByText("最近运行任务")).toBeTruthy(); expect(screen.queryByText("任务运行中")).toBeNull();
  expect(screen.queryByText(/已超过有效期|最近观测/)).toBeNull();
  expect(screen.queryByRole("button", { name: "重新读取" })).toBeNull();
  expect(within(screen.getByRole("region", { name: "近期完成任务" })).queryByRole("status")).toBeNull();
});

it("shows only the two most recent completions across DFT and MD by their actual timestamps", async () => {
  fetcher.mockImplementation(async () => json(gpuTaskHistory({ tasks: [
    gpuHistoricalTask({ id: "11111111111111111111", module: "monomer_md", calculation_type: "md_demo", started_at: "2026-11-11T00:00:00Z", finished_at: "2026-11-11T00:57:00Z", duration_seconds: 3420 }),
    gpuHistoricalTask({ id: "22222222222222222222", started_at: "2026-11-11T00:00:00Z", finished_at: "2026-11-11T00:58:00Z", duration_seconds: 3480 }),
    gpuHistoricalTask({ id: "33333333333333333333", module: "monomer_md", calculation_type: "md_formal", started_at: "2026-11-11T00:00:00Z", finished_at: "2026-11-11T00:59:00Z", duration_seconds: 3540 }),
    gpuHistoricalTask({ id: "44444444444444444444", gpu_uuid: "gpu-second", calculation_type: "optimization", started_at: "2026-11-11T00:00:00Z", finished_at: "2026-11-11T01:00:00Z", duration_seconds: 3600 })
  ] })));
  const view = render(<GpuTaskHistory gpuUuid="gpu-first" />); await flush();
  const rows = within(screen.getByRole("region", { name: "近期完成任务" })).getAllByRole("listitem");
  expect(rows).toHaveLength(2); expect(within(rows[0]).getByText("单体 MD · 正式")).toBeTruthy(); expect(within(rows[1]).getByText("DFT 单点能")).toBeTruthy();
  expect(screen.queryByText("单体 MD · 演示")).toBeNull(); expect(screen.queryByRole("combobox")).toBeNull();
  view.rerender(<GpuTaskHistory gpuUuid="gpu-second" />);
  expect(screen.getByText("DFT 结构优化")).toBeTruthy(); expect(screen.queryByText("单体 MD · 正式")).toBeNull(); expect(fetcher).toHaveBeenCalledTimes(1);
});

it.each(["cross-gpu", "same-gpu"])("takes the global newest two completions without per-GPU quotas: %s", distribution => {
  const latestGpu = distribution === "cross-gpu" ? "gpu-second" : "gpu-first";
  const tasks = [
    gpuHistoricalTask({ id: "11111111111111111111", gpu_uuid: "gpu-second", module: "monomer_md", calculation_type: "md_demo", started_at: "2026-11-11T00:00:00Z", finished_at: "2026-11-11T00:57:00Z", duration_seconds: 3420 }),
    gpuHistoricalTask({ id: "22222222222222222222", gpu_uuid: "gpu-first", started_at: "2026-11-11T00:00:00Z", finished_at: "2026-11-11T00:58:00Z", duration_seconds: 3480 }),
    gpuHistoricalTask({ id: "33333333333333333333", gpu_uuid: latestGpu, module: "monomer_md", calculation_type: "md_formal", started_at: "2026-11-11T00:00:00Z", finished_at: "2026-11-11T00:59:00Z", duration_seconds: 3540 }),
  ];
  fetcher.mockImplementation(async () => json(gpuTaskHistory({ tasks })));
  return (async () => {
    render(<GpuTaskHistory gpuUuid={null} gpus={[{ uuid: "gpu-first", index: 2 }, { uuid: "gpu-second", index: 7 }]} onSelectGpu={vi.fn()} />); await flush();
    const rows = within(screen.getByRole("region", { name: "近期完成任务" })).getAllByRole("listitem");
    expect(rows).toHaveLength(2);
    expect(within(rows[0]).getByText("单体 MD · 正式")).toBeTruthy(); expect(within(rows[1]).getByText("DFT 单点能")).toBeTruthy();
    expect(within(rows[0]).getByRole("button", { name: distribution === "cross-gpu" ? "切换到 GPU 7" : "切换到 GPU 2" })).toBeTruthy();
    expect(within(rows[1]).getByRole("button", { name: "切换到 GPU 2" })).toBeTruthy();
    expect(screen.queryByText("单体 MD · 演示")).toBeNull();
  })();
});

it("switches all and single GPU views from the same cache without recreating its request lifecycle", async () => {
  const data = gpuTaskHistory({ tasks: [gpuHistoricalTask(), gpuHistoricalTask({ id: "11111111111111111111", gpu_uuid: "gpu-second", calculation_type: "optimization" })] });
  data.activity.items = [runningTask(), runningTask({ id: "bbbbbbbbbbbbbbbbbbbb", gpu_uuid: "gpu-second", label: "MD任务", module: "monomer_md" }),
    gpuActivityItem({ id: "cccccccccccccccccccc", label: "常驻平台服务", module: "platform" }),
    gpuActivityItem({ id: "dddddddddddddddddddd", kind: "process", status: "observed", label: "GPU进程", module: "other", environment: "unknown", gpu_uuid: "gpu-second" })];
  fetcher.mockImplementation(async () => json(data));
  const gpus = [{ uuid: "gpu-first", index: 0 }, { uuid: "gpu-second", index: 3 }];
  const view = render(<GpuTaskHistory gpuUuid={null} gpus={gpus} onSelectGpu={vi.fn()} />); await flush();
  for (const gpuUuid of [null, "gpu-first", null, "gpu-second", null]) {
    view.rerender(<GpuTaskHistory gpuUuid={gpuUuid} gpus={gpus} onSelectGpu={vi.fn()} />);
    expect(within(screen.getByRole("region", { name: "当前正在进行" })).getAllByRole("listitem")).toHaveLength(gpuUuid === null ? 2 : 1);
    expect(within(screen.getByRole("region", { name: "近期完成任务" })).getAllByRole("listitem")).toHaveLength(gpuUuid === null ? 2 : 1);
    expect(screen.getAllByText("任务运行中")).toHaveLength(gpuUuid === null ? 2 : 1);
    expect(screen.queryByText("常驻平台服务")).toBeNull(); expect(screen.queryByText("GPU进程")).toBeNull();
    if (gpuUuid !== null) expect(screen.queryByRole("button", { name: /切换到 GPU/ })).toBeNull();
  }
  expect(fetcher).toHaveBeenCalledTimes(1);
});

it("maps aggregate task links to actual physical GPU indices and returns the corresponding UUID", async () => {
  const data = gpuTaskHistory({ tasks: [gpuHistoricalTask({ gpu_uuid: "gpu-second" })] });
  data.activity.items = [runningTask()];
  fetcher.mockImplementation(async () => json(data));
  const select = vi.fn();
  const view = render(<GpuTaskHistory gpuUuid={null} gpus={[{ uuid: "gpu-second", index: 7 }, { uuid: "gpu-first", index: 2 }]} onSelectGpu={select} />); await flush();
  fireEvent.click(within(screen.getByRole("region", { name: "当前正在进行" })).getByRole("button", { name: "切换到 GPU 2" }));
  fireEvent.click(within(screen.getByRole("region", { name: "近期完成任务" })).getByRole("button", { name: "切换到 GPU 7" }));
  expect(select.mock.calls).toEqual([["gpu-first"], ["gpu-second"]]);
  expect(view.container.textContent).not.toMatch(/gpu-first|gpu-second/);
  expect(fetcher).toHaveBeenCalledTimes(1);
});

it("keeps an unmapped or removed GPU's record as a noninteractive historical reference", async () => {
  const select = vi.fn();
  const view = render(<GpuTaskHistory gpuUuid={null} gpus={[{ uuid: "gpu-first", index: 4 }]} onSelectGpu={select} />); await flush();
  expect(screen.getByRole("button", { name: "切换到 GPU 4" })).toBeTruthy();
  view.rerender(<GpuTaskHistory gpuUuid={null} gpus={[{ uuid: "gpu-second", index: 1 }]} onSelectGpu={select} />);
  const history = within(screen.getByRole("region", { name: "近期完成任务" }));
  expect(history.getByText("历史 GPU").tagName).toBe("SPAN"); expect(history.getByText("DFT 单点能")).toBeTruthy();
  expect(history.queryByRole("button")).toBeNull(); expect(history.queryByText("暂无可关联的历史记录")).toBeNull();
  fireEvent.click(history.getByText("历史 GPU")); expect(select).not.toHaveBeenCalled();
  expect(view.container.textContent).not.toContain("gpu-first"); expect(fetcher).toHaveBeenCalledTimes(1);
  view.rerender(<GpuTaskHistory gpuUuid={null} onSelectGpu={select} />);
  expect(history.getByText("历史 GPU")).toBeTruthy();
});

it.each(["monitoring_unavailable", "authentication_unavailable"])("does not mistake an aggregate request error for empty task groups: %s", async code => {
  fetcher.mockImplementation(async () => json({ code }, 503));
  render(<GpuTaskHistory gpuUuid={null} />); await flush();
  expect(screen.queryByText("暂无正在进行的任务")).toBeNull(); expect(screen.queryByText("暂无可关联的历史记录")).toBeNull();
  expect(screen.queryByRole("listitem")).toBeNull(); expect(screen.getAllByText("--")).toHaveLength(2);
  expect(Boolean(screen.queryByRole("button", { name: "重新读取" }))).toBe(code === "authentication_unavailable");
});

it("shows genuine aggregate empty states only after a successful read", async () => {
  fetcher.mockImplementation(async () => json(gpuTaskHistory({ tasks: [] })));
  render(<GpuTaskHistory gpuUuid={null} />); await flush();
  expect(screen.getByText("暂无正在进行的任务")).toBeTruthy(); expect(screen.getByText("暂无可关联的历史记录")).toBeTruthy();
  expect(screen.queryByRole("listitem")).toBeNull();
});

it("keeps the last successful tasks through a transport failure and recovers by background polling", async () => {
  vi.useFakeTimers();
  const data = gpuTaskHistory(); data.activity.items = [runningTask()];
  fetcher.mockResolvedValueOnce(json(data)).mockRejectedValueOnce(new TypeError("Failed to fetch"))
    .mockResolvedValueOnce(json(gpuTaskHistory({ tasks: [gpuHistoricalTask({ calculation_type: "optimization" })] })));
  render(<GpuTaskHistory gpuUuid={null} />); await flush();
  expect(screen.getByText("任务运行中")).toBeTruthy(); expect(screen.getByText("DFT 单点能")).toBeTruthy();
  await advance(5_000);
  expect(screen.getByText("DFT任务")).toBeTruthy(); expect(screen.getByText("DFT 单点能")).toBeTruthy();
  expect(screen.getByText("最近运行任务")).toBeTruthy(); expect(screen.queryByText("任务运行中")).toBeNull();
  expect(screen.queryByText(/读取失败|未更新|有效期|仅展示已核验/)).toBeNull();
  expect(screen.queryByRole("button", { name: "重新读取" })).toBeNull();
  await advance(5_000);
  expect(fetcher).toHaveBeenCalledTimes(3); expect(screen.getByText("DFT 结构优化")).toBeTruthy();
  expect(screen.queryByText("DFT 单点能")).toBeNull(); expect(screen.queryByText("DFT任务")).toBeNull();
  expect(screen.getByText("暂无正在进行的任务")).toBeTruthy();
});

it("does not present a cached empty activity sample as a confirmed absence of current tasks", async () => {
  vi.useFakeTimers();
  fetcher.mockResolvedValueOnce(json(gpuTaskHistory({ tasks: [] }))).mockRejectedValueOnce(new TypeError("Failed to fetch"));
  render(<GpuTaskHistory gpuUuid={null} />); await flush();
  expect(screen.getByText("暂无正在进行的任务")).toBeTruthy(); expect(screen.getByText("暂无可关联的历史记录")).toBeTruthy();
  await advance(5_000);
  expect(screen.queryByText("暂无正在进行的任务")).toBeNull(); expect(screen.queryByText("暂无可关联的历史记录")).toBeNull();
  expect(screen.getAllByText("--")).toHaveLength(2); expect(screen.queryByRole("button", { name: "重新读取" })).toBeNull();
});

it.each(["partial", "unavailable"] as const)("keeps the last successful rows without a verification banner when a later response is %s", async status => {
  vi.useFakeTimers();
  const data = gpuTaskHistory(); data.activity.items = [runningTask()];
  const incomplete = gpuTaskHistory({ status, tasks: [], captured_at: "2026-11-11T01:00:05Z" });
  incomplete.activity = { captured_at: incomplete.captured_at, status, error_code: "verification_failed", items: [] };
  fetcher.mockResolvedValueOnce(json(data)).mockResolvedValueOnce(json(incomplete));
  render(<GpuTaskHistory gpuUuid={null} />); await flush();
  await advance(5_000);
  expect(screen.getByText("DFT任务")).toBeTruthy(); expect(screen.getByText("DFT 单点能")).toBeTruthy();
  expect(screen.getByText("最近运行任务")).toBeTruthy(); expect(screen.queryByText("任务运行中")).toBeNull();
  expect(screen.queryByText(/读取失败|无法核验|已核验记录|暂不可用/)).toBeNull();
  expect(screen.queryByRole("button", { name: "重新读取" })).toBeNull();
});

it("clears previously visible tasks on authentication failure and requires a new authorized response to restore them", async () => {
  vi.useFakeTimers();
  const data = gpuTaskHistory(); data.activity.items = [runningTask()];
  fetcher.mockResolvedValueOnce(json(data)).mockResolvedValueOnce(json({ code: "authentication_unavailable" }, 503));
  render(<GpuTaskHistory gpuUuid={null} />); await flush();
  expect(screen.getByText("DFT任务")).toBeTruthy(); expect(screen.getByText("DFT 单点能")).toBeTruthy();
  await advance(5_000);
  expect(screen.queryByRole("listitem")).toBeNull(); expect(screen.queryByText("DFT任务")).toBeNull();
  expect(screen.getByText(/暂时无法确认访问权限/)).toBeTruthy();
  let complete: (response: Response) => void = () => {};
  fetcher.mockImplementationOnce(() => new Promise<Response>(resolve => { complete = resolve; }));
  fireEvent.click(screen.getByRole("button", { name: "重新读取" })); await flush();
  expect(screen.queryByRole("listitem")).toBeNull(); expect(screen.getByRole("button", { name: "正在读取…" }).hasAttribute("disabled")).toBe(true);
  await act(async () => { complete(json(gpuTaskHistory())); });
  expect(screen.getByText("DFT 单点能")).toBeTruthy(); expect(screen.queryByRole("button", { name: "重新读取" })).toBeNull();
  expect(screen.queryByText(/暂时无法确认访问权限/)).toBeNull();
});
