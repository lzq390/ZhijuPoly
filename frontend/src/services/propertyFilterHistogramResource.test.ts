/* @vitest-environment jsdom */

import { beforeEach, describe, expect, it, vi } from "vitest";
import type { PropertyFilterHistogramFetchResult } from "./api";

const apiMocks = vi.hoisted(() => ({
  fetchHistogram: vi.fn()
}));

vi.mock("./api", () => ({
  fetchPropertyFilterHistogram: apiMocks.fetchHistogram
}));

import {
  loadPropertyFilterHistogram,
  readPropertyFilterHistogram,
  resetPropertyFilterHistogramResourceForTests
} from "./propertyFilterHistogramResource";

function response(optionKey = "std:tg:C"): PropertyFilterHistogramFetchResult {
  return {
    status: "success",
    etag: 'W/"histogram-1"',
    data: {
      query_time_ms: 2,
      option_key: optionKey,
      data_source: "postgres",
      source_status: "ready",
      source_message: null,
      histogram: {
        domain_min: 0,
        domain_max: 100,
        domain_kind: "p5_p95",
        bin_count: 2,
        counts: [4, 5],
        underflow_count: 1,
        overflow_count: 0,
        total_count: 10
      }
    }
  };
}

beforeEach(() => {
  resetPropertyFilterHistogramResourceForTests();
  apiMocks.fetchHistogram.mockReset();
});

describe("propertyFilterHistogramResource", () => {
  it("merges in-flight requests and reuses the result for the same catalog revision", async () => {
    let resolveRequest!: (value: PropertyFilterHistogramFetchResult) => void;
    apiMocks.fetchHistogram.mockReturnValueOnce(
      new Promise<PropertyFilterHistogramFetchResult>((resolve) => {
        resolveRequest = resolve;
      })
    );

    const first = loadPropertyFilterHistogram("std:tg:C", 'W/"options-1"');
    const second = loadPropertyFilterHistogram("std:tg:C", 'W/"options-1"');
    expect(first).toBe(second);
    expect(apiMocks.fetchHistogram).toHaveBeenCalledOnce();

    resolveRequest(response());
    await expect(first).resolves.toMatchObject({ optionKey: "std:tg:C" });
    await loadPropertyFilterHistogram("std:tg:C", 'W/"options-1"');
    expect(apiMocks.fetchHistogram).toHaveBeenCalledOnce();
    expect(readPropertyFilterHistogram("std:tg:C", 'W/"options-1"')?.data.histogram.total_count).toBe(10);
  });

  it("uses a separate cache entry after the options catalog revision changes", async () => {
    apiMocks.fetchHistogram.mockResolvedValue(response());

    await loadPropertyFilterHistogram("std:tg:C", 'W/"options-1"');
    await loadPropertyFilterHistogram("std:tg:C", 'W/"options-2"');

    expect(apiMocks.fetchHistogram).toHaveBeenCalledTimes(2);
  });

  it("rejects histogram payloads whose bins do not add up to the total", async () => {
    const invalid = response();
    if (invalid.status === "success") invalid.data.histogram.total_count = 11;
    apiMocks.fetchHistogram.mockResolvedValue(invalid);

    await expect(
      loadPropertyFilterHistogram("std:tg:C", 'W/"options-1"')
    ).rejects.toThrow("属性直方图响应格式无效");
    expect(readPropertyFilterHistogram("std:tg:C", 'W/"options-1"')).toBeNull();
  });
});
