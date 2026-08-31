// @vitest-environment jsdom

import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiRequestError } from "../services/api";
import type { PolytaoJobStatusResponse, PolytaoStatusResponse } from "../types";
import {
  getPolytaoRuntimeDisplayState,
  shouldAutoRefreshPolytaoStatus,
  usePolytaoGeneration
} from "./usePolytaoGeneration";

const api = vi.hoisted(() => ({
  createPolytaoJob: vi.fn(),
  fetchPolytaoJob: vi.fn(),
  fetchPolytaoStatus: vi.fn()
}));

vi.mock("../services/api", async () => {
  const actual = await vi.importActual<typeof import("../services/api")>("../services/api");
  return {
    ...actual,
    createPolytaoJob: api.createPolytaoJob,
    fetchPolytaoJob: api.fetchPolytaoJob,
    fetchPolytaoStatus: api.fetchPolytaoStatus
  };
});

function status(overrides: Partial<PolytaoStatusResponse> = {}): PolytaoStatusResponse {
  return {
    enabled: true,
    available: true,
    worker_base_url_configured: false,
    worker_status: "cold",
    worker_mode: null,
    db_configured: null,
    db_ready: null,
    db_error: null,
    runtime_ready: false,
    runtime_error: null,
    active_jobs: 0,
    model_id: "polytao",
    model_revision: null,
    default_params: {},
    worker_version: null,
    message: "PolyTAO backend runtime is cold",
    ...overrides
  };
}

function terminalJob(overrides: Partial<PolytaoJobStatusResponse> = {}): PolytaoJobStatusResponse {
  return {
    job_id: "polytao-job",
    status: "failed",
    input_smiles: null,
    canonical_smiles: null,
    prompt: "",
    requested_count: 10,
    returned_count: 0,
    attempts: 1,
    progress_percent: 100,
    progress_stage: "failed",
    progress_message: "",
    created_at: "2026-08-28T00:00:00Z",
    updated_at: "2026-08-28T00:00:01Z",
    started_at: "2026-08-28T00:00:00Z",
    finished_at: "2026-08-28T00:00:01Z",
    worker_id: null,
    worker_job_id: null,
    worker_version: null,
    engine: "polytao-backend",
    error_message: null,
    result: null,
    ...overrides
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  api.fetchPolytaoStatus.mockResolvedValue(status({ runtime_ready: true, worker_status: "ready" }));
  api.createPolytaoJob.mockResolvedValue({ job_id: "polytao-job", status: "submitted" });
});

describe("getPolytaoRuntimeDisplayState", () => {
  it("starts in the checking state", () => {
    expect(getPolytaoRuntimeDisplayState(null, null, true)).toBe("checking");
  });

  it("distinguishes disabled and database failures", () => {
    expect(
      getPolytaoRuntimeDisplayState(status({ enabled: false, available: false }), null, false)
    ).toBe("disabled");
    expect(
      getPolytaoRuntimeDisplayState(status({ available: false, db_ready: false }), null, false)
    ).toBe("db_unavailable");
    expect(getPolytaoRuntimeDisplayState(status(), null, false)).toBe("cold");
  });

  it("distinguishes cold, loading, ready, and retryable runtime errors", () => {
    expect(getPolytaoRuntimeDisplayState(status(), null, false)).toBe("cold");
    expect(
      getPolytaoRuntimeDisplayState(status({ worker_status: "loading" }), null, false)
    ).toBe("loading");
    expect(
      getPolytaoRuntimeDisplayState(status({ runtime_ready: true, worker_status: "ready" }), null, false)
    ).toBe("ready");
    expect(
      getPolytaoRuntimeDisplayState(status({ runtime_error: "checkpoint failed" }), null, false)
    ).toBe("runtime_error");
  });

  it("reports a status request failure as a runtime error", () => {
    expect(getPolytaoRuntimeDisplayState(null, "offline", false)).toBe("runtime_error");
  });
});

describe("shouldAutoRefreshPolytaoStatus", () => {
  it("refreshes unknown, unavailable, and cold/loading states", () => {
    expect(shouldAutoRefreshPolytaoStatus(null, null)).toBe(true);
    expect(shouldAutoRefreshPolytaoStatus(status({ available: false }), null)).toBe(true);
    expect(shouldAutoRefreshPolytaoStatus(status(), null)).toBe(true);
    expect(shouldAutoRefreshPolytaoStatus(status({ worker_status: "loading" }), null)).toBe(true);
  });

  it("stops automatic polling when the runtime is ready", () => {
    expect(shouldAutoRefreshPolytaoStatus(status({ runtime_ready: true }), null)).toBe(false);
  });
});

describe("usePolytaoGeneration local messages", () => {
  it("uses Chinese fallback text for failed and expired jobs", async () => {
    api.fetchPolytaoJob.mockResolvedValueOnce(terminalJob());
    const failed = renderHook(() => usePolytaoGeneration());
    await waitFor(() => expect(failed.result.current.serviceStatus?.runtime_ready).toBe(true));
    await act(async () => {
      await failed.result.current.submit();
    });
    expect(failed.result.current.error).toBe("PolyTAO 生成任务执行失败。");
    failed.unmount();

    api.fetchPolytaoJob.mockRejectedValueOnce(new ApiRequestError(410, "gone"));
    const expired = renderHook(() => usePolytaoGeneration());
    await waitFor(() => expect(expired.result.current.serviceStatus?.runtime_ready).toBe(true));
    await act(async () => {
      await expired.result.current.submit();
    });
    expect(expired.result.current.error).toBe("后端服务已重启或生成任务已过期，请重新提交。");
    expired.unmount();
  });

  it("keeps backend-provided task details ahead of local fallbacks", async () => {
    api.fetchPolytaoJob.mockResolvedValueOnce(
      terminalJob({ error_message: "后端返回的明确失败原因" })
    );
    const view = renderHook(() => usePolytaoGeneration());
    await waitFor(() => expect(view.result.current.serviceStatus?.runtime_ready).toBe(true));
    await act(async () => {
      await view.result.current.submit();
    });
    expect(view.result.current.error).toBe("后端返回的明确失败原因");
    view.unmount();
  });
});
