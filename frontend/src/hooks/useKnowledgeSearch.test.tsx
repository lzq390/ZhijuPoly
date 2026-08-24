/* @vitest-environment jsdom */

import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { KnowledgeSearchResponse } from "../types";
import { useKnowledgeSearch } from "./useKnowledgeSearch";

const apiMocks = vi.hoisted(() => ({ searchKnowledge: vi.fn() }));

vi.mock("../services/api", () => ({ searchKnowledge: apiMocks.searchKnowledge }));

function response(query: string): KnowledgeSearchResponse {
  return {
    query,
    groups: [{ terms: [query] }],
    terms: [query],
    page: 1,
    page_size: 20,
    query_time_ms: 10,
    total: 0,
    results: []
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((next) => { resolve = next; });
  return { promise, resolve };
}

describe("useKnowledgeSearch", () => {
  beforeEach(() => apiMocks.searchKnowledge.mockReset());
  afterEach(() => vi.restoreAllMocks());

  it("中止旧请求并阻止旧响应覆盖最新检索", async () => {
    const first = deferred<KnowledgeSearchResponse>();
    const second = deferred<KnowledgeSearchResponse>();
    apiMocks.searchKnowledge.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const { result } = renderHook(() => useKnowledgeSearch());

    let firstSubmission!: Promise<KnowledgeSearchResponse | null>;
    let secondSubmission!: Promise<KnowledgeSearchResponse | null>;
    act(() => { firstSubmission = result.current.submit("polyimide", 20); });
    const firstSignal = apiMocks.searchKnowledge.mock.calls[0][1] as AbortSignal;
    act(() => { secondSubmission = result.current.submit("PLA", 20); });
    const secondSignal = apiMocks.searchKnowledge.mock.calls[1][1] as AbortSignal;

    expect(firstSignal.aborted).toBe(true);
    expect(secondSignal.aborted).toBe(false);

    await act(async () => {
      second.resolve(response("PLA"));
      await secondSubmission;
      first.resolve(response("polyimide"));
      await firstSubmission;
    });

    expect(result.current.data?.query).toBe("PLA");
    expect(result.current.isLoading).toBe(false);
  });

  it("卸载时中止当前请求", async () => {
    const pending = deferred<KnowledgeSearchResponse>();
    apiMocks.searchKnowledge.mockReturnValue(pending.promise);
    const { result, unmount } = renderHook(() => useKnowledgeSearch());
    let submission!: Promise<KnowledgeSearchResponse | null>;
    act(() => { submission = result.current.submit("polyimide", 20); });
    const signal = apiMocks.searchKnowledge.mock.calls[0][1] as AbortSignal;
    unmount();
    expect(signal.aborted).toBe(true);
    pending.resolve(response("polyimide"));
    await submission;
  });

  it("清理分组后发送结构化 AND/OR 查询", async () => {
    apiMocks.searchKnowledge.mockResolvedValue(response("polyimide；NMP | solvent"));
    const { result } = renderHook(() => useKnowledgeSearch());

    await act(async () => {
      await result.current.submit(
        "polyimide；NMP | solvent",
        20,
        [{ terms: [" polyimide "] }, { terms: ["NMP", "nmp", " solvent "] }],
        1,
        20
      );
    });

    expect(apiMocks.searchKnowledge).toHaveBeenCalledWith(
      expect.objectContaining({
        groups: [{ terms: ["polyimide"] }, { terms: ["NMP", "solvent"] }],
        page: 1,
        page_size: 20
      }),
      expect.any(AbortSignal)
    );
  });
});
