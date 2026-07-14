import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchPolytaoJob, fetchPolytaoStatus } from "./api";

describe("fetchPolytaoStatus", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("bypasses browser caches", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          enabled: true,
          available: true,
          worker_base_url_configured: false,
          default_params: {},
          message: "ready"
        }),
        { status: 200, headers: { "Content-Type": "application/json" } }
      )
    );
    vi.stubGlobal("fetch", fetchMock);

    await fetchPolytaoStatus();

    expect(fetchMock).toHaveBeenCalledOnce();
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/conditional-generation/polytao/status",
      expect.objectContaining({ cache: "no-store", signal: expect.any(AbortSignal) })
    );
  });

  it("aborts status requests after ten seconds", async () => {
    const setTimeoutSpy = vi
      .spyOn(globalThis, "setTimeout")
      .mockImplementation((callback: TimerHandler) => {
        if (typeof callback === "function") {
          callback();
        }
        return 1 as unknown as ReturnType<typeof setTimeout>;
      });
    vi.spyOn(globalThis, "clearTimeout").mockImplementation(() => undefined);
    const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      expect(init?.signal?.aborted).toBe(true);
      return Promise.reject(new Error("aborted"));
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(fetchPolytaoStatus()).rejects.toThrow("timed out after 10 seconds");

    expect(setTimeoutSpy).toHaveBeenCalledWith(expect.any(Function), 10_000);
  });
});

describe("job polling errors", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("preserves HTTP 410 so polling hooks can stop immediately", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: "Job expired" }), {
          status: 410,
          headers: { "Content-Type": "application/json" }
        })
      )
    );

    const request = fetchPolytaoJob("old-instance-polytao-job");
    await expect(request).rejects.toMatchObject({
      name: "ApiRequestError",
      status: 410,
      message: "Job expired"
    });
  });
});
