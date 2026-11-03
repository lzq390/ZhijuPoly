// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { UsageActivity, UsageActivityRecord } from "../../types/usage";
import fixture from "../../../scripts/fixtures/entitlements.json";
import { UsageRequestError } from "../../services/usage";
import { PersonalEntitlementsPanel } from "./PersonalEntitlementsPanel";
import { UsageActivityPanel } from "./UsageActivityPanel";
const mocks = vi.hoisted(() => ({ activity: vi.fn(), balance: vi.fn(), operations: vi.fn(), receipt: vi.fn(), result: vi.fn() }));
vi.mock("../../auth/AuthProvider", () => ({ useAuth: () => ({ session: { account_type: "member" } }) }));
vi.mock("../../services/usage", async () => ({ ...await vi.importActual<typeof import("../../services/usage")>("../../services/usage"), fetchUsageActivity: mocks.activity }));
vi.mock("../../services/entitlements", () => ({ fetchEntitlements: mocks.balance, fetchUsageOperations: mocks.operations, fetchUsageOperation: mocks.receipt, fetchUsageOperationResult: mocks.result }));
const record: UsageActivityRecord = {
  module: "monomer_md", task_id: "task-with-a-complete-and-long-identifier", attempt_id: "latest-attempt-2", kind: "task", status: "running",
  submitted_at: "2026-11-03T01:00:00Z", started_at: "2026-11-03T01:01:00Z", finished_at: null, updated_at: "2026-11-03T01:02:00Z",
  parent_task_id: null, root_task_id: "task-with-a-complete-and-long-identifier", source: "fixture", attempt_count: 2,
  metrics: { execution_ms: 0, provider_total_tokens: null, artifact_bytes: 1024 }
};
const activity: UsageActivity = { mode: "activity_only", items: [record], offset: 0, limit: 25, has_more: false,
  modules: [{ id: "monomer_md", label: "分子动力学" }, { id: "predict", label: "性能预测" }], warnings: ["仅包含已接入的任务"] };
beforeEach(() => {
  vi.stubGlobal("matchMedia", () => ({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() }));
  mocks.activity.mockReset().mockResolvedValue(activity); mocks.balance.mockReset().mockResolvedValue({ enabled: false, mode: "not_enabled", pools: [], policy: null });
  mocks.operations.mockReset().mockResolvedValue(fixture.operations); mocks.receipt.mockReset().mockResolvedValue(fixture.operations.items[0]); mocks.result.mockReset().mockResolvedValue(fixture.result);
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
const source = (name: "任务记录" | "计次回执") => fireEvent.click(screen.getByRole("button", { name }));
function choose(label: string) { fireEvent.click(screen.getByRole("combobox", { name: "操作功能" })); fireEvent.click(screen.getByRole("option", { name: label })); }

it("defaults to independent task history with grouped attempts even when admission is off", async () => {
  render(<PersonalEntitlementsPanel activeTab="operations" />); const table = within(await screen.findByRole("region", { name: "任务记录表，可横向滚动" }));
  expect(screen.getByRole("button", { name: "任务记录" }).getAttribute("aria-pressed")).toBe("true");
  expect(table.getAllByRole("row")).toHaveLength(2); expect(table.getByText("2 次任务尝试")).toBeTruthy(); expect(table.getByText("执行中")).toBeTruthy();
  expect(screen.getByText(/独立记录不扣减次数/)).toBeTruthy(); expect(screen.getByText("仅包含已接入的任务")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "读取已有结果" })).toBeNull(); expect(mocks.activity).toHaveBeenCalledWith(0, undefined, expect.any(AbortSignal));
  expect(mocks.receipt).not.toHaveBeenCalled(); expect(mocks.result).not.toHaveBeenCalled();
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
  const view = render(<PersonalEntitlementsPanel activeTab="operations" />); await screen.findByRole("button", { name: "查看详情" }); choose("性能预测");
  await waitFor(() => expect(mocks.activity.mock.calls.at(-1)?.slice(0, 2)).toEqual([0, "predict"]));
  await waitFor(() => expect((screen.getByRole("button", { name: "下一页任务" }) as HTMLButtonElement).disabled).toBe(false)); fireEvent.click(screen.getByRole("button", { name: "下一页任务" }));
  await waitFor(() => expect(mocks.activity.mock.calls.at(-1)?.[0]).toBe(25)); source("计次回执"); choose("分子动力学");
  await waitFor(() => expect((screen.getByRole("button", { name: "下一页操作" }) as HTMLButtonElement).disabled).toBe(false)); fireEvent.click(screen.getByRole("button", { name: "下一页操作" }));
  await waitFor(() => expect(mocks.operations.mock.calls.at(-1)?.[0]).toBe(25)); source("任务记录");
  expect(screen.getByRole("combobox", { name: "操作功能" }).textContent).toContain("性能预测"); expect(within(screen.getByRole("navigation", { name: "任务记录分页" })).getByText("第 2 页")).toBeTruthy();
  view.rerender(<PersonalEntitlementsPanel activeTab="resources" />); view.rerender(<PersonalEntitlementsPanel activeTab="operations" />);
  expect(screen.getByRole("button", { name: "任务记录" }).getAttribute("aria-pressed")).toBe("true"); source("计次回执");
  expect(screen.getByRole("combobox", { name: "操作功能" }).textContent).toContain("分子动力学"); expect(within(screen.getByRole("navigation", { name: "操作回执分页" })).getByText("第 2 页")).toBeTruthy();
});
it("shows full task details, latest attempt and unknown versus zero without extra requests", async () => {
  render(<section className="np-usage-page"><div className="np-usage-body"><PersonalEntitlementsPanel activeTab="operations" /></div></section>); fireEvent.click(await screen.findByRole("button", { name: "查看详情" }));
  const dialog = await screen.findByRole("dialog", { name: "任务记录详情" }); const detail = within(dialog);
  expect(detail.getByText(record.task_id, { selector: "code", exact: true })).toBeTruthy(); expect(detail.getByText(record.attempt_id!, { exact: true })).toBeTruthy();
  expect(detail.getByText("0 秒")).toBeTruthy(); expect(detail.getByText("1 KiB")).toBeTruthy(); expect(detail.getAllByText("未记录").length).toBeGreaterThan(0);
  expect(detail.queryByRole("button", { name: "读取已有结果" })).toBeNull(); expect(dialog.closest(".np-usage-body")).toBeNull(); expect(dialog.closest(".np-usage-page")).toBeTruthy();
  expect(mocks.activity).toHaveBeenCalledOnce(); expect(mocks.receipt).not.toHaveBeenCalled(); expect(mocks.result).not.toHaveBeenCalled();
});
it("copies full IDs, traps focus, closes on Escape and restores trigger focus", async () => {
  const writeText = vi.fn().mockResolvedValue(undefined); Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
  render(<PersonalEntitlementsPanel activeTab="operations" />); const trigger = await screen.findByRole("button", { name: "查看详情" }); trigger.focus(); fireEvent.click(trigger);
  const detail = within(await screen.findByRole("dialog", { name: "任务记录详情" })); const close = detail.getByRole("button", { name: "关闭任务记录详情" });
  await waitFor(() => expect(document.activeElement).toBe(close)); fireEvent.keyDown(close, { key: "Tab", shiftKey: true }); expect(document.activeElement).toBe(detail.getByRole("button", { name: "复制任务 ID" }));
  fireEvent.click(detail.getByRole("button", { name: "复制任务 ID" })); await detail.findByText("任务 ID 已复制"); expect(writeText).toHaveBeenCalledWith(record.task_id);
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
  await screen.findByRole("heading", { name: "任务记录尚未启用" }); expect(screen.getByRole("button", { name: "重新检查任务记录" })).toBeTruthy(); source("计次回执"); expect(screen.getAllByRole("button", { name: "查看回执" })).toHaveLength(2);
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
  await screen.findByText("暂无操作回执。"); fireEvent.click(screen.getByRole("button", { name: "上一页操作" }));
  await screen.findByRole("region", { name: "操作回执表，可横向滚动" }); expect(mocks.operations.mock.calls.at(-1)?.[0]).toBe(0);
});
it("stops task pagination at the API offset limit while allowing a previous page", () => {
  const onOffsetChange = vi.fn();
  render(<UsageActivityPanel data={{ ...activity, offset: 100000, has_more: true }} loading={false} retry={vi.fn()} active offset={100000} module="" onOffsetChange={onOffsetChange} onModuleChange={vi.fn()} />);
  const next = screen.getByRole("button", { name: "下一页任务" }); expect((next as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(next); expect(onOffsetChange).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "上一页任务" })); expect(onOffsetChange).toHaveBeenCalledWith(99975);
});
