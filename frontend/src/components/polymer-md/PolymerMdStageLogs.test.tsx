// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { PolymerMdStage } from "../../types/polymerMd";
import { PolymerMdStageLogs } from "./PolymerMdStageLogs";
import { retireSession } from "../../auth/session";
const api = vi.hoisted(() => ({ logs: vi.fn() }));
vi.mock("../../services/polymerMdApi", () => ({ fetchPolymerMdStageLogs: api.logs }));
const stage: PolymerMdStage = { stage_run_id: "eq3-1", stage: "eq3", attempt: 1, status: "completed", quality_status: "passed", results: {}, quality: {}, errors: [] };
const response = { job_id: "j", stage_run_id: stage.stage_run_id, lines: ["Step Temp Density", "100 300 1.02"], truncated: true,
  resolved_source: "engine", source_label: "engine.log", available: true, read_at: "2026-10-29T00:00:00Z", available_sources: [{ source: "engine", label: "engine.log" }, { source: "stdout", label: "engine.stdout" }, { source: "runner", label: "runner.log" }] };
function openLogs() { const details = screen.getByText("查看EQ3 平衡采样日志").closest("details")!; details.open = true; fireEvent(details, new Event("toggle")); return details; }
beforeEach(() => api.logs.mockReset());
afterEach(() => { cleanup(); vi.useRealTimers(); });
describe("formal stage log reading", () => {
  it("bounds a hung refresh to 10 seconds while keeping its last successful source and tail", async () => {
    vi.useFakeTimers();
    api.logs.mockResolvedValueOnce(response).mockImplementationOnce(() => new Promise(() => {}));
    render(<PolymerMdStageLogs jobId="j" stage={stage} available />); openLogs();
    await act(async () => {});
    expect(screen.getByText(/Step Temp Density/)).toBeTruthy();
    const readAt = screen.getByText(/北京时间/).querySelector("time")?.dateTime;
    expect(readAt).toBe(response.read_at);
    fireEvent.click(screen.getByRole("button", { name: "刷新阶段日志" }));
    expect(screen.getByText(/保留上次日志/)).toBeTruthy();
    const signal = api.logs.mock.calls[1][2] as AbortSignal;
    await act(() => vi.advanceTimersByTimeAsync(10000));
    expect(signal.aborted).toBe(true);
    expect(screen.getByRole("alert").textContent).toContain("日志读取等待已超时");
    expect(screen.getByText(/Step Temp Density/)).toBeTruthy();
    expect(screen.getByText("engine.log")).toBeTruthy();
    expect(screen.getByText(/北京时间/).querySelector("time")?.dateTime).toBe(readAt);
    expect((screen.getByRole("button", { name: "刷新阶段日志" }) as HTMLButtonElement).disabled).toBe(false);
    await act(() => vi.advanceTimersByTimeAsync(20000));
    expect(api.logs).toHaveBeenCalledTimes(2);
  });
  it("labels actual sources and rejects a late response after switching the requested log", async () => {
    let resolveStdout!: (value: unknown) => void;
    api.logs.mockResolvedValueOnce(response).mockImplementationOnce(() => new Promise(done => { resolveStdout = done; }))
      .mockResolvedValueOnce({ ...response, resolved_source: "runner", source_label: "runner.log", lines: ["Stage runner summary"] });
    render(<PolymerMdStageLogs jobId="j" stage={stage} available />); openLogs();
    await screen.findByText(/Step Temp Density/);
    fireEvent.click(screen.getByRole("combobox", { name: "日志来源" }));
    fireEvent.click(within(screen.getByRole("listbox", { name: "日志来源" })).getByText("程序输出 · engine.stdout", { selector: "strong" }));
    expect(api.logs).toHaveBeenLastCalledWith("j", "eq3-1", expect.any(AbortSignal), { source: "stdout" });
    expect(screen.getByText("engine.log")).toBeTruthy();
    const oldSignal = api.logs.mock.calls[1][2] as AbortSignal;
    fireEvent.click(screen.getByRole("combobox", { name: "日志来源" }));
    fireEvent.click(within(screen.getByRole("listbox", { name: "日志来源" })).getByText("阶段执行摘要 · runner.log", { selector: "strong" }));
    expect(oldSignal.aborted).toBe(true);
    await screen.findByText("Stage runner summary");
    await act(async () => resolveStdout({ ...response, resolved_source: "stdout", source_label: "engine.stdout", lines: ["LATE_STDOUT"] }));
    expect(screen.queryByText("LATE_STDOUT")).toBeNull();
    expect(screen.getByText("runner.log")).toBeTruthy();
  });
  it("accepts legacy responses and stops a pending read when collapsed", async () => {
    api.logs.mockResolvedValueOnce({ job_id: "j", stage_run_id: "eq3-1", lines: ["Legacy tail"], truncated: false })
      .mockImplementationOnce(() => new Promise(() => {}));
    render(<PolymerMdStageLogs jobId="j" stage={{ ...stage, status: "running" }} available />);
    const details = openLogs(); await screen.findByText("Legacy tail");
    expect(screen.getByText("来源未记录")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "刷新阶段日志" }));
    const signal = api.logs.mock.calls[1][2] as AbortSignal;
    details.open = false; fireEvent(details, new Event("toggle"));
    expect(signal.aborted).toBe(true);
    expect(screen.queryByText("Legacy tail")).toBeNull();
  });
  it("aborts a retired session and ignores its late private log response", async () => {
    let resolve!: (value: unknown) => void;
    api.logs.mockImplementationOnce(() => new Promise(done => { resolve = done; }));
    render(<PolymerMdStageLogs jobId="j" stage={stage} available />); openLogs();
    const signal = api.logs.mock.calls[0][2] as AbortSignal;
    act(() => retireSession());
    expect(signal.aborted).toBe(true);
    await act(async () => resolve({ ...response, lines: ["PRIVATE_LATE_LOG"] }));
    expect(screen.queryByText("PRIVATE_LATE_LOG")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "刷新阶段日志" }));
    expect(api.logs).toHaveBeenCalledOnce();
  });
});
