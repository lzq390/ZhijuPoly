import { afterEach, describe, expect, it, vi } from "vitest";
import {
  cancelMonomerMdJob,
  deleteMonomerMdJob,
  fetchDevGpuSessionStatus,
  fetchMonomerMdJobs,
  fetchPolytaoJob,
  fetchPolytaoStatus,
  recoverDevGpuSession
} from "./api";

describe("deleteMonomerMdJob", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("does not parse the empty 204 response body", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(deleteMonomerMdJob("a".repeat(32))).resolves.toBeUndefined();
    expect((fetchMock.mock.calls[0][1] as RequestInit).method).toBe("DELETE");
  });
});

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

describe("development GPU session API", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("uses only the fixed status and recovery routes", async () => {
    const response = {
      schema_version: 1,
      operator_available: true,
      phase: "stopped",
      controller_status: "stopped",
      can_recover: true,
      operation_id: null,
      message: "stopped",
      source_sha: "a".repeat(40),
      source_tree: "b".repeat(40),
      updated_at: "2026-07-27T00:00:00Z"
    };
    const fetchMock = vi
      .fn()
      .mockImplementation(() =>
        Promise.resolve(
          new Response(JSON.stringify(response), {
            status: 200,
            headers: { "Content-Type": "application/json" }
          })
        )
      );
    vi.stubGlobal("fetch", fetchMock);
    const controller = new AbortController();

    await fetchDevGpuSessionStatus(controller.signal);
    await recoverDevGpuSession(controller.signal);

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "/api/v1/dev-gpu-session/status",
      expect.objectContaining({ cache: "no-store", signal: controller.signal })
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/api/v1/dev-gpu-session/recover",
      expect.objectContaining({
        method: "POST",
        body: "{}",
        signal: controller.signal
      })
    );
  });
});

describe("monomer MD queue API", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("encodes every queue filter and forwards the caller AbortSignal", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          items: [
            {
              job_id: "formal-a",
              status: "cancel_requested",
              input_smiles: "CO",
              progress_percent: 25,
              progress_message: "cancelling"
            }
          ],
          total: 21,
          page: 2,
          page_size: 10
        }),
        { status: 200, headers: { "Content-Type": "application/json" } }
      )
    );
    vi.stubGlobal("fetch", fetchMock);
    const controller = new AbortController();

    const page = await fetchMonomerMdJobs(
      {
        run_mode: "formal",
        active_only: false,
        protocol: "Density",
        status: "cancel_requested",
        page: 2,
        page_size: 10
      },
      controller.signal
    );

    expect(fetchMock).toHaveBeenCalledOnce();
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/monomer-md/jobs?run_mode=formal&active_only=false&protocol=Density&status=cancel_requested&page=2&page_size=10",
      expect.objectContaining({ signal: controller.signal })
    );
    expect(page.items[0]).toMatchObject({
      smiles: "CO",
      progress: 25,
      message: "cancelling"
    });
  });

  it.each([200, 202])(
    "sends one abort-aware cancellation POST and accepts HTTP %i",
    async (status) => {
      const fetchMock = vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            job_id: "formal/a b",
            status: "cancel_requested",
            progress_percent: 90
          }),
          { status, headers: { "Content-Type": "application/json" } }
        )
      );
      vi.stubGlobal("fetch", fetchMock);
      const controller = new AbortController();

      const job = await cancelMonomerMdJob("formal/a b", controller.signal);

      expect(fetchMock).toHaveBeenCalledOnce();
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/v1/monomer-md/jobs/formal%2Fa%20b/cancel",
        expect.objectContaining({
          method: "POST",
          body: "{}",
          signal: controller.signal
        })
      );
      expect(job).toMatchObject({
        job_id: "formal/a b",
        status: "cancel_requested",
        progress: 90
      });
    }
  );

  it.each([404, 409])("preserves HTTP %i cancellation failures", async (status) => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: `cancel failed: ${status}` }), {
          status,
          headers: { "Content-Type": "application/json" }
        })
      )
    );

    await expect(cancelMonomerMdJob("formal-a")).rejects.toMatchObject({
      name: "ApiRequestError",
      status,
      message: `cancel failed: ${status}`
    });
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
