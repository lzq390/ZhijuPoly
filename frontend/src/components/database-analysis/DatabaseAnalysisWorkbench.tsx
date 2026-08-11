import {
  AlertTriangle,
  Atom,
  Check,
  ChevronDown,
  Clock3,
  Database,
  FlaskConical,
  Grid2X2,
  Layers3,
  Network,
  RefreshCw,
  Sigma,
  TableProperties
} from "lucide-react";
import type { ReactNode } from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { fetchDatabaseAnalytics, fetchDatabaseDatasetSummary } from "../../services/api";
import type { DatasetSummaryResponse } from "../../types";
import "../../styles/database-analysis.css";
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
import { DatabaseRecordDrawer } from "./DatabaseRecordDrawer";
import { DftAnalysisView } from "./DftAnalysisView";
import { databaseAnalysisErrorMessage } from "./errors";
import type {
  AnalysisViewKey,
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
import { isDatasetReady, toDisplayDataset } from "./types";

const DATASETS: DatasetDefinition[] = [
  {
    key: "process",
    routeKey: "process",
    title: "实验过程数据",
    subtitle: "EXPERIMENTAL PROCESS DATA",
    description: "过程关键词、材料实体、产品名称与反应条件",
    accent: "#3b82f6",
    soft: "#eef5ff"
  },
  {
    key: "property",
    routeKey: "property",
    title: "实验性能数据",
    subtitle: "EXPERIMENTAL PROPERTY DATA",
    description: "性能类别、属性排行、数值范围与代表属性",
    accent: "#06a7c5",
    soft: "#ecfbfd"
  },
  {
    key: "structureEffect",
    routeKey: "structure-effect",
    title: "结构–性能数据",
    subtitle: "STRUCTURE–PROPERTY DATA",
    description: "数据来源、单位分布与结构–性能关联",
    accent: "#8b5cf6",
    soft: "#f5f1ff"
  },
  {
    key: "dft",
    routeKey: "dft",
    title: "DFT 构象数据",
    subtitle: "DFT CONFORMATION DATA",
    description: "PCA、真实三维构象、能量轨迹与优化步骤",
    accent: "#4f46e5",
    soft: "#f0f0ff"
  },
  {
    key: "formulation",
    routeKey: "formulation",
    title: "配方比例数据",
    subtitle: "FORMULATION RATIO DATA",
    description: "组分、比例、聚合物家族与工艺覆盖",
    accent: "#0f9f8f",
    soft: "#edf9f6"
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

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    fetchDatabaseDatasetSummary(controller.signal)
      .then(setSummary)
      .catch((nextError) => {
        if (!controller.signal.aborted) setError(databaseAnalysisErrorMessage(nextError, "数据源状态加载失败"));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, []);

  return { summary, loading, error };
}

function useDatabaseAnalytics() {
  const [state, setState] = useState<AnalyticsState>({
    analytics: null,
    loading: true,
    refreshing: false,
    error: null,
    source: null,
    generatedAt: null
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
      setState({
        analytics: response.datasets as DatabaseAnalyticsPayload,
        loading: false,
        refreshing: false,
        error: null,
        source: response.source,
        generatedAt: response.generated_at ?? new Date().toISOString()
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

  return { ...state, refresh: () => load(true) };
}

export function DatabaseAnalysis(props: DatabaseAnalysisProps) {
  const summaryState = useDatasetSummary();
  const analyticsState = useDatabaseAnalytics();
  const [datasetPopoverOpen, setDatasetPopoverOpen] = useState(false);
  const [transientMessage, setTransientMessage] = useState<string | null>(null);
  const [drawerRequest, setDrawerRequest] = useState<DrawerRequest | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [drawerSeen, setDrawerSeen] = useState(false);
  const datasetButtonRef = useRef<HTMLButtonElement | null>(null);
  const datasetPopoverRef = useRef<HTMLElement | null>(null);
  const lastDrawerTriggerRef = useRef<HTMLElement | null>(null);
  const messageTimerRef = useRef<number | null>(null);

  const displayDatasets = useMemo(() => {
    const byKey = new Map(summaryState.summary?.datasets.map((item) => [item.key, item]) ?? []);
    return DATASETS.map((definition) =>
      toDisplayDataset(definition, byKey.get(definition.key), summaryState.loading, summaryState.error)
    );
  }, [summaryState.error, summaryState.loading, summaryState.summary]);

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
        value: "实时重算",
        description: "本次结果由刷新操作按当前数据库实时重新计算。"
      }
    : analyticsState.source === "snapshot"
      ? {
          value: "预计算统计",
          description: "读取后端提前生成并保存的数据库聚合统计；记录仍来自真实数据库。"
        }
      : {
          value: currentDataset?.dataSource === "postgres" ? "数据库统计" : "统计数据",
          description: "当前页面展示数据库聚合统计结果。"
        };

  useEffect(() => {
    setDatasetPopoverOpen(false);
  }, [props.selectedKey]);

  useEffect(() => {
    if (!datasetPopoverOpen) return;
    function handlePointerDown(event: PointerEvent) {
      const target = event.target as Node;
      if (!datasetPopoverRef.current?.contains(target) && !datasetButtonRef.current?.contains(target)) {
        setDatasetPopoverOpen(false);
      }
    }
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key !== "Escape") return;
      event.preventDefault();
      setDatasetPopoverOpen(false);
      datasetButtonRef.current?.focus();
    }
    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    requestAnimationFrame(() => datasetPopoverRef.current?.querySelector<HTMLButtonElement>("button:not(:disabled)")?.focus());
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [datasetPopoverOpen]);

  useEffect(() => () => {
    if (messageTimerRef.current !== null) window.clearTimeout(messageTimerRef.current);
  }, []);

  async function handleRefresh() {
    setTransientMessage(null);
    const outcome = await analyticsState.refresh();
    if (!outcome) return;
    setTransientMessage(outcome === "unchanged"
      ? "数据表未发生变化，无需重新计算；已保留当前统计结果。"
      : "已按当前数据库完成实时重算，当前数据集和浏览位置保持不变。");
    if (messageTimerRef.current !== null) window.clearTimeout(messageTimerRef.current);
    messageTimerRef.current = window.setTimeout(() => setTransientMessage(null), 2600);
  }

  function selectDataset(key: AnalysisViewKey) {
    setDatasetPopoverOpen(false);
    if (key === "overview") props.onBackDatabase();
    else props.onOpenDataset(key);
    requestAnimationFrame(() => datasetButtonRef.current?.focus());
  }

  function openRecords(request: DrawerRequest, trigger?: HTMLElement) {
    if (trigger) lastDrawerTriggerRef.current = trigger;
    setDrawerRequest(request);
    setDrawerOpen(true);
    setDrawerSeen(true);
  }

  function closeRecords() {
    setDrawerOpen(false);
    window.setTimeout(() => lastDrawerTriggerRef.current?.focus(), 0);
  }

  const banner = analyticsState.refreshing
    ? {
        tone: "info",
        message: "正在检查数据表变更；检测到更新后才会重新计算，期间保留当前结果。"
      }
    : analyticsState.error
      ? { tone: "error", message: analyticsState.analytics ? `统计更新失败，仍显示上次成功结果：${analyticsState.error}` : analyticsState.error }
      : summaryState.error
        ? { tone: "warning", message: `数据源状态暂不可用：${summaryState.error}` }
        : transientMessage
          ? { tone: "success", message: transientMessage }
          : null;

  return (
    <div className="database-analysis-page" data-view-key={currentView === "structureEffect" ? "structure-effect" : currentView}>
      <header className="dba-pagebar"><h1>数据库分析</h1></header>
      <div className="dba-workbench-column">
        <section className="dba-analysis-surface" aria-labelledby="dba-surface-title">
          <header className="dba-surface-head">
            <div className="dba-surface-identity">
              <span className="dba-surface-icon">{datasetIcon(currentView)}</span>
              <div className="dba-surface-heading">
                <h2 id="dba-surface-title">{currentDataset?.title ?? "全库概览"}</h2>
                <p>{currentDataset?.subtitle ?? "POLYMER DATABASE OVERVIEW"}</p>
              </div>
            </div>

            <div className="dba-surface-head-actions">
              <div className="dba-surface-meta">
                <MetaItem
                  label="统计方式"
                  value={analyticsMode.value}
                  status={currentDataset ? isDatasetReady(currentDataset) : readyCount > 0}
                  description={analyticsMode.description}
                />
                <MetaItem label="记录量" value={`${formatNumber(currentDataset?.recordCount ?? totalRecords, 0)} 条`} />
                <MetaItem label="更新时间" value={formatTimestamp(updatedAt)} icon={<Clock3 aria-hidden="true" />} />
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
                  onClick={() => setDatasetPopoverOpen((open) => !open)}
                >
                  <Layers3 aria-hidden="true" /><span>数据集</span><ChevronDown aria-hidden="true" />
                </button>
                {datasetPopoverOpen ? (
                  <DatasetPopover
                    ref={datasetPopoverRef}
                    currentView={currentView}
                    datasets={displayDatasets}
                    readyCount={readyCount}
                    onSelect={selectDataset}
                  />
                ) : null}
              </div>
            </div>
          </header>

          {banner ? (
            <div className={`dba-surface-banner ${banner.tone}`} role="status" aria-live="polite">
              <span>
                {banner.tone === "error" || banner.tone === "warning" ? <AlertTriangle aria-hidden="true" /> : <Check aria-hidden="true" />}
                {banner.message}
              </span>
              {banner.tone === "error" ? <button type="button" onClick={() => void handleRefresh()}>重试</button> : null}
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
                updatedAt={updatedAt}
                onOpenDataset={props.onOpenDataset}
                onOpenRecords={openRecords}
              />
            )}
            {analyticsState.refreshing ? <RefreshOverlay /> : null}
          </div>
        </section>
      </div>

      <DatabaseRecordDrawer open={drawerOpen} request={drawerRequest} onClose={closeRecords} />
      {drawerSeen && !drawerOpen && drawerRequest ? (
        <button className="dba-drawer-reopen" type="button" onClick={() => setDrawerOpen(true)}>重新打开记录</button>
      ) : null}
    </div>
  );
}

function MetaItem({
  label,
  value,
  status,
  icon,
  description
}: {
  label: string;
  value: string;
  status?: boolean;
  icon?: ReactNode;
  description?: string;
}) {
  return (
    <div className={`dba-meta-item ${description ? "has-description" : ""}`} title={description}>
      <span>{label}</span>
      <strong>{status !== undefined ? <i className={status ? "is-ready" : ""} /> : null}{icon}{value}</strong>
    </div>
  );
}

const DatasetPopover = function DatasetPopover({
  ref,
  currentView,
  datasets,
  readyCount,
  onSelect
}: {
  ref: React.Ref<HTMLElement>;
  currentView: AnalysisViewKey;
  datasets: DisplayDataset[];
  readyCount: number;
  onSelect: (key: AnalysisViewKey) => void;
}) {
  return (
    <section ref={ref} className="dba-dataset-popover" id="dba-dataset-popover" aria-label="选择数据集">
      <header>
        <div><h3>切换分析数据集</h3><p>选择后进入对应的完整分析工作面</p></div>
        <span><i />{readyCount} 个可用</span>
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
  updatedAt,
  onOpenDataset,
  onOpenRecords
}: {
  view: AnalysisViewKey;
  datasets: DisplayDataset[];
  analytics: DatabaseAnalyticsPayload | null;
  analyticsError: string | null;
  updatedAt: string | null;
  onOpenDataset: (key: DatasetKey) => void;
  onOpenRecords: (request: DrawerRequest, trigger?: HTMLElement) => void;
}) {
  if (view === "overview") {
    return <Overview datasets={datasets} analytics={analytics} updatedAt={updatedAt} onOpenDataset={onOpenDataset} />;
  }
  const dataset = datasets.find((item) => item.key === view);
  if (!dataset) return <WorkbenchState title="未知数据集" message="当前深链没有对应的数据集定义。" />;
  if (!isDatasetReady(dataset)) {
    return <WorkbenchState title="该数据源尚未就绪" message={dataset.sourceMessage ?? "分析入口已保留，数据准备完成后会在当前工作面展示。"} />;
  }
  const data = analytics?.[view];
  if (!data) {
    return <WorkbenchState title={analyticsError ? "分析数据加载失败" : "暂无可展示的分析快照"} message={analyticsError ?? "数据源已经连接，但统计快照尚未返回当前数据集。"} />;
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
        { label: "分析数据集", value: String(datasets.length), unit: "个", note: "五类真实统计工作面" },
        { label: "可用数据源", value: `${readyCount} / ${datasets.length}`, note: readyCount === datasets.length ? "全部数据源可用" : "部分数据源待就绪" },
        { label: "总记录量", value: formatNumber(totalRecords, 0), unit: "条", note: "按五类数据集汇总" },
        { label: "最近同步", value: time, note: `${date} · 统计结果` }
      ]} />
      <div className="dba-section-row"><h3>数据集概览</h3><span>{readyCount} 个真实数据源</span></div>
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
        { label: "聚合物实体", value: formatNumber(data.uniquePolymers, 0), unit: "个", note: "真实名称去重统计" },
        { label: "产品名称", value: formatNumber(data.uniqueProducts, 0), unit: "个", note: "实验产物与材料名称" },
        { label: "过程文本中位数", value: formatNumber(data.processSignalSummary?.medianChars, 0), unit: "字符", note: `平均 ${formatNumber(data.avgProcessTextLength, 1)} 字符` }
      ]} />
      <div className="dba-dashboard-grid">
        <Panel title="过程关键词" subtitle="实验记录中的高频过程语义" meta="Top terms"><BarList data={data.topTerms} onSelect={drill} /></Panel>
        <Panel title="材料实体" subtitle="从原始材料描述中提取的高频实体" meta={`${formatNumber(data.topMaterials.length, 0)} entities`}><ChipCloud data={data.topMaterials} onSelect={drill} /></Panel>
        <Panel title="产品排行" subtitle="按产品名称统计实验记录频次" meta="Top products"><BarList data={data.topProducts} onSelect={drill} /></Panel>
        <Panel title="过程条件" subtitle="温度、时间、溶剂及操作信号覆盖" meta={`${formatNumber(data.processSignalSummary?.uniqueSnippets, 0)} records`}><SignalCoverage data={data.processSignals} onSelect={drill} /></Panel>
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
        <Panel title="性能类别" subtitle="记录数量的类别占比" meta={`${formatNumber(data.categories.length, 0)} categories`}><DonutBlock data={data.categories} /></Panel>
        <Panel title="属性排行" subtitle="归一化名称后的记录频次" meta="Top properties"><BarList data={data.topProperties} onSelect={drill} /></Panel>
        <Panel title="数值范围" subtitle="可解析数值的 P5—P95 区间" meta="P5—P95"><RangeList data={data.ranges} /></Panel>
        <Panel title="代表属性" subtitle="每个性能类别中的最高频属性" meta="Representative"><BarList data={data.categoryTop} onSelect={drill} /></Panel>
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
        { label: "高频属性", value: formatNumber(data.properties.length, 0), unit: "种", note: "当前快照展示范围" },
        { label: "数据来源", value: formatNumber(data.sources.length, 0), unit: "类", note: "实验、模拟与未标注" }
      ]} />
      <div className="dba-dashboard-grid">
        <Panel title="来源 × 属性矩阵" subtitle="高频属性在实验与模拟来源中的记录量" meta="records"><SourceMatrix data={data.sourceMatrix} /></Panel>
        <Panel title="单位分布" subtitle="原始属性单位的使用频次" meta="Top units"><DonutBlock data={data.units} /></Panel>
        <Panel title="属性数量" subtitle="结构–性能记录中的高频属性" meta="Top properties"><BarList data={data.properties} onSelect={drill} /></Panel>
        <Panel title="典型属性范围" subtitle="真实数值记录的最小值、中位数和最大值" meta="Normalized"><RangeList data={data.ranges} /></Panel>
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
        { label: "高频组分", value: formatNumber(data.topComponents.length, 0), unit: "类", note: "当前快照展示范围" },
        { label: "聚合物家族", value: formatNumber(data.polymerFamilies.length, 0), unit: "类", note: "按聚合物名称归类" },
        { label: "平均组分数", value: formatNumber(average, 1), unit: "个", note: "按可解析配方估算" }
      ]} />
      <div className="dba-dashboard-grid">
        <Panel title="字段覆盖率" subtitle="配方记录中的关键字段完整度" meta={`${formatNumber(data.rows, 0)} records`}><CoverageList data={data.coverage} /></Panel>
        <Panel title="组分数量" subtitle="每条配方的可解析组分数分布" meta="components"><BarList data={data.componentCounts} /></Panel>
        <Panel title="聚合物家族" subtitle="按配方记录数统计" meta="families"><DonutBlock data={data.polymerFamilies} centerLabel="配方" /></Panel>
        <Panel title="比例 / 温度 / 时间" subtitle="配方表达与工艺条件分布" meta="process"><DistributionGroups groups={[{ label: "比例表达", data: data.ratioTypes }, { label: "温度区间", data: data.tempBands }, { label: "时间单位", data: data.timeUnits }]} /></Panel>
        <Panel title="高频催化剂与溶剂" subtitle="标准名称及关联配方数" meta="Top entities"><div className="dba-entity-groups"><h4>催化剂</h4><ChipCloud data={data.topCatalysts} limit={6} onSelect={(item, trigger) => drill(item, trigger, "催化剂相关配方")} /><h4>溶剂</h4><ChipCloud data={data.topSolvents} limit={6} onSelect={(item, trigger) => drill(item, trigger, "溶剂相关配方")} /></div></Panel>
        <Panel title="代表配方" subtitle="真实数据库中的配方与工艺示例" meta={`${formatNumber(data.examples.length, 0)} examples`}>
          <DataTable caption="代表配方" headers={["体系", "聚合物", "配方", "条件"]} rows={data.examples.map((item) => [item.title, item.polymer, item.formula, item.condition])} />
        </Panel>
      </div>
    </>
  );
}

function WorkbenchState({ title, message }: { title: string; message: string }) {
  return <div className="dba-workbench-state"><div><Database aria-hidden="true" /></div><h2>{title}</h2><p>{message}</p></div>;
}

function AnalysisSkeleton() {
  return <div className="dba-analysis-skeleton" role="status" aria-label="正在加载数据库分析"><div className="dba-skeleton-strip">{[0, 1, 2, 3].map((item) => <span key={item}><i /><b /></span>)}</div><div className="dba-skeleton-grid">{[0, 1, 2, 3].map((item) => <span key={item} />)}</div></div>;
}

function RefreshOverlay() {
  return <div className="dba-refresh-overlay" role="status"><span className="dba-sr-only">正在更新分析数据，旧数据仍保留在背景中</span><div className="dba-skeleton-strip">{[0, 1, 2, 3].map((item) => <span key={item}><i /><b /></span>)}</div><div className="dba-skeleton-grid">{[0, 1, 2, 3].map((item) => <span key={item} />)}</div></div>;
}
