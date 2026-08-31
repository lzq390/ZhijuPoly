// @vitest-environment jsdom

import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { PredictRequest, PredictResponse } from "../types";
import { usePredict } from "./usePredict";

const mocks = vi.hoisted(() => ({
  predictSmiles: vi.fn()
}));

vi.mock("../services/api", () => ({
  predictSmiles: mocks.predictSmiles
}));

const request: PredictRequest = {
  smiles: "*CC*",
  properties: ["Glass transition temperature"]
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((promiseResolve, promiseReject) => {
    resolve = promiseResolve;
    reject = promiseReject;
  });
  return { promise, resolve, reject };
}

beforeEach(() => vi.clearAllMocks());
afterEach(() => cleanup());

describe("usePredict", () => {
  it("新请求取消旧请求且旧响应不会覆盖新结果", async () => {
    const first = deferred<PredictResponse>();
    const secondData: PredictResponse = {
      predictions: { "Glass transition temperature": 222 },
      query_time_ms: 2
    };
    const signals: AbortSignal[] = [];
    mocks.predictSmiles
      .mockImplementationOnce((_payload: PredictRequest, signal: AbortSignal) => {
        signals.push(signal);
        return first.promise;
      })
      .mockImplementationOnce((_payload: PredictRequest, signal: AbortSignal) => {
        signals.push(signal);
        return Promise.resolve(secondData);
      });
    const { result } = renderHook(() => usePredict());

    let firstRequest!: Promise<PredictResponse>;
    act(() => {
      firstRequest = result.current.submit(request);
    });
    await waitFor(() => expect(signals).toHaveLength(1));

    await act(async () => {
      await result.current.submit({ ...request, smiles: "CCO" });
    });
    expect(signals[0].aborted).toBe(true);
    expect(result.current.data).toEqual(secondData);

    await act(async () => {
      first.resolve({
        predictions: { "Glass transition temperature": 111 },
        query_time_ms: 1
      });
      await firstRequest;
    });
    expect(result.current.data).toEqual(secondData);
    expect(result.current.isLoading).toBe(false);
  });

  it("卸载时取消当前请求", async () => {
    const pending = deferred<PredictResponse>();
    const signals: AbortSignal[] = [];
    mocks.predictSmiles.mockImplementation((_payload: PredictRequest, nextSignal: AbortSignal) => {
      signals.push(nextSignal);
      return pending.promise;
    });
    const { result, unmount } = renderHook(() => usePredict());

    act(() => {
      void result.current.submit(request).catch(() => undefined);
    });
    await waitFor(() => expect(signals).toHaveLength(1));
    unmount();
    expect(signals[0].aborted).toBe(true);
  });

  it("保留服务错误并为未知失败提供中文兜底", async () => {
    mocks.predictSmiles.mockRejectedValueOnce(new Error("后端 detail"));
    const first = renderHook(() => usePredict());
    await act(async () => {
      await first.result.current.submit(request).catch(() => undefined);
    });
    expect(first.result.current.error).toBe("后端 detail");
    first.unmount();

    mocks.predictSmiles.mockRejectedValueOnce({ code: "unknown" });
    const second = renderHook(() => usePredict());
    await act(async () => {
      await second.result.current.submit(request).catch(() => undefined);
    });
    expect(second.result.current.error).toBe("性质预测失败，请稍后重试。");
    second.unmount();

    mocks.predictSmiles.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    const third = renderHook(() => usePredict());
    await act(async () => {
      await third.result.current.submit(request).catch(() => undefined);
    });
    expect(third.result.current.error).toBe("性质预测请求失败，请检查网络或稍后重试。");
  });
});
