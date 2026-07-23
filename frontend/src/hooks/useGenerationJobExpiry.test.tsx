import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  ConditionalGenerationTgRequest,
  PolytaoGenerationRequest,
  PolytaoStatusResponse
} from "../types";
import { useConditionalGeneration } from "./useConditionalGeneration";
import {
  DEFAULT_POLYTAO_DESCRIPTORS,
  usePolytaoGeneration
} from "./usePolytaoGeneration";

const api = vi.hoisted(() => ({
  createConditionalGenerationTgJob: vi.fn(),
  fetchConditionalGenerationTgJob: vi.fn(),
  createPolytaoJob: vi.fn(),
  fetchPolytaoJob: vi.fn(),
  fetchPolytaoStatus: vi.fn()
}));

vi.mock("../services/api", () => ({
  ...api,
  isApiRequestError: (error: unknown, status?: number) => {
    const actualStatus =
      typeof error === "object" && error !== null && "status" in error
        ? (error as { status: unknown }).status
        : null;
    return typeof actualStatus === "number" &&
      (status === undefined || actualStatus === status);
  }
}));

const EXPIRED_MESSAGE = "Backend restarted or the job expired. Please resubmit.";

const readyPolytaoStatus: PolytaoStatusResponse = {
  enabled: true,
  available: true,
  worker_base_url_configured: false,
  worker_status: "ready",
  worker_mode: "backend-in-memory",
  db_configured: null,
  db_ready: null,
  db_error: null,
  runtime_ready: true,
  runtime_error: null,
  active_jobs: 0,
  model_id: "polytao",
  model_revision: null,
  default_params: {},
  worker_version: null,
  message: "PolyTAO backend runtime is ready"
};

type ConditionalHook = ReturnType<typeof useConditionalGeneration>;
type PolytaoHook = ReturnType<typeof usePolytaoGeneration>;

let conditionalHook: ConditionalHook | null = null;
let polytaoHook: PolytaoHook | null = null;

function ConditionalHarness() {
  conditionalHook = useConditionalGeneration();
  return null;
}

function PolytaoHarness() {
  polytaoHook = usePolytaoGeneration();
  return null;
}

function currentConditionalHook(): ConditionalHook {
  if (conditionalHook === null) {
    throw new Error("conditional hook has not rendered");
  }
  return conditionalHook;
}

function currentPolytaoHook(): PolytaoHook {
  if (polytaoHook === null) {
    throw new Error("PolyTAO hook has not rendered");
  }
  return polytaoHook;
}

beforeEach(() => {
  vi.clearAllMocks();
  conditionalHook = null;
  polytaoHook = null;
  api.fetchPolytaoStatus.mockResolvedValue(readyPolytaoStatus);
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean })
    .IS_REACT_ACT_ENVIRONMENT = true;
  vi.stubGlobal("window", {
    setTimeout: globalThis.setTimeout.bind(globalThis),
    clearTimeout: globalThis.clearTimeout.bind(globalThis),
    setInterval: globalThis.setInterval.bind(globalThis),
    clearInterval: globalThis.clearInterval.bind(globalThis),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn()
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("in-memory generation job expiry", () => {
  it("stops Conditional Generation polling after one HTTP 410 and preserves context", async () => {
    const request: ConditionalGenerationTgRequest = {
      smiles: "*CC*",
      delta_tg: 42,
      candidate_count: 1,
      top_k: 5,
      temperature: 1
    };
    api.createConditionalGenerationTgJob.mockResolvedValue({
      job_id: "conditional.instance.job",
      status: "pending"
    });
    api.fetchConditionalGenerationTgJob.mockRejectedValue({
      status: 410,
      message: "job belongs to a previous backend instance"
    });

    let renderer: ReactTestRenderer;
    await act(async () => {
      renderer = create(<ConditionalHarness />);
    });
    await act(async () => {
      currentConditionalHook().setRequest(request);
    });
    await act(async () => {
      await currentConditionalHook().submit();
    });
    await new Promise((resolve) => globalThis.setTimeout(resolve, 1_200));

    const state = currentConditionalHook();
    expect(api.createConditionalGenerationTgJob).toHaveBeenCalledOnce();
    expect(api.createConditionalGenerationTgJob).toHaveBeenCalledWith(request, expect.any(AbortSignal));
    expect(api.fetchConditionalGenerationTgJob).toHaveBeenCalledOnce();
    expect(api.fetchConditionalGenerationTgJob).toHaveBeenCalledWith(
      "conditional.instance.job",
      expect.any(AbortSignal)
    );
    expect(state.request).toEqual(request);
    expect(state.job?.job_id).toBe("conditional.instance.job");
    expect(state.job?.status).toBe("pending");
    expect(state.isLoading).toBe(false);
    expect(state.error).toBe(EXPIRED_MESSAGE);

    act(() => renderer!.unmount());
  });

  it("stops PolyTAO polling after one HTTP 410 and preserves context", async () => {
    const request: PolytaoGenerationRequest = {
      descriptors: { ...DEFAULT_POLYTAO_DESCRIPTORS },
      input_smiles: "*CC*",
      candidate_count: 1,
      temperature: 1,
      top_k: 100,
      top_p: 0.999,
      max_length: 300
    };
    api.createPolytaoJob.mockResolvedValue({
      job_id: "polytao.instance.job",
      status: "pending"
    });
    api.fetchPolytaoJob.mockRejectedValue({
      status: 410,
      message: "job belongs to a previous backend instance"
    });

    let renderer: ReactTestRenderer;
    await act(async () => {
      renderer = create(<PolytaoHarness />);
      await Promise.resolve();
    });
    await act(async () => {
      await currentPolytaoHook().submit(request);
    });
    await new Promise((resolve) => globalThis.setTimeout(resolve, 1_600));

    const state = currentPolytaoHook();
    expect(api.createPolytaoJob).toHaveBeenCalledOnce();
    expect(api.createPolytaoJob).toHaveBeenCalledWith(request);
    expect(api.fetchPolytaoJob).toHaveBeenCalledOnce();
    expect(api.fetchPolytaoJob).toHaveBeenCalledWith("polytao.instance.job");
    expect(state.request).toEqual(request);
    expect(state.request).not.toBe(request);
    expect(state.request.descriptors).not.toBe(request.descriptors);
    expect(state.job?.job_id).toBe("polytao.instance.job");
    expect(state.job?.status).toBe("pending");
    expect(state.isLoading).toBe(false);
    expect(state.error).toBe(EXPIRED_MESSAGE);

    act(() => renderer!.unmount());
  });
});
