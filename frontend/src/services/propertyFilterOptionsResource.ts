import {
  API_BASE_URL,
  fetchPropertyFilterOptions
} from "./api";
import { isPropertyFilterHistogram } from "./propertyFilterHistogramResource";
import type { PropertyFilterOptionsResponse } from "../types";

export const PROPERTY_FILTER_OPTIONS_CACHE_SCHEMA_VERSION = 1;
export const PROPERTY_FILTER_OPTIONS_FRESH_MS = 60_000;
export const PROPERTY_FILTER_OPTIONS_MAX_AGE_MS = 24 * 60 * 60 * 1000;
const PROPERTY_FILTER_OPTIONS_TIMEOUT_MS = 15_000;
const CACHE_KEY = `nexpoly.property-filter-options.v1:${API_BASE_URL}`;

export type PropertyFilterOptionsCache = {
  schemaVersion: 1;
  cachedAt: number;
  etag: string | null;
  data: PropertyFilterOptionsResponse;
};

let memoryCache: PropertyFilterOptionsCache | null = null;
let inFlight: Promise<PropertyFilterOptionsCache> | null = null;

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function isNonNegativeInteger(value: unknown): value is number {
  return isFiniteNumber(value) && Number.isInteger(value) && value >= 0;
}

function isNullableString(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}

function isNullableNumber(value: unknown): value is number | null {
  return value === null || isFiniteNumber(value);
}

function isOptionsResponse(value: unknown): value is PropertyFilterOptionsResponse {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<PropertyFilterOptionsResponse>;
  return (
    isFiniteNumber(candidate.query_time_ms) &&
    candidate.query_time_ms >= 0 &&
    isNonNegativeInteger(candidate.total_records) &&
    isNonNegativeInteger(candidate.mapped_records) &&
    isNonNegativeInteger(candidate.raw_records) &&
    candidate.mapped_records + candidate.raw_records === candidate.total_records &&
    typeof candidate.data_source === "string" &&
    typeof candidate.source_status === "string" &&
    isNullableString(candidate.source_message) &&
    Array.isArray(candidate.options) &&
    candidate.options.every(
      (option) =>
        Boolean(option) &&
        typeof option.option_key === "string" &&
        typeof option.label === "string" &&
        (option.filter_type === "standardized" || option.filter_type === "raw") &&
        isNullableString(option.property_key) &&
        isNullableString(option.property_name) &&
        isNullableString(option.property_unit_clean) &&
        isNullableString(option.canonical_unit) &&
        isNonNegativeInteger(option.rows) &&
        isNonNegativeInteger(option.unique_smiles) &&
        isNullableNumber(option.min_value) &&
        isNullableNumber(option.p5_value) &&
        isNullableNumber(option.median_value) &&
        isNullableNumber(option.p95_value) &&
        isNullableNumber(option.max_value) &&
        (option.histogram === undefined ||
          option.histogram === null ||
          isPropertyFilterHistogram(option.histogram)) &&
        (option.filter_type === "standardized"
          ? Boolean(option.property_key)
          : Boolean(option.property_name))
    )
  );
}

function parseCache(value: string | null): PropertyFilterOptionsCache | null {
  if (!value) return null;
  try {
    const candidate = JSON.parse(value) as Partial<PropertyFilterOptionsCache>;
    if (
      candidate.schemaVersion !== PROPERTY_FILTER_OPTIONS_CACHE_SCHEMA_VERSION ||
      typeof candidate.cachedAt !== "number" ||
      !Number.isFinite(candidate.cachedAt) ||
      candidate.cachedAt < 0 ||
      (candidate.etag !== null && typeof candidate.etag !== "string") ||
      !isOptionsResponse(candidate.data)
    ) {
      return null;
    }
    return candidate as PropertyFilterOptionsCache;
  } catch {
    return null;
  }
}

function sessionCache(): PropertyFilterOptionsCache | null {
  if (typeof window === "undefined") return null;
  try {
    const stored = window.sessionStorage.getItem(CACHE_KEY);
    const cache = parseCache(stored);
    if (stored !== null && cache === null) {
      window.sessionStorage.removeItem(CACHE_KEY);
    }
    return cache;
  } catch {
    return null;
  }
}

function persist(cache: PropertyFilterOptionsCache) {
  memoryCache = cache;
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.setItem(CACHE_KEY, JSON.stringify(cache));
  } catch {
    // Storage may be disabled or full; the in-memory cache still works.
  }
}

function removePersistedCache() {
  memoryCache = null;
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.removeItem(CACHE_KEY);
  } catch {
    // Ignore unavailable storage.
  }
}

export function readPropertyFilterOptionsCache(now = Date.now()): PropertyFilterOptionsCache | null {
  const cache = memoryCache ?? sessionCache();
  if (!cache) return null;
  const age = now - cache.cachedAt;
  if (age < 0 || age > PROPERTY_FILTER_OPTIONS_MAX_AGE_MS) {
    removePersistedCache();
    return null;
  }
  memoryCache = cache;
  return cache;
}

export function isPropertyFilterOptionsCacheFresh(
  cache: PropertyFilterOptionsCache,
  now = Date.now()
): boolean {
  const age = now - cache.cachedAt;
  return age >= 0 && age <= PROPERTY_FILTER_OPTIONS_FRESH_MS;
}

export function refreshPropertyFilterOptions(): Promise<PropertyFilterOptionsCache> {
  if (inFlight) return inFlight;
  const request = (async () => {
    const cached = readPropertyFilterOptionsCache();
    const controller = new AbortController();
    let timedOut = false;
    const timeout = window.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, PROPERTY_FILTER_OPTIONS_TIMEOUT_MS);
    try {
      let result = await fetchPropertyFilterOptions({
        etag: cached?.etag,
        signal: controller.signal
      });
      if (result.status === "not-modified" && !cached) {
        result = await fetchPropertyFilterOptions({ signal: controller.signal });
      }
      if (result.status === "not-modified") {
        const refreshed = {
          ...cached as PropertyFilterOptionsCache,
          cachedAt: Date.now(),
          etag: result.etag
        };
        persist(refreshed);
        return refreshed;
      }
      if (!isOptionsResponse(result.data)) {
        throw new Error("属性目录响应格式无效，请稍后重试。");
      }
      const refreshed: PropertyFilterOptionsCache = {
        schemaVersion: PROPERTY_FILTER_OPTIONS_CACHE_SCHEMA_VERSION,
        cachedAt: Date.now(),
        etag: result.etag,
        data: result.data
      };
      persist(refreshed);
      return refreshed;
    } catch (error) {
      if (timedOut) throw new Error("属性目录请求超时，请稍后重试。");
      throw error;
    } finally {
      window.clearTimeout(timeout);
    }
  })();
  inFlight = request;
  void request.then(
    () => {
      if (inFlight === request) inFlight = null;
    },
    () => {
      if (inFlight === request) inFlight = null;
    }
  );
  return request;
}

export function resetPropertyFilterOptionsResourceForTests() {
  inFlight = null;
  removePersistedCache();
}
