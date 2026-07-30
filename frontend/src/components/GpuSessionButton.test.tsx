// @vitest-environment jsdom

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiRequestError } from "../services/api";
import type { DevGpuSessionStatusResponse } from "../types";

const api = vi.hoisted(() => ({
  fetchDevGpuSessionStatus: vi.fn(),
  recoverDevGpuSession: vi.fn()
}));

vi.mock("../services/api", async () => {
  const actual = await vi.importActual<typeof import("../services/api")>("../services/api");
  return {
    ...actual,
    fetchDevGpuSessionStatus: api.fetchDevGpuSessionStatus,
    recoverDevGpuSession: api.recoverDevGpuSession
  };
});

import { GpuSessionButton, useDevGpuSessionControl } from "./GpuSessionButton";


const status = (
  phase: DevGpuSessionStatusResponse["phase"],
  overrides: Partial<DevGpuSessionStatusResponse> = {}
): DevGpuSessionStatusResponse => ({
  schema_version: 1,
  operator_available: true,
  phase,
  controller_status: phase,
  can_recover: phase === "stopped" || phase === "failed" || phase === "recovering",
  operation_id: null,
  message: `status: ${phase}`,
  source_sha: "a".repeat(40),
  source_tree: "b".repeat(40),
  updated_at: "2026-07-27T00:00:00Z",
  ...overrides
});

function Harness() {
  const control = useDevGpuSessionControl(true);
  return <GpuSessionButton control={control} statusId="gpu-test-status" />;
}

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((nextResolve) => {
    resolve = nextResolve;
  });
  return { promise, resolve };
}

describe("GpuSessionButton", () => {
  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.restoreAllMocks();
    vi.resetAllMocks();
  });

  it("shows ready as an always-visible disabled control", async () => {
    api.fetchDevGpuSessionStatus.mockResolvedValue(status("ready"));

    render(<Harness />);

    const button = await screen.findByRole("button", { name: "GPU 服务已启动" });
    expect((button as HTMLButtonElement).disabled).toBe(true);
  });

  it("deduplicates rapid recovery clicks into one POST", async () => {
    api.fetchDevGpuSessionStatus.mockResolvedValue(status("stopped"));
    let resolveRecovery: ((value: DevGpuSessionStatusResponse) => void) | undefined;
    api.recoverDevGpuSession.mockImplementation(
      () =>
        new Promise<DevGpuSessionStatusResponse>((resolve) => {
          resolveRecovery = resolve;
        })
    );

    render(<Harness />);
    const button = await screen.findByRole("button", { name: "一键恢复 GPU 服务" });
    fireEvent.click(button);
    fireEvent.click(button);

    await waitFor(() => expect(api.recoverDevGpuSession).toHaveBeenCalledTimes(1));
    resolveRecovery?.(
      status("starting", {
        can_recover: false,
        operation_id: "c".repeat(32)
      })
    );
    await screen.findByRole("button", { name: "GPU 服务启动中" });
  });

  it.each([
    ["HTTP 500", () => new ApiRequestError(500, "Request failed with status 500")],
    ["网络错误", () => new TypeError("Failed to fetch")]
  ])(
    "keeps the active phase and fast polling across a transient %s",
    async (_label, createError) => {
      vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
      api.fetchDevGpuSessionStatus
        .mockResolvedValueOnce(status("starting"))
        .mockRejectedValueOnce(createError())
        .mockResolvedValueOnce(status("starting"))
        .mockResolvedValueOnce(status("ready"));

      render(<Harness />);
      await flush();
      expect(screen.getByRole("button", { name: "GPU 服务启动中" })).toBeTruthy();

      await act(async () => {
        await vi.advanceTimersByTimeAsync(1_500);
      });
      expect(api.fetchDevGpuSessionStatus).toHaveBeenCalledTimes(2);
      expect(screen.getByRole("button", { name: "GPU 服务启动中" })).toBeTruthy();
      expect(
        screen.getByText("Backend 正在切换，正在重新连接 GPU 控制服务")
      ).toBeTruthy();

      await act(async () => {
        await vi.advanceTimersByTimeAsync(1_499);
      });
      expect(api.fetchDevGpuSessionStatus).toHaveBeenCalledTimes(2);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1);
      });
      expect(api.fetchDevGpuSessionStatus).toHaveBeenCalledTimes(3);
      expect(screen.getByRole("button", { name: "GPU 服务启动中" })).toBeTruthy();

      await act(async () => {
        await vi.advanceTimersByTimeAsync(1_500);
      });
      expect(api.fetchDevGpuSessionStatus).toHaveBeenCalledTimes(4);
      expect(screen.getByRole("button", { name: "GPU 服务已启动" })).toBeTruthy();
    }
  );

  it("does not hide a client status error behind the active phase", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    api.fetchDevGpuSessionStatus
      .mockResolvedValueOnce(status("starting"))
      .mockRejectedValueOnce(new ApiRequestError(409, "invalid status request"));

    render(<Harness />);
    await flush();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_500);
    });

    expect(
      screen.getByRole("button", { name: "GPU 控制服务不可用" })
    ).toBeTruthy();
    expect(screen.getByText("invalid status request")).toBeTruthy();
  });

  it("surfaces an operator-reported unavailable status immediately during startup", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    api.fetchDevGpuSessionStatus
      .mockResolvedValueOnce(status("starting"))
      .mockResolvedValueOnce(
        status("unavailable", {
          operator_available: false,
          can_recover: false,
          message: "operator socket unavailable"
        })
      );

    render(<Harness />);
    await flush();
    expect(screen.getByRole("button", { name: "GPU 服务启动中" })).toBeTruthy();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_500);
    });

    expect(screen.getByRole("button", { name: "GPU 控制服务不可用" })).toBeTruthy();
    expect(screen.getByText("operator socket unavailable")).toBeTruthy();
  });

  it("stops masking a persistent status outage after the reconnect grace period", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
    vi.setSystemTime(new Date("2026-07-30T00:00:00Z"));
    api.fetchDevGpuSessionStatus
      .mockResolvedValueOnce(status("starting"))
      .mockRejectedValue(new TypeError("persistent network error"));

    render(<Harness />);
    await flush();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_500);
    });
    expect(screen.getByRole("button", { name: "GPU 服务启动中" })).toBeTruthy();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(
      screen.getByRole("button", { name: "GPU 控制服务不可用" })
    ).toBeTruthy();
    expect(screen.getByText("persistent network error")).toBeTruthy();
  });

  it("ignores an old poll response that settles after recovery starts", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const stalePoll = deferred<DevGpuSessionStatusResponse>();
    api.fetchDevGpuSessionStatus
      .mockResolvedValueOnce(status("stopped"))
      .mockImplementationOnce(() => stalePoll.promise)
      .mockResolvedValueOnce(status("stopped"))
      .mockResolvedValueOnce(status("ready"));
    api.recoverDevGpuSession.mockResolvedValue(
      status("starting", {
        can_recover: false,
        operation_id: "c".repeat(32)
      })
    );

    render(<Harness />);
    await flush();
    expect(screen.getByRole("button", { name: "一键恢复 GPU 服务" })).toBeTruthy();

    act(() => {
      vi.advanceTimersByTime(5_000);
    });
    expect(api.fetchDevGpuSessionStatus).toHaveBeenCalledTimes(2);

    fireEvent.click(screen.getByRole("button", { name: "一键恢复 GPU 服务" }));
    await flush();
    expect(api.recoverDevGpuSession).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "GPU 服务启动中" })).toBeTruthy();

    stalePoll.resolve(
      status("unavailable", {
        operator_available: false,
        can_recover: false,
        message: "stale proxy failure"
      })
    );
    await flush();
    expect(screen.getByRole("button", { name: "GPU 服务启动中" })).toBeTruthy();
    expect(screen.queryByText("stale proxy failure")).toBeNull();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(api.fetchDevGpuSessionStatus).toHaveBeenCalledTimes(4);
    expect(screen.getByRole("button", { name: "GPU 服务已启动" })).toBeTruthy();
  });

  it("resumes polling after an in-flight recovery request is aborted", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    api.fetchDevGpuSessionStatus
      .mockResolvedValueOnce(status("stopped"))
      .mockResolvedValueOnce(status("stopped"))
      .mockResolvedValueOnce(status("ready"));
    api.recoverDevGpuSession.mockImplementation(
      (signal: AbortSignal) =>
        new Promise<never>((_resolve, reject) => {
          signal.addEventListener(
            "abort",
            () => reject(new DOMException("aborted", "AbortError")),
            { once: true }
          );
        })
    );

    render(<Harness />);
    await flush();
    fireEvent.click(screen.getByRole("button", { name: "一键恢复 GPU 服务" }));
    await flush();
    expect(api.recoverDevGpuSession).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(8_000);
    });
    await flush();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(api.fetchDevGpuSessionStatus).toHaveBeenCalledTimes(3);
    expect(screen.getByRole("button", { name: "GPU 服务已启动" })).toBeTruthy();
  });
});
