/* @vitest-environment jsdom */
import { act, renderHook } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { KnowledgeRecordingProvider, useKnowledgeRecording } from "./useKnowledgeRecording";

const api = vi.hoisted(() => ({ start: vi.fn(), stop: vi.fn(), summarize: vi.fn() }));
vi.mock("../services/api", () => ({ startKnowledgeRecording: api.start, stopKnowledgeRecording: api.stop, summarizeKnowledgeRecording: api.summarize }));

beforeEach(() => {
  api.start.mockReset().mockImplementation((recording_id: string) => Promise.resolve({ recording_id, status: "recording" }));
  api.stop.mockReset().mockResolvedValue({ status: "stopped", events: [] });
  api.summarize.mockReset().mockResolvedValue({ summary: "本次记录总结", generated: true });
});

it("开始失败重试同一 ID，重复开始不重置，完成后可以开始新记录", async () => {
  api.start.mockRejectedValueOnce(new Error("offline"));
  const { result } = renderHook(() => useKnowledgeRecording()!, { wrapper: KnowledgeRecordingProvider });
  await act(() => result.current.start());
  expect(result.current.phase).toBe("idle");
  expect(result.current.error).toContain("offline");
  await act(() => result.current.start());
  expect(api.start.mock.calls[0]).toEqual(api.start.mock.calls[1]);
  await act(() => result.current.start());
  expect(api.start).toHaveBeenCalledTimes(2);
  await act(() => result.current.stop());
  await act(() => result.current.start());
  expect(api.start.mock.calls[2]).not.toEqual(api.start.mock.calls[1]);
});

it("待完成的操作阻止结束，操作失败明确标记清单可能不完整", async () => {
  const { result } = renderHook(() => useKnowledgeRecording()!, { wrapper: KnowledgeRecordingProvider });
  await act(() => result.current.start());
  let reject!: (cause: Error) => void;
  let operation!: Promise<unknown>;
  const request = vi.fn(() => new Promise((_, fail) => { reject = fail; }));
  act(() => { operation = result.current.track(request).catch(() => undefined); });
  expect(request).toHaveBeenCalledWith(api.start.mock.calls[0][0]);
  expect(result.current.pending).toBe(1);
  await act(() => result.current.stop());
  expect(api.stop).not.toHaveBeenCalled();
  await act(async () => { reject(new Error("offline")); await operation; });
  expect(result.current.pending).toBe(0);
  expect(result.current.incomplete).toBe(true);
  await act(() => result.current.stop());
  expect(result.current.phase).toBe("stopped");
});

it("结束响应失败后冻结本地收集，重试仍使用原 ID", async () => {
  api.stop.mockRejectedValueOnce(new Error("offline"));
  const { result } = renderHook(() => useKnowledgeRecording()!, { wrapper: KnowledgeRecordingProvider });
  await act(() => result.current.start());
  await act(() => result.current.stop());
  expect(result.current.phase).toBe("stop_failed");
  const request = vi.fn().mockResolvedValue({});
  await act(() => result.current.track(request));
  expect(request).toHaveBeenCalledWith();
  await act(() => result.current.stop());
  expect(api.stop.mock.calls[0]).toEqual(api.stop.mock.calls[1]);
  expect(result.current.phase).toBe("stopped");
});
