/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { KnowledgeRecordingProvider, useKnowledgeRecording } from "../../hooks/useKnowledgeRecording";
import type { ActiveModule } from "../../routing";
import { BrowsingRecordingControls, BrowsingRecordingUIProvider } from "./BrowsingRecording";

const api = vi.hoisted(() => ({ start: vi.fn(), stop: vi.fn(), summarize: vi.fn() }));
vi.mock("../../services/api", async (original) => ({
  ...await original<typeof import("../../services/api")>(),
  startKnowledgeRecording: api.start, stopKnowledgeRecording: api.stop, summarizeKnowledgeRecording: api.summarize
}));
beforeEach(() => {
  api.start.mockReset().mockImplementation(async (recording_id: string) => ({ recording_id, status: "recording" }));
  api.stop.mockReset().mockResolvedValue({ recording_id: "test", events: [
    { sequence: 1, time: "2026-09-09T10:00:00Z", event: "search.completed", query: "debug-query", total: 2, page: 1 }
  ] });
  api.summarize.mockReset().mockResolvedValue({ summary: "浏览主题。\n\n阅读收获。\n\n内容联系。", generated: true });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
function mount(canStart = true) {
  return render(<KnowledgeRecordingProvider><BrowsingRecordingUIProvider activeModule="knowledge" canStart={canStart}><BrowsingRecordingControls module="knowledge" /></BrowsingRecordingUIProvider></KnowledgeRecordingProvider>);
}
async function startAndSummarize() {
  fireEvent.click(screen.getByRole("button", { name: "开始记录" }));
  fireEvent.click(await screen.findByRole("button", { name: "结束并总结" }));
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}

it("正文逐步显示，收起、切页及移动入口后继续同一次流式请求", async () => {
  const desktop = installDesktopMedia();
  const pending = deferred<{ summary: string; generated: boolean }>();
  let append!: (text: string) => void;
  api.summarize.mockImplementationOnce((_id: string, onPartial: (text: string) => void) => {
    append = onPartial;
    return pending.promise;
  });
  const view = render(<Hosts module="knowledge" />);
  await startAndSummarize();
  await screen.findByText("正在整理本次阅读");
  act(() => append("已经生成的第一段。"));
  expect(within(screen.getByRole("region", { name: "正在生成的总结" })).getByText("已经生成的第一段。")).not.toBeNull();
  expect(screen.queryByText("正在整理本次阅读")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "关闭总结" }));
  view.rerender(<Hosts module="databaseQuery" />);
  desktop(false);
  act(() => append("已经生成的第一段。\n\n收起后继续生成的内容。"));
  expect(screen.queryByRole("dialog")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "AI 浏览总结：查看生成进度" }));
  expect(screen.getByText("收起后继续生成的内容。")).not.toBeNull();
  fireEvent.keyDown(window, { key: "Escape" });
  await act(() => pending.resolve({ summary: "最终完整总结。", generated: true }));
  expect(screen.queryByRole("dialog")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "AI 浏览总结：查看总结" }));
  expect(screen.getByText("最终完整总结。")).not.toBeNull();
  expect(screen.queryByRole("region", { name: "正在生成的总结" })).toBeNull();
  expect(api.summarize).toHaveBeenCalledTimes(1);
  expect(api.summarize.mock.calls[0][0]).toBe(api.start.mock.calls[0][0]);
});

it("断流保留未完成正文，重试清空旧正文并沿用原记录", async () => {
  const pending = deferred<{ summary: string; generated: boolean }>();
  api.summarize.mockImplementationOnce(async (_id: string, onPartial: (text: string) => void) => {
    onPartial("中断前的内容。");
    throw new Error("总结连接中断");
  }).mockReturnValueOnce(pending.promise);
  mount();
  await startAndSummarize();
  await screen.findByRole("button", { name: "重试总结" });
  expect(within(screen.getByRole("region", { name: "未完成的总结" })).getByText("中断前的内容。")).not.toBeNull();
  expect(screen.queryByRole("button", { name: "开始新记录" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "重试总结" }));
  expect(screen.queryByText("中断前的内容。")).toBeNull();
  await screen.findByText("正在整理本次阅读");
  await act(() => pending.resolve({ summary: "重试后完整的内容。", generated: true }));
  expect(screen.getByText("重试后完整的内容。")).not.toBeNull();
  expect(api.summarize.mock.calls[1][0]).toBe(api.summarize.mock.calls[0][0]);
  expect(api.stop).toHaveBeenCalledTimes(1);
});

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
  fireEvent.click(screen.getByRole("button", { name: "结束并总结" }));
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
  expect(screen.getByRole("button", { name: "结束并总结" })).not.toBeNull();
  expect(api.start.mock.calls[1][0]).not.toBe(api.start.mock.calls[0][0]);
});

it("点击开始记录后生成阅读总结，不展示调试操作清单", async () => {
  mount();
  const start = screen.getByRole("button", { name: "开始记录" });
  fireEvent.click(start);
  fireEvent.click(start);
  fireEvent.click(await screen.findByRole("button", { name: "结束并总结" }));
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
  await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: "查看总结" })));
  fireEvent.click(screen.getByRole("button", { name: "查看总结" }));
  fireEvent.click(screen.getByRole("button", { name: "开始新记录" }));
  await screen.findByRole("button", { name: "结束并总结" });
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

it("点击面板外的操作收起总结，保留该操作的焦点", async () => {
  render(<KnowledgeRecordingProvider><BrowsingRecordingUIProvider activeModule="knowledge" canStart>
    <BrowsingRecordingControls module="knowledge" /><button><span>页面操作</span></button>
  </BrowsingRecordingUIProvider></KnowledgeRecordingProvider>);
  await startAndSummarize();
  await screen.findByText("阅读收获。");
  const outside = screen.getByRole("button", { name: "页面操作" });
  fireEvent.pointerDown(screen.getByText("页面操作"));
  outside.focus();
  await act(() => new Promise<void>(resolve => requestAnimationFrame(() => resolve())));
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(document.activeElement).toBe(outside);
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
  await screen.findByRole("button", { name: "结束并总结" });
  expect(api.start.mock.calls[0]).toEqual(api.start.mock.calls[1]);
});

it("开始失败提示可点击外部、Escape 或失焦收起，再次失败仍提示且保留同一记录", async () => {
  api.start.mockRejectedValueOnce(new Error("offline")).mockRejectedValueOnce(new Error("offline"));
  mount();
  const button = screen.getByRole("button", { name: "开始记录" });
  fireEvent.click(button);
  const hint = await screen.findByRole("alert");
  expect(hint.classList.contains("is-visible")).toBe(true);
  fireEvent.pointerDown(document.body);
  expect(hint.classList.contains("is-visible")).toBe(false);
  expect(button.querySelector(".np-recording-warning")).not.toBeNull();
  fireEvent.focus(button);
  expect(hint.classList.contains("is-visible")).toBe(true);
  fireEvent.keyDown(window, { key: "Escape" });
  expect(hint.classList.contains("is-visible")).toBe(false);
  fireEvent.mouseEnter(button.parentElement!);
  expect(hint.classList.contains("is-visible")).toBe(true);
  fireEvent.blur(button);
  expect(hint.classList.contains("is-visible")).toBe(false);
  fireEvent.click(button);
  await waitFor(() => expect(screen.getByRole("alert").classList.contains("is-visible")).toBe(true));
  fireEvent.pointerDown(document.body);
  fireEvent.click(button);
  await screen.findByRole("button", { name: "结束并总结" });
  expect(button.querySelector(".np-recording-warning")).toBeNull();
  expect(api.start.mock.calls[1]).toEqual(api.start.mock.calls[0]);
  expect(api.start.mock.calls[2]).toEqual(api.start.mock.calls[0]);
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

function installDesktopMedia() {
  let matches = true;
  const listeners = new Set<() => void>();
  vi.stubGlobal("matchMedia", () => ({ get matches() { return matches; },
    addEventListener: (_: string, listener: () => void) => listeners.add(listener),
    removeEventListener: (_: string, listener: () => void) => listeners.delete(listener)
  }));
  return (next: boolean) => act(() => { matches = next; listeners.forEach(listener => listener()); });
}

function Hosts({ module }: { module: ActiveModule }) {
  return <KnowledgeRecordingProvider>
    <BrowsingRecordingUIProvider activeModule={module} canStart={module === "knowledge" || module === "databaseFilter"}>
      <BrowsingRecordingControls placement="mobile" />
      <BrowsingRecordingControls module="home" placement="sidebar" />
      <BrowsingRecordingControls module="knowledge" />
      <BrowsingRecordingControls module="databaseFilter" />
      <BrowsingRecordingControls module="databaseQuery" />
      <BrowsingRecordingControls module="structureWorkbench" />
    </BrowsingRecordingUIProvider>
  </KnowledgeRecordingProvider>;
}

it("入口换页或移入移动导航，单例面板保持打开并把焦点还给当前入口", async () => {
  const desktop = installDesktopMedia();
  const pendingSummary = deferred<{ summary: string; generated: boolean }>();
  api.summarize.mockReturnValueOnce(pendingSummary.promise);
  const view = render(<Hosts module="knowledge" />);
  await startAndSummarize();
  await screen.findByText("正在整理本次阅读");
  const panel = screen.getByRole("dialog");
  const entry = () => document.querySelectorAll("[data-recording-entry]");
  view.rerender(<Hosts module="databaseQuery" />);
  expect(entry().length).toBe(1);
  expect(screen.getByRole("dialog")).toBe(panel);
  expect(within(panel).getByText(/当前页面操作不计入记录/)).not.toBeNull();
  desktop(false);
  expect(entry().length).toBe(1);
  expect(entry()[0].getAttribute("data-recording-entry")).toBe("mobile");
  expect(screen.getByRole("dialog")).toBe(panel);
  fireEvent.keyDown(window, { key: "Escape" });
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "AI 浏览总结：查看生成进度" }));
  await act(() => pendingSummary.resolve({ summary: "同一次浏览总结", generated: true }));
  expect(screen.queryByRole("dialog")).toBeNull();
  desktop(true);
  view.rerender(<Hosts module="home" />);
  expect(entry().length).toBe(1);
  expect(entry()[0].getAttribute("data-recording-entry")).toBe("sidebar");
  fireEvent.click(screen.getByRole("button", { name: "AI 浏览总结：查看总结" }));
  expect(screen.getByText("同一次浏览总结")).not.toBeNull();
  expect(screen.queryByRole("button", { name: "开始新记录" })).toBeNull();
  view.rerender(<Hosts module="databaseFilter" />);
  expect(within(screen.getByRole("dialog")).getByRole("button", { name: "开始新记录" })).not.toBeNull();
  expect(api.start).toHaveBeenCalledTimes(1);
  expect(api.stop).toHaveBeenCalledTimes(1);
  expect(api.summarize).toHaveBeenCalledTimes(1);
});

it("未接入模块可结束已有记录，结束失败可从面板重试原记录", async () => {
  const view = render(<Hosts module="knowledge" />);
  fireEvent.click(screen.getByRole("button", { name: "开始记录" }));
  await screen.findByRole("button", { name: "结束并总结" });
  view.rerender(<Hosts module="databaseQuery" />);
  api.stop.mockRejectedValueOnce(new Error("offline"));
  fireEvent.click(screen.getByRole("button", { name: "结束并总结" }));
  const panel = within(screen.getByRole("dialog"));
  fireEvent.click(await panel.findByRole("button", { name: "重试结束记录" }));
  await panel.findByText("阅读收获。");
  expect(api.stop.mock.calls[0]).toEqual(api.stop.mock.calls[1]);
  expect(panel.queryByRole("button", { name: "开始新记录" })).toBeNull();
});

it("记录不完整时保留状态说明，并且一次 Escape 只关闭总结面板", async () => {
  function FailedObservation() {
    const recording = useKnowledgeRecording()!;
    return <button onClick={() => void recording.track(() => Promise.reject(new Error("offline"))).catch(() => {})}>查看失败</button>;
  }
  render(<KnowledgeRecordingProvider><BrowsingRecordingUIProvider activeModule="knowledge" canStart>
    <BrowsingRecordingControls module="knowledge" /><FailedObservation />
  </BrowsingRecordingUIProvider></KnowledgeRecordingProvider>);
  fireEvent.click(screen.getByRole("button", { name: "开始记录" }));
  await screen.findByRole("button", { name: "结束并总结" });
  fireEvent.click(screen.getByRole("button", { name: "查看失败" }));
  await screen.findByText(/结束并总结，部分内容未能记录/);
  fireEvent.click(screen.getByRole("button", { name: "结束并总结" }));
  expect(within(screen.getByRole("dialog")).getByRole("alert").textContent).toContain("总结可能不完整");
  const lowerDrawerEscape = vi.fn();
  document.addEventListener("keydown", lowerDrawerEscape);
  try {
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(lowerDrawerEscape).not.toHaveBeenCalled();
  } finally { document.removeEventListener("keydown", lowerDrawerEscape); }
  await act(async () => {});
});
