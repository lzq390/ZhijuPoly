// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { UsageActivity, UsageActivityRecord } from "../../types/usage";
import rawFixture from "../../../scripts/fixtures/entitlements.json";
import { UsageRequestError } from "../../services/usage";
import { PersonalEntitlementsPanel } from "./PersonalEntitlementsPanel";
import { UsageActivityPanel } from "./UsageActivityPanel";
const moduleGroups = rawFixture.operations.module_groups;
const fixture = rawFixture;
const staticWarning = "末次观测状态不保证进程已终止；无法确认的任务等待核实。";
const degradedWarning = "计量写入出现异常，部分终态尚待补记；当前数据可能不完整。";
const mocks = vi.hoisted(() => ({ activity: vi.fn(), balance: vi.fn(), operations: vi.fn(), receipt: vi.fn(), result: vi.fn() }));
vi.mock("../../auth/AuthProvider", () => ({ useAuth: () => ({ session: { account_type: "member" } }) }));
vi.mock("../../services/usage", async () => ({ ...await vi.importActual<typeof import("../../services/usage")>("../../services/usage"), fetchUsageActivityByGroup: mocks.activity }));
vi.mock("../../services/entitlements", () => ({ fetchEntitlements: mocks.balance, fetchUsageOperationsByGroup: mocks.operations, fetchUsageOperation: mocks.receipt, fetchUsageOperationResult: mocks.result }));
const record: UsageActivityRecord = {
  module: "monomer_md", task_id: "task-with-a-complete-and-long-identifier", attempt_id: "latest-attempt-2", kind: "task", status: "running",
  submitted_at: "2026-11-03T01:00:00Z", started_at: "2026-11-03T01:01:00Z", finished_at: null, updated_at: "2026-11-03T01:02:00Z",
  parent_task_id: null, root_task_id: "task-with-a-complete-and-long-identifier", source: "fixture", attempt_count: 2,
  metrics: { execution_ms: 0, provider_total_tokens: null, artifact_bytes: 1024 }
};
const activity: UsageActivity = { mode: "activity_only", items: [record], offset: 0, limit: 25, has_more: false,
  modules: [{ id: "monomer_md", label: "分子动力学" }, { id: "predict", label: "性能预测" }], module_groups: moduleGroups, warnings: [staticWarning] };
beforeEach(() => {
  vi.stubGlobal("matchMedia", () => ({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() }));
  mocks.activity.mockReset().mockResolvedValue(activity); mocks.balance.mockReset().mockResolvedValue({ enabled: false, mode: "not_enabled", pools: [], policy: null });
  mocks.operations.mockReset().mockResolvedValue(fixture.operations); mocks.receipt.mockReset().mockResolvedValue(fixture.operations.items[0]); mocks.result.mockReset().mockResolvedValue(fixture.result);
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
const source = (name: "任务记录" | "计次回执") => fireEvent.click(screen.getByRole("button", { name }));
function choose(label: string) { fireEvent.click(screen.getByRole("combobox", { name: "功能模块" })); fireEvent.click(screen.getByRole("option", { name: label })); }

it("defaults to independent task history with grouped attempts even when admission is off", async () => {
  render(<PersonalEntitlementsPanel activeTab="operations" />); const table = within(await screen.findByRole("region", { name: "任务记录表，可横向滚动" }));
  expect(screen.getByRole("button", { name: "任务记录" }).getAttribute("aria-pressed")).toBe("true");
  expect(table.getAllByRole("row")).toHaveLength(2); expect(table.getByText("2 次任务尝试")).toBeTruthy(); expect(table.getByText("执行中")).toBeTruthy();
  for (const text of [/独立记录不扣减次数/, staticWarning, "部分历史或公共记录没有独立模块归属，可在“全部模块”查看。", /同一任务的重试合并为一条记录/]) {
    expect(within(document.getElementById("usage-source-activity")!).getByText(text).closest("[hidden]")).toBeTruthy();
  }
  expect(table.getByText("2 次任务尝试").closest("[hidden]")).toBeNull();
  expect(screen.queryByRole("button", { name: "读取已有结果" })).toBeNull(); expect(mocks.activity).toHaveBeenCalledWith(0, undefined, expect.any(AbortSignal));
  expect(mocks.receipt).not.toHaveBeenCalled(); expect(mocks.result).not.toHaveBeenCalled();
});
it("retains runtime and unknown task warnings while hiding known static explanations", async () => {
  const unknownWarning = "任务采集器存在尚待核实的数据";
  mocks.activity.mockResolvedValue({ ...activity, warnings: [staticWarning, degradedWarning, unknownWarning] });
  render(<PersonalEntitlementsPanel activeTab="operations" />);
  await screen.findByRole("button", { name: "查看详情" });
  expect(screen.getByText(staticWarning).closest("[hidden]")).toBeTruthy();
  for (const warning of [degradedWarning, unknownWarning]) expect(screen.getByText(warning).closest("[hidden]")).toBeNull();
});
it("keeps observed states distinct from counted success", async () => {
  mocks.activity.mockResolvedValue({ ...activity, items: ["queued", "running", "completed", "failed"].map((status, index) => ({ ...record, task_id: `task-${index}`, status })) });
  render(<PersonalEntitlementsPanel activeTab="operations" />); const table = within(await screen.findByRole("region", { name: "任务记录表，可横向滚动" }));
  for (const [label, state] of [["排队中", "queued"], ["执行中", "running"], ["已完成", "completed"], ["失败", "failed"]]) expect(table.getByText(label).className).toContain(`np-usage-state-${state}`);
  expect(table.queryByText("已计成功次数")).toBeNull();
});
it("retains separate source filters/pages and source selection across outer tabs", async () => {
  mocks.activity.mockImplementation((offset, module) => Promise.resolve({ ...activity, offset, has_more: offset === 0, items: [{ ...record, module: module ?? record.module }] }));
  mocks.operations.mockImplementation(offset => Promise.resolve({ ...fixture.operations, offset, has_more: offset === 0 }));
  const view = render(<PersonalEntitlementsPanel activeTab="operations" />); await screen.findByRole("button", { name: "查看详情" }); choose("均聚物性质预测");
  await waitFor(() => expect(mocks.activity.mock.calls.at(-1)?.slice(0, 2)).toEqual([0, "homopolymerPrediction"]));
  await waitFor(() => expect((screen.getByRole("button", { name: "下一页任务" }) as HTMLButtonElement).disabled).toBe(false)); fireEvent.click(screen.getByRole("button", { name: "下一页任务" }));
  await waitFor(() => expect(mocks.activity.mock.calls.at(-1)?.[0]).toBe(25)); source("计次回执"); choose("MD 模拟");
  await waitFor(() => expect((screen.getByRole("button", { name: "下一页操作" }) as HTMLButtonElement).disabled).toBe(false)); fireEvent.click(screen.getByRole("button", { name: "下一页操作" }));
  await waitFor(() => expect(mocks.operations.mock.calls.at(-1)?.[0]).toBe(25)); source("任务记录");
  expect(screen.getByRole("combobox", { name: "功能模块" }).textContent).toContain("均聚物性质预测"); expect(within(screen.getByRole("navigation", { name: "任务记录分页" })).getByText("第 2 页")).toBeTruthy();
  view.rerender(<PersonalEntitlementsPanel activeTab="resources" />); view.rerender(<PersonalEntitlementsPanel activeTab="operations" />);
  expect(screen.getByRole("button", { name: "任务记录" }).getAttribute("aria-pressed")).toBe("true"); source("计次回执");
  expect(screen.getByRole("combobox", { name: "功能模块" }).textContent).toContain("MD 模拟"); expect(within(screen.getByRole("navigation", { name: "操作回执分页" })).getByText("第 2 页")).toBeTruthy();
});
it("shows full task details, latest attempt and unknown versus zero without extra requests", async () => {
  render(<section className="np-usage-page"><div className="np-usage-body"><PersonalEntitlementsPanel activeTab="operations" /></div></section>); fireEvent.click(await screen.findByRole("button", { name: "查看详情" }));
  const dialog = await screen.findByRole("dialog", { name: "任务记录详情" }); const detail = within(dialog);
  expect(detail.getByText(record.task_id, { selector: "code", exact: true })).toBeTruthy(); expect(detail.getByText(record.attempt_id!, { exact: true })).toBeTruthy();
  expect(detail.getByText("0 秒").closest("[hidden]")).toBeNull(); expect(detail.getByText("1 KiB").closest("[hidden]")).toBeNull(); expect(detail.getAllByText("未记录").length).toBeGreaterThan(0);
  expect(detail.getByText(/独立记录不扣减次数/).closest("[hidden]")).toBeTruthy();
  expect(detail.getByText(/未采集的指标显示“未记录”，不等于零/).closest("[hidden]")).toBeTruthy();
  expect(detail.queryByRole("button", { name: "读取已有结果" })).toBeNull(); expect(dialog.closest(".np-usage-body")).toBeNull(); expect(dialog.closest(".np-usage-page")).toBeTruthy();
  expect(mocks.activity).toHaveBeenCalledOnce(); expect(mocks.receipt).not.toHaveBeenCalled(); expect(mocks.result).not.toHaveBeenCalled();
});
it("keeps an empty metric state visible inside task details", async () => {
  mocks.activity.mockResolvedValue({ ...activity, items: [{ ...record, metrics: {} }] });
  render(<PersonalEntitlementsPanel activeTab="operations" />);
  fireEvent.click(await screen.findByRole("button", { name: "查看详情" }));
  const detail = within(await screen.findByRole("dialog", { name: "任务记录详情" }));
  expect(detail.getByText("未记录任务指标。").closest("[hidden]")).toBeNull();
});
it("copies full IDs, traps focus, closes on Escape and restores trigger focus", async () => {
  const writeText = vi.fn().mockResolvedValue(undefined); Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
  render(<PersonalEntitlementsPanel activeTab="operations" />); const trigger = await screen.findByRole("button", { name: "查看详情" }); trigger.focus(); fireEvent.click(trigger);
  const detail = within(await screen.findByRole("dialog", { name: "任务记录详情" })); const close = detail.getByRole("button", { name: "关闭任务记录详情" });
  await waitFor(() => expect(document.activeElement).toBe(close)); fireEvent.keyDown(close, { key: "Tab", shiftKey: true }); expect(document.activeElement).toBe(detail.getByRole("button", { name: "复制任务 ID" }));
  fireEvent.click(detail.getByRole("button", { name: "复制任务 ID" })); expect((await detail.findByText("任务 ID 已复制")).closest("[hidden]")).toBeNull(); expect(writeText).toHaveBeenCalledWith(record.task_id);
  fireEvent.keyDown(document, { key: "Escape" }); await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull()); await waitFor(() => expect(document.activeElement).toBe(trigger));
});
it.each(["source", "tab"])("closes task details when changing %s", async change => {
  const view = render(<PersonalEntitlementsPanel activeTab="operations" />); fireEvent.click(await screen.findByRole("button", { name: "查看详情" })); await screen.findByRole("dialog");
  if (change === "source") fireEvent.click(screen.getByRole("button", { name: "计次回执", hidden: true })); else view.rerender(<PersonalEntitlementsPanel activeTab="overview" />);
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
});
it("refreshes all sources, retains pending data and then displays finished/new tasks", async () => {
  const busy = vi.fn(); const view = render(<PersonalEntitlementsPanel activeTab="operations" onBusyChange={busy} />); await screen.findByRole("button", { name: "查看详情" });
  let finish!: (data: UsageActivity) => void; mocks.activity.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  view.rerender(<PersonalEntitlementsPanel activeTab="operations" refreshVersion={1} onBusyChange={busy} />); await screen.findByText("任务记录更新中…"); expect(screen.getByRole("button", { name: "查看详情" })).toBeTruthy();
  await waitFor(() => expect(mocks.operations).toHaveBeenCalledTimes(2)); expect(mocks.balance).toHaveBeenCalledTimes(2); expect(busy.mock.calls.at(-1)?.[0]).toBe(true);
  await act(async () => finish({ ...activity, items: [{ ...record, status: "completed" }, { ...record, task_id: "new-task", status: "queued" }] }));
  const table = within(screen.getByRole("region", { name: "任务记录表，可横向滚动" })); expect(table.getByText("已完成")).toBeTruthy(); expect(table.getByText("new-task")).toBeTruthy(); await waitFor(() => expect(busy.mock.calls.at(-1)?.[0]).toBe(false));
});
it("retries only activity after a refresh error and keeps receipts independently readable", async () => {
  const view = render(<PersonalEntitlementsPanel activeTab="operations" />); await screen.findByRole("button", { name: "查看详情" }); mocks.activity.mockRejectedValueOnce(new Error("任务记录暂时不可用"));
  view.rerender(<PersonalEntitlementsPanel activeTab="operations" refreshVersion={1} />); await screen.findByRole("alert"); expect(screen.getByRole("alert").textContent).toContain("上次成功读取的数据"); expect(screen.getByRole("button", { name: "查看详情" })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "重试" })); await waitFor(() => expect(mocks.activity).toHaveBeenCalledTimes(3)); expect(mocks.balance).toHaveBeenCalledTimes(2); expect(mocks.operations).toHaveBeenCalledTimes(2);
  source("计次回执"); expect(screen.getAllByRole("button", { name: "查看回执" })).toHaveLength(2);
});
it("shows disabled activity without disabling historical receipts", async () => {
  mocks.activity.mockRejectedValue(new UsageRequestError(503, "usage_metering_disabled", "disabled")); render(<PersonalEntitlementsPanel activeTab="operations" />);
  await screen.findByRole("heading", { name: "任务记录尚未启用" }); expect(screen.getByRole("button", { name: "重新检查任务记录" })).toBeTruthy();
  expect(screen.getByText(/启用任务用量记录后，可在这里查看已接入的任务/).closest("[hidden]")).toBeTruthy(); source("计次回执"); expect(screen.getAllByRole("button", { name: "查看回执" })).toHaveLength(2);
});
it("aborts a retired account's activity and never exposes late rows", async () => {
  let finish!: (data: UsageActivity) => void; mocks.activity.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  const view = render(<PersonalEntitlementsPanel key="a" activeTab="operations" />); await waitFor(() => expect(mocks.activity).toHaveBeenCalledOnce()); const signal = mocks.activity.mock.calls[0][2] as AbortSignal;
  mocks.activity.mockResolvedValue({ ...activity, items: [] }); view.rerender(<PersonalEntitlementsPanel key="b" activeTab="operations" />); await screen.findByText("暂无任务记录。"); expect(signal.aborted).toBe(true);
  await act(async () => finish(activity)); expect(screen.queryByRole("button", { name: "查看详情" })).toBeNull();
});

it.each(["activity", "receipts"])("allows returning to the first %s page after the next page fails", async kind => {
  const read = kind === "activity" ? mocks.activity : mocks.operations;
  const data = kind === "activity" ? activity : fixture.operations;
  read.mockImplementation((offset: number) => offset ? Promise.reject(new Error("page unavailable")) : Promise.resolve({ ...data, has_more: true }));
  render(<PersonalEntitlementsPanel activeTab="operations" />);
  if (kind === "receipts") source("计次回执");
  const suffix = kind === "activity" ? "任务" : "操作";
  const next = await screen.findByRole("button", { name: `下一页${suffix}` });
  await waitFor(() => expect((next as HTMLButtonElement).disabled).toBe(false)); fireEvent.click(next);
  await screen.findByRole("alert");
  expect((screen.getByRole("button", { name: `下一页${suffix}` }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: `上一页${suffix}` }));
  await waitFor(() => expect(read.mock.calls.at(-1)?.[0]).toBe(0));
  await screen.findByRole("region", { name: kind === "activity" ? "任务记录表，可横向滚动" : "操作回执表，可横向滚动" });
});
it("keeps receipt pagination available after an empty second page", async () => {
  mocks.operations.mockImplementation((offset: number) => Promise.resolve({ ...fixture.operations, offset, has_more: !offset, items: offset ? [] : fixture.operations.items }));
  render(<PersonalEntitlementsPanel activeTab="operations" />); source("计次回执");
  const next = await screen.findByRole("button", { name: "下一页操作" });
  await waitFor(() => expect((next as HTMLButtonElement).disabled).toBe(false)); fireEvent.click(next);
  expect((await screen.findByText("暂无操作回执。")).closest("[hidden]")).toBeNull(); fireEvent.click(screen.getByRole("button", { name: "上一页操作" }));
  await screen.findByRole("region", { name: "操作回执表，可横向滚动" }); expect(mocks.operations.mock.calls.at(-1)?.[0]).toBe(0);
});
it("stops task pagination at the API offset limit while allowing a previous page", () => {
  const onOffsetChange = vi.fn();
  render(<UsageActivityPanel data={{ ...activity, offset: 100000, has_more: true }} loading={false} retry={vi.fn()} active offset={100000} moduleGroup="" onOffsetChange={onOffsetChange} onModuleGroupChange={vi.fn()} />);
  const next = screen.getByRole("button", { name: "下一页任务" }); expect((next as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(next); expect(onOffsetChange).not.toHaveBeenCalled();
  expect(screen.getByText("已达分页查询上限，请调整功能筛选继续查找。").closest("[hidden]")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "上一页任务" })); expect(onOffsetChange).toHaveBeenCalledWith(99975);
});

it("keeps polymer MD and demo together while filtering monomer MD separately with server pagination", async () => {
  const rows = Array.from({ length: 30 }, (_, index) => ({ ...record, module: index % 2 ? "md_demo" : "polymer_md", task_id: `md-task-${index}` }));
  mocks.activity.mockImplementation((offset: number, group?: string) => Promise.resolve(group === "mdSimulationDemo"
    ? { ...activity, items: rows.slice(offset, offset + 25), offset, has_more: offset === 0,
      modules: [{ id: "polymer_md", label: "聚合物分子动力学" }, { id: "md_demo", label: "分子动力学演示" }] }
    : activity));
  render(<PersonalEntitlementsPanel activeTab="operations" />); await screen.findByRole("button", { name: "查看详情" });
  fireEvent.click(screen.getByRole("combobox", { name: "功能模块" }));
  expect(screen.getAllByRole("option").map(option => option.textContent)).toEqual(["全部模块", ...moduleGroups.map(group => group.label)]);
  expect(screen.getByRole("option", { name: "单体 MD 模拟" })).toBeTruthy();
  expect(screen.getByRole("option", { name: "单体 DFT" })).toBeTruthy(); expect(screen.getByRole("option", { name: "数据库分析" })).toBeTruthy();
  expect(screen.queryByRole("option", { name: "分子动力学演示" })).toBeNull();
  fireEvent.click(screen.getByRole("option", { name: "MD 模拟" }));
  await waitFor(() => expect(mocks.activity.mock.calls.at(-1)?.slice(0, 2)).toEqual([0, "mdSimulationDemo"]));
  const table = within(await screen.findByRole("region", { name: "任务记录表，可横向滚动" }));
  expect(table.getAllByRole("row")).toHaveLength(26);
  expect(table.getAllByText("聚合物分子动力学").length).toBeGreaterThan(0); expect(table.getAllByText("分子动力学演示").length).toBeGreaterThan(0);
  expect(table.queryByText("单体分子动力学")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "下一页任务" }));
  await waitFor(() => expect(mocks.activity.mock.calls.at(-1)?.slice(0, 2)).toEqual([25, "mdSimulationDemo"]));
  await waitFor(() => expect(within(screen.getByRole("region", { name: "任务记录表，可横向滚动" })).getAllByRole("row")).toHaveLength(6));
  expect(mocks.activity).toHaveBeenCalledTimes(3);
  choose("单体 MD 模拟"); await waitFor(() => expect(mocks.activity.mock.calls.at(-1)?.slice(0, 2)).toEqual([0, "monomerMdSimulation"]));
  const monomerTable = within(await screen.findByRole("region", { name: "任务记录表，可横向滚动" }));
  expect(monomerTable.getByText("单体分子动力学")).toBeTruthy(); expect(monomerTable.queryByText("聚合物分子动力学")).toBeNull();
});

it("shortens module-prefixed IDs but keeps the full identity and specific MD type in details", async () => {
  const task = { ...record, module: "polymer_md", task_id: "polymer_md:12345678-1234-1234-1234-123456789abc" };
  mocks.activity.mockResolvedValue({ ...activity, items: [task], modules: [{ id: "polymer_md", label: "聚合物分子动力学" }] });
  render(<PersonalEntitlementsPanel activeTab="operations" />);
  const short = await screen.findByText("12345678…9abc"); expect(short.getAttribute("title")).toBe(task.task_id);
  const table = within(screen.getByRole("region", { name: "任务记录表，可横向滚动" }));
  expect(table.getByText("MD 模拟")).toBeTruthy(); expect(table.getByText("聚合物分子动力学")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "查看详情" }));
  const detail = within(await screen.findByRole("dialog", { name: "任务记录详情" }));
  expect(detail.getByText("聚合物分子动力学")).toBeTruthy(); expect(detail.getByText(task.task_id, { selector: "code" })).toBeTruthy();
});

it("retains unattributed legacy records in all modules and allows selecting an empty sidebar module", async () => {
  const modules = [
    { id: "assistant", label: "通用助手" }, { id: "recording_summary", label: "浏览记录摘要" },
    { id: "database_browser", label: "材料数据库浏览" },
  ];
  const rows = [...modules.map(module => module.id), "openscience"].map(module => ({ ...record, module, task_id: `${module}:legacy-task` }));
  mocks.activity.mockImplementation((offset: number, group?: string) => Promise.resolve({ ...activity, modules, offset, items: group ? [] : rows }));
  render(<PersonalEntitlementsPanel activeTab="operations" />);
  const table = within(await screen.findByRole("region", { name: "任务记录表，可横向滚动" }));
  expect(table.getAllByRole("row")).toHaveLength(5);
  expect(table.getAllByText("未归属侧栏模块")).toHaveLength(4);
  expect(table.getByText("历史数据库操作（未细分）")).toBeTruthy(); expect(table.getByText("OpenScience 独立服务")).toBeTruthy();
  fireEvent.click(screen.getByRole("combobox", { name: "功能模块" }));
  expect(screen.getAllByRole("option").map(option => option.textContent)).toEqual(["全部模块", ...moduleGroups.map(group => group.label)]);
  expect(screen.queryByRole("option", { name: "未归属侧栏模块" })).toBeNull();
  fireEvent.click(screen.getByRole("option", { name: "高通量优化演示" }));
  await screen.findByText("暂无任务记录。");
  expect(mocks.activity.mock.calls.at(-1)?.slice(0, 2)).toEqual([0, "highThroughputWorkflowDemo"]);
  choose("全部模块");
  await screen.findByRole("region", { name: "任务记录表，可横向滚动" });
  expect(mocks.activity.mock.calls.at(-1)?.slice(0, 2)).toEqual([0, undefined]);
  expect(screen.getAllByRole("button", { name: "查看详情" })).toHaveLength(4);
});
