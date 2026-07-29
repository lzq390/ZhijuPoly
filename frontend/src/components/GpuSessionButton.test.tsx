// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
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

describe("GpuSessionButton", () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
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
});
