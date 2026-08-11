/* @vitest-environment jsdom */

import { beforeEach, describe, expect, it, vi } from "vitest";
import type { PropertyFilterOptionsResponse } from "../types";

const apiMocks = vi.hoisted(() => ({ fetchOptions: vi.fn() }));

vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof import("./api")>("./api");
  return { ...actual, fetchPropertyFilterOptions: apiMocks.fetchOptions };
});

import {
  PROPERTY_FILTER_OPTIONS_FRESH_MS,
  PROPERTY_FILTER_OPTIONS_MAX_AGE_MS,
  isPropertyFilterOptionsCacheFresh,
  readPropertyFilterOptionsCache,
  refreshPropertyFilterOptions,
  resetPropertyFilterOptionsResourceForTests
} from "./propertyFilterOptionsResource";

const response: PropertyFilterOptionsResponse = {
  query_time_ms: 3,
  total_records: 1,
  mapped_records: 1,
  raw_records: 0,
  data_source: "postgres",
  source_status: "ready",
  source_message: null,
  options: [
    {
      filter_type: "standardized",
      option_key: "std:tg:C",
      label: "Tg",
      property_key: "tg",
      property_name: null,
      property_unit_clean: null,
      canonical_unit: "C",
      rows: 1,
      unique_smiles: 1,
      min_value: 100,
      p5_value: 100,
      median_value: 100,
      p95_value: 100,
      max_value: 100
    }
  ]
};

beforeEach(() => {
  vi.restoreAllMocks();
  resetPropertyFilterOptionsResourceForTests();
  window.sessionStorage.clear();
  apiMocks.fetchOptions.mockReset();
});

describe("propertyFilterOptionsResource", () => {
  it("coalesces concurrent requests and persists one validated cache entry", async () => {
    let resolveRequest!: (value: unknown) => void;
    apiMocks.fetchOptions.mockReturnValue(new Promise((resolve) => {
      resolveRequest = resolve;
    }));

    const first = refreshPropertyFilterOptions();
    const second = refreshPropertyFilterOptions();
    expect(first).toBe(second);
    expect(apiMocks.fetchOptions).toHaveBeenCalledOnce();

    resolveRequest({ status: "success", data: response, etag: 'W/"catalog-1"' });
    const cache = await first;
    expect(cache.data).toEqual(response);
    expect(readPropertyFilterOptionsCache()?.etag).toBe('W/"catalog-1"');
    expect(window.sessionStorage.length).toBe(1);
  });

  it("keeps cached data on 304 and refreshes its freshness timestamp", async () => {
    const now = vi.spyOn(Date, "now").mockReturnValue(1_000);
    apiMocks.fetchOptions.mockResolvedValueOnce({
      status: "success",
      data: response,
      etag: 'W/"catalog-1"'
    });
    await refreshPropertyFilterOptions();

    now.mockReturnValue(1_000 + PROPERTY_FILTER_OPTIONS_FRESH_MS + 1);
    apiMocks.fetchOptions.mockResolvedValueOnce({
      status: "not-modified",
      data: null,
      etag: 'W/"catalog-1"'
    });
    const refreshed = await refreshPropertyFilterOptions();

    expect(refreshed.data).toEqual(response);
    expect(refreshed.cachedAt).toBe(1_000 + PROPERTY_FILTER_OPTIONS_FRESH_MS + 1);
    expect(isPropertyFilterOptionsCacheFresh(refreshed)).toBe(true);
  });

  it("rejects corrupt and hard-expired session entries", async () => {
    apiMocks.fetchOptions.mockResolvedValueOnce({
      status: "success",
      data: response,
      etag: null
    });
    const cache = await refreshPropertyFilterOptions();
    const key = window.sessionStorage.key(0) as string;

    resetPropertyFilterOptionsResourceForTests();
    window.sessionStorage.setItem(key, "{broken");
    expect(readPropertyFilterOptionsCache()).toBeNull();
    expect(window.sessionStorage.getItem(key)).toBeNull();

    window.sessionStorage.setItem(
      key,
      JSON.stringify({
        ...cache,
        data: {
          ...cache.data,
          options: [{ ...cache.data.options[0], property_key: 42 }]
        }
      })
    );
    expect(readPropertyFilterOptionsCache()).toBeNull();
    expect(window.sessionStorage.getItem(key)).toBeNull();

    window.sessionStorage.setItem(key, JSON.stringify(cache));
    expect(readPropertyFilterOptionsCache(cache.cachedAt + PROPERTY_FILTER_OPTIONS_MAX_AGE_MS + 1)).toBeNull();
  });
});
