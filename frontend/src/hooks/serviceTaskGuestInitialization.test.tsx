// @vitest-environment jsdom
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { useMonomerMdSimulation } from "./useMonomerMdSimulation";
import { useMonomerDftJob } from "./useMonomerDftJob";
import { usePolytaoGeneration } from "./usePolytaoGeneration";
import { usePolymerizationBatchJob } from "./usePolymerizationBatchJob";

const requests = vi.hoisted(() => vi.fn(() => Promise.reject(new Error("Unexpected guest service request"))));
vi.mock("../services/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../services/api")>();
  return Object.fromEntries(Object.entries(actual).map(([key, value]) => [key, key.startsWith("fetch") ? requests : value]));
});
vi.mock("../services/polymerizationBatchApi", async (importOriginal) => ({
  ...await importOriginal<typeof import("../services/polymerizationBatchApi")>(),
  fetchBatchJob: requests,
  fetchBatchJobs: requests
}));

afterEach(() => { cleanup(); vi.useRealTimers(); window.history.replaceState(null, "", "/"); });

it("does not initialize or poll private services for guest pages, including saved task URLs", async () => {
  vi.useFakeTimers();
  window.history.replaceState(null, "", `/monomer-polymerization?mode=batch&job_id=${"a".repeat(32)}`);
  renderHook(() => {
    useMonomerMdSimulation({ enabled: false, taskCenterActive: true, initialJobId: "saved-md-job" });
    useMonomerDftJob({ enabled: false, initialJobId: "11111111-1111-4111-8111-111111111111" });
    usePolytaoGeneration(false);
    usePolymerizationBatchJob(false);
  });
  await act(async () => {
    window.dispatchEvent(new Event("focus"));
    window.dispatchEvent(new PopStateEvent("popstate"));
    await vi.advanceTimersByTimeAsync(30000);
  });
  expect(requests).not.toHaveBeenCalled();
});
