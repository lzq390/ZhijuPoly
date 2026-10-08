// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GUEST_SESSION } from "../auth/session";
import type { UsageCatalog, UsageRecords, UsageSummary } from "../types/usage";
import { UsageRequestError } from "../services/usage";
const mocks = vi.hoisted(() => ({ auth: vi.fn(), catalog: vi.fn(), summary: vi.fn(), records: vi.fn() }));
vi.mock("../auth/AuthProvider", () => ({ useAuth: mocks.auth }));
vi.mock("../services/usage", async () => ({ ...await vi.importActual<typeof import("../services/usage")>("../services/usage"), fetchUsageCatalog: mocks.catalog, fetchUsageSummary: mocks.summary, fetchUsageRecords: mocks.records }));
import { PersonalUsagePage } from "./PersonalUsagePage";

const catalog: UsageCatalog = { enabled: true, mode: "statistics_only", retention_days: 365,
  modules: [{ id: "md", label: "单体 MD", category: "compute", coverage: "measured" }, { id: "assistant", label: "AI 助手", category: "ai", coverage: "partial", note: "仅记录提供方返回的指标" }],
  metrics: [{ id: "execution_ms", label: "执行耗时", unit: "ms" }, { id: "provider_total_tokens", label: "总令牌", unit: "tokens" }, { id: "artifact_bytes", label: "体积", unit: "bytes" }] };
const summary: UsageSummary = { period: { start: "2026-09-01T00:00:00Z", end: "2026-10-01T00:00:00Z", timezone: "UTC", basis: "task_submitted_at" },
  totals: { tasks: 3, attempts: 4, child_calls: 1, states: { completed: 2, failed: 1 }, metrics: { execution_ms: { value: 0, known_count: 2, unknown_count: 1 }, provider_total_tokens: { value: null, known_count: 0, unknown_count: 3 } } },
  modules: [], daily: [{ date: "2026-09-30", tasks: 3, states: { completed: 2, failed: 1 } }], coverage: { uncovered: ["assistant"], warnings: ["部分历史任务没有计量记录"] } };
const records: UsageRecords = { items: [], page: 1, page_size: 25, total: 0 };
function auth(id = "a") { return { status: "authenticated", session: { authenticated: true, user: { id, username: id, must_change_password: false }, session_id: `session-${id}`, csrf_token: "fixture", capabilities: {} }, login: vi.fn() }; }
beforeEach(() => {
  mocks.auth.mockReturnValue(auth()); mocks.catalog.mockReset().mockResolvedValue(catalog);
  mocks.summary.mockReset().mockResolvedValue(summary); mocks.records.mockReset().mockResolvedValue(records);
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe("personal usage page", () => {
  it("offers login without requesting private statistics for guests", () => {
    const login = vi.fn(); mocks.auth.mockReturnValue({ status: "guest", session: GUEST_SESSION, login });
    render(<PersonalUsagePage />); fireEvent.click(screen.getByRole("button", { name: "登录账号" }));
    expect(login).toHaveBeenCalledOnce(); expect(mocks.catalog).not.toHaveBeenCalled();
  });
  it("renders unknown separately from measured zero and explains coverage", async () => {
    render(<PersonalUsagePage />); await screen.findByRole("heading", { name: "已观测指标" });
    expect(screen.getByText("0 秒")).toBeTruthy(); expect(screen.getAllByText("未记录").length).toBeGreaterThan(0);
    expect(screen.getByText("0 条已记录 · 3 条未记录")).toBeTruthy();
    expect(screen.getByText("累计已观测产物体积")).toBeTruthy(); expect(screen.getByText("尚未覆盖：AI 助手")).toBeTruthy();
    expect(screen.getByText(/不等于 GPU 小时/)).toBeTruthy();
    expect(screen.getByText("最多 366 天；结束日期包含当天。仅记录启用后的任务；删除任务不会清除统计。")).toBeTruthy();
    expect(screen.queryByText(/历史记录保留/)).toBeNull();
  });
  it("sends inclusive UTC dates as exclusive next day and resets pagination on filters", async () => {
    mocks.records.mockImplementation(filter => Promise.resolve({ ...records, page: filter.page, total: 52 }));
    render(<PersonalUsagePage />); await screen.findByRole("button", { name: "下一页" });
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    await waitFor(() => expect(mocks.records.mock.calls.at(-1)?.[0].page).toBe(2));
    fireEvent.change(screen.getByLabelText("开始日期（UTC）"), { target: { value: "2024-02-01" } });
    fireEvent.change(screen.getByLabelText("结束日期（含当天）"), { target: { value: "2024-02-29" } });
    fireEvent.change(screen.getByLabelText("功能"), { target: { value: "md" } });
    fireEvent.click(screen.getByRole("button", { name: "应用筛选" }));
    await waitFor(() => expect(mocks.summary.mock.calls.at(-1)?.[0]).toEqual({ start: "2024-02-01T00:00:00.000Z", end: "2024-03-01T00:00:00.000Z", module: "md" }));
    expect(mocks.records.mock.calls.at(-1)?.[0].page).toBe(1);
  });
  it("rejects date spans exceeding366 days without another request", async () => {
    render(<PersonalUsagePage />); await screen.findByRole("heading", { name: "已观测指标" });
    fireEvent.change(screen.getByLabelText("开始日期（UTC）"), { target: { value: "2024-01-01" } });
    fireEvent.change(screen.getByLabelText("结束日期（含当天）"), { target: { value: "2025-01-01" } });
    fireEvent.click(screen.getByRole("button", { name: "应用筛选" }));
    expect(screen.getByRole("alert").textContent).toContain("366"); expect(mocks.summary).toHaveBeenCalledOnce();
  });
  it("only filters details by state and child calls", async () => {
    render(<PersonalUsagePage />); await screen.findByRole("heading", { name: "已观测指标" });
    fireEvent.change(screen.getByLabelText("明细状态"), { target: { value: "failed" } });
    fireEvent.click(screen.getByLabelText("显示子调用明细"));
    await waitFor(() => expect(mocks.records.mock.calls.at(-1)?.[0]).toMatchObject({ status: "failed", include_children: true, page: 1 }));
    expect(mocks.summary).toHaveBeenCalledOnce();
  });
  it("handles disabled catalog without summary or records requests", async () => {
    mocks.catalog.mockResolvedValue({ ...catalog, enabled: false }); render(<PersonalUsagePage />);
    await screen.findByRole("heading", { name: "用量统计尚未启用" });
    expect(mocks.summary).not.toHaveBeenCalled(); expect(mocks.records).not.toHaveBeenCalled();
  });
  it("recognizes the summary503 feature flag while retaining the account", async () => {
    mocks.summary.mockRejectedValue(new UsageRequestError(503, "usage_metering_disabled", "disabled")); render(<PersonalUsagePage />);
    await screen.findByRole("heading", { name: "用量统计尚未启用" }); expect(screen.queryByRole("button", { name: "登录账号" })).toBeNull();
  });
  it("provides retry after a catalog failure", async () => {
    mocks.catalog.mockRejectedValueOnce(new Error("暂时不可用")); render(<PersonalUsagePage />);
    await screen.findByRole("alert"); fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await screen.findByRole("heading", { name: "已观测指标" }); expect(mocks.catalog).toHaveBeenCalledTimes(2);
  });
  it("does not show a retired account's late summary after switching users", async () => {
    let finish!: (value: UsageSummary) => void;
    mocks.summary.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const view = render(<PersonalUsagePage />); await waitFor(() => expect(mocks.summary).toHaveBeenCalledOnce());
    mocks.auth.mockReturnValue(auth("b")); mocks.summary.mockResolvedValue({ ...summary, totals: { ...summary.totals, tasks: 17 } });
    view.rerender(<PersonalUsagePage />);
    await waitFor(() => expect(within(screen.getByRole("region", { name: "用量概览" })).getByText("17")).toBeTruthy());
    await act(async () => { finish({ ...summary, totals: { ...summary.totals, tasks: 9999 } }); });
    expect(screen.queryByText("9,999")).toBeNull(); expect(screen.getByText("17")).toBeTruthy();
  });
  it("shows empty data explicitly and displays attempt/source detail without task actions", async () => {
    mocks.summary.mockResolvedValue({ ...summary, totals: { ...summary.totals, tasks: 0 } });
    mocks.records.mockResolvedValue({ ...records, total: 1, items: [{ module: "md", task_id: "own-task", attempt_id: "own-attempt", kind: "task", status: "completed", submitted_at: "2026-09-30T01:00:00Z", started_at: null, finished_at: null, parent_task_id: null, root_task_id: "own-task", source: "md", metrics: { execution_ms: 0 } }] });
    render(<PersonalUsagePage />); await screen.findByText("所选范围内暂无已记录的任务。");
    expect(screen.getByText("own-attempt")).toBeTruthy(); expect(screen.queryByRole("button", { name: "删除" })).toBeNull();
  });
});
