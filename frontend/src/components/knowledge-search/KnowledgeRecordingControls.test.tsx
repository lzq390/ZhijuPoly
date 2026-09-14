/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { KnowledgeRecordingProvider } from "../../hooks/useKnowledgeRecording";
import { KnowledgeRecordingControls } from "./KnowledgeRecordingControls";

const api = vi.hoisted(() => ({ start: vi.fn(), stop: vi.fn(), summarize: vi.fn() }));
vi.mock("../../services/api", () => ({
  startKnowledgeRecording: api.start, stopKnowledgeRecording: api.stop, summarizeKnowledgeRecording: api.summarize
}));
beforeEach(() => {
  api.start.mockReset().mockImplementation(async (recording_id: string) => ({ recording_id, status: "recording" }));
  api.stop.mockReset().mockResolvedValue({ recording_id: "test", events: [
    { sequence: 1, time: "2026-09-09T10:00:00Z", event: "search.completed", query: "debug-query", total: 2, page: 1 }
  ] });
  api.summarize.mockReset().mockResolvedValue({ summary: "浏览主题。\n\n阅读收获。\n\n内容联系。", generated: true });
});
afterEach(cleanup);
function mount(localMode = true) {
  return render(<KnowledgeRecordingProvider><KnowledgeRecordingControls localMode={localMode} /></KnowledgeRecordingProvider>);
}
async function startAndSummarize() {
  fireEvent.click(screen.getByRole("button", { name: "开始记录" }));
  fireEvent.click(await screen.findByRole("button", { name: "正在记录 · 总结" }));
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}

it.each([
  { visibility: "打开", closed: false, outcome: "成功", failed: false },
  { visibility: "关闭", closed: true, outcome: "成功", failed: false },
  { visibility: "打开", closed: false, outcome: "失败", failed: true },
  { visibility: "关闭", closed: true, outcome: "失败", failed: true },
])("延迟请求完成且总结$outcome时，面板保持用户选择的$visibility状态", async ({ closed, failed }) => {
  const starting = deferred<{ recording_id: string; status: string }>();
  const stopping = deferred<{ recording_id: string; events: [] }>();
  const summarizing = deferred<{ summary: string; generated: boolean }>();
  api.start.mockReturnValueOnce(starting.promise);
  api.stop.mockReturnValueOnce(stopping.promise);
  api.summarize.mockReturnValueOnce(summarizing.promise);
  mount();
  const trigger = screen.getByRole("button", { name: "开始记录" });
  fireEvent.click(trigger);
  fireEvent.click(trigger);
  expect(api.start).toHaveBeenCalledTimes(1);
  expect(screen.queryByRole("dialog")).toBeNull();
  await act(async () => starting.resolve({ recording_id: api.start.mock.calls[0][0], status: "recording" }));
  expect(screen.queryByRole("dialog")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "正在记录 · 总结" }));
  expect(screen.getByRole("dialog", { name: "本次浏览总结" })).not.toBeNull();
  expect(screen.getByText("正在整理本次记录")).not.toBeNull();
  if (closed) fireEvent.click(screen.getByRole("button", { name: "关闭总结" }));
  await act(async () => stopping.resolve({ recording_id: api.start.mock.calls[0][0], events: [] }));
  expect(Boolean(screen.queryByRole("dialog"))).toBe(!closed);
  if (!closed) expect(screen.getByText("正在整理本次阅读")).not.toBeNull();
  await act(async () => {
    if (failed) summarizing.reject(new Error("offline"));
    else summarizing.resolve({ summary: "延迟完成的回顾", generated: true });
  });
  expect(Boolean(screen.queryByRole("dialog"))).toBe(!closed);
  if (closed) fireEvent.click(screen.getByRole("button", { name: "查看总结" }));
  const dialog = within(screen.getByRole("dialog", { name: "本次浏览总结" }));
  if (failed) expect(dialog.getByRole("alert").textContent).toContain("offline");
  else expect(dialog.getByText("延迟完成的回顾")).not.toBeNull();
  expect(api.stop).toHaveBeenCalledTimes(1);
  expect(api.summarize).toHaveBeenCalledTimes(1);
});

it("查看旧总结时开始新记录，立即收起面板且开始请求完成后保持关闭", async () => {
  mount();
  await startAndSummarize();
  await screen.findByText("阅读收获。");
  const starting = deferred<{ recording_id: string; status: string }>();
  api.start.mockReturnValueOnce(starting.promise);
  fireEvent.click(screen.getByRole("button", { name: "开始新记录" }));
  expect(screen.queryByRole("dialog")).toBeNull();
  await act(async () => starting.resolve({ recording_id: api.start.mock.calls[1][0], status: "recording" }));
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(screen.getByRole("button", { name: "正在记录 · 总结" })).not.toBeNull();
  expect(api.start.mock.calls[1][0]).not.toBe(api.start.mock.calls[0][0]);
});

it("点击开始记录后生成阅读总结，不展示调试操作清单", async () => {
  mount();
  const start = screen.getByRole("button", { name: "开始记录" });
  fireEvent.click(start);
  fireEvent.click(start);
  fireEvent.click(await screen.findByRole("button", { name: "正在记录 · 总结" }));
  const dialog = within(await screen.findByRole("dialog", { name: "本次浏览总结" }));
  expect(await dialog.findByText("阅读收获。")).not.toBeNull();
  expect(dialog.queryByText(/debug-query|本次操作清单|个操作|查看对应内容/)).toBeNull();
  expect(api.start).toHaveBeenCalledTimes(1);
  expect(api.summarize).toHaveBeenCalledTimes(1);
});

it("生成中可关闭和重新打开，关闭后完成不抢占页面或重复请求", async () => {
  let finish!: (value: object) => void;
  api.summarize.mockReturnValueOnce(new Promise((resolve) => { finish = resolve; }));
  mount();
  await startAndSummarize();
  await screen.findByText("正在整理本次阅读");
  fireEvent.click(screen.getByRole("button", { name: "关闭总结" }));
  expect(screen.queryByRole("dialog")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "查看生成进度" }));
  expect(screen.getByRole("dialog")).not.toBeNull();
  fireEvent.keyDown(window, { key: "Escape" });
  await act(async () => finish({ summary: "完成的回顾", generated: true }));
  expect(screen.queryByRole("dialog")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "查看总结" }));
  expect(screen.getByText("完成的回顾")).not.toBeNull();
  expect(api.stop).toHaveBeenCalledTimes(1);
  expect(api.summarize).toHaveBeenCalledTimes(1);
});

it("点击外部关闭并可开始新记录，不复用上一轮 ID", async () => {
  mount();
  await startAndSummarize();
  await screen.findByText("阅读收获。");
  fireEvent.pointerDown(document.body);
  expect(screen.queryByRole("dialog")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "开始新记录" }));
  await screen.findByRole("button", { name: "正在记录 · 总结" });
  expect(api.start.mock.calls[0][0]).not.toBe(api.start.mock.calls[1][0]);
});

it("总结失败仍可关闭，重新打开后重试原记录", async () => {
  api.summarize.mockRejectedValueOnce(new Error("offline"));
  mount();
  await startAndSummarize();
  await screen.findByRole("button", { name: "重试总结" });
  fireEvent.click(screen.getByRole("button", { name: "关闭总结" }));
  fireEvent.click(screen.getByRole("button", { name: "查看总结" }));
  fireEvent.click(screen.getByRole("button", { name: "重试总结" }));
  await screen.findByText("阅读收获。");
  expect(api.stop).toHaveBeenCalledTimes(1);
  expect(api.summarize.mock.calls[0]).toEqual(api.summarize.mock.calls[1]);
});

it("不支持采集的模块不开放开始入口", () => {
  mount(false);
  fireEvent.click(screen.getByRole("button", { name: "开始记录" }));
  expect(api.start).not.toHaveBeenCalled();
});

it("开始失败后可以直接点击按钮重试", async () => {
  api.start.mockRejectedValueOnce(new Error("offline"));
  mount();
  fireEvent.click(screen.getByRole("button", { name: "开始记录" }));
  expect(await screen.findByRole("alert")).not.toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "开始记录" }));
  await screen.findByRole("button", { name: "正在记录 · 总结" });
  expect(api.start.mock.calls[0]).toEqual(api.start.mock.calls[1]);
});

it("空记录只显示说明，也能关闭并重新打开", async () => {
  api.stop.mockResolvedValueOnce({ recording_id: "empty", events: [] });
  api.summarize.mockResolvedValueOnce({ summary: "本次没有记录到操作，暂无可总结的内容。", generated: false });
  mount();
  await startAndSummarize();
  await screen.findByRole("region", { name: "记录说明" });
  expect(screen.queryByRole("heading", { name: /阅读收获/ })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "继续浏览" }));
  expect(screen.queryByRole("dialog")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "查看总结" }));
  expect(screen.getByText("本次没有记录到操作，暂无可总结的内容。")).not.toBeNull();
});
