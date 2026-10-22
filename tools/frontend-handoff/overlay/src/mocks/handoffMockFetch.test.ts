// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";
import entitlementsFixture from "../../scripts/fixtures/entitlements.json";
import { installSession, privateFetch, retireSession } from "../auth/session";
import {
  createHandoffMockFetch,
  HANDOFF_MOCK_SCENARIOS
} from "./handoffMockFetch";

const PAGE_URL = "http://127.0.0.1:5173/database";

function mockFetch(pageUrl = PAGE_URL) {
  return createHandoffMockFetch({
    pageUrl,
    apiBaseUrl: "/api/v1",
    nativeFetch: vi.fn().mockResolvedValue(new Response(null, { status: 204 }))
  });
}

describe("handoff mock fetch", () => {
  it("exposes the four documented scenarios", () => {
    expect(HANDOFF_MOCK_SCENARIOS).toEqual(["success", "loading", "empty", "error"]);
  });

  it("returns deterministic success and empty query payloads", async () => {
    const success = await mockFetch()(`${PAGE_URL.replace("/database", "")}/api/v1/query/smiles`, {
      method: "POST",
      body: JSON.stringify({ smiles: "CCO", match_mode: "structure" })
    });
    expect(success.status).toBe(200);
    expect((await success.json()).results).toHaveLength(1);

    const empty = await mockFetch(`${PAGE_URL}?mockScenario=empty`)("/api/v1/query/smiles", {
      method: "POST",
      body: JSON.stringify({ smiles: "CCO", match_mode: "structure" })
    });
    expect((await empty.json()).results).toEqual([]);
  });

  it("returns a controlled error without contacting a backend", async () => {
    const response = await mockFetch(`${PAGE_URL}?mockScenario=error`)("/api/v1/predict", {
      method: "POST",
      body: "{}"
    });
    expect(response.status).toBe(503);
    expect((await response.json()).code).toBe("handoff_mock_error");
  });

  it("keeps loading requests pending until the caller aborts", async () => {
    const controller = new AbortController();
    const pending = mockFetch(`${PAGE_URL}?mockScenario=loading`)("/api/v1/predict", {
      signal: controller.signal
    });
    controller.abort();
    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
  });

  it("blocks external requests and delegates same-origin static assets", async () => {
    const nativeFetch = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    const fetcher = createHandoffMockFetch({
      pageUrl: PAGE_URL,
      apiBaseUrl: "/api/v1",
      nativeFetch
    });

    const blocked = await fetcher("https://example.invalid/private");
    expect(blocked.status).toBe(451);
    expect(nativeFetch).not.toHaveBeenCalled();

    const local = await fetcher("/images/nexpoly-black-gold-hero.png");
    expect(local.status).toBe(204);
    expect(nativeFetch).toHaveBeenCalledOnce();
  });

  it("reports the synthetic PolyTAO runtime as ready without an external worker", async () => {
    const response = await mockFetch()("/api/v1/conditional-generation/polytao/status");
    expect(await response.json()).toMatchObject({
      available: true,
      runtime_ready: true,
      db_configured: true,
      db_ready: true,
      worker_base_url_configured: false,
      worker_mode: "handoff"
    });
  });

  const coveredRequests: Array<[string, string]> = [
    ["POST", "/query/smiles"],
    ["POST", "/predict"],
    ["GET", "/dev-gpu-session/status"],
    ["POST", "/database-browser/smiles-lookup"],
    ["GET", "/md-demo/defaults"],
    ["POST", "/md-demo/run"],
    ["POST", "/md-demo/atom-distance"],
    ["GET", "/monomer-dft/status"],
    ["GET", "/monomer-dft/capabilities"],
    ["POST", "/monomer-dft/jobs"],
    ["GET", "/monomer-dft/jobs/d0000000-0000-4000-8000-000000000001"],
    ["GET", "/monomer-md/status"],
    ["GET", "/monomer-md/protocols"],
    ["POST", "/monomer-md/jobs"],
    ["GET", "/monomer-md/jobs/a0000000000000000000000000000001"],
    ["POST", "/knowledge/search"],
    ["GET", "/online-knowledge/default-config"],
    ["POST", "/online-knowledge/jobs"],
    ["GET", "/online-knowledge/jobs/synthetic-job"],
    ["GET", "/online-knowledge/history"],
    ["POST", "/reverse-design/tg/jobs"],
    ["GET", "/reverse-design/tg/jobs/synthetic-job"],
    ["GET", "/conditional-generation/tg/status"],
    ["POST", "/conditional-generation/tg/jobs"],
    ["GET", "/conditional-generation/tg/jobs/synthetic-job"],
    ["GET", "/conditional-generation/polytao/status"],
    ["POST", "/conditional-generation/polytao/descriptors"],
    ["POST", "/conditional-generation/polytao/jobs"],
    ["GET", "/conditional-generation/polytao/jobs/synthetic-job"],
    ["POST", "/structure/3d"],
    ["POST", "/structure/2d"],
    ["POST", "/structure/standardize-smiles"],
    ["POST", "/structure/recognize-image"],
    ["POST", "/monomer-retrosynthesis"],
    ["GET", "/monomer-polymerization/status"],
    ["POST", "/monomer-polymerization"],
    ["GET", "/dft/pca-sample?limit=20"],
    ["GET", "/dft/molecule/SYN-DFT-001"],
    ["GET", "/database-browser/property-filter/options"],
    ["GET", "/database-browser/property-filter/histogram?option_key=standardized%3Atg%3AC"],
    ["POST", "/database-browser/property-filter/search"],
    ["GET", "/database-browser/datasets/summary"],
    ["GET", "/database-browser/datasets/analytics"],
    ["GET", "/database-browser/structure-property"],
    ["GET", "/database-browser/dft/molecules"],
    ["GET", "/database-browser/dft/steps"],
    ["GET", "/database-browser/experimental-process"],
    ["GET", "/database-browser/experimental-property"],
    ["GET", "/database-browser/formulation"],
    ["GET", "/lab-data/test-projects"],
    ["GET", "/lab-data/sample-measurements"],
    ["GET", "/lab-data/sample-measurements/count"],
    ["GET", "/lab-data/sample-measurements/stats/by-project"],
    ["GET", "/lab-data/sample-measurements/stats/summary"]
  ];

  it.each(coveredRequests)("covers %s %s", async (method, path) => {
    const response = await mockFetch()(`/api/v1${path}`, {
      method,
      body: method === "POST" ? "{}" : undefined
    });
    expect(response.status).not.toBe(501);
    expect(response.status).toBeLessThan(400);
  });

  it("fails closed for an unimplemented API path", async () => {
    const response = await mockFetch()("/api/v1/not-implemented");
    expect(response.status).toBe(501);
    expect((await response.json()).code).toBe("handoff_mock_missing");
  });
});

describe("current handoff contracts", () => {
  it.each(HANDOFF_MOCK_SCENARIOS)("keeps login, password and logout usable in the %s scenario", async scenario => {
    const fetcher = mockFetch(`${PAGE_URL}?mockScenario=${scenario}&mockAuth=guest`);
    expect(await (await fetcher("/api/v1/auth/session")).json()).toMatchObject({ authenticated: false });
    const login = await fetcher("/api/v1/auth/login", { method: "POST", body: JSON.stringify({ username: "handoff", password: "demo-password" }) });
    expect(await login.json()).toMatchObject({ authenticated: true, user: { username: "handoff" } });
    expect((await fetcher("/api/v1/auth/password", { method: "POST", body: JSON.stringify({ current_password: "demo-password", new_password: "another-demo-password" }) })).status).toBe(204);
    expect((await fetcher("/api/v1/auth/logout", { method: "POST", body: "{}" })).status).toBe(204);
    expect(await (await fetcher("/api/v1/auth/session")).json()).toMatchObject({ authenticated: false });
    expect((await fetcher("/api/v1/predict", { method: "POST", body: "{}" })).status).toBe(401);
  });

  it.each(HANDOFF_MOCK_SCENARIOS)("reports unknown endpoints as 501 even in %s", async scenario => {
    expect((await mockFetch(`${PAGE_URL}?mockScenario=${scenario}`)("/api/v1/missing-endpoint")).status).toBe(501);
  });

  it("predicts every requested property rather than only Tg", async () => {
    const properties = ["Glass transition temperature", "Melting temperature", "O2 Permeability Barrer"];
    const response = await mockFetch()("/api/v1/predict", { method: "POST", body: JSON.stringify({ smiles: "*CC*", properties }) });
    expect(Object.keys((await response.json()).predictions)).toEqual(properties);
  });

  it.each(["md", "dft"])("retains %s create/cancel/delete changes and publishes task snapshots", async module => {
    const fetcher = mockFetch();
    const controller = new AbortController();
    const response = await fetcher(`/api/v1/task-events?module=${module}`, { signal: controller.signal });
    expect(response.headers.get("content-type")).toBe("text/event-stream");
    const reader = response.body!.getReader();
    const decode = (value: Uint8Array | undefined) => new TextDecoder().decode(value);
    expect(decode((await reader.read()).value)).toContain('"module":"' + module + '"');
    const created = await (await fetcher(`/api/v1/monomer-${module}/jobs`, { method: "POST", body: JSON.stringify({ smiles: "CCO" }) })).json();
    expect(decode((await reader.read()).value)).toContain(created.job_id);
    expect((await (await fetcher(`/api/v1/monomer-${module}/jobs`)).json()).total).toBe(3);
    const cancelled = await (await fetcher(`/api/v1/monomer-${module}/jobs/${created.job_id}/cancel`, { method: "POST" })).json();
    expect(cancelled.status).toBe("cancelled");
    expect(decode((await reader.read()).value)).toContain('"status":"cancelled"');
    expect((await (await fetcher(`/api/v1/monomer-${module}/jobs/${created.job_id}`)).json()).status).toBe("cancelled");
    expect((await fetcher(`/api/v1/monomer-${module}/jobs/${created.job_id}`, { method: "DELETE" })).status).toBe(204);
    const deleted = decode((await reader.read()).value);
    expect(deleted).not.toContain(created.job_id);
    expect((await fetcher(`/api/v1/monomer-${module}/jobs/${created.job_id}`)).status).toBe(404);
    // No end-of-stream after a snapshot: a pending read exits only on abort.
    const pending = reader.read();
    controller.abort();
    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
    reader.releaseLock();
  });

  it("removes task stream abort listeners on reader cancellation", async () => {
    const controller = new AbortController();
    const remove = vi.spyOn(controller.signal, "removeEventListener");
    const response = await mockFetch()("/api/v1/task-events?module=dft", { signal: controller.signal });
    const reader = response.body!.getReader();
    await reader.read(); await reader.cancel();
    expect(remove).toHaveBeenCalledWith("abort", expect.any(Function));
    controller.abort();
    reader.releaseLock();
  });

  it("returns terminal assistant and recording summary events", async () => {
    const fetcher = mockFetch();
    const assistant = await fetcher("/api/v1/assistant/tg/chat/stream", { method: "POST", body: "{}" });
    const text = await assistant.text();
    expect(text).toContain("event: token");
    expect(text).toContain("event: done");
    await fetcher("/api/v1/knowledge/recordings", { method: "POST", body: JSON.stringify({ recording_id: "demo-recording" }) });
    await fetcher("/api/v1/knowledge/observations", { method: "POST", body: JSON.stringify({ recording_id: "demo-recording", event: "article.opened" }) });
    const stopped = await (await fetcher("/api/v1/knowledge/recordings/demo-recording/stop", { method: "POST" })).json();
    expect(stopped.events).toHaveLength(1);
    const summary = await fetcher("/api/v1/knowledge/recordings/demo-recording/summary", { method: "POST", headers: { Accept: "text/event-stream" } });
    expect(await summary.text()).toContain('event: done\ndata: {"recording_id":"demo-recording"');
  });

  it("aborts an assistant stream without leaving its delayed emissions running", async () => {
    const controller = new AbortController();
    const remove = vi.spyOn(controller.signal, "removeEventListener");
    const response = await mockFetch()("/api/v1/assistant/tg/chat/stream", { method: "POST", body: "{}", signal: controller.signal });
    const reader = response.body!.getReader(); await reader.read();
    const pending = reader.read(); controller.abort();
    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
    expect(remove).toHaveBeenCalledWith("abort", expect.any(Function));
    reader.releaseLock();
  });

  it("uses an isolated batch import, idempotent submission and paged result contract", async () => {
    const fetcher = mockFetch();
    const form = new FormData();
    form.append("file_a", new File(["id,smiles\nA,NCCN"], "my-a.csv", { type: "text/csv" }));
    form.append("file_b", new File(["id,smiles\nB,O=C(O)CC(=O)O"], "my-b.csv", { type: "text/csv" }));
    const imported = await (await fetcher("/api/v1/monomer-polymerization/batch/imports", { method: "POST", body: form })).json();
    expect(imported.files.a.filename).toBe("my-a.csv");
    const preview = await (await fetcher(`/api/v1/monomer-polymerization/batch/imports/${imported.import_id}/preview`, { method: "POST", body: "{}" })).json();
    expect(preview.can_submit).toBe(true);
    const init = { method: "POST", headers: { "Idempotency-Key": "demo-submit" }, body: JSON.stringify({ import_id: imported.import_id, preview_revision: preview.preview_revision, target_class: "polyamide" }) };
    const job = await (await fetcher("/api/v1/monomer-polymerization/batch/jobs", init)).json();
    const replay = await (await fetcher("/api/v1/monomer-polymerization/batch/jobs", init)).json();
    expect(replay.job_id).toBe(job.job_id);
    expect(job.job_id).toMatch(/^[0-9a-f]{32}$/);
    expect(Object.keys(job.artifacts)).toContain("results.zip");
    const results = await (await fetcher(`/api/v1/monomer-polymerization/batch/jobs/${job.job_id}/results`)).json();
    expect(results.items).toHaveLength(4);
    expect((await (await fetcher(`/api/v1/monomer-polymerization/batch/jobs/${job.job_id}/results?offset=50`)).json()).items).toEqual([]);
    expect((await (await fetcher("/api/v1/monomer-polymerization/batch/jobs")).json()).total).toBe(1);
    await fetcher(`/api/v1/monomer-polymerization/batch/jobs/${job.job_id}/cancel`, { method: "POST" });
    expect((await (await fetcher(`/api/v1/monomer-polymerization/batch/jobs/${job.job_id}`)).json()).status).toBe("cancelled");
  });
});

describe("handoff result fidelity", () => {
  it("serves a timeline the production MD decoder can decompress", async () => {
    const { decodeMonomerMdTrajectoryTimeline } = await import("../components/monomer-md-simulation/trajectoryTimeline");
    const response = await mockFetch()("/api/v1/monomer-md/jobs/a0000000000000000000000000000001/visualization/stages/synthetic-stage/trajectory");
    expect(response.status).toBe(200);
    const decoded = await decodeMonomerMdTrajectoryTimeline(await response.json());
    expect(decoded).toBeTruthy();
  });

  it("matches the single-monomer polymerization input count", async () => {
    const response = await mockFetch()("/api/v1/monomer-polymerization", { method: "POST", body: JSON.stringify({ monomer_a_smiles: "C=C", target_class: "polyolefin" }) });
    const result = await response.json();
    expect(result.input_monomers).toHaveLength(1);
    expect(result.results[0]).toMatchObject({ monomer_a_smiles: "C=C", monomer_b_smiles: null });
  });
});

it("provides coherent DFT optimization results and downloadable trajectory frames", async () => {
  const { buildFrames } = await import("../components/MonomerDftResults");
  const fetcher = mockFetch();
  const job = await (await fetcher("/api/v1/monomer-dft/jobs", { method: "POST", body: JSON.stringify({ calculation_type: "optimization", input: { smiles: "O", net_charge: 0, multiplicity: 1 }, model: "aimnet2" }) })).json();
  expect(job.job_id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-8[0-9a-f]{3}-[0-9a-f]{12}$/);
  expect(job.result.calculation_type).toBe("optimization");
  const artifact = await (await fetcher(`/api/v1/monomer-dft/jobs/${job.job_id}/artifacts/trajectory`)).json();
  const frames = buildFrames(job.result, artifact);
  expect(frames.length).toBeGreaterThan(1);
  expect(frames.every(frame => frame.atoms.length === job.result.atoms.count)).toBe(true);
  await fetcher(`/api/v1/monomer-dft/jobs/${job.job_id}/artifacts`, { method: "DELETE" });
  expect((await fetcher(`/api/v1/monomer-dft/jobs/${job.job_id}/artifacts/trajectory`)).status).toBe(410);
});

it("shows newly collected synthetic lab measurements in the list and summary", async () => {
  const fetcher = mockFetch();
  await fetcher("/api/v1/lab-data/sample-measurements", { method: "POST", body: JSON.stringify({ sampleId: "SYN-NEW", experimentProject: "Synthetic tensile test", resultValue: 75, resultUnit: "MPa" }) });
  expect((await (await fetcher("/api/v1/lab-data/sample-measurements?sampleId=SYN-NEW")).json()).items).toHaveLength(1);
  expect((await (await fetcher("/api/v1/lab-data/sample-measurements/stats/summary")).json()).totalCount).toBe(2);
});

describe("handoff entitlement control contract", () => {
  it.each(HANDOFF_MOCK_SCENARIOS)("preserves the canonical enabled example policy in %s", async scenario => {
    const nativeFetch = vi.fn<typeof fetch>();
    const fetcher = createHandoffMockFetch({ pageUrl: PAGE_URL + "?mockScenario=" + scenario, nativeFetch });
    const response = await fetcher("/api/v1/usage/entitlements");
    expect(response.status).toBe(200);
    expect(response.headers.get("cache-control")).toBe("no-store");
    const snapshot = await response.json();
    expect(snapshot).toEqual(entitlementsFixture.entitlements);
    expect(snapshot.enabled).toBe(true);
    expect(snapshot.policy.example).toBe(true);
    expect(snapshot.policy.modules.retrosynthesis.variants.default.fields.num_return_sequences.max).toBe(10);
    expect(snapshot.policy.modules.structure.variants["3d"].fields.atom_count.max).toBe(500);
    expect(snapshot.pools).toHaveLength(8);
    expect(nativeFetch).not.toHaveBeenCalled();
  });

  it.each(HANDOFF_MOCK_SCENARIOS)("keeps entitlement reads behind login and logout in %s", async scenario => {
    const nativeFetch = vi.fn<typeof fetch>();
    const fetcher = createHandoffMockFetch({ pageUrl: PAGE_URL + "?mockScenario=" + scenario + "&mockAuth=guest", nativeFetch });
    expect((await fetcher("/api/v1/usage/entitlements")).status).toBe(401);
    expect((await fetcher("/api/v1/auth/login", {
      method: "POST", body: JSON.stringify({ username: "handoff", password: "demo-password" }),
    })).status).toBe(200);
    expect(await (await fetcher("/api/v1/usage/entitlements")).json()).toEqual(entitlementsFixture.entitlements);
    expect((await fetcher("/api/v1/auth/logout", { method: "POST" })).status).toBe(204);
    expect((await fetcher("/api/v1/usage/entitlements")).status).toBe(401);
    expect(nativeFetch).not.toHaveBeenCalled();
  });

  it.each(["POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])("rejects unsupported %s entitlement requests", async method => {
    const response = await mockFetch()("/api/v1/usage/entitlements", { method });
    expect(response.status).toBe(501);
    expect((await response.json()).code).toBe("handoff_mock_missing");
  });

  it("keeps unknown and external entitlement paths closed", async () => {
    const nativeFetch = vi.fn<typeof fetch>();
    const fetcher = createHandoffMockFetch({ pageUrl: PAGE_URL, nativeFetch });
    for (const path of ["/api/v1/usage/entitlements/extra", "/api/v1/usage/entitlements-other", "/api/v1/usage/operations"]) {
      expect((await fetcher(path)).status).toBe(501);
    }
    expect((await fetcher("https://outside.example.invalid/api/v1/usage/entitlements")).status).toBe(451);
    expect(nativeFetch).not.toHaveBeenCalled();
  });

  it("does not share returned policy mutations between reads or sessions", async () => {
    const first = mockFetch();
    const snapshot = await (await first("/api/v1/usage/entitlements")).json();
    expect(snapshot.policy).toBeDefined();
    snapshot.enabled = false;
    snapshot.policy.example = false;
    snapshot.pools[0].remaining = 0;
    expect(await (await first("/api/v1/usage/entitlements")).json()).toEqual(entitlementsFixture.entitlements);
    expect(await (await mockFetch()("/api/v1/usage/entitlements")).json()).toEqual(entitlementsFixture.entitlements);
  });

  it("allows the real client to probe the enabled policy and send a counted request", async () => {
    const transport = mockFetch();
    const fetcher = vi.fn<typeof fetch>((input, init) => transport(input, init));
    const session = await (await transport("/api/v1/auth/session")).json();
    retireSession();
    installSession(session);
    vi.stubGlobal("fetch", fetcher);
    try {
      const response = await privateFetch("/api/v1/monomer-retrosynthesis", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ smiles: "CCO", target_role: "auto", num_return_sequences: 1 }),
      });
      expect(response.status).toBe(200);
      expect((await response.json()).candidates).toHaveLength(1);
      const calls = fetcher.mock.calls;
      expect(calls.map(([input]) => new URL(String(input), PAGE_URL).pathname)).toEqual([
        "/api/v1/usage/entitlements", "/api/v1/monomer-retrosynthesis",
      ]);
      const headers = new Headers(calls[1][1]?.headers);
      expect(headers.get("Idempotency-Key")).toMatch(/^usage-/);
      expect(headers.get("X-Session-Context")).toBe(session.session_id);
    } finally {
      retireSession();
      vi.unstubAllGlobals();
    }
  });
});
