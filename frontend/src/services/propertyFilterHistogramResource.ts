import { fetchPropertyFilterHistogram } from "./api";
import type { PropertyFilterHistogram, PropertyFilterHistogramResponse } from "../types";

const PROPERTY_FILTER_HISTOGRAM_TIMEOUT_MS = 15_000;

export type PropertyFilterHistogramCache = {
  catalogRevision: string;
  optionKey: string;
  etag: string | null;
  data: PropertyFilterHistogramResponse;
};

const memoryCache = new Map<string, PropertyFilterHistogramCache>();
const inFlight = new Map<string, Promise<PropertyFilterHistogramCache>>();

function resourceKey(optionKey: string, catalogRevision: string) {
  return `${catalogRevision}\u0000${optionKey}`;
}

function isNonNegativeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0;
}

export function isPropertyFilterHistogram(value: unknown): value is PropertyFilterHistogram {
  if (!value || typeof value !== "object") return false;
  const histogram = value as Partial<PropertyFilterHistogram>;
  if (
    typeof histogram.domain_min !== "number" ||
    !Number.isFinite(histogram.domain_min) ||
    typeof histogram.domain_max !== "number" ||
    !Number.isFinite(histogram.domain_max) ||
    histogram.domain_min > histogram.domain_max ||
    (histogram.domain_kind !== "p5_p95" && histogram.domain_kind !== "full_range") ||
    !isNonNegativeInteger(histogram.bin_count) ||
    histogram.bin_count < 1 ||
    histogram.bin_count > 40 ||
    !Array.isArray(histogram.counts) ||
    histogram.counts.length !== histogram.bin_count ||
    !histogram.counts.every(isNonNegativeInteger) ||
    !isNonNegativeInteger(histogram.underflow_count) ||
    !isNonNegativeInteger(histogram.overflow_count) ||
    !isNonNegativeInteger(histogram.total_count)
  ) {
    return false;
  }
  return (
    histogram.counts.reduce((total, count) => total + count, 0) +
      histogram.underflow_count +
      histogram.overflow_count ===
    histogram.total_count
  );
}

function isHistogramResponse(
  value: unknown,
  optionKey: string
): value is PropertyFilterHistogramResponse {
  if (!value || typeof value !== "object") return false;
  const response = value as Partial<PropertyFilterHistogramResponse>;
  return (
    response.option_key === optionKey &&
    typeof response.query_time_ms === "number" &&
    Number.isFinite(response.query_time_ms) &&
    response.query_time_ms >= 0 &&
    typeof response.data_source === "string" &&
    typeof response.source_status === "string" &&
    (response.source_message === null || typeof response.source_message === "string") &&
    isPropertyFilterHistogram(response.histogram)
  );
}

export function readPropertyFilterHistogram(
  optionKey: string,
  catalogRevision: string
): PropertyFilterHistogramCache | null {
  return memoryCache.get(resourceKey(optionKey, catalogRevision)) ?? null;
}

export function loadPropertyFilterHistogram(
  optionKey: string,
  catalogRevision: string,
  options: { force?: boolean } = {}
): Promise<PropertyFilterHistogramCache> {
  const key = resourceKey(optionKey, catalogRevision);
  const cached = memoryCache.get(key) ?? null;
  if (cached && !options.force) return Promise.resolve(cached);
  const pending = inFlight.get(key);
  if (pending) return pending;

  const request = (async () => {
    const controller = new AbortController();
    let timedOut = false;
    const timeout = window.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, PROPERTY_FILTER_HISTOGRAM_TIMEOUT_MS);
    try {
      let result = await fetchPropertyFilterHistogram(optionKey, {
        etag: cached?.etag,
        signal: controller.signal
      });
      if (result.status === "not-modified" && !cached) {
        result = await fetchPropertyFilterHistogram(optionKey, { signal: controller.signal });
      }
      if (result.status === "not-modified") {
        return cached as PropertyFilterHistogramCache;
      }
      if (!isHistogramResponse(result.data, optionKey)) {
        throw new Error("属性直方图响应格式无效，请稍后重试。");
      }
      const refreshed: PropertyFilterHistogramCache = {
        catalogRevision,
        optionKey,
        etag: result.etag,
        data: result.data
      };
      memoryCache.set(key, refreshed);
      return refreshed;
    } catch (error) {
      if (timedOut) throw new Error("属性直方图请求超时，请稍后重试。");
      throw error;
    } finally {
      window.clearTimeout(timeout);
    }
  })();
  inFlight.set(key, request);
  void request.then(
    () => {
      if (inFlight.get(key) === request) inFlight.delete(key);
    },
    () => {
      if (inFlight.get(key) === request) inFlight.delete(key);
    }
  );
  return request;
}

export function resetPropertyFilterHistogramResourceForTests() {
  memoryCache.clear();
  inFlight.clear();
}
