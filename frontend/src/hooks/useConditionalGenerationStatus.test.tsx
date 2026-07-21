// @vitest-environment jsdom

import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ConditionalGenerationTgStatusResponse } from "../types";
import { CONDITIONAL_STATUS_RETRY_DELAYS_MS, useConditionalGenerationStatus } from "./useConditionalGenerationStatus";

const apiMocks = vi.hoisted(() => ({
  fetchStatus: vi.fn()
}));

vi.mock("../services/api", async () => {
  const actual = await vi.importActual<typeof import("../services/api")>("../services/api");
  return {
    ...actual,
    fetchConditionalGenerationTgStatus: apiMocks.fetchStatus
  };
});

const readyStatus: ConditionalGenerationTgStatusResponse = {
  enabled: true,
  available: true,
  model_dir: "/app/model/conditional-generation",
  missing_artifacts: [],
  message: "ready"
};

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

describe("useConditionalGenerationStatus", () => {
  beforeEach(() => {
    apiMocks.fetchStatus.mockReset();
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("stops after finite retries and lets a manual refresh recover", async () => {
    apiMocks.fetchStatus.mockRejectedValue(new TypeError("status network unavailable"));
    const { result, unmount } = renderHook(() => useConditionalGenerationStatus());
    await flush();
    expect(apiMocks.fetchStatus).toHaveBeenCalledTimes(1);

    for (let index = 0; index < CONDITIONAL_STATUS_RETRY_DELAYS_MS.length; index += 1) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(CONDITIONAL_STATUS_RETRY_DELAYS_MS[index]);
      });
      expect(apiMocks.fetchStatus).toHaveBeenCalledTimes(index + 2);
    }

    expect(result.current.isStatusLoading).toBe(false);
    expect(result.current.serviceStatus).toBeNull();
    expect(result.current.serviceStatusError).toBe("status network unavailable");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(apiMocks.fetchStatus).toHaveBeenCalledTimes(5);

    apiMocks.fetchStatus.mockResolvedValue(readyStatus);
    await act(async () => {
      await result.current.refreshStatus();
    });

    expect(apiMocks.fetchStatus).toHaveBeenCalledTimes(6);
    expect(result.current.serviceStatus).toEqual(readyStatus);
    expect(result.current.serviceStatusError).toBeNull();
    expect(result.current.isStatusLoading).toBe(false);
    expect(apiMocks.fetchStatus).toHaveBeenLastCalledWith(expect.any(AbortSignal));
    unmount();
  });

  it("aborts an in-flight status request on unmount", async () => {
    const signals: AbortSignal[] = [];
    apiMocks.fetchStatus.mockImplementation((nextSignal: AbortSignal) => {
      signals.push(nextSignal);
      return new Promise((_resolve, reject) => {
        nextSignal.addEventListener(
          "abort",
          () => reject(new DOMException("aborted", "AbortError")),
          { once: true }
        );
      });
    });

    const { unmount } = renderHook(() => useConditionalGenerationStatus());
    await flush();
    expect(signals).toHaveLength(1);
    unmount();
    expect(signals[0].aborted).toBe(true);
  });
});
