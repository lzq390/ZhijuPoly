import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { searchPropertyFilterRecords } from "../services/api";
import {
  isPropertyFilterOptionsCacheFresh,
  readPropertyFilterOptionsCache,
  refreshPropertyFilterOptions
} from "../services/propertyFilterOptionsResource";
import type {
  PropertyFilterCondition,
  PropertyFilterOption,
  PropertyFilterOptionsResponse,
  PropertyFilterSearchResponse
} from "../types";

export const PROPERTY_FILTER_MAX_CONDITIONS = 8;
export const PROPERTY_FILTER_PAGE_SIZES = [10, 25, 50, 100] as const;

export type PropertyFilterDraft = {
  id: number;
  optionKey: string;
  minValue: string;
  maxValue: string;
  error: string | null;
};

export type SubmittedPropertyFilterCondition = {
  optionKey: string;
  label: string;
  unit: string;
  minValue: number | null;
  maxValue: number | null;
  expression: string;
};

export type SubmittedPropertyFilter = {
  filters: PropertyFilterCondition[];
  conditions: SubmittedPropertyFilterCondition[];
  query: string;
  expression: string;
  requestKey: string;
};

function preferredDefaultOption(options: PropertyFilterOption[]): PropertyFilterOption | undefined {
  return (
    options.find((option) => option.filter_type === "standardized" && option.property_key?.toLowerCase() === "tg") ??
    options.find(
      (option) =>
        option.filter_type === "standardized" &&
        /(^|[^a-z])tg([^a-z]|$)/i.test(`${option.label} ${option.property_key ?? ""}`)
    ) ??
    options[0]
  );
}

export function propertyFilterOptionUnit(option: PropertyFilterOption): string {
  return option.canonical_unit ?? option.property_unit_clean ?? "";
}

export function propertyFilterOptionShortLabel(option: PropertyFilterOption): string {
  const propertyKey = option.property_key?.toLowerCase() ?? "";
  if (propertyKey === "tg") return "Tg";
  if (propertyKey === "tm") return "Tm";
  if (propertyKey.includes("bandgap")) return "Bandgap";
  return option.property_key || option.property_name || option.label;
}

function parseBound(value: string): number | null {
  const normalized = value.trim();
  if (!normalized) return null;
  const parsed = Number(normalized);
  return Number.isFinite(parsed) ? parsed : Number.NaN;
}

function formatExpressionNumber(value: number): string {
  return Number(value.toPrecision(8)).toLocaleString("zh-CN", { maximumFractionDigits: 6 });
}

function buildConditionExpression(
  option: PropertyFilterOption,
  minValue: number | null,
  maxValue: number | null
): string {
  const label = propertyFilterOptionShortLabel(option);
  const unit = propertyFilterOptionUnit(option);
  const unitSuffix = unit ? ` ${unit}` : "";
  if (minValue !== null && maxValue !== null) {
    return `${label} ${formatExpressionNumber(minValue)}–${formatExpressionNumber(maxValue)}${unitSuffix}`;
  }
  if (minValue !== null) return `${label} ≥ ${formatExpressionNumber(minValue)}${unitSuffix}`;
  return `${label} ≤ ${formatExpressionNumber(maxValue as number)}${unitSuffix}`;
}

function buildRequestCondition(
  option: PropertyFilterOption,
  minValue: number | null,
  maxValue: number | null
): PropertyFilterCondition {
  if (option.filter_type === "standardized") {
    return {
      filter_type: "standardized",
      property_key: option.property_key,
      canonical_unit: option.canonical_unit ?? "",
      min_value: minValue,
      max_value: maxValue
    };
  }
  return {
    filter_type: "raw",
    property_name: option.property_name,
    property_unit_clean: option.property_unit_clean ?? "",
    min_value: minValue,
    max_value: maxValue
  };
}

function initialDraft(optionKey = ""): PropertyFilterDraft {
  return { id: 1, optionKey, minValue: "", maxValue: "", error: null };
}

function optionsCatalogRevision(cache: { etag: string | null; cachedAt: number } | null) {
  if (!cache) return "loading";
  return cache.etag ?? `live:${cache.cachedAt}`;
}

export function usePropertyFilter() {
  const initialOptionsCache = useMemo(() => readPropertyFilterOptionsCache(), []);
  const [optionsData, setOptionsData] = useState<PropertyFilterOptionsResponse | null>(
    initialOptionsCache?.data ?? null
  );
  const [optionsRevision, setOptionsRevision] = useState(
    optionsCatalogRevision(initialOptionsCache)
  );
  const [optionsPending, setOptionsPending] = useState(!initialOptionsCache);
  const [optionsRefreshing, setOptionsRefreshing] = useState(false);
  const [optionsError, setOptionsError] = useState<string | null>(null);
  const [optionsRefreshError, setOptionsRefreshError] = useState<string | null>(null);
  const [optionsRetryKey, setOptionsRetryKey] = useState(0);
  const [drafts, setDrafts] = useState<PropertyFilterDraft[]>([
    initialDraft(preferredDefaultOption(initialOptionsCache?.data.options ?? [])?.option_key ?? "")
  ]);
  const [queryDraft, setQueryDraft] = useState("");
  const [pageSize, setPageSizeState] = useState<number>(25);
  const [submitted, setSubmitted] = useState<SubmittedPropertyFilter | null>(null);
  const [page, setPageState] = useState(1);
  const [searchData, setSearchData] = useState<PropertyFilterSearchResponse | null>(null);
  const [searchLoading, setSearchLoading] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [searchRetryKey, setSearchRetryKey] = useState(0);
  const [validationError, setValidationError] = useState<string | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const nextDraftId = useRef(2);
  const searchRequestId = useRef(0);

  useEffect(() => {
    let subscribed = true;
    const cached = readPropertyFilterOptionsCache();
    const applyOptions = (
      response: PropertyFilterOptionsResponse,
      cache: { etag: string | null; cachedAt: number }
    ) => {
      setOptionsData(response);
      setOptionsRevision(optionsCatalogRevision(cache));
      const defaultKey = preferredDefaultOption(response.options)?.option_key ?? "";
      const validKeys = new Set(response.options.map((option) => option.option_key));
      setDrafts((current) =>
        current.map((draft) =>
          draft.optionKey && validKeys.has(draft.optionKey)
            ? draft
            : { ...draft, optionKey: defaultKey, minValue: "", maxValue: "", error: null }
        )
      );
    };
    if (cached) applyOptions(cached.data, cached);
    const shouldRefresh = optionsRetryKey > 0 || !cached || !isPropertyFilterOptionsCacheFresh(cached);
    if (!shouldRefresh) {
      setOptionsPending(false);
      setOptionsRefreshing(false);
      setOptionsError(null);
      setOptionsRefreshError(null);
      return () => {
        subscribed = false;
      };
    }

    setOptionsPending(!cached);
    setOptionsRefreshing(Boolean(cached));
    setOptionsError(null);
    setOptionsRefreshError(null);

    refreshPropertyFilterOptions()
      .then((cache) => {
        if (!subscribed) return;
        applyOptions(cache.data, cache);
      })
      .catch((error: unknown) => {
        if (!subscribed) return;
        const message = error instanceof Error ? error.message : "属性目录加载失败。";
        if (cached) {
          setOptionsRefreshError(message);
        } else {
          setOptionsError(message);
        }
      })
      .finally(() => {
        if (!subscribed) return;
        setOptionsPending(false);
        setOptionsRefreshing(false);
      });

    return () => {
      subscribed = false;
    };
  }, [optionsRetryKey]);

  useEffect(() => {
    if (!submitted) {
      setSearchLoading(false);
      return;
    }

    const requestId = ++searchRequestId.current;
    const controller = new AbortController();
    let timedOut = false;
    const timeout = window.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, 30_000);
    setSearchLoading(true);
    setSearchError(null);
    setSearchData(null);

    searchPropertyFilterRecords(
      {
        filters: submitted.filters,
        q: submitted.query,
        page,
        page_size: pageSize
      },
      controller.signal
    )
      .then((response) => {
        if (requestId !== searchRequestId.current) return;
        setSearchData(response);
      })
      .catch((error: unknown) => {
        if (requestId !== searchRequestId.current) return;
        if (timedOut) {
          setSearchError("数据库筛选请求超时，请缩小范围后重试。");
          return;
        }
        if (controller.signal.aborted) return;
        setSearchError(error instanceof Error ? error.message : "数据库筛选失败。");
      })
      .finally(() => {
        window.clearTimeout(timeout);
        if (requestId === searchRequestId.current) setSearchLoading(false);
      });

    return () => {
      window.clearTimeout(timeout);
      controller.abort();
    };
  }, [page, pageSize, searchRetryKey, submitted]);

  const options = optionsData?.options ?? [];
  const optionsByKey = useMemo(
    () => new Map(options.map((option) => [option.option_key, option])),
    [options]
  );
  const standardizedOptions = useMemo(
    () => options.filter((option) => option.filter_type === "standardized"),
    [options]
  );
  const rawOptions = useMemo(
    () => options.filter((option) => option.filter_type === "raw"),
    [options]
  );

  const draftExpression = useMemo(() => {
    const conditions = drafts.map((draft) => {
      const option = optionsByKey.get(draft.optionKey);
      if (!option) return "未选择属性";
      const minValue = parseBound(draft.minValue);
      const maxValue = parseBound(draft.maxValue);
      if (Number.isNaN(minValue) || Number.isNaN(maxValue)) {
        return `${propertyFilterOptionShortLabel(option)}（阈值无效）`;
      }
      if (minValue === null && maxValue === null) {
        return `${propertyFilterOptionShortLabel(option)}（待填写阈值）`;
      }
      return buildConditionExpression(option, minValue, maxValue);
    });
    const expression = conditions.join(" ∧ ");
    return queryDraft.trim() ? `${expression} · 关键词 “${queryDraft.trim()}”` : expression;
  }, [drafts, optionsByKey, queryDraft]);

  const updateBound = useCallback((id: number, field: "minValue" | "maxValue", value: string) => {
    setDrafts((current) =>
      current.map((draft) => (draft.id === id ? { ...draft, [field]: value, error: null } : draft))
    );
    setValidationError(null);
  }, []);

  const selectProperty = useCallback((id: number, optionKey: string) => {
    setDrafts((current) =>
      current.map((draft) =>
        draft.id === id
          ? { ...draft, optionKey, minValue: "", maxValue: "", error: null }
          : draft
      )
    );
    setValidationError(null);
  }, []);

  const addCondition = useCallback(() => {
    if (drafts.length >= PROPERTY_FILTER_MAX_CONDITIONS || options.length === 0) return;
    const usedKeys = new Set(drafts.map((draft) => draft.optionKey));
    const nextOption = standardizedOptions.find((option) => !usedKeys.has(option.option_key)) ??
      options.find((option) => !usedKeys.has(option.option_key)) ??
      options[0];
    setDrafts((current) => [
      ...current,
      {
        id: nextDraftId.current++,
        optionKey: nextOption?.option_key ?? "",
        minValue: "",
        maxValue: "",
        error: null
      }
    ]);
  }, [drafts, options, standardizedOptions]);

  const removeCondition = useCallback((id: number) => {
    setDrafts((current) => (current.length <= 1 ? current : current.filter((draft) => draft.id !== id)));
    setValidationError(null);
  }, []);

  const run = useCallback((): boolean => {
    const trimmedQuery = queryDraft.trim();
    if (trimmedQuery.length > 200) {
      setValidationError("关键词最多输入 200 个字符。");
      return false;
    }

    const filters: PropertyFilterCondition[] = [];
    const submittedConditions: SubmittedPropertyFilterCondition[] = [];
    let firstError: string | null = null;
    const validatedDrafts = drafts.map((draft) => {
      const option = optionsByKey.get(draft.optionKey);
      let error: string | null = null;
      if (!option) {
        error = "请选择筛选属性。";
      }
      const minValue = parseBound(draft.minValue);
      const maxValue = parseBound(draft.maxValue);
      if (!error && (Number.isNaN(minValue) || Number.isNaN(maxValue))) {
        error = "请输入有效数字。";
      } else if (!error && minValue === null && maxValue === null) {
        error = "至少填写一个阈值。";
      } else if (!error && minValue !== null && maxValue !== null && minValue > maxValue) {
        error = "最小值不能大于最大值。";
      }
      if (error) {
        firstError ??= error;
      } else if (option) {
        filters.push(buildRequestCondition(option, minValue, maxValue));
        submittedConditions.push({
          optionKey: option.option_key,
          label: option.label,
          unit: propertyFilterOptionUnit(option),
          minValue,
          maxValue,
          expression: buildConditionExpression(option, minValue, maxValue)
        });
      }
      return { ...draft, error };
    });

    setDrafts(validatedDrafts);
    if (firstError) {
      setValidationError(firstError);
      return false;
    }

    const expression = submittedConditions.map((condition) => condition.expression).join(" ∧ ");
    const requestKey = JSON.stringify({ filters, query: trimmedQuery, pageSize });
    setValidationError(null);
    if (searchLoading && page === 1 && submitted?.requestKey === requestKey) {
      setDrawerOpen(true);
      return true;
    }
    setPageState(1);
    setSearchData(null);
    setSearchError(null);
    setSearchLoading(true);
    setSubmitted({
      filters,
      conditions: submittedConditions,
      query: trimmedQuery,
      expression: trimmedQuery ? `${expression} · 关键词 “${trimmedQuery}”` : expression,
      requestKey
    });
    setDrawerOpen(true);
    return true;
  }, [drafts, optionsByKey, page, pageSize, queryDraft, searchLoading, submitted]);

  const reset = useCallback(() => {
    const defaultKey = preferredDefaultOption(options)?.option_key ?? "";
    searchRequestId.current += 1;
    nextDraftId.current = 2;
    setDrafts([initialDraft(defaultKey)]);
    setQueryDraft("");
    setPageSizeState(25);
    setPageState(1);
    setSubmitted(null);
    setSearchData(null);
    setSearchError(null);
    setSearchLoading(false);
    setValidationError(null);
    setDrawerOpen(false);
  }, [options]);

  const setPage = useCallback((nextPage: number) => {
    const normalizedPage = Math.max(1, nextPage || page);
    if (normalizedPage === page) return;
    setSearchData(null);
    setSearchError(null);
    setSearchLoading(true);
    setPageState(normalizedPage);
  }, [page]);

  const setPageSize = useCallback((nextPageSize: number) => {
    if (!PROPERTY_FILTER_PAGE_SIZES.includes(nextPageSize as (typeof PROPERTY_FILTER_PAGE_SIZES)[number])) return;
    if (nextPageSize === pageSize) return;
    setPageState(1);
    setPageSizeState(nextPageSize);
    if (submitted) {
      setSearchData(null);
      setSearchError(null);
      setSearchLoading(true);
    }
  }, [pageSize, submitted]);

  const retrySearch = useCallback(() => {
    if (!submitted) return;
    setSearchData(null);
    setSearchError(null);
    setSearchLoading(true);
    setSearchRetryKey((current) => current + 1);
  }, [submitted]);
  const retryOptions = useCallback(() => {
    setOptionsError(null);
    setOptionsRefreshError(null);
    setOptionsRetryKey((current) => current + 1);
  }, []);

  const matchedRecords = searchData?.matched_records ?? 0;
  const totalPages = Math.max(1, Math.ceil(matchedRecords / pageSize));

  return {
    optionsData,
    optionsRevision,
    options,
    optionsByKey,
    standardizedOptions,
    rawOptions,
    optionsLoading: optionsPending,
    optionsPending,
    optionsRefreshing,
    optionsError,
    optionsRefreshError,
    retryOptions,
    drafts,
    updateBound,
    selectProperty,
    addCondition,
    removeCondition,
    queryDraft,
    setQueryDraft,
    pageSize,
    setPageSize,
    draftExpression,
    submitted,
    run,
    reset,
    validationError,
    searchData,
    searchLoading,
    searchError,
    retrySearch,
    page,
    setPage,
    matchedRecords,
    totalPages,
    drawerOpen,
    setDrawerOpen
  };
}
