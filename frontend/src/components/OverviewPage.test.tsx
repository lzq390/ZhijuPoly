// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useOverviewData } from "../hooks/useOverviewData";
import type { OverviewData, OverviewDataState, OverviewTask } from "../types/overview";
import { OverviewPage } from "./OverviewPage";
import { buildActivityDays, buildOverviewModel, dayKey } from "./overviewPresentation";

vi.mock("../hooks/useOverviewData", () => ({ useOverviewData: vi.fn() }));
const useData = vi.mocked(useOverviewData);
const stamp = "2026-11-05T05:00:00Z", yesterday = "2026-11-04T03:00:00Z";
function task(source: OverviewTask["source"], id: string, overrides: Partial<OverviewTask> = {}): OverviewTask {
  return { id: `${source}:${id}`, jobId: id, source, title: `计算 ${id}`, module: "monomerMdSimulation", status: "completed", statusLabel: "已完成", outcome: "completed", progress: 100, stage: "计算完成", createdAt: stamp, updatedAt: stamp, finishedAt: stamp, ...overrides };
}
function fixture(): OverviewData {
  return {
    asOf: "2026-11-05T08:00:00Z",
    structures: { status: "ready", data: [{ id: "s-1", name: "我的材料结构", createdAt: stamp, updatedAt: stamp }, { id: "s-2", name: "前日结构", createdAt: yesterday, updatedAt: stamp }] },
    taskSources: [
      { id: "monomerMd", label: "单体 MD", status: "ready", total: 2, tasks: [task("monomerMd", "md-done"), task("monomerMd", "md-running", { status: "running", statusLabel: "运行中", outcome: "active", progress: 42.25, createdAt: yesterday, finishedAt: null })] },
      { id: "monomerDft", label: "单体 DFT", status: "ready", total: 1, tasks: [task("monomerDft", "dft-failed", { status: "failed", statusLabel: "失败", outcome: "attention", progress: null })] },
      { id: "polymerMd", label: "聚合物 MD", status: "ready", total: 1, tasks: [task("polymerMd", "pmd-running", { status: "running", statusLabel: "运行中", outcome: "active", module: "mdSimulationDemo", progress: null, stage: "NPT 平衡", finishedAt: null })] },
      { id: "polymerizationBatch", label: "聚合批任务", status: "ready", total: 1, tasks: [task("polymerizationBatch", "batch-warning", { status: "completed_with_errors", statusLabel: "部分完成", outcome: "attention", progress: null, module: "monomerPolymerization" })] }
    ],
    knowledge: { status: "ready", data: { sources: [{ id: "legacy", label: "基础知识库", total: 11, enabled: true, ready: true }, { id: "extracted_literature", label: "提取文献", total: 7, enabled: true, ready: true }] } },
    knowledgeActivity: { status: "ready", data: { dailyAvailable: true, recentAvailable: true, daily: [{ date: "2026-11-05", count: 3 }, { date: "2026-11-04", count: 2 }], recent: [{ id: "knowledge:k1", title: "知识检索", status: "completed", statusLabel: "已完成", submittedAt: stamp, updatedAt: stamp }], warnings: [] } }
  };
}
const readState = (data: OverviewData | null, overrides: Partial<OverviewDataState> = {}): OverviewDataState => ({ data, access: "member", loading: false, refreshing: false, error: null, refresh: vi.fn(), ...overrides });
const metric = (title: string) => within(screen.getByRole("heading", { name: title }).closest("article")!);
beforeEach(() => { useData.mockReturnValue(readState(fixture())); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe("overview with platform data", () => {
  it("renders exact scoped counts and separates shared knowledge sources", () => {
    render(<OverviewPage onNavigate={vi.fn()} />);
    expect(metric("已记录计算任务").getByText("5")).toBeTruthy(); expect(metric("进行中计算").getByText("2")).toBeTruthy(); expect(metric("已保存结构").getByText("2")).toBeTruthy();
    expect(metric("知识库文献").getByText("7")).toBeTruthy(); expect(metric("知识库文献").getByText("另有 11 条知识")).toBeTruthy();
    expect(screen.queryByText(/示例数据|耐高温聚酰亚胺筛选|EP-036|NP-024/)).toBeNull(); expect(screen.queryByRole("button", { name: "全部标为已读" })).toBeNull();
  });
  it("changes the reporting window while retaining inventory and today's real progress", () => {
    render(<OverviewPage onNavigate={vi.fn()} />); fireEvent.click(screen.getByRole("button", { name: "近 30 天" }));
    expect(metric("已记录计算任务").getByText("近 30 天提交 5")).toBeTruthy(); expect(metric("已保存结构").getByText("2")).toBeTruthy();
    const daily = within(screen.getByRole("region", { name: "今日产出与计算进展" }));
    for (const label of ["今日完成", "运行中", "需关注"]) expect(daily.getByText(label, { selector: "dt" }).parentElement?.textContent).toContain("02");
    expect(daily.getByRole("progressbar", { name: "计算 md-running进度" }).getAttribute("aria-valuenow")).toBe("42.25"); expect(daily.getByText("42.3%")).toBeTruthy();
    expect(daily.getByText("NPT 平衡")).toBeTruthy(); expect(daily.queryByRole("progressbar", { name: /pmd/ })).toBeNull();
  });
  it("shows genuine zero and empty states only for complete empty lists", () => {
    const data = fixture(); data.structures.data = []; data.taskSources.forEach(source => { source.tasks = []; source.total = 0; }); data.knowledgeActivity.data!.daily = []; data.knowledgeActivity.data!.recent = [];
    useData.mockReturnValue(readState(data)); render(<OverviewPage onNavigate={vi.fn()} />);
    expect(metric("已记录计算任务").getByText("0")).toBeTruthy(); expect(metric("已保存结构").getByText("0")).toBeTruthy(); expect(screen.getByText("暂无计算任务")).toBeTruthy(); expect(screen.getByText("暂无研发动态")).toBeTruthy(); expect(screen.queryByRole("progressbar")).toBeNull();
  });
  it("does not turn unavailable or incomplete sources into a zero", () => {
    const data = fixture(); data.structures = { status: "unavailable", data: null }; data.taskSources[0].status = "partial"; data.knowledge = { status: "unavailable", data: null };
    useData.mockReturnValue(readState(data)); render(<OverviewPage onNavigate={vi.fn()} />);
    for (const name of ["已记录计算任务", "进行中计算", "已保存结构", "知识库条目"]) expect(metric(name).getByText("—")).toBeTruthy();
    expect(screen.getByText("部分数据可用")).toBeTruthy(); expect(screen.getByRole("status").textContent).toContain("缺失统计以 — 显示"); expect(screen.getByLabelText("当日分类统计").textContent).toContain("结构 —"); expect(screen.getByLabelText("当日分类统计").textContent).toContain("计算 —");
  });
  it("marks recent-knowledge failure as partial while retaining daily knowledge counts", () => {
    const data = fixture(); data.knowledgeActivity.status = "partial"; data.knowledgeActivity.data!.recentAvailable = false; data.knowledgeActivity.data!.recent = [];
    useData.mockReturnValue(readState(data)); render(<OverviewPage onNavigate={vi.fn()} />); expect(screen.getByText("部分数据可用")).toBeTruthy(); expect(screen.getByLabelText("当日分类统计").textContent).toContain("检索 3");
  });
  it("offers sign in to guests and hides all private counts", () => {
    useData.mockReturnValue(readState(null, { access: "guest" })); const login = vi.fn(); window.addEventListener("nexpoly:login-required", login);
    try { render(<OverviewPage onNavigate={vi.fn()} />); expect(metric("已保存结构").getByText("—")).toBeTruthy(); fireEvent.click(screen.getByRole("button", { name: "登录账号" })); expect(login).toHaveBeenCalledOnce(); expect(screen.queryByLabelText(/过去一年每日研究活动/)).toBeNull(); } finally { window.removeEventListener("nexpoly:login-required", login); }
  });
  it("retains same-session data on refresh failure and offers retry", () => {
    const refresh = vi.fn(); useData.mockReturnValue(readState(fixture(), { error: "network failed", refresh })); const view = render(<OverviewPage onNavigate={vi.fn()} />);
    expect(screen.getByText("刷新未完成，正在保留上次可用数据。")).toBeTruthy(); expect(metric("已保存结构").getByText("2")).toBeTruthy(); fireEvent.click(screen.getByRole("button", { name: "重新读取" })); expect(refresh).toHaveBeenCalledOnce();
    useData.mockReturnValue(readState(null, { loading: true })); view.rerender(<OverviewPage onNavigate={vi.fn()} />); expect(screen.getByTestId("overview-page").getAttribute("aria-busy")).toBe("true"); expect(metric("已记录计算任务").getByText("—")).toBeTruthy();
  });
  it("opens real details and restores focus on Escape", async () => {
    render(<OverviewPage onNavigate={vi.fn()} />); const trigger = within(screen.getByRole("region", { name: "最近计算任务" })).getByRole("button", { name: /计算 md-running/ }); act(() => trigger.focus()); fireEvent.click(trigger);
    const dialog = screen.getByRole("dialog", { name: "计算 md-running" }); expect(within(dialog).getByText("md-running")).toBeTruthy(); expect(within(dialog).getByText("42.3%")).toBeTruthy();
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: "关闭详情" }))); fireEvent.keyDown(document, { key: "Escape" }); expect(screen.queryByRole("dialog")).toBeNull(); await waitFor(() => expect(document.activeElement).toBe(trigger));
  });
  it("removes private details immediately when the session snapshot disappears", () => {
    const view = render(<OverviewPage onNavigate={vi.fn()} />); fireEvent.click(within(screen.getByRole("region", { name: "最近计算任务" })).getByRole("button", { name: /计算 md-running/ })); expect(screen.getByRole("dialog")).toBeTruthy();
    useData.mockReturnValue(readState(null, { loading: true })); view.rerender(<OverviewPage onNavigate={vi.fn()} />); expect(screen.queryByRole("dialog")).toBeNull(); expect(screen.queryByText("md-running")).toBeNull();
  });
  it("navigates from plan entries and explicit task-detail action", () => {
    const onNavigate = vi.fn(); render(<OverviewPage onNavigate={onNavigate} />); fireEvent.click(screen.getByRole("button", { name: "继续结构设计" })); fireEvent.click(screen.getByRole("button", { name: "补充文献线索" })); expect(onNavigate.mock.calls).toEqual([["structureWorkbench"], ["knowledge"]]);
    fireEvent.click(within(screen.getByRole("region", { name: "最近计算任务" })).getByRole("button", { name: /计算 pmd-running/ })); fireEvent.click(screen.getByRole("button", { name: "打开计算模块" })); expect(onNavigate).toHaveBeenLastCalledWith("mdSimulationDemo"); expect(screen.queryByRole("dialog")).toBeNull();
  });
  it("filters real activity without losing the date or keyboard navigation", () => {
    render(<OverviewPage onNavigate={vi.fn()} />); const today = screen.getByRole("button", { name: "2026-11-05，8 次研究活动" }); expect(today.getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(screen.getByRole("combobox", { name: "活动来源" })); fireEvent.click(screen.getByRole("option", { name: "计算任务" })); expect(screen.getByRole("button", { name: "2026-11-05，4 次任务提交" })).toBe(today);
    act(() => today.focus()); fireEvent.keyDown(today, { key: "ArrowUp" }); const previous = screen.getByRole("button", { name: "2026-11-04，1 次任务提交" }); expect(document.activeElement).toBe(previous); expect(previous.getAttribute("aria-pressed")).toBe("true");
    expect(within(screen.getByLabelText(/过去一年每日研究活动/)).getAllByRole("button").filter(button => button.tabIndex === 0)).toEqual([previous]);
  });
  it("filters task activity before limiting the displayed recent events", () => {
    const data = fixture();
    data.structures.data = Array.from({ length: 31 }, (_, index) => ({ id: `newer-${index}`, name: `较新的结构 ${index}`, createdAt: "2026-11-05T07:00:00Z", updatedAt: stamp }));
    useData.mockReturnValue(readState(data)); render(<OverviewPage onNavigate={vi.fn()} />);
    const activity = within(screen.getByRole("region", { name: "近期动态" }));
    expect(activity.getAllByText("保存了新的材料结构")).toHaveLength(3);
    fireEvent.click(activity.getByRole("button", { name: "计算任务" }));
    expect(activity.getByText("计算 md-done · 已完成")).toBeTruthy();
    expect(activity.queryByText("暂无研发动态")).toBeNull();
  });
  it.each([".ov-scroll", ".ov-heatmap-scroll"])("hides the tooltip on %s scroll without moving focus", selector => {
    const view = render(<OverviewPage onNavigate={vi.fn()} />); const today = screen.getByRole("button", { name: "2026-11-05，8 次研究活动" }); act(() => today.focus()); expect(screen.getByRole("tooltip")).toBeTruthy(); fireEvent.scroll(view.container.querySelector(selector)!); expect(screen.queryByRole("tooltip")).toBeNull(); expect(document.activeElement).toBe(today);
  });
  it("dismisses source selection with Escape without changing the filter", () => {
    render(<OverviewPage onNavigate={vi.fn()} />); const source = screen.getByRole("combobox", { name: "活动来源" }); act(() => source.focus()); fireEvent.keyDown(source, { key: "ArrowDown" }); fireEvent.keyDown(source, { key: "ArrowDown" }); fireEvent.keyDown(source, { key: "Escape" }); expect(source.getAttribute("aria-expanded")).toBe("false"); expect(source.textContent).toContain("全部活动"); expect(document.activeElement).toBe(source);
  });
});

describe("overview statistical boundaries", () => {
  it("uses Beijing days and separates submissions from completions", () => {
    expect(dayKey("2026-11-04T15:59:59Z")).toBe("2026-11-04"); expect(dayKey("2026-11-04T16:00:00Z")).toBe("2026-11-05"); const model = buildOverviewModel(fixture(), 7); expect(model.completedToday).toBe(2); expect(model.attention).toHaveLength(2); expect(buildActivityDays(model, "1").find(day => day.key === "2026-11-05")?.count).toBe(4);
  });
  it("does not invent a zero history when task timestamps are missing", () => {
    const data = fixture(); data.taskSources[0].tasks[0].createdAt = null; data.taskSources[0].tasks[0].finishedAt = null; const model = buildOverviewModel(data, 7); expect(model.totalTasks).toBe(5); expect(model.completedToday).toBeNull(); expect(model.submittedInPeriod).toBeNull(); expect(model.taskSeries).toBeNull(); expect(buildActivityDays(model, "1").find(day => day.inRange)?.count).toBeNull();
  });
});
