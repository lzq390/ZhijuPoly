import { afterEach, describe, expect, it, vi } from "vitest";
import {
  cancelMonomerMdJob,
  deleteMonomerMdJob,
  browseExperimentalProcessRecords,
  fetchDatabaseAnalytics,
  fetchDatabaseDatasetSummary,
  fetchDevGpuSessionStatus,
  fetchMonomerMdJobs,
  fetchPropertyFilterHistogram,
  fetchPropertyFilterOptions,
  fetchPolytaoJob,
  fetchPolytaoStatus,
  fetchStructure2D,
  fetchTgAssistantGuide,
  fetchTgAssistantStatus,
  recoverDevGpuSession,
  searchPropertyFilterRecords
} from "./api";

describe("Tg assistant metadata API", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("loads status and the versioned guide without caching", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({
        enabled: false,
        configured: true,
        image: {
          supported: true,
          max_files: 2,
          max_canvas_snapshots: 1,
          max_user_upload_files: 1,
          max_bytes: 5 * 1024 * 1024,
          max_total_bytes: 10 * 1024 * 1024,
          accepted_mime_types: ["image/png", "image/jpeg", "image/webp"]
        }
      }), {
        status: 200,
        headers: { "Content-Type": "application/json" }
      }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        module: "reverseDesign",
        version: 3,
        language: "zh-CN",
        defaults: {},
        sections: []
      }), {
        status: 200,
        headers: { "Content-Type": "application/json" }
      }));
    vi.stubGlobal("fetch", fetchMock);
    const controller = new AbortController();

    await fetchTgAssistantStatus(controller.signal);
    await fetchTgAssistantGuide(controller.signal);

    expect(fetchMock).toHaveBeenNthCalledWith(1, "/api/v1/assistant/tg/status", {
      cache: "no-store",
      signal: controller.signal
    });
    expect(fetchMock).toHaveBeenNthCalledWith(2, "/api/v1/assistant/tg/guide", {
      cache: "no-store",
      signal: controller.signal
    });
  });
});

describe("database analysis API", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("forwards AbortSignal to summary and refresh requests", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ query_time_ms: 1, backend: "postgres", datasets: [] }), {
        status: 200,
        headers: { "Content-Type": "application/json" }
      }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ query_time_ms: 2, backend: "postgres", source: "live", generated_at: null, datasets: {} }), {
        status: 200,
        headers: { "Content-Type": "application/json" }
      }));
    vi.stubGlobal("fetch", fetchMock);
    const controller = new AbortController();

    await fetchDatabaseDatasetSummary(controller.signal);
    await fetchDatabaseAnalytics({ refresh: true, signal: controller.signal });

    expect(fetchMock).toHaveBeenNthCalledWith(1, "/api/v1/database-browser/datasets/summary", { signal: controller.signal });
    expect(fetchMock).toHaveBeenNthCalledWith(2, "/api/v1/database-browser/datasets/analytics?refresh=true", { signal: controller.signal });
  });

  it("keeps record query contracts and forwards AbortSignal", async () => {
    const response = {
      query: "polyimide",
      page: 2,
      page_size: 10,
      query_time_ms: 1,
      total_records: 100,
      matched_records: 12,
      data_source: "postgres",
      source_status: "ready",
      source_message: null,
      results: []
    };
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(response), {
      status: 200,
      headers: { "Content-Type": "application/json" }
    }));
    vi.stubGlobal("fetch", fetchMock);
    const controller = new AbortController();

    await browseExperimentalProcessRecords({ q: "polyimide", page: 2, page_size: 10 }, controller.signal);

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/database-browser/experimental-process?q=polyimide&page=2&page_size=10",
      { signal: controller.signal }
    );
  });
});

describe("property filter API", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("uses the fixed options route and forwards AbortSignal", async () => {
    const payload = {
      query_time_ms: 1,
      total_records: 0,
      mapped_records: 0,
      raw_records: 0,
      data_source: "postgres",
      source_status: "empty",
      source_message: null,
      options: []
    };
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(payload), {
        status: 200,
        headers: { "Content-Type": "application/json" }
      })
    );
    vi.stubGlobal("fetch", fetchMock);
    const controller = new AbortController();

    const result = await fetchPropertyFilterOptions({ signal: controller.signal });

    expect(result).toEqual({ status: "success", data: payload, etag: null });
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/database-browser/property-filter/options",
      expect.objectContaining({
        cache: "no-cache",
        headers: expect.any(Headers),
        signal: controller.signal
      })
    );
  });

  it("sends the cached ETag and handles a 304 without parsing JSON", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(null, { status: 304, headers: { ETag: 'W/"pf-options-v1-2"' } })
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await fetchPropertyFilterOptions({ etag: 'W/"pf-options-v1-1"' });

    expect(result).toEqual({
      status: "not-modified",
      data: null,
      etag: 'W/"pf-options-v1-2"'
    });
    const requestInit = fetchMock.mock.calls[0][1] as RequestInit;
    expect(new Headers(requestInit.headers).get("If-None-Match")).toBe('W/"pf-options-v1-1"');
  });

  it("loads the selected property's real histogram with an encoded option key", async () => {
    const payload = {
      query_time_ms: 3,
      option_key: "std:tg:C",
      data_source: "postgres",
      source_status: "ready",
      source_message: null,
      histogram: {
        domain_min: -20,
        domain_max: 300,
        domain_kind: "p5_p95",
        bin_count: 2,
        counts: [4, 5],
        underflow_count: 1,
        overflow_count: 0,
        total_count: 10
      }
    };
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(payload), {
        status: 200,
        headers: { "Content-Type": "application/json", ETag: 'W/"histogram-1"' }
      })
    );
    vi.stubGlobal("fetch", fetchMock);
    const controller = new AbortController();

    const result = await fetchPropertyFilterHistogram("std:tg:C", {
      etag: 'W/"histogram-0"',
      signal: controller.signal
    });

    expect(result).toEqual({ status: "success", data: payload, etag: 'W/"histogram-1"' });
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/database-browser/property-filter/histogram?option_key=std%3Atg%3AC",
      expect.objectContaining({ cache: "no-cache", signal: controller.signal })
    );
    const requestInit = fetchMock.mock.calls[0][1] as RequestInit;
    expect(new Headers(requestInit.headers).get("If-None-Match")).toBe('W/"histogram-0"');
  });

  it("posts the unchanged search contract and forwards AbortSignal", async () => {
    const response = {
      query: "poly",
      page: 1,
      page_size: 25,
      query_time_ms: 2,
      total_records: 10,
      matched_records: 0,
      data_source: "postgres",
      source_status: "ready",
      source_message: null,
      results: []
    };
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(response), {
        status: 200,
        headers: { "Content-Type": "application/json" }
      })
    );
    vi.stubGlobal("fetch", fetchMock);
    const controller = new AbortController();
    const payload = {
      filters: [
        {
          filter_type: "standardized" as const,
          property_key: "tg",
          canonical_unit: "C",
          min_value: 100,
          max_value: 200
        }
      ],
      q: "poly",
      page: 1,
      page_size: 25
    };

    await searchPropertyFilterRecords(payload, controller.signal);

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/database-browser/property-filter/search",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify(payload),
        signal: controller.signal
      })
    );
  });
});

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

describe("fetchStructure2D", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("posts the shared SMILES and forwards AbortSignal", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ structure_svg: "<svg />" }), {
        status: 200,
        headers: { "Content-Type": "application/json" }
      })
    );
    vi.stubGlobal("fetch", fetchMock);
    const controller = new AbortController();

    await expect(fetchStructure2D("*CC*", controller.signal)).resolves.toEqual({
      structure_svg: "<svg />"
    });
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/structure/2d",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ smiles: "*CC*" }),
        signal: controller.signal
      })
    );
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
