/* @vitest-environment jsdom */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import App from "./App";

const mocks = vi.hoisted(() => ({ start: vi.fn(), stop: vi.fn(), summary: vi.fn(), operation: vi.fn() }));
vi.mock("./services/api", async (original) => ({
  ...await original<typeof import("./services/api")>(),
  startKnowledgeRecording: mocks.start, stopKnowledgeRecording: mocks.stop, summarizeKnowledgeRecording: mocks.summary
}));
vi.mock("./components/KnowledgeSearch", async () => {
  const { useKnowledgeRecording } = await import("./hooks/useKnowledgeRecording");
  return { KnowledgeSearch: () => {
    const recording = useKnowledgeRecording();
    return <button onClick={() => void recording?.track(mocks.operation)}>检索测试</button>;
  } };
});
vi.mock("./components/DatabaseFilterPage", async () => {
  const { useKnowledgeRecording } = await import("./hooks/useKnowledgeRecording");
  return { DatabaseFilterPage: () => {
    const recording = useKnowledgeRecording();
    return <button onClick={() => void recording?.track(mocks.operation)}>筛选测试</button>;
  } };
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it("跨知识库和筛选页面保留同一记录，切换后仍等待在途操作并统一总结", async () => {
  window.history.replaceState({}, "", "/knowledge");
  vi.spyOn(window, "scrollTo").mockImplementation(() => {});
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 503, json: async () => ({}) }));
  mocks.start.mockImplementation(async (recording_id: string) => ({ recording_id, status: "recording" }));
  mocks.stop.mockResolvedValue({ recording_id: "test", events: [] });
  mocks.summary.mockResolvedValue({ summary: "两个模块的共同回顾", generated: true });
  mocks.operation.mockResolvedValue(undefined);
  render(<App />);
  for (const key of "adad") fireEvent.keyDown(window, { key });
  await screen.findByRole("button", { name: "正在记录 · 总结" });
  fireEvent.click(screen.getByText("检索测试"));
  await waitFor(() => expect(mocks.operation).toHaveBeenCalledTimes(1));
  const id = mocks.start.mock.calls[0][0];
  expect(mocks.operation).toHaveBeenLastCalledWith(id);
  fireEvent.click(screen.getByRole("button", { name: "数据库筛选" }));
  let release!: () => void;
  mocks.operation.mockImplementationOnce(() => new Promise<void>((resolve) => { release = resolve; }));
  fireEvent.click(await screen.findByText("筛选测试"));
  expect(mocks.operation).toHaveBeenLastCalledWith(id);
  window.history.pushState({}, "", "/knowledge");
  fireEvent.popState(window);
  expect((screen.getByRole("button", { name: "正在记录 · 总结" }) as HTMLButtonElement).disabled).toBe(true);
  release();
  await waitFor(() => expect((screen.getByRole("button", { name: "正在记录 · 总结" }) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByRole("button", { name: "正在记录 · 总结" }));
  expect(await screen.findByText("两个模块的共同回顾")).not.toBeNull();
  expect(mocks.start).toHaveBeenCalledTimes(1);
  expect(mocks.stop).toHaveBeenCalledWith(id);
  expect(mocks.summary).toHaveBeenCalledWith(id);
});
