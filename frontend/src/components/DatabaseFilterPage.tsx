import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  Filter,
  Info,
  LoaderCircle,
  Plus,
  RefreshCw,
  RotateCcw,
  Search,
  Trash2,
  X
} from "lucide-react";
import {
  memo,
  useCallback,
  useDeferredValue,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type FormEvent
} from "react";
import {
  PROPERTY_FILTER_MAX_CONDITIONS,
  PROPERTY_FILTER_PAGE_SIZES,
  propertyFilterOptionShortLabel,
  propertyFilterOptionUnit,
  usePropertyFilter,
  type PropertyFilterDraft
} from "../hooks/usePropertyFilter";
import {
  loadPropertyFilterHistogram,
  readPropertyFilterHistogram
} from "../services/propertyFilterHistogramResource";
import type {
  PropertyFilterHistogram as PropertyFilterHistogramData,
  PropertyFilterOption
} from "../types";
import { DatabaseFilterResultsDrawer } from "./DatabaseFilterResultsDrawer";
import "../styles/database-filter.css";

function formatInteger(value: number | null | undefined) {
  if (value === null || value === undefined) return "—";
  return value.toLocaleString("zh-CN");
}

function formatValue(value: number | null | undefined) {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return value.toLocaleString("zh-CN", { maximumFractionDigits: 5 });
}

function optionSearchText(option: PropertyFilterOption) {
  const cached = optionSearchTextCache.get(option);
  if (cached) return cached;
  const value = [
    option.label,
    option.property_key,
    option.property_name,
    option.canonical_unit,
    option.property_unit_clean,
    option.filter_type
  ]
    .filter(Boolean)
    .join(" ")
    .toLocaleLowerCase("zh-CN");
  optionSearchTextCache.set(option, value);
  return value;
}

const optionSearchTextCache = new WeakMap<PropertyFilterOption, string>();

function optionRange(option: PropertyFilterOption) {
  const unit = propertyFilterOptionUnit(option);
  if (option.min_value === null || option.max_value === null) return "暂无数值范围";
  return `全范围 ${formatValue(option.min_value)}–${formatValue(option.max_value)}${unit ? ` ${unit}` : ""}`;
}

function PropertyTypeDot({ raw = false }: { raw?: boolean }) {
  return <i className={`dbf-property-dot${raw ? " is-raw" : ""}`} aria-hidden="true" />;
}

function distributionPosition(value: number, minimum: number, maximum: number) {
  if (maximum === minimum) return 50;
  return Math.min(100, Math.max(0, ((value - minimum) / (maximum - minimum)) * 100));
}

function histogramRangeLabel(
  histogram: PropertyFilterHistogramData,
  index: number,
  unit: string
) {
  if (histogram.domain_min === histogram.domain_max) {
    return `${formatValue(histogram.domain_min)}${unit ? ` ${unit}` : ""}`;
  }
  const width = (histogram.domain_max - histogram.domain_min) / histogram.bin_count;
  const lower = histogram.domain_min + width * index;
  const upper = index === histogram.bin_count - 1
    ? histogram.domain_max
    : histogram.domain_min + width * (index + 1);
  return `${formatValue(lower)}–${formatValue(upper)}${unit ? ` ${unit}` : ""}`;
}

const PropertyHistogram = memo(function PropertyHistogram({
  option,
  catalogRevision
}: {
  option: PropertyFilterOption;
  catalogRevision: string;
}) {
  const unit = propertyFilterOptionUnit(option);
  const embeddedHistogram = option.histogram ?? null;
  const initialCache = embeddedHistogram
    ? null
    : readPropertyFilterHistogram(option.option_key, catalogRevision);
  const [histogram, setHistogram] = useState<PropertyFilterHistogramData | null>(
    embeddedHistogram ?? initialCache?.data.histogram ?? null
  );
  const [loading, setLoading] = useState(!histogram);
  const [error, setError] = useState<string | null>(null);
  const [retryKey, setRetryKey] = useState(0);

  useEffect(() => {
    if (embeddedHistogram) {
      setHistogram(embeddedHistogram);
      setLoading(false);
      setError(null);
      return;
    }
    let subscribed = true;
    const cached = readPropertyFilterHistogram(option.option_key, catalogRevision);
    if (cached && retryKey === 0) {
      setHistogram(cached.data.histogram);
      setLoading(false);
      setError(null);
      return () => {
        subscribed = false;
      };
    }
    setLoading(true);
    setError(null);
    loadPropertyFilterHistogram(option.option_key, catalogRevision, { force: retryKey > 0 })
      .then((resource) => {
        if (subscribed) setHistogram(resource.data.histogram);
      })
      .catch((requestError: unknown) => {
        if (!subscribed) return;
        setError(requestError instanceof Error ? requestError.message : "真实分布加载失败。");
      })
      .finally(() => {
        if (subscribed) setLoading(false);
      });
    return () => {
      subscribed = false;
    };
  }, [catalogRevision, embeddedHistogram, option.option_key, retryKey]);

  if (loading && !histogram) {
    return (
      <div className="dbf-histogram is-loading" role="status" aria-label={`${option.label} 真实分布加载中`}>
        <div className="dbf-histogram-head">
          <strong>真实分布</strong>
          <span>读取区间计数…</span>
        </div>
        <div className="dbf-histogram-skeleton" aria-hidden="true">
          {Array.from({ length: 18 }, (_, index) => <i key={index} />)}
        </div>
      </div>
    );
  }

  if (error && !histogram) {
    return (
      <div className="dbf-histogram dbf-histogram--empty" role="alert">
        <div className="dbf-histogram-head">
          <strong>真实分布</strong>
          <button type="button" onClick={() => setRetryKey((current) => current + 1)}>重试</button>
        </div>
        <p title={error}>分布加载失败</p>
      </div>
    );
  }

  if (!histogram || histogram.counts.length === 0) {
    return (
      <div className="dbf-histogram dbf-histogram--empty">
        <div className="dbf-histogram-head">
          <strong>真实分布</strong>
          <span>{formatInteger(option.rows)} 条 · {unit || "无单位"}</span>
        </div>
        <p>当前属性暂无可用的数值分布。</p>
      </div>
    );
  }

  const maximumCount = Math.max(1, ...histogram.counts);
  const centralCount = histogram.counts.reduce((total, count) => total + count, 0);
  const robustDomain = histogram.domain_kind === "p5_p95";
  const domainStartLabel = robustDomain ? "P5" : "Min";
  const domainEndLabel = robustDomain ? "P95" : "Max";
  const medianPosition = option.median_value === null
    ? null
    : distributionPosition(option.median_value, histogram.domain_min, histogram.domain_max);
  const medianStyle = medianPosition === null
    ? undefined
    : ({ "--dbf-median-position": `${medianPosition}%` } as CSSProperties);
  const accessibleSummary = [
    `${option.label} 全库测量记录真实直方图`,
    `${formatInteger(histogram.total_count)} 条记录`,
    `${domainStartLabel} 到 ${domainEndLabel} 主区间 ${formatInteger(centralCount)} 条`,
    `左侧尾部 ${formatInteger(histogram.underflow_count)} 条`,
    `右侧尾部 ${formatInteger(histogram.overflow_count)} 条`,
    `${formatInteger(option.unique_smiles)} 个 SMILES`,
    `${domainStartLabel} ${formatValue(histogram.domain_min)}`,
    `P50 ${formatValue(option.median_value)}`,
    `${domainEndLabel} ${formatValue(histogram.domain_max)}`,
    `单位 ${unit || "无单位"}`
  ].join("，");

  return (
    <div
      className={`dbf-histogram${option.filter_type === "raw" ? " is-raw" : ""}`}
      role="img"
      aria-label={accessibleSummary}
    >
      <div className="dbf-histogram-head">
        <strong>真实分布</strong>
        <span title={`左右尾部另计 ${formatInteger(histogram.underflow_count + histogram.overflow_count)} 条`}>
          {formatInteger(centralCount)} / {formatInteger(histogram.total_count)} 条 · {unit || "无单位"}
        </span>
      </div>
      <div className="dbf-histogram-plot" style={medianStyle} aria-hidden="true">
        <div className="dbf-histogram-bars">
          {histogram.counts.map((count, index) => (
            <i
              key={index}
              className={count === 0 ? "is-empty" : undefined}
              style={{ height: `${(count / maximumCount) * 100}%` }}
              title={`${histogramRangeLabel(histogram, index, unit)}：${formatInteger(count)} 条`}
            />
          ))}
        </div>
        {medianPosition !== null ? <i className="dbf-histogram-median" /> : null}
      </div>
      <div className="dbf-histogram-labels">
        <span title={`${domainStartLabel} 以下 ${formatInteger(histogram.underflow_count)} 条`}>{domainStartLabel} <b>{formatValue(histogram.domain_min)}</b></span>
        <span>P50 <b>{formatValue(option.median_value)}</b></span>
        <span title={`${domainEndLabel} 以上 ${formatInteger(histogram.overflow_count)} 条`}>{domainEndLabel} <b>{formatValue(histogram.domain_max)}</b></span>
      </div>
    </div>
  );
});

type PropertyPickerProps = {
  draft: PropertyFilterDraft;
  option: PropertyFilterOption | undefined;
  open: boolean;
  search: string;
  standardizedOptions: PropertyFilterOption[];
  rawOptions: PropertyFilterOption[];
  onToggle: () => void;
  onSearch: (value: string) => void;
  onSelect: (optionKey: string) => void;
};

const PropertyPicker = memo(function PropertyPicker({
  draft,
  option,
  open,
  search,
  standardizedOptions,
  rawOptions,
  onToggle,
  onSearch,
  onSelect
}: PropertyPickerProps) {
  const searchInputRef = useRef<HTMLInputElement>(null);
  const deferredSearch = useDeferredValue(search);
  const normalizedSearch = deferredSearch.trim().toLocaleLowerCase("zh-CN");
  const visibleStandardized = useMemo(
    () => normalizedSearch
      ? standardizedOptions.filter((candidate) => optionSearchText(candidate).includes(normalizedSearch))
      : standardizedOptions,
    [normalizedSearch, standardizedOptions]
  );
  const visibleRaw = useMemo(
    () => normalizedSearch
      ? rawOptions.filter((candidate) => optionSearchText(candidate).includes(normalizedSearch))
      : rawOptions,
    [normalizedSearch, rawOptions]
  );

  useEffect(() => {
    if (open) window.setTimeout(() => searchInputRef.current?.focus(), 0);
  }, [open]);

  function renderGroup(label: string, options: PropertyFilterOption[], raw: boolean) {
    if (options.length === 0) return null;
    return (
      <section className="dbf-picker-group" aria-label={label}>
        <header>
          <span>{label}</span>
          <b>{options.length}</b>
        </header>
        {options.map((candidate) => {
          const selected = candidate.option_key === draft.optionKey;
          return (
            <button
              type="button"
              className={selected ? "is-selected" : ""}
              key={candidate.option_key}
              onClick={() => onSelect(candidate.option_key)}
            >
              <PropertyTypeDot raw={raw} />
              <span>
                <strong>{candidate.label}</strong>
                <small>{candidate.filter_type} · {optionRange(candidate)}</small>
              </span>
              <em>{formatInteger(candidate.rows)} rows</em>
              {selected ? <CheckCircle2 aria-hidden="true" /> : null}
            </button>
          );
        })}
      </section>
    );
  }

  return (
    <div className="dbf-property-picker-root" data-dbf-picker-root>
      <button
        className="dbf-property-trigger"
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        aria-controls={`dbf-property-picker-${draft.id}`}
      >
        <PropertyTypeDot raw={option?.filter_type === "raw"} />
        <span>
          <strong>{option?.label || "选择筛选属性"}</strong>
          <small>{option ? `${option.filter_type} · ${optionRange(option)}` : "属性目录加载后可选择"}</small>
        </span>
        <ChevronDown aria-hidden="true" />
      </button>

      {open ? (
        <div className="dbf-property-picker" id={`dbf-property-picker-${draft.id}`} role="dialog" aria-label="选择筛选属性">
          <label className="dbf-picker-search">
            <Search aria-hidden="true" />
            <input
              ref={searchInputRef}
              type="search"
              value={search}
              onChange={(event) => onSearch(event.target.value)}
              placeholder="搜索属性名、key 或单位"
              autoComplete="off"
            />
            {search ? (
              <button type="button" onClick={() => onSearch("")} aria-label="清除属性搜索">
                <X aria-hidden="true" />
              </button>
            ) : null}
          </label>
          <div className="dbf-picker-options">
            {renderGroup("标准化属性", visibleStandardized, false)}
            {renderGroup("原始属性", visibleRaw, true)}
            {visibleStandardized.length === 0 && visibleRaw.length === 0 ? (
              <div className="dbf-picker-empty">没有匹配的属性</div>
            ) : null}
          </div>
        </div>
      ) : null}
    </div>
  );
});

const ConditionRow = memo(function ConditionRow({
  draft,
  index,
  option,
  catalogRevision,
  canRemove,
  pickerOpen,
  pickerSearch,
  standardizedOptions,
  rawOptions,
  onTogglePicker,
  onPickerSearch,
  onSelectProperty,
  onBoundChange,
  onRemove
}: {
  draft: PropertyFilterDraft;
  index: number;
  option: PropertyFilterOption | undefined;
  catalogRevision: string;
  canRemove: boolean;
  pickerOpen: boolean;
  pickerSearch: string;
  standardizedOptions: PropertyFilterOption[];
  rawOptions: PropertyFilterOption[];
  onTogglePicker: (id: number) => void;
  onPickerSearch: (value: string) => void;
  onSelectProperty: (id: number, optionKey: string) => void;
  onBoundChange: (id: number, field: "minValue" | "maxValue", value: string) => void;
  onRemove: (id: number) => void;
}) {
  const unit = option ? propertyFilterOptionUnit(option) : "";
  return (
    <div className="dbf-condition-wrap">
      <div className={`dbf-condition-row${draft.error ? " has-error" : ""}`}>
        <div className="dbf-condition-label">属性 {index + 1}</div>
        <div className="dbf-condition-property">
          <PropertyPicker
            draft={draft}
            option={option}
            open={pickerOpen}
            search={pickerSearch}
            standardizedOptions={standardizedOptions}
            rawOptions={rawOptions}
            onToggle={() => onTogglePicker(draft.id)}
            onSearch={onPickerSearch}
            onSelect={(optionKey) => onSelectProperty(draft.id, optionKey)}
          />
        </div>
        <div className="dbf-condition-distribution">
          {option ? (
            <PropertyHistogram
              key={`${catalogRevision}:${option.option_key}`}
              option={option}
              catalogRevision={catalogRevision}
            />
          ) : (
            <div className="dbf-histogram-placeholder">请选择属性以查看真实分布</div>
          )}
        </div>
        <label className="dbf-bound-field">
          <span>最小值 <small>可选</small></span>
          <span className="dbf-number-shell">
            <input
              type="number"
              inputMode="decimal"
              value={draft.minValue}
              onChange={(event) => onBoundChange(draft.id, "minValue", event.target.value)}
              aria-invalid={Boolean(draft.error)}
              aria-label={`属性 ${index + 1} 最小值`}
              step="any"
            />
            {unit ? <em>{unit}</em> : null}
          </span>
        </label>
        <label className="dbf-bound-field">
          <span>最大值 <small>可选</small></span>
          <span className="dbf-number-shell">
            <input
              type="number"
              inputMode="decimal"
              value={draft.maxValue}
              onChange={(event) => onBoundChange(draft.id, "maxValue", event.target.value)}
              aria-invalid={Boolean(draft.error)}
              aria-label={`属性 ${index + 1} 最大值`}
              step="any"
            />
            {unit ? <em>{unit}</em> : null}
          </span>
        </label>
        <button
          className="dbf-remove-condition"
          type="button"
          disabled={!canRemove}
          onClick={() => onRemove(draft.id)}
          aria-label={`删除属性 ${index + 1}`}
        >
          <Trash2 aria-hidden="true" />
        </button>
      </div>
      {draft.error ? (
        <div className="dbf-condition-error" role="alert">
          <AlertTriangle aria-hidden="true" />
          {draft.error}
        </div>
      ) : null}
    </div>
  );
});

export function DatabaseFilterPage() {
  const filter = usePropertyFilter();
  const [drawerWidth, setDrawerWidth] = useState(380);
  const [openPickerId, setOpenPickerId] = useState<number | null>(null);
  const [pickerSearch, setPickerSearch] = useState("");
  const pickerTriggerRefs = useRef(new Map<number, HTMLButtonElement>());
  const previousDrawerOpen = useRef(filter.drawerOpen);
  const drawerReopenRef = useRef<HTMLButtonElement | null>(null);

  const sourceReady = filter.optionsData?.source_status === "ready" || Boolean(filter.optionsData && !filter.optionsError);
  const sourceError = Boolean(filter.optionsError || filter.optionsRefreshError);

  useEffect(() => {
    function handlePointerDown(event: PointerEvent) {
      const target = event.target as HTMLElement | null;
      if (openPickerId !== null && !target?.closest("[data-dbf-picker-root]")) {
        setOpenPickerId(null);
        setPickerSearch("");
      }
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key !== "Escape") return;
      if (openPickerId !== null) {
        event.stopImmediatePropagation();
        const trigger = pickerTriggerRefs.current.get(openPickerId);
        setOpenPickerId(null);
        setPickerSearch("");
        window.setTimeout(() => trigger?.focus(), 0);
      }
    }

    document.addEventListener("pointerdown", handlePointerDown);
    window.addEventListener("keydown", handleKeyDown, true);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
      window.removeEventListener("keydown", handleKeyDown, true);
    };
  }, [openPickerId]);

  useEffect(() => {
    if (previousDrawerOpen.current && !filter.drawerOpen && filter.submitted) {
      window.setTimeout(() => drawerReopenRef.current?.focus(), 0);
    }
    previousDrawerOpen.current = filter.drawerOpen;
  }, [filter.drawerOpen, filter.submitted]);

  const metrics = useMemo(
    () => [
      { label: "属性记录", value: filter.optionsData?.total_records },
      { label: "标准化记录", value: filter.optionsData?.mapped_records },
      { label: "原始记录", value: filter.optionsData?.raw_records },
      { label: "可筛选属性", value: filter.options.length }
    ],
    [filter.options.length, filter.optionsData]
  );

  const handleSubmit = useCallback((event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    filter.run();
  }, [filter.run]);

  const togglePicker = useCallback((id: number) => {
    setOpenPickerId((current) => (current === id ? null : id));
    setPickerSearch("");
  }, []);

  const handleSelectProperty = useCallback((id: number, optionKey: string) => {
    filter.selectProperty(id, optionKey);
    setOpenPickerId(null);
    setPickerSearch("");
    window.setTimeout(() => pickerTriggerRefs.current.get(id)?.focus(), 0);
  }, [filter.selectProperty]);
  const handleBoundChange = useCallback(
    (id: number, field: "minValue" | "maxValue", value: string) => filter.updateBound(id, field, value),
    [filter.updateBound]
  );
  const handleRemoveCondition = useCallback((id: number) => filter.removeCondition(id), [filter.removeCondition]);
  const closeDrawer = useCallback(() => filter.setDrawerOpen(false), [filter.setDrawerOpen]);
  const openDrawer = useCallback(() => filter.setDrawerOpen(true), [filter.setDrawerOpen]);

  return (
    <div
      className={`database-filter-page${filter.drawerOpen ? " dbf-has-open-drawer" : ""}`}
      style={{ "--dbf-drawer-width": `${drawerWidth}px` } as CSSProperties}
    >
      <div className="dbf-background" aria-hidden="true" />

      <div className="dbf-page-heading">
        <h1>数据库筛选</h1>
      </div>

      <div className="dbf-workbench-shell">
        <div className="dbf-workbench-column">
          <div className="dbf-toolbar" aria-label="数据库筛选工具栏">
            <span className={`dbf-tool-status${sourceError ? " is-error" : sourceReady ? " is-ready" : ""}`}>
              <i aria-hidden="true" />
              {filter.optionsPending
                ? "正在连接数据源"
                : filter.optionsRefreshing
                  ? "正在同步属性目录"
                  : filter.optionsRefreshError || filter.optionsError
                    ? "目录同步异常"
                    : "数据源就绪 · PostgreSQL"}
            </span>
            <button
              type="button"
              onClick={filter.retryOptions}
              disabled={filter.optionsPending || filter.optionsRefreshing}
              aria-label="刷新数据，检查数据库属性目录是否有更新"
              title="检查数据库属性目录是否有更新"
            >
              <RefreshCw className={filter.optionsRefreshing ? "dbf-spin" : undefined} aria-hidden="true" />
              {filter.optionsRefreshing ? "刷新中" : "刷新数据"}
            </button>
            <button type="button" onClick={filter.reset}>
              <RotateCcw aria-hidden="true" />
              重置条件
            </button>
          </div>

          <section className="dbf-filter-surface" aria-labelledby="database-filter-surface-title">
            <header className="dbf-surface-header">
              <div className="dbf-surface-heading">
                <span><Filter aria-hidden="true" /></span>
                <div>
                  <h2 id="database-filter-surface-title">多性质阈值筛选</h2>
                  <p>在同一聚合物 SMILES 上组合属性区间，条件之间按 AND 取交集</p>
                </div>
              </div>
            </header>

            <div className="dbf-metric-strip" aria-label="数据库摘要">
              {metrics.map((metric) => (
                <div className="dbf-metric" key={metric.label}>
                  <span>{metric.label}</span>
                  <strong>{filter.optionsPending ? "···" : formatInteger(metric.value)}</strong>
                </div>
              ))}
            </div>

            {filter.optionsLoading ? (
              <div className="dbf-options-state" aria-live="polite">
                <LoaderCircle className="dbf-spin" aria-hidden="true" />
                <div><strong>正在读取属性目录</strong><span>加载真实属性范围与分布统计…</span></div>
              </div>
            ) : null}

            {!filter.optionsLoading && filter.optionsError ? (
              <div className="dbf-options-state is-error" role="alert">
                <AlertTriangle aria-hidden="true" />
                <div><strong>属性目录加载失败</strong><span>{filter.optionsError}</span></div>
                <button type="button" onClick={filter.retryOptions}><RefreshCw aria-hidden="true" />重新加载</button>
              </div>
            ) : null}

            {filter.optionsData && filter.optionsRefreshError ? (
              <div className="dbf-options-refresh-warning" role="status">
                <AlertTriangle aria-hidden="true" />
                <span>属性目录同步失败，当前继续使用本次会话中的缓存数据。</span>
                <button type="button" onClick={filter.retryOptions}>重新同步</button>
              </div>
            ) : null}

            {!filter.optionsLoading && !filter.optionsError && filter.options.length === 0 ? (
              <div className="dbf-options-state">
                <Info aria-hidden="true" />
                <div><strong>暂无可筛选属性</strong><span>数据库已连接，但属性目录当前为空。</span></div>
              </div>
            ) : null}

            {!filter.optionsLoading && !filter.optionsError && filter.options.length > 0 ? (
              <form className="dbf-filter-form" onSubmit={handleSubmit} noValidate>
                <div className="dbf-conditions-header">
                  <div>
                    <strong>筛选条件</strong>
                    <span>AND 同时满足</span>
                  </div>
                  <em>{filter.drafts.length} / {PROPERTY_FILTER_MAX_CONDITIONS}</em>
                </div>

                <div className="dbf-conditions-list">
                  {filter.drafts.map((draft, index) => (
                    <div key={draft.id}>
                      {index > 0 ? <div className="dbf-and-connector"><span>AND</span></div> : null}
                      <div
                        ref={(node) => {
                          const trigger = node?.querySelector<HTMLButtonElement>(".dbf-property-trigger");
                          if (trigger) pickerTriggerRefs.current.set(draft.id, trigger);
                          else pickerTriggerRefs.current.delete(draft.id);
                        }}
                      >
                        <ConditionRow
                          draft={draft}
                          index={index}
                          option={filter.optionsByKey.get(draft.optionKey)}
                          catalogRevision={filter.optionsRevision}
                          canRemove={filter.drafts.length > 1}
                          pickerOpen={openPickerId === draft.id}
                          pickerSearch={openPickerId === draft.id ? pickerSearch : ""}
                          standardizedOptions={filter.standardizedOptions}
                          rawOptions={filter.rawOptions}
                          onTogglePicker={togglePicker}
                          onPickerSearch={setPickerSearch}
                          onSelectProperty={handleSelectProperty}
                          onBoundChange={handleBoundChange}
                          onRemove={handleRemoveCondition}
                        />
                      </div>
                    </div>
                  ))}
                </div>

                <div className="dbf-add-row">
                  <button
                    type="button"
                    onClick={filter.addCondition}
                    disabled={filter.drafts.length >= PROPERTY_FILTER_MAX_CONDITIONS}
                  >
                    <Plus aria-hidden="true" />
                    {filter.drafts.length >= PROPERTY_FILTER_MAX_CONDITIONS ? "已达 8 条上限" : "添加条件"}
                  </button>
                  <span>每个条件至少填写一个阈值</span>
                </div>

                <div className="dbf-query-zone">
                  <label className="dbf-query-input">
                    <Search aria-hidden="true" />
                    <input
                      type="search"
                      value={filter.queryDraft}
                      maxLength={200}
                      onChange={(event) => filter.setQueryDraft(event.target.value)}
                      placeholder="可选：按聚合物名称或 SMILES 进一步缩小范围"
                      aria-label="关键词"
                    />
                    <span>{filter.queryDraft.length}/200</span>
                  </label>
                  <select
                    className="dbf-page-size"
                    value={filter.pageSize}
                    onChange={(event) => filter.setPageSize(Number(event.target.value))}
                    aria-label="每页结果数"
                  >
                    {PROPERTY_FILTER_PAGE_SIZES.map((size) => <option value={size} key={size}>{size} 条 / 页</option>)}
                  </select>
                  <button className="dbf-run-button" type="submit" aria-busy={filter.searchLoading}>
                    {filter.searchLoading ? <LoaderCircle className="dbf-spin" aria-hidden="true" /> : <Search aria-hidden="true" />}
                    {filter.searchLoading ? "正在筛选" : "运行筛选"}
                  </button>
                </div>

                <div className="dbf-expression-capsule">
                  <span><Filter aria-hidden="true" /></span>
                  <div>
                    <small>当前草稿表达式</small>
                    <code>{filter.draftExpression}</code>
                  </div>
                  <em>按 canonical SMILES 聚合</em>
                </div>

                {filter.validationError ? (
                  <div className="dbf-validation-banner" role="alert">
                    <AlertTriangle aria-hidden="true" />
                    {filter.validationError}
                  </div>
                ) : null}
              </form>
            ) : null}

            <div className="dbf-surface-note">
              <Info aria-hidden="true" />
              <span>直方图来自全库测量记录真实计数；数据充足时展示 P5–P95 等宽区间及两侧尾部，小样本使用完整范围。</span>
            </div>
          </section>
        </div>
      </div>

      <DatabaseFilterResultsDrawer
        open={filter.drawerOpen}
        submitted={filter.submitted}
        data={filter.searchData}
        loading={filter.searchLoading}
        error={filter.searchError}
        page={filter.page}
        pageSize={filter.pageSize}
        matchedRecords={filter.matchedRecords}
        totalPages={filter.totalPages}
        width={drawerWidth}
        onWidthChange={setDrawerWidth}
        reopenButtonRef={drawerReopenRef}
        onClose={closeDrawer}
        onOpen={openDrawer}
        onRetry={filter.retrySearch}
        onPageChange={filter.setPage}
      />
    </div>
  );
}
