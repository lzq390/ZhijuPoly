import { afterEach, describe, expect, it, vi } from "vitest";
import {
  createMonomerDftJob,
  deleteMonomerDftArtifactsAndReloadJob,
  fetchMonomerDftCapabilities,
  fetchMonomerDftJobs,
  getMonomerDftArtifactUrl,
  getMonomerDftBundleUrl,
  parseMonomerDftRetryAfterSeconds
} from "./api";
import type { MonomerDftJobCreateRequest } from "../types";

const request: MonomerDftJobCreateRequest = {
  calculation_type: "optimization",
  input: { smiles: "CCO", net_charge: null, multiplicity: 1, psmiles_mode: null },
  model: "aimnet2",
  conformer: { seed: 1, max_iterations: 500 },
  optimization: { fmax_eV_per_A: 0.01, max_steps: 50, post_optimization_properties: ["frequencies"] }
};

describe("monomer DFT API client", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("keeps idempotency out of the strict JSON body", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ job_id: "job", status: "pending" }), { status: 202, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);

    await createMonomerDftJob(request, "5bcf0cb8-b593-4cb9-9e7f-f2bd7327ece7");

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect((init.headers as Record<string, string>)["Idempotency-Key"]).toBe("5bcf0cb8-b593-4cb9-9e7f-f2bd7327ece7");
    expect(JSON.parse(String(init.body))).toEqual(request);
    expect(String(init.body)).not.toContain("request_id");
  });

  it("uses the approved paged global-history query", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ items: [], page: 2, page_size: 20, total: 0 }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    await fetchMonomerDftJobs({ page: 2, page_size: 20, status: "completed", calculation_type: "single_point" });
    const url = String(fetchMock.mock.calls[0][0]);
    expect(url).toContain("/monomer-dft/jobs?");
    expect(url).toContain("page=2");
    expect(url).toContain("page_size=20");
    expect(url).toContain("status=completed");
    expect(url).toContain("calculation_type=single_point");
  });

  it("omits inactive history filters instead of sending invalid empty literals", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ items: [], page: 1, page_size: 20, total: 0 }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    await fetchMonomerDftJobs({ page: 1, page_size: 20, status: "", calculation_type: "" });
    const url = new URL(String(fetchMock.mock.calls[0][0]), "http://frontend.test");
    expect(url.searchParams.get("page")).toBe("1");
    expect(url.searchParams.get("page_size")).toBe("20");
    expect(url.searchParams.has("status")).toBe(false);
    expect(url.searchParams.has("calculation_type")).toBe(false);
  });

  it("normalizes a legacy string model catalog into capability descriptors", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      enabled: true,
      available: true,
      calculation_types: ["single_point", "optimization"],
      models: ["aimnet2", "aimnet2-nse"],
      properties: ["energy", "charges", "forces", "hessian", "frequencies"],
      limits: {}
    }), { status: 200, headers: { "Content-Type": "application/json" } })));
    const capabilities = await fetchMonomerDftCapabilities();
    expect(capabilities.default_model).toBe("aimnet2");
    expect(capabilities.models.find((model) => model.id === "aimnet2-nse")?.supports_spin).toBe(true);
    expect(capabilities.defaults.optimization.fmax_eV_per_A).toBe(0.01);
    expect(capabilities.limits).toMatchObject({
      max_concurrent_jobs: 1,
      max_queued_jobs: 8,
      max_active_jobs: 9
    });
  });

  it("reloads the canonical job after deleting artifacts", async () => {
    const canonicalJob = {
      job_id: "188817d4-bdd2-4e25-9336-bfcbccc16a61",
      artifacts_state: "deleted",
      artifacts_deleted: true,
      artifacts: [{ artifact_id: "scientific_result", available: false }],
      updated_at: "2026-07-14T09:00:00Z"
    };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({
        job_id: canonicalJob.job_id,
        deleted: true,
        deleted_artifacts: 1,
        artifacts_state: "deleted",
        message: "deleted"
      }), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify(canonicalJob), {
        status: 200,
        headers: { "Content-Type": "application/json" }
      }));
    vi.stubGlobal("fetch", fetchMock);

    const reloadedJob = await deleteMonomerDftArtifactsAndReloadJob(canonicalJob.job_id);

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(String(fetchMock.mock.calls[0][0])).toContain(`/monomer-dft/jobs/${canonicalJob.job_id}/artifacts`);
    expect((fetchMock.mock.calls[0][1] as RequestInit).method).toBe("DELETE");
    expect(String(fetchMock.mock.calls[1][0])).toContain(`/monomer-dft/jobs/${canonicalJob.job_id}`);
    expect((fetchMock.mock.calls[1][1] as RequestInit | undefined)?.method).toBeUndefined();
    expect(reloadedJob).toEqual(canonicalJob);
  });

  it("exposes artifact-id and bundle URLs without host filesystem paths", () => {
    expect(getMonomerDftArtifactUrl("job id", "result/json")).toBe("/api/v1/monomer-dft/jobs/job%20id/artifacts/result%2Fjson");
    expect(getMonomerDftBundleUrl("job id")).toBe("/api/v1/monomer-dft/jobs/job%20id/bundle");
  });

  it("preserves structured retry information", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: { code: "capacity_full", message: "busy", retryable: true } }), { status: 429, headers: { "Content-Type": "application/json", "Retry-After": "9" } })));
    await expect(fetchMonomerDftJobs({ page: 1, page_size: 20 })).rejects.toMatchObject({ status: 429, code: "capacity_full", retryable: true, retryAfterSeconds: 9 });
  });

  it("parses Retry-After delay seconds and HTTP dates with a 1-60 second bound", () => {
    const now = Date.parse("2026-07-14T00:00:00Z");
    expect(parseMonomerDftRetryAfterSeconds("9", now)).toBe(9);
    expect(parseMonomerDftRetryAfterSeconds("0", now)).toBe(1);
    expect(parseMonomerDftRetryAfterSeconds("900", now)).toBe(60);
    expect(parseMonomerDftRetryAfterSeconds("Tue, 14 Jul 2026 00:00:12 GMT", now)).toBe(12);
    expect(parseMonomerDftRetryAfterSeconds("Mon, 13 Jul 2026 00:00:00 GMT", now)).toBe(1);
    expect(parseMonomerDftRetryAfterSeconds("not-a-date", now)).toBeNull();
  });
});
