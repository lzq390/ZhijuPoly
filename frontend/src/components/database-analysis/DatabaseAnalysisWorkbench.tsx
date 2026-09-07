import {
  AlertTriangle,
  Atom,
  Check,
  ChevronDown,
  Database,
  FlaskConical,
  Grid2X2,
  Layers3,
  Network,
  RefreshCw,
  Sigma,
  TableProperties
} from "lucide-react";
import type { CSSProperties } from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { fetchDatabaseAnalytics, fetchDatabaseDatasetSummary } from "../../services/api";
import type { DatasetSummaryResponse } from "../../types";
import "../../styles/structure-workbench.css";
import "../../styles/database-analysis.css";
import { MaterialDiscoveryPageTitle } from "../MaterialDiscoveryPageTitle";
import {
  averageComponentCount,
  BarList,
  ChipCloud,
  CoverageList,
  DataTable,
  DistributionGroups,
  DonutBlock,
  EmptyPanel,
  formatNumber,
  formatTimestamp,
  KpiStrip,
  Panel,
  RangeList,
  SourceMatrix
} from "./charts";
import { DatabaseRecordDrawer, useDatabaseRecordDrawerSizing } from "./DatabaseRecordDrawer";
import { DftAnalysisView } from "./DftAnalysisView";
import { databaseAnalysisErrorMessage, databaseAnalysisSourceMessage } from "./errors";
import type {
  AnalysisViewKey,
  AnalyticsValidationErrors,
  DatabaseAnalyticsPayload,
  DftAnalytics,
  DatasetDefinition,
  DatasetKey,
  DisplayDataset,
  DrawerRequest,
  FormulationAnalytics,
  ProcessAnalytics,
  PropertyAnalytics,
  RankedItem,
  StructureEffectAnalytics
} from "./types";
import { isDatasetReady, toDisplayDataset, validateDatabaseAnalyticsPayload } from "./types";

const DATASETS: DatasetDefinition[] = [
  {
    key: "process",
    routeKey: "process",
    title: "实验过程数据",
    subtitle: "EXPERIMENTAL PROCESS DATA",
    description: "过程关键词、材料实体、产品名称与反应条件",
    accent: "#2563eb",
    soft: "#eef4ff"
  },
  {
    key: "property",
    routeKey: "property",
    title: "实验性能数据",
    subtitle: "EXPERIMENTAL PROPERTY DATA",
    description: "性能类别、属性排行、数值范围与代表属性",
    accent: "#0891b2",
    soft: "#eafcff"
  },
  {
    key: "structureEffect",
    routeKey: "structure-effect",
    title: "结构–性能数据",
    subtitle: "STRUCTURE–PROPERTY DATA",
    description: "数据来源、单位分布与结构–性能关联",
    accent: "#1d4ed8",
    soft: "#e8f1ff"
  },
  {
    key: "dft",
    routeKey: "dft",
    title: "DFT 构象数据",
    subtitle: "DFT CONFORMATION DATA",
    description: "PCA、三维构象、能量轨迹与优化步骤",
    accent: "#4f46e5",
    soft: "#eef0ff"
  },
  {
    key: "formulation",
    routeKey: "formulation",
    title: "配方比例数据",
    subtitle: "FORMULATION RATIO DATA",
    description: "组分、比例、聚合物家族与工艺覆盖",
    accent: "#0284c7",
    soft: "#ecf8ff"
  }
];

type DatabaseAnalysisProps = {
  onBackHome: () => void;
  onBackDatabase: () => void;
  onOpenDataset: (key: DatasetKey) => void;
  selectedKey: DatasetKey | null;
};

type AnalyticsState = {
  analytics: DatabaseAnalyticsPayload | null;
  loading: boolean;
  refreshing: boolean;
  error: string | null;
  source: string | null;
  generatedAt: string | null;
  validationErrors: AnalyticsValidationErrors;
};

function datasetIcon(key: AnalysisViewKey, className?: string) {
  const props = { className, "aria-hidden": true };
  if (key === "overview") return <Grid2X2 {...props} />;
  if (key === "process") return <FlaskConical {...props} />;
  if (key === "property") return <Sigma {...props} />;
  if (key === "structureEffect") return <Network {...props} />;
  if (key === "dft") return <Atom {...props} />;
  return <TableProperties {...props} />;
}

function useDatasetSummary() {
  const [summary, setSummary] = useState<DatasetSummaryResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const controllerRef = useRef<AbortController | null>(null);

  const load = useCallback(async () => {
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    setLoading(true);
    setError(null);
    try {
      const response = await fetchDatabaseDatasetSummary(controller.signal);
      if (controller.signal.aborted) return false;
      setSummary(response);
      return true;
    } catch (nextError) {
      if (!controller.signal.aborted) setError(databaseAnalysisErrorMessage(nextError, "数据源状态加载失败"));
      return false;
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    return () => controllerRef.current?.abort();
  }, [load]);

  return { summary, loading, error, reload: load };
}

function useDatabaseAnalytics() {
  const [state, setState] = useState<AnalyticsState>({
    analytics: null,
    loading: true,
    refreshing: false,
    error: null,
    source: null,
    generatedAt: null,
    validationErrors: {}
  });
  const controllerRef = useRef<AbortController | null>(null);

  const load = useCallback(async (refresh: boolean) => {
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    setState((current) => ({
      ...current,
      loading: refresh ? current.loading : true,
      refreshing: refresh,
      error: null
    }));

    try {
      const response = await fetchDatabaseAnalytics({ refresh, signal: controller.signal });
      if (controller.signal.aborted) return false;
      const validated = validateDatabaseAnalyticsPayload(response.datasets);
      setState({
        analytics: validated.analytics,
        loading: false,
        refreshing: false,
        error: null,
        source: response.source,
        generatedAt: response.generated_at ?? new Date().toISOString(),
        validationErrors: validated.errors
      });
      if (!refresh) return "loaded";
      return response.refresh_status ?? (response.source === "snapshot" ? "unchanged" : "recomputed");
    } catch (nextError) {
      if (controller.signal.aborted) return false;
      setState((current) => ({
        ...current,
        loading: false,
        refreshing: false,
        error: databaseAnalysisErrorMessage(nextError, "分析数据加载失败")
      }));
      return false;
    }
  }, []);

  useEffect(() => {
    void load(false);
    return () => controllerRef.current?.abort();
  }, [load]);

  return { ...state, reload: () => load(false), refresh: () => load(true) };
}

function analyticsRecordCount(analytics: DatabaseAnalyticsPayload | null, key: DatasetKey) {
  const data = analytics?.[key];
  if (!data) return undefined;
  return data.rows;
}

function focusableElements(container: HTMLElement) {
  return Array.from(container.querySelectorAll<HTMLElement>(
    'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
  )).filter((element) => !element.hasAttribute("inert") && element.getAttribute("aria-hidden") !== "true");
}

export function DatabaseAnalysis(props: DatabaseAnalysisProps) {
  const summaryState = useDatasetSummary();
  const analyticsState = useDatabaseAnalytics();
  const [datasetPopoverOpen, setDatasetPopoverOpen] = useState(false);
  const [transientMessage, setTransientMessage] = useState<string | null>(null);
  const [drawerRequest, setDrawerRequest] = useState<DrawerRequest | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [drawerRestoreTarget, setDrawerRestoreTarget] = useState<HTMLElement | null>(null);
  const [datasetPopoverOverlay, setDatasetPopoverOverlay] = useState(false);
  const drawerSizing = useDatabaseRecordDrawerSizing();
  const rootRef = useRef<HTMLDivElement | null>(null);
  const datasetButtonRef = useRef<HTMLButtonElement | null>(null);
  const datasetPopoverRef = useRef<HTMLElement | null>(null);
  const datasetPopoverOpenRef = useRef(datasetPopoverOpen);
  const restoreDatasetFocusRef = useRef(false);
  const messageTimerRef = useRef<number | null>(null);
  datasetPopoverOpenRef.current = datasetPopoverOpen;

  const displayDatasets = useMemo(() => {
    const byKey = new Map(summaryState.summary?.datasets.map((item) => [item.key, item]) ?? []);
    return DATASETS.map((definition) =>
      toDisplayDataset(
        definition,
        byKey.get(definition.key),
        summaryState.loading,
        summaryState.error,
        analyticsRecordCount(analyticsState.analytics, definition.key)
      )
    );
  }, [analyticsState.analytics, summaryState.error, summaryState.loading, summaryState.summary]);

  const currentView: AnalysisViewKey = props.selectedKey ?? "overview";
  const currentDataset = props.selectedKey
    ? displayDatasets.find((dataset) => dataset.key === props.selectedKey) ?? displayDatasets[0]
    : null;
  const totalRecords = displayDatasets.reduce((sum, dataset) => sum + (dataset.recordCount ?? 0), 0);
  const readyCount = displayDatasets.filter(isDatasetReady).length;
  const latestImport = displayDatasets
    .map((dataset) => dataset.latestImportFinishedAt)
    .filter((value): value is string => Boolean(value))
    .sort()
    .at(-1) ?? null;
  const updatedAt = analyticsState.generatedAt ?? latestImport;
  const analyticsMode = analyticsState.source === "live"
    ? {
        value: "已更新",
        description: "已根据当前数据更新聚合统计结果。"
      }
    : analyticsState.source === "snapshot"
      ? {
          value: "统计数据",
          description: "展示当前数据源的聚合统计结果。"
        }
      : {
          value: currentDataset?.dataSource === "postgres" ? "数据库统计" : "统计数据",
          description: "当前页面展示数据库聚合统计结果。"
        };

  useEffect(() => {
    restoreDatasetFocusRef.current = false;
    setDatasetPopoverOpen(false);
    setDrawerOpen(false);
    setDrawerRequest(null);
    setDrawerRestoreTarget(null);
  }, [props.selectedKey]);

  useEffect(() => {
    const root = rootRef.current;
    if (!root) return;
    const container = root.querySelector<HTMLElement>(".np-sw-workspace") ?? root;
    const update = () => {
      const measuredWidth = container.getBoundingClientRect().width;
      const width = measuredWidth > 0 ? measuredWidth : window.innerWidth;
      setDatasetPopoverOverlay(width < 900);
    };
    update();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(update);
    observer.observe(container);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (!datasetPopoverOpen) return;
    function handlePointerDown(event: PointerEvent) {
      const target = event.target as Node;
      if (!datasetPopoverRef.current?.contains(target) && !datasetButtonRef.current?.contains(target)) {
        restoreDatasetFocusRef.current = false;
        setDatasetPopoverOpen(false);
      }
    }
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        restoreDatasetFocusRef.current = false;
        setDatasetPopoverOpen(false);
        datasetButtonRef.current?.focus();
        return;
      }
      if (!datasetPopoverOverlay || event.key !== "Tab" || !datasetPopoverRef.current) return;
      const focusable = focusableElements(datasetPopoverRef.current);
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    requestAnimationFrame(() => datasetPopoverRef.current?.querySelector<HTMLButtonElement>("button:not(:disabled)")?.focus());
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
      if (!datasetPopoverOpenRef.current && restoreDatasetFocusRef.current) {
        restoreDatasetFocusRef.current = false;
        window.requestAnimationFrame(() => datasetButtonRef.current?.focus());
      }
    };
  }, [datasetPopoverOpen, datasetPopoverOverlay]);

  useEffect(() => () => {
    if (messageTimerRef.current !== null) window.clearTimeout(messageTimerRef.current);
  }, []);

  async function handleRefresh() {
    setTransientMessage(null);
    const outcome = await analyticsState.refresh();
    if (!outcome) return;
    await summaryState.reload();
    setTransientMessage(outcome === "unchanged"
      ? "数据已是最新，当前统计结果保持不变。"
      : "数据已更新，当前数据集和浏览位置保持不变。");
    if (messageTimerRef.current !== null) window.clearTimeout(messageTimerRef.current);
    messageTimerRef.current = window.setTimeout(() => setTransientMessage(null), 2600);
  }

  function selectDataset(key: AnalysisViewKey) {
    restoreDatasetFocusRef.current = true;
    setDatasetPopoverOpen(false);
    if (key === "overview") props.onBackDatabase();
    else props.onOpenDataset(key);
    requestAnimationFrame(() => datasetButtonRef.current?.focus());
  }

  function openRecords(request: DrawerRequest, trigger?: HTMLElement) {
    setDrawerRestoreTarget(trigger ?? null);
    setDrawerRequest(request);
    setDrawerOpen(true);
  }

  const banner = analyticsState.refreshing
    ? {
        tone: "info",
        message: "正在检查数据更新，期间保留当前结果。"
      }
    : analyticsState.error
      ? { tone: "error", message: analyticsState.analytics ? `统计更新失败，仍显示上次成功结果：${analyticsState.error}` : analyticsState.error }
      : summaryState.error
        ? { tone: "warning", message: `数据源状态暂不可用：${summaryState.error}` }
        : transientMessage
          ? { tone: "success", message: transientMessage }
          : null;

  const rootStyle = { "--np-sw-drawer-width": `${drawerSizing.width}px` } as CSSProperties;

  return (
    <div
      ref={rootRef}
      className="np-structure-workbench np-database-analysis np-material-discovery-page"
      data-view-key={currentView === "structureEffect" ? "structure-effect" : currentView}
      style={rootStyle}
    >
      <div className={`np-sw-page${drawerOpen ? " has-open-drawer" : ""}`}>
        <MaterialDiscoveryPageTitle className="np-sw-page-title">数据库分析</MaterialDiscoveryPageTitle>
        <div className={`np-sw-layout${drawerOpen ? " has-open-drawer" : ""}`}>
          <main className={`np-sw-workspace${datasetPopoverOpen && datasetPopoverOverlay ? " has-dataset-modal" : ""}`}>
            <div className="dba-module-toolbar" aria-label="数据库分析工具栏">
              <div
                className={`dba-toolbar-status${readyCount === displayDatasets.length ? " is-ready" : " is-warning"}`}
                title={`${analyticsMode.description} 最近更新：${formatTimestamp(updatedAt)}`}
              >
                <Database aria-hidden="true" />
                <span>{analyticsMode.value}</span>
                <span className="dba-toolbar-detail">{formatNumber(currentDataset?.recordCount ?? totalRecords, 0)} 条</span>
                <time className="dba-toolbar-detail" dateTime={updatedAt ?? undefined}>{formatTimestamp(updatedAt)}</time>
                <i aria-hidden="true" />
                <strong>{readyCount} / {displayDatasets.length} 数据源</strong>
              </div>
            </div>

            <div className="dba-analysis-scroll">
              <section className="dba-analysis-surface" aria-labelledby="dba-surface-title">
                <header className={`dba-surface-head${datasetPopoverOpen && datasetPopoverOverlay ? " has-dataset-modal" : ""}`}>
                  <div className="dba-surface-identity">
                    <span className="dba-surface-icon">{datasetIcon(currentView)}</span>
                    <div className="dba-surface-heading">
                      <h2 id="dba-surface-title">{currentDataset?.title ?? "全库概览"}</h2>
                      <p>{currentDataset?.description ?? "汇总五类聚合物数据源，查看统计覆盖与数据状态"}</p>
                    </div>
                  </div>
                  <div className="dba-toolbar" aria-label="数据操作">
                    <button
                      type="button"
                      className={`dba-tool-button dba-refresh-button ${analyticsState.refreshing ? "is-refreshing" : ""}`}
                      data-workbench-tool="refresh"
                      aria-busy={analyticsState.refreshing}
                      disabled={analyticsState.refreshing}
                      onClick={() => void handleRefresh()}
                    >
                      <RefreshCw aria-hidden="true" /><span>{analyticsState.refreshing ? "刷新中" : "刷新数据"}</span>
                    </button>
                    <button
                      ref={datasetButtonRef}
                      type="button"
                      className="dba-tool-button dba-dataset-button"
                      aria-expanded={datasetPopoverOpen}
                      aria-controls="dba-dataset-popover"
                      onClick={() => {
                        restoreDatasetFocusRef.current = true;
                        setDatasetPopoverOpen((open) => !open);
                      }}
                    >
                      <Layers3 aria-hidden="true" /><span>选择数据集</span><ChevronDown aria-hidden="true" />
                    </button>
                    {datasetPopoverOpen ? (
                      <>
                        {datasetPopoverOverlay ? (
                          <button
                            type="button"
                            className="dba-dataset-backdrop"
                            aria-label="关闭数据集选择"
                            onPointerDown={(event) => {
                              event.stopPropagation();
                              restoreDatasetFocusRef.current = true;
                              setDatasetPopoverOpen(false);
                            }}
                            onClick={() => {
                              restoreDatasetFocusRef.current = true;
                              setDatasetPopoverOpen(false);
                            }}
                          />
                        ) : null}
                        <DatasetPopover
                          ref={datasetPopoverRef}
                          currentView={currentView}
                          datasets={displayDatasets}
                          readyCount={readyCount}
                          modal={datasetPopoverOverlay}
                          onSelect={selectDataset}
                        />
                      </>
                    ) : null}
                  </div>
                </header>

                {banner ? (
                  <div className={`dba-surface-banner ${banner.tone}`} role="status" aria-live="polite">
                    <span>
                      {banner.tone === "error" || banner.tone === "warning" ? <AlertTriangle aria-hidden="true" /> : <Check aria-hidden="true" />}
                      {banner.message}
                    </span>
                    {banner.tone === "error" ? (
                      <button type="button" onClick={() => void (analyticsState.analytics ? handleRefresh() : analyticsState.reload())}>重试</button>
                    ) : banner.tone === "warning" ? (
                      <button type="button" onClick={() => void summaryState.reload()}>重试状态</button>
                    ) : null}
                  </div>
                ) : null}

                <div className={`dba-surface-body ${currentView === "overview" ? "is-overview" : ""}`} aria-busy={analyticsState.loading || analyticsState.refreshing}>
                  {analyticsState.loading && !analyticsState.analytics ? (
                    <AnalysisSkeleton />
                  ) : (
                    <AnalysisContent
                      view={currentView}
                      datasets={displayDatasets}
                      analytics={analyticsState.analytics}
                      analyticsError={analyticsState.error}
                      validationErrors={analyticsState.validationErrors}
                      updatedAt={updatedAt}
                      onRetry={() => void analyticsState.reload()}
                      onOpenDataset={props.onOpenDataset}
                      onOpenRecords={openRecords}
                    />
                  )}
                  {analyticsState.refreshing ? <RefreshOverlay /> : null}
                </div>
              </section>
            </div>
          </main>
          <DatabaseRecordDrawer
            open={drawerOpen}
            request={drawerRequest}
            width={drawerSizing.width}
            profile={drawerSizing.profile}
            restoreFocusTarget={drawerRestoreTarget}
            onWidthChange={drawerSizing.setWidth}
            onClose={() => setDrawerOpen(false)}
            onOpen={(trigger) => {
              setDrawerRestoreTarget(trigger ?? null);
              setDrawerOpen(true);
            }}
          />
        </div>
      </div>
    </div>
  );
}

const DatasetPopover = function DatasetPopover({
  ref,
  currentView,
  datasets,
  readyCount,
  modal,
  onSelect
}: {
  ref: React.Ref<HTMLElement>;
  currentView: AnalysisViewKey;
  datasets: DisplayDataset[];
  readyCount: number;
  modal: boolean;
  onSelect: (key: AnalysisViewKey) => void;
}) {
  const availabilityTone = readyCount === datasets.length
    ? "is-ready"
    : readyCount > 0
      ? "is-partial"
      : "is-unavailable";
  return (
    <section
      ref={ref}
      className={`dba-dataset-popover${modal ? " is-modal" : ""}`}
      id="dba-dataset-popover"
      role={modal ? "dialog" : "region"}
      aria-modal={modal ? "true" : undefined}
      aria-label="选择数据集"
    >
      <header>
        <div><h3>切换分析数据集</h3><p>选择后进入对应的完整分析工作面</p></div>
        <span className={availabilityTone}><i />{readyCount} 个可用</span>
      </header>
      <div className="dba-dataset-grid">
        <button
          type="button"
          className={`dba-dataset-option is-overview ${currentView === "overview" ? "is-active" : ""}`}
          data-dataset-key="overview"
          onClick={() => onSelect("overview")}
        >
          <span className="dba-dataset-option-top"><span className="dba-dataset-option-icon">{datasetIcon("overview")}</span><span><strong>全库概览</strong><small>POLYMER DATABASE OVERVIEW</small></span></span>
          <em>{currentView === "overview" ? <><Check aria-hidden="true" />当前视图</> : "返回概览"}</em>
        </button>
        {datasets.map((dataset) => (
          <button
            type="button"
            className={`dba-dataset-option ${currentView === dataset.key ? "is-active" : ""}`}
            data-dataset-key={dataset.routeKey}
            style={{ "--dba-option-accent": dataset.accent, "--dba-option-soft": dataset.soft } as React.CSSProperties}
            key={dataset.key}
            onClick={() => onSelect(dataset.key)}
          >
            <span className="dba-dataset-option-top"><span className="dba-dataset-option-icon">{datasetIcon(dataset.key)}</span><i className={isDatasetReady(dataset) ? "is-ready" : ""} /></span>
            <strong>{dataset.title}</strong><small>{dataset.subtitle}</small>
          </button>
        ))}
      </div>
    </section>
  );
};

function AnalysisContent({
  view,
  datasets,
  analytics,
  analyticsError,
  validationErrors,
  updatedAt,
  onRetry,
  onOpenDataset,
  onOpenRecords
}: {
  view: AnalysisViewKey;
  datasets: DisplayDataset[];
  analytics: DatabaseAnalyticsPayload | null;
  analyticsError: string | null;
  validationErrors: AnalyticsValidationErrors;
  updatedAt: string | null;
  onRetry: () => void;
  onOpenDataset: (key: DatasetKey) => void;
  onOpenRecords: (request: DrawerRequest, trigger?: HTMLElement) => void;
}) {
  if (view === "overview") {
    return <Overview datasets={datasets} analytics={analytics} updatedAt={updatedAt} onOpenDataset={onOpenDataset} />;
  }
  const dataset = datasets.find((item) => item.key === view);
  if (!dataset) return <WorkbenchState title="未找到数据集" message="当前地址没有对应的数据集。" />;
  const data = analytics?.[view];
  const summaryUnavailable = ["loading", "unknown", "unavailable"].includes(dataset.sourceStatus);
  if (!isDatasetReady(dataset) && !(data && summaryUnavailable)) {
    return <WorkbenchState title="该数据源尚未就绪" message={databaseAnalysisSourceMessage(dataset.sourceMessage)} />;
  }
  if (validationErrors[view]) {
    return <WorkbenchState title="分析数据格式异常" message={validationErrors[view] ?? "分析数据暂时无法读取。"} actionLabel="重新加载" onAction={onRetry} />;
  }
  if (!data) {
    return <WorkbenchState title={analyticsError ? "分析数据加载失败" : "暂无可展示的分析数据"} message={analyticsError ?? "当前数据源暂无可展示的分析结果。"} actionLabel="重新加载" onAction={onRetry} />;
  }
  if (view === "process") return <ProcessView data={data as ProcessAnalytics} recordCount={dataset.recordCount} onOpenRecords={onOpenRecords} />;
  if (view === "property") return <PropertyView data={data as PropertyAnalytics} recordCount={dataset.recordCount} onOpenRecords={onOpenRecords} />;
  if (view === "structureEffect") return <StructureEffectView data={data as StructureEffectAnalytics} recordCount={dataset.recordCount} onOpenRecords={onOpenRecords} />;
  if (view === "dft") return <DftAnalysisView data={data as DftAnalytics} recordCount={dataset.recordCount} onOpenRecords={onOpenRecords} />;
  return <FormulationView data={data as FormulationAnalytics} recordCount={dataset.recordCount} onOpenRecords={onOpenRecords} />;
}

function Overview({
  datasets,
  analytics,
  updatedAt,
  onOpenDataset
}: {
  datasets: DisplayDataset[];
  analytics: DatabaseAnalyticsPayload | null;
  updatedAt: string | null;
  onOpenDataset: (key: DatasetKey) => void;
}) {
  const readyCount = datasets.filter(isDatasetReady).length;
  const totalRecords = datasets.reduce((sum, dataset) => sum + (dataset.recordCount ?? 0), 0);
  const updated = formatTimestamp(updatedAt);
  const [date = "—", time = "—"] = updated.split(" ");
  return (
    <>
      <KpiStrip items={[
        { label: "分析数据集", value: String(datasets.length), unit: "个", note: "五类统计分析工作面" },
        { label: "可用数据源", value: `${readyCount} / ${datasets.length}`, note: readyCount === datasets.length ? "全部数据源可用" : "部分数据源待就绪" },
        { label: "总记录量", value: formatNumber(totalRecords, 0), unit: "条", note: "按五类数据集汇总" },
        { label: "最近同步", value: time, note: `${date} · 统计结果` }
      ]} />
      <div className="dba-section-row"><h3>数据集概览</h3></div>
      <div className="dba-overview-grid">
        {datasets.map((dataset) => {
          const stats = overviewStats(dataset.key, analytics);
          return (
            <button
              type="button"
              className="dba-overview-card"
              data-dataset-key={dataset.routeKey}
              style={{ "--dba-card-accent": dataset.accent, "--dba-card-soft": dataset.soft } as React.CSSProperties}
              key={dataset.key}
              onClick={() => onOpenDataset(dataset.key)}
            >
              <span className="dba-overview-card-top"><span>{datasetIcon(dataset.key)}</span><em className={isDatasetReady(dataset) ? "is-ready" : ""}><i />{isDatasetReady(dataset) ? "可用" : "待就绪"}</em></span>
              <span className="dba-overview-card-heading"><strong>{dataset.title}</strong><small>{dataset.subtitle}</small></span>
              <span className="dba-overview-card-description">{dataset.description}</span>
              <span className="dba-overview-card-stats">
                <span><small>记录量</small><strong>{formatNumber(dataset.recordCount, 0)}</strong></span>
                {stats.map((stat) => <span key={stat.label}><small>{stat.label}</small><strong>{stat.value}</strong></span>)}
              </span>
            </button>
          );
        })}
      </div>
    </>
  );
}

function overviewStats(key: DatasetKey, analytics: DatabaseAnalyticsPayload | null) {
  if (key === "process") return [
    { label: "聚合物实体", value: formatNumber(analytics?.process?.uniquePolymers, 0) },
    { label: "产品名称", value: formatNumber(analytics?.process?.uniqueProducts, 0) }
  ];
  if (key === "property") return [
    { label: "标准属性", value: formatNumber(analytics?.property?.uniqueProperties, 0) },
    { label: "聚合物实体", value: formatNumber(analytics?.property?.uniquePolymers, 0) }
  ];
  if (key === "structureEffect") return [
    { label: "有效结构", value: formatNumber(analytics?.structureEffect?.uniqueSmiles, 0) },
    { label: "最高频属性", value: formatNumber(analytics?.structureEffect?.properties?.[0]?.value, 0) }
  ];
  if (key === "dft") return [
    { label: "分子构象", value: formatNumber(analytics?.dft?.molCount, 0) },
    { label: "中位步数", value: formatNumber(analytics?.dft?.stepRange?.median, 0) }
  ];
  return [
    { label: "文档来源", value: formatNumber(analytics?.formulation?.files, 0) },
    { label: "平均组分", value: formatNumber(averageComponentCount(analytics?.formulation?.componentCounts ?? []), 1) }
  ];
}

function ProcessView({ data, recordCount, onOpenRecords }: { data: ProcessAnalytics; recordCount: number | null; onOpenRecords: (request: DrawerRequest, trigger?: HTMLElement) => void }) {
  const drill = (item: RankedItem, trigger: HTMLElement) => onOpenRecords({ dataset: "process", context: `${item.label} · 相关过程记录`, query: item.label }, trigger);
  return (
    <>
      <KpiStrip items={[
        { label: "过程记录", value: formatNumber(recordCount ?? data.rows, 0), unit: "条", note: `${formatNumber(data.uniqueRecordIds, 0)} 个独立来源记录` },
        { label: "聚合物实体", value: formatNumber(data.uniquePolymers, 0), unit: "个", note: "按名称去重统计" },
        { label: "产品名称", value: formatNumber(data.uniqueProducts, 0), unit: "个", note: "实验产物与材料名称" },
        { label: "过程文本中位数", value: formatNumber(data.processSignalSummary?.medianChars, 0), unit: "字符", note: `平均 ${formatNumber(data.avgProcessTextLength, 1)} 字符` }
      ]} />
      <div className="dba-dashboard-grid">
        <Panel title="过程关键词" subtitle="实验记录中的高频过程语义" meta="高频词"><BarList data={data.topTerms} onSelect={drill} /></Panel>
        <Panel title="材料实体" subtitle="从原始材料描述中提取的高频实体" meta={`${formatNumber(data.topMaterials.length, 0)} 个实体`}><ChipCloud data={data.topMaterials} onSelect={drill} /></Panel>
        <Panel title="产品排行" subtitle="按产品名称统计实验记录频次" meta="高频产品"><BarList data={data.topProducts} onSelect={drill} /></Panel>
        <Panel title="过程条件" subtitle="温度、时间、溶剂及操作信号覆盖" meta={`${formatNumber(data.processSignalSummary?.uniqueSnippets, 0)} 条记录`}><SignalCoverage data={data.processSignals} onSelect={drill} /></Panel>
      </div>
    </>
  );
}

function SignalCoverage({ data, onSelect }: { data: ProcessAnalytics["processSignals"]; onSelect: (item: RankedItem, trigger: HTMLElement) => void }) {
  if (!data.length) return <EmptyPanel />;
  return <div className="dba-coverage-list">{data.slice(0, 7).map((item) => {
    const pct = item.total ? (item.value / item.total) * 100 : 0;
    return <button className="dba-coverage-row" type="button" key={item.label} onClick={(event) => onSelect(item, event.currentTarget)}><span>{item.label}</span><span className="dba-coverage-track"><span style={{ width: `${Math.max(2, pct)}%` }} /></span><strong>{formatNumber(item.value, 0)}</strong></button>;
  })}</div>;
}

function PropertyView({ data, recordCount, onOpenRecords }: { data: PropertyAnalytics; recordCount: number | null; onOpenRecords: (request: DrawerRequest, trigger?: HTMLElement) => void }) {
  const drill = (item: RankedItem, trigger: HTMLElement) => onOpenRecords({ dataset: "property", context: `${item.label} · 相关性能记录`, query: item.label.includes(":") ? item.label.split(":").at(-1)?.trim() : item.label }, trigger);
  const numericSamples = data.ranges.reduce((sum, item) => sum + item.count, 0);
  return (
    <>
      <KpiStrip items={[
        { label: "性能记录", value: formatNumber(recordCount ?? data.rows, 0), unit: "条", note: "实验性能原始记录" },
        { label: "标准属性", value: formatNumber(data.uniqueProperties, 0), unit: "种", note: "按英文属性名称去重" },
        { label: "聚合物实体", value: formatNumber(data.uniquePolymers, 0), unit: "个", note: "关联实验聚合物" },
        { label: "数值样本", value: formatNumber(numericSamples, 0), unit: "条", note: "当前高频属性范围样本" }
      ]} />
      <div className="dba-dashboard-grid">
        <Panel title="性能类别" subtitle="记录数量的类别占比" meta={`${formatNumber(data.categories.length, 0)} 类`}><DonutBlock data={data.categories} /></Panel>
        <Panel title="属性排行" subtitle="按标准属性名称统计记录频次" meta="高频属性"><BarList data={data.topProperties} onSelect={drill} /></Panel>
        <Panel title="数值范围" subtitle="数值记录的 P5—P95 区间" meta="P5—P95"><RangeList data={data.ranges} /></Panel>
        <Panel title="代表属性" subtitle="每个性能类别中的最高频属性" meta="代表项"><BarList data={data.categoryTop} onSelect={drill} /></Panel>
      </div>
    </>
  );
}

function StructureEffectView({ data, recordCount, onOpenRecords }: { data: StructureEffectAnalytics; recordCount: number | null; onOpenRecords: (request: DrawerRequest, trigger?: HTMLElement) => void }) {
  const drill = (item: RankedItem, trigger: HTMLElement) => onOpenRecords({ dataset: "structureEffect", context: `${item.label} · 结构–性能记录`, query: item.label }, trigger);
  return (
    <>
      <KpiStrip items={[
        { label: "结构–性能记录", value: formatNumber(recordCount ?? data.rows, 0), unit: "条", note: "聚合物属性关联记录" },
        { label: "有效结构", value: formatNumber(data.uniqueSmiles, 0), unit: "个", note: "按聚合物结构去重" },
        { label: "高频属性", value: formatNumber(data.properties.length, 0), unit: "种", note: "当前统计范围" },
        { label: "数据来源", value: formatNumber(data.sources.length, 0), unit: "类", note: "实验、模拟与未标注" }
      ]} />
      <div className="dba-dashboard-grid">
        <Panel title="来源 × 属性矩阵" subtitle="高频属性在实验与模拟来源中的记录量" meta="记录数"><SourceMatrix data={data.sourceMatrix} /></Panel>
        <Panel title="单位分布" subtitle="原始属性单位的使用频次" meta="高频单位"><DonutBlock data={data.units} /></Panel>
        <Panel title="属性数量" subtitle="结构–性能记录中的高频属性" meta="高频属性"><BarList data={data.properties} onSelect={drill} /></Panel>
        <Panel title="典型属性范围" subtitle="数值记录的最小值、中位数和最大值" meta="数值范围"><RangeList data={data.ranges} /></Panel>
      </div>
    </>
  );
}

function FormulationView({ data, recordCount, onOpenRecords }: { data: FormulationAnalytics; recordCount: number | null; onOpenRecords: (request: DrawerRequest, trigger?: HTMLElement) => void }) {
  const drill = (item: RankedItem, trigger: HTMLElement, context = "配方记录") => onOpenRecords({ dataset: "formulation", context: `${item.label} · ${context}`, query: item.label }, trigger);
  const average = averageComponentCount(data.componentCounts);
  return (
    <>
      <KpiStrip items={[
        { label: "配方记录", value: formatNumber(recordCount ?? data.rows, 0), unit: "条", note: `${formatNumber(data.files, 0)} 个文档来源` },
        { label: "高频组分", value: formatNumber(data.topComponents.length, 0), unit: "类", note: "当前统计范围" },
        { label: "聚合物家族", value: formatNumber(data.polymerFamilies.length, 0), unit: "类", note: "按聚合物名称归类" },
        { label: "平均组分数", value: formatNumber(average, 1), unit: "个", note: "按已识别的配方组分估算" }
      ]} />
      <div className="dba-dashboard-grid">
        <Panel title="字段覆盖率" subtitle="配方记录中的关键字段完整度" meta={`${formatNumber(data.rows, 0)} 条记录`}><CoverageList data={data.coverage} /></Panel>
        <Panel title="组分数量" subtitle="每条配方的可识别组分数分布" meta="组分数"><BarList data={data.componentCounts} /></Panel>
        <Panel title="聚合物家族" subtitle="按配方记录数统计" meta="家族分布"><DonutBlock data={data.polymerFamilies} centerLabel="配方" /></Panel>
        <Panel title="比例、温度与时间" subtitle="配方表达与工艺条件分布" meta="工艺条件"><DistributionGroups groups={[{ label: "比例表达", data: data.ratioTypes }, { label: "温度区间", data: data.tempBands }, { label: "时间单位", data: data.timeUnits }]} /></Panel>
        <Panel title="高频催化剂与溶剂" subtitle="标准名称及关联配方数" meta="高频实体" className="dba-formulation-entity-panel"><div className="dba-entity-groups"><h4>催化剂</h4><ChipCloud data={data.topCatalysts} limit={6} onSelect={(item, trigger) => drill(item, trigger, "催化剂相关配方")} /><h4>溶剂</h4><ChipCloud data={data.topSolvents} limit={6} onSelect={(item, trigger) => drill(item, trigger, "溶剂相关配方")} /></div></Panel>
        <Panel title="代表配方" subtitle="数据源中的配方与工艺示例" meta={`${formatNumber(data.examples.length, 0)} 个示例`} className="dba-formulation-table-panel">
          <DataTable caption="代表配方" headers={["体系", "聚合物", "配方", "条件"]} rows={data.examples.map((item) => [item.title, item.polymer, item.formula, item.condition])} />
        </Panel>
      </div>
    </>
  );
}

function WorkbenchState({
  title,
  message,
  actionLabel,
  onAction
}: {
  title: string;
  message: string;
  actionLabel?: string;
  onAction?: () => void;
}) {
  return <div className="dba-workbench-state"><div><Database aria-hidden="true" /></div><h2>{title}</h2><p>{message}</p>{actionLabel && onAction ? <button type="button" onClick={onAction}>{actionLabel}</button> : null}</div>;
}

function AnalysisSkeleton() {
  return <div className="dba-analysis-skeleton" role="status" aria-label="正在加载数据库分析"><div className="dba-skeleton-strip">{[0, 1, 2, 3].map((item) => <span key={item}><i /><b /></span>)}</div><div className="dba-skeleton-grid">{[0, 1, 2, 3].map((item) => <span key={item} />)}</div></div>;
}

function RefreshOverlay() {
  return <div className="dba-refresh-overlay" role="status"><span className="dba-sr-only">正在更新分析数据，旧数据仍保留在背景中</span><div className="dba-skeleton-strip">{[0, 1, 2, 3].map((item) => <span key={item}><i /><b /></span>)}</div><div className="dba-skeleton-grid">{[0, 1, 2, 3].map((item) => <span key={item} />)}</div></div>;
}
