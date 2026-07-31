// @vitest-environment jsdom

import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiRequestError } from "../services/api";
import type { ConditionalGenerationTgRequest, OnlineKnowledgeSearchRequest, ReverseDesignTgRequest } from "../types";
import { JOB_POLL_BACKOFF_MS } from "./jobPolling";
import { useConditionalGeneration } from "./useConditionalGeneration";
import { useMonomerMdSimulation } from "./useMonomerMdSimulation";
import { useOnlineKnowledgeSearch } from "./useOnlineKnowledgeSearch";
import { useReverseDesign } from "./useReverseDesign";

const apiMocks = vi.hoisted(() => ({
  createConditional: vi.fn(),
  fetchConditional: vi.fn(),
  createReverse: vi.fn(),
  fetchReverse: vi.fn(),
  createMonomerMd: vi.fn(),
  fetchMonomerMd: vi.fn(),
  fetchMonomerMdStatus: vi.fn(),
  fetchMonomerMdProtocols: vi.fn(),
  createOnline: vi.fn(),
  fetchOnline: vi.fn(),
  fetchOnlineHistory: vi.fn()
}));

vi.mock("../services/api", async () => {
  const actual = await vi.importActual<typeof import("../services/api")>("../services/api");
  return {
    ...actual,
    createConditionalGenerationTgJob: apiMocks.createConditional,
    fetchConditionalGenerationTgJob: apiMocks.fetchConditional,
    createReverseDesignTgJob: apiMocks.createReverse,
    fetchReverseDesignTgJob: apiMocks.fetchReverse,
    createMonomerMdJob: apiMocks.createMonomerMd,
    fetchMonomerMdJob: apiMocks.fetchMonomerMd,
    fetchMonomerMdStatus: apiMocks.fetchMonomerMdStatus,
    fetchMonomerMdProtocols: apiMocks.fetchMonomerMdProtocols,
    createOnlineKnowledgeJob: apiMocks.createOnline,
    fetchOnlineKnowledgeJob: apiMocks.fetchOnline,
    fetchOnlineKnowledgeHistory: apiMocks.fetchOnlineHistory
  };
});

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

function pendingUntilAbort(signals: AbortSignal[]) {
  return (_jobId: string, signal: AbortSignal) => {
    signals.push(signal);
    return new Promise<never>((_resolve, reject) => {
      signal.addEventListener(
        "abort",
        () => reject(new DOMException("aborted", "AbortError")),
        { once: true }
      );
    });
  };
}

function pendingCreateUntilAbort(signals: AbortSignal[]) {
  return (_payload: unknown, signal: AbortSignal) => {
    signals.push(signal);
    return new Promise<never>((_resolve, reject) => {
      signal.addEventListener(
        "abort",
        () => reject(new DOMException("aborted", "AbortError")),
        { once: true }
      );
    });
  };
}

describe("job polling hook wiring", () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    Object.values(apiMocks).forEach((mock) => mock.mockReset());
    apiMocks.fetchMonomerMdStatus.mockResolvedValue({ busy: false, draining: false });
    apiMocks.fetchMonomerMdProtocols.mockResolvedValue({
      enabled: true,
      available: true,
      protocols: [],
      message: "ready"
    });
    apiMocks.fetchOnlineHistory.mockResolvedValue({ history: [] });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("keeps one Conditional POST across transient GET retries", async () => {
    const request: ConditionalGenerationTgRequest = {
      smiles: "*CC*",
      delta_tg: 30,
      candidate_count: 1,
      top_k: 5,
      temperature: 1
    };
    apiMocks.createConditional.mockResolvedValue({ job_id: "conditional-job", status: "pending" });
    apiMocks.fetchConditional
      .mockRejectedValueOnce(new TypeError("network 1"))
      .mockRejectedValueOnce(new TypeError("network 2"))
      .mockRejectedValueOnce(new TypeError("network 3"))
      .mockRejectedValueOnce(new TypeError("network 4"))
      .mockResolvedValue({
        job_id: "conditional-job",
        status: "completed",
        delta_tg: 30,
        created_at: "2026-07-21T00:00:00Z",
        updated_at: "2026-07-21T00:00:01Z",
        started_at: "2026-07-21T00:00:00Z",
        finished_at: "2026-07-21T00:00:01Z",
        attempts: 1,
        accepted_count: 0,
        message: null,
        error: null,
        result: null
      });

    const { result, unmount } = renderHook(() => useConditionalGeneration());
    let submission!: Promise<void>;
    act(() => {
      submission = result.current.submit(request);
    });
    await flush();
    for (const delay of JOB_POLL_BACKOFF_MS) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(delay);
      });
    }
    await act(async () => {
      await submission;
    });

    expect(apiMocks.createConditional).toHaveBeenCalledOnce();
    expect(apiMocks.createConditional).toHaveBeenCalledWith(request, expect.any(AbortSignal));
    expect(apiMocks.fetchConditional).toHaveBeenCalledTimes(5);
    for (const call of apiMocks.fetchConditional.mock.calls) {
      expect(call).toEqual(["conditional-job", expect.any(AbortSignal)]);
    }
    expect(result.current.job?.status).toBe("completed");
    expect(result.current.isLoading).toBe(false);
    unmount();
  });

  it("aborts an in-flight create POST on task switch and unmount", async () => {
    const createSignals: AbortSignal[] = [];
    apiMocks.createConditional.mockImplementation(pendingCreateUntilAbort(createSignals));
    const request: ConditionalGenerationTgRequest = {
      smiles: "*CC*",
      delta_tg: 30,
      candidate_count: 1,
      top_k: 5,
      temperature: 1
    };

    const { result, unmount } = renderHook(() => useConditionalGeneration());
    let first!: Promise<void>;
    let second!: Promise<void>;
    act(() => {
      first = result.current.submit(request);
    });
    await flush();
    expect(createSignals).toHaveLength(1);
    act(() => {
      second = result.current.submit({ ...request, delta_tg: 40 });
    });
    await flush();

    expect(createSignals).toHaveLength(2);
    expect(createSignals[0].aborted).toBe(true);
    expect(createSignals[1].aborted).toBe(false);
    expect(apiMocks.fetchConditional).not.toHaveBeenCalled();
    unmount();
    expect(createSignals[1].aborted).toBe(true);
    await Promise.all([first, second]);
  });

  it("aborts Reverse Design polling on task switch and unmount", async () => {
    const signals: AbortSignal[] = [];
    apiMocks.createReverse
      .mockResolvedValueOnce({ job_id: "reverse-a", status: "pending" })
      .mockResolvedValueOnce({ job_id: "reverse-b", status: "pending" });
    apiMocks.fetchReverse.mockImplementation(pendingUntilAbort(signals));
    const requestA: ReverseDesignTgRequest = {
      target_tg: 250,
      smiles: "*CC*",
      similarity_threshold: 0.8,
      candidate_size: 10
    };
    const requestB = { ...requestA, target_tg: 300 };

    const { result, unmount } = renderHook(() => useReverseDesign());
    let first!: Promise<void>;
    let second!: Promise<void>;
    act(() => {
      first = result.current.submit(requestA);
    });
    await flush();
    expect(signals).toHaveLength(1);
    act(() => {
      second = result.current.submit(requestB);
    });
    await flush();

    expect(signals).toHaveLength(2);
    expect(signals[0].aborted).toBe(true);
    expect(signals[1].aborted).toBe(false);
    expect(apiMocks.createReverse).toHaveBeenCalledTimes(2);
    expect(apiMocks.createReverse.mock.calls[0][1]).toBe(signals[0]);
    expect(apiMocks.createReverse.mock.calls[1][1]).toBe(signals[1]);
    unmount();
    expect(signals[1].aborted).toBe(true);
    await Promise.all([first, second]);
  });

  it("keeps the submitted Reverse Design request snapshot until reset", async () => {
    const signals: AbortSignal[] = [];
    apiMocks.createReverse.mockResolvedValue({ job_id: "reverse-snapshot", status: "pending" });
    apiMocks.fetchReverse.mockImplementation(pendingUntilAbort(signals));
    const request: ReverseDesignTgRequest = {
      target_tg: 450,
      smiles: "*CC*",
      similarity_threshold: 0.7,
      candidate_size: 25
    };

    const { result, unmount } = renderHook(() => useReverseDesign());
    let submission!: Promise<void>;
    act(() => {
      submission = result.current.submit(request);
    });
    await flush();

    expect(result.current.submittedRequest).toEqual(request);
    expect(result.current.isLoading).toBe(true);

    act(() => {
      result.current.reset();
    });
    expect(signals[0].aborted).toBe(true);
    expect(result.current.submittedRequest).toBeNull();
    expect(result.current.isLoading).toBe(false);
    expect(result.current.data).toBeNull();
    expect(result.current.job).toBeNull();

    await submission;
    unmount();
  });

  it("aborts Monomer MD polling on reset and on unmount", async () => {
    const signals: AbortSignal[] = [];
    apiMocks.createMonomerMd
      .mockResolvedValueOnce({ job_id: "md-a", status: "pending" })
      .mockResolvedValueOnce({ job_id: "md-b", status: "pending" });
    apiMocks.fetchMonomerMd.mockImplementation(pendingUntilAbort(signals));

    const { result, unmount } = renderHook(() => useMonomerMdSimulation());
    await flush();
    let first!: Promise<void>;
    let second!: Promise<void>;
    act(() => {
      first = result.current.submit("CCO");
    });
    await flush();
    expect(signals).toHaveLength(1);
    act(() => result.current.reset());
    expect(signals[0].aborted).toBe(true);
    expect(result.current.isLoading).toBe(false);

    act(() => {
      second = result.current.submit("CCN");
    });
    await flush();
    expect(signals).toHaveLength(2);
    expect(apiMocks.createMonomerMd.mock.calls[0][1]).toBe(signals[0]);
    expect(apiMocks.createMonomerMd.mock.calls[1][1]).toBe(signals[1]);
    unmount();
    expect(signals[1].aborted).toBe(true);
    await Promise.all([first, second]);
  });

  it("stops Online Knowledge polling on HTTP 410 without another POST", async () => {
    const payload: OnlineKnowledgeSearchRequest = {
      material: "polyimide",
      base_url: "https://example.invalid/v1",
      model: "test-model",
      mode: "synthesis",
      max_papers: 100,
      extraction_delay_seconds: 0
    };
    apiMocks.createOnline.mockResolvedValue({ job_id: "online-job", status: "pending" });
    apiMocks.fetchOnline.mockRejectedValue(new ApiRequestError(410, "expired"));

    const { result, unmount } = renderHook(() => useOnlineKnowledgeSearch());
    let submission!: Promise<void>;
    act(() => {
      submission = result.current.submit(payload);
    });
    await flush();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_200);
      await submission;
    });

    expect(apiMocks.createOnline).toHaveBeenCalledOnce();
    expect(apiMocks.createOnline).toHaveBeenCalledWith(payload, expect.any(AbortSignal));
    expect(apiMocks.fetchOnline).toHaveBeenCalledOnce();
    expect(apiMocks.fetchOnline).toHaveBeenCalledWith("online-job", expect.any(AbortSignal));
    expect(apiMocks.createOnline.mock.calls[0][1]).toBe(apiMocks.fetchOnline.mock.calls[0][1]);
    expect(result.current.isLoading).toBe(false);
    expect(result.current.error).toContain("expired");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(apiMocks.fetchOnline).toHaveBeenCalledOnce();
    unmount();
  });
});
