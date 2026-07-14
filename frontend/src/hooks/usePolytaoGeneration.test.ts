import { describe, expect, it } from "vitest";
import type { PolytaoStatusResponse } from "../types";
import {
  getPolytaoRuntimeDisplayState,
  shouldAutoRefreshPolytaoStatus
} from "./usePolytaoGeneration";

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
