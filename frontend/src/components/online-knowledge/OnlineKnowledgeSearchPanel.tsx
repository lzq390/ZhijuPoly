import {
  AlertTriangle,
  BarChart3,
  BookMarked,
  Check,
  Clock3,
  Download,
  FileClock,
  FlaskConical,
  Globe2,
  History,
  KeyRound,
  LoaderCircle,
  RefreshCw,
  Search,
  SearchX,
  Trash2
} from "lucide-react";
import { type CSSProperties, type FormEvent, type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import {
  ONLINE_KNOWLEDGE_DEFAULT_BASE_URL,
  ONLINE_KNOWLEDGE_DEFAULT_MAX_PAPERS,
  ONLINE_KNOWLEDGE_DEFAULT_MODEL
} from "../../constants/onlineKnowledgeDefaults";
import { useOnlineKnowledgeSearch } from "../../hooks/useOnlineKnowledgeSearch";
import { exportOnlineKnowledgeCsv, fetchOnlineKnowledgeDefaultConfig } from "../../services/api";
import type {
  OnlineKnowledgeCountItem,
  OnlineKnowledgeHistoryItem,
  OnlineKnowledgeMode,
  OnlineKnowledgePropertyPoint,
  OnlineKnowledgeSearchRequest,
  OnlineKnowledgeSearchResponse,
  OnlineKnowledgeSynthesis
} from "../../types";
import { KnowledgeDetailDrawer, type KnowledgeDrawerTab } from "../knowledge-search/KnowledgeDetailDrawer";

type OnlineKnowledgeSearchPanelProps = {
  initialMaterial?: string;
  modeNavigation: ReactNode;
};

type DrawerView = "detail" | "history";

const JOB_STAGES = ["searching", "deduplicating", "enriching", "fallback", "extracting", "finalizing", "completed"];

function optional(value: string | number | null | undefined) {
  if (value === null || value === undefined || value === "") return "—";
  return String(value);
}

function isDesktopViewport() {
  return typeof window === "undefined" || typeof window.matchMedia !== "function"
    ? true
    : !window.matchMedia("(max-width: 899px)").matches;
}

function DetailGrid({ fields }: { fields: Array<{ label: string; value: string | number | null | undefined }> }) {
  return (
    <dl className="ks-detail-grid">
      {fields.map((field) => (
        <div key={field.label}><dt>{field.label}</dt><dd>{optional(field.value)}</dd></div>
      ))}
    </dl>
  );
}

function provenanceNotice(message: string) {
  return (
    <div className="ks-drawer-callout is-warning">
      <AlertTriangle aria-hidden="true" />
      <span>{message}</span>
    </div>
  );
}

function buildPropertyTabs(item: OnlineKnowledgePropertyPoint, data: OnlineKnowledgeSearchResponse): KnowledgeDrawerTab[] {
  return [
    {
      id: "overview",
      label: "结果概览",
      content: (
        <div className="ks-drawer-stack">
          <section className="ks-drawer-section">
            <p className="ks-drawer-title-main">{item.property_name} · {item.property_value}</p>
            <p className="ks-drawer-title-secondary">{item.polymer_name}</p>
            <div className="ks-chip-row"><span className="ks-relation-chip">{item.relationship}</span><span className="ks-chip">{item.polymer_type}</span></div>
          </section>
          <section className="ks-drawer-section">
            <h3><BookMarked aria-hidden="true" />抽取字段</h3>
            <DetailGrid fields={[
              { label: "聚合物类型", value: item.polymer_type },
              { label: "聚合物名称", value: item.polymer_name },
              { label: "性质名称", value: item.property_name },
              { label: "性质值", value: item.property_value },
              { label: "关系类型", value: item.relationship }
            ]} />
          </section>
        </div>
      )
    },
    {
      id: "fields",
      label: "条件与字段",
      content: (
        <div className="ks-drawer-stack">
          <section className="ks-drawer-section">
            <h3><FlaskConical aria-hidden="true" />条件信息</h3>
            <DetailGrid fields={[
              { label: "条件名称", value: item.condition_name },
              { label: "条件值", value: item.condition_value },
              { label: "性质名称", value: item.property_name },
              { label: "性质值", value: item.property_value }
            ]} />
          </section>
          <section className="ks-drawer-section">{provenanceNotice("在线结果为模型从可用摘要中抽取的结构化关系，应结合具体论文原文核验。")}</section>
        </div>
      )
    },
    {
      id: "source",
      label: "论文溯源",
      content: (
        <div className="ks-drawer-stack">
          <section className="ks-drawer-section"><h3><BookMarked aria-hidden="true" />论文题名</h3><p className="ks-drawer-title-main">{optional(item.paper_title)}</p></section>
          <section className="ks-drawer-section"><DetailGrid fields={[
            { label: "检索材料", value: data.material },
            { label: "抽取模式", value: "property" },
            { label: "论文来源", value: "聚合来源去重结果" },
            { label: "DOI / URL", value: "当前接口未返回" }
          ]} /></section>
          <section className="ks-drawer-section">{provenanceNotice("当前返回类型包含论文题名，但不包含 DOI、作者和原文 URL；页面明确保留这一溯源边界。")}</section>
        </div>
      )
    }
  ];
}

function buildSynthesisTabs(item: OnlineKnowledgeSynthesis, data: OnlineKnowledgeSearchResponse): KnowledgeDrawerTab[] {
  return [
    {
      id: "overview",
      label: "结果概览",
      content: (
        <div className="ks-drawer-stack">
          <section className="ks-drawer-section">
            <p className="ks-drawer-title-main">{item.method || "未命名合成方法"}</p>
            <p className="ks-drawer-title-secondary">{item.product_name} {item.product_abbreviation ? `· ${item.product_abbreviation}` : ""}</p>
            <div className="ks-chip-row"><span className="ks-chip is-match">{item.reaction_type}</span></div>
          </section>
          <section className="ks-drawer-section">
            <h3><FlaskConical aria-hidden="true" />合成字段</h3>
            <DetailGrid fields={[
              { label: "产物", value: item.product_name },
              { label: "反应类型", value: item.reaction_type },
              { label: "反应物", value: item.reactants },
              { label: "催化剂", value: item.catalyst },
              { label: "性质", value: item.properties }
            ]} />
          </section>
        </div>
      )
    },
    {
      id: "reaction",
      label: "条件与字段",
      content: <section className="ks-drawer-section"><h3><FlaskConical aria-hidden="true" />反应条件</h3><DetailGrid fields={[
        { label: "温度", value: item.temperature },
        { label: "时间", value: item.time },
        { label: "溶剂", value: item.solvent },
        { label: "气氛", value: item.atmosphere },
        { label: "压力", value: item.pressure },
        { label: "引发剂", value: item.initiator }
      ]} /></section>
    },
    {
      id: "source",
      label: "论文溯源",
      content: <div className="ks-drawer-stack"><section className="ks-drawer-section">{provenanceNotice("当前 OnlineKnowledgeSynthesis 接口类型不包含论文题名、DOI 或 URL；页面不会虚构论文溯源信息。")}</section><section className="ks-drawer-section"><DetailGrid fields={[
        { label: "检索材料", value: data.material },
        { label: "抽取模式", value: "synthesis" },
        { label: "检索论文", value: `${data.totalPapers} 篇` },
        { label: "具体论文定位", value: "当前接口未返回" }
      ]} /></section></div>
    }
  ];
}

function Metric({ icon, label, value }: { icon: ReactNode; label: string; value: string }) {
  return (
    <div className="ks-metric">
      <span>{icon}{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function DistributionPanel({ title, items }: { title: string; items: OnlineKnowledgeCountItem[] }) {
  const visible = items.slice(0, 5);
  const maxCount = Math.max(1, ...visible.map((item) => item.count));
  return (
    <section className="ks-distribution">
      <header><strong>{title}</strong><span>百分比基于该字段的非空记录</span></header>
      {visible.length ? (
        <div className="ks-bar-list">
          {visible.map((item) => (
            <div className="ks-bar-row" key={item.label}>
              <span title={item.label}>{item.label}</span>
              <i><b style={{ width: `${Math.max(5, (item.count / maxCount) * 100)}%` }} /></i>
              <strong>{item.count}</strong>
            </div>
          ))}
        </div>
      ) : <p className="ks-distribution-empty">暂无可统计字段。</p>}
    </section>
  );
}

function OnlineProgress({ state }: { state: ReturnType<typeof useOnlineKnowledgeSearch> }) {
  const job = state.job;
  const progress = job && job.total_papers > 0
    ? Math.min(100, Math.round((job.processed_papers / job.total_papers) * 100))
    : 0;
  const stage = job?.progress_stage || state.jobStatus || "pending";
  const currentStageIndex = Math.max(0, JOB_STAGES.indexOf(stage));

  return (
    <div className="ks-progress-card" role="status">
      <div className="ks-progress-head">
        <div><strong>{job?.progress_message || "正在创建在线检索任务"}</strong><span>任务运行期间可切换到其他知识模式。</span></div>
        <b>{progress}%</b>
      </div>
      <div className="ks-progress-track"><span style={{ width: `${progress}%` }} /></div>
      <div className="ks-progress-stages">
        {["searching", "deduplicating", "extracting", "finalizing"].map((item) => {
          const itemIndex = JOB_STAGES.indexOf(item);
          return <span className={itemIndex < currentStageIndex ? "is-done" : item === stage ? "is-active" : ""} key={item}><i />{item}</span>;
        })}
      </div>
      <div className="ks-meta-row">
        <span>处理论文：{job?.processed_papers ?? 0} / {job?.total_papers || job?.max_papers || "—"}</span>
        <span>任务阶段：{stage}</span>
        <span>进入抽取阶段后，论文总数可能按有效摘要重新定基</span>
      </div>
    </div>
  );
}

export function OnlineKnowledgeSearchPanel({ initialMaterial = "", modeNavigation }: OnlineKnowledgeSearchPanelProps) {
  const searchState = useOnlineKnowledgeSearch();
  const [material, setMaterial] = useState(initialMaterial.trim());
  const [mode, setMode] = useState<OnlineKnowledgeMode>("property");
  const [maxPapers, setMaxPapers] = useState(ONLINE_KNOWLEDGE_DEFAULT_MAX_PAPERS);
  const [baseUrl, setBaseUrl] = useState(ONLINE_KNOWLEDGE_DEFAULT_BASE_URL);
  const [model, setModel] = useState(ONLINE_KNOWLEDGE_DEFAULT_MODEL);
  const [hasServerApiKey, setHasServerApiKey] = useState(false);
  const [configError, setConfigError] = useState<string | null>(null);
  const [csvError, setCsvError] = useState<string | null>(null);
  const [historyActionError, setHistoryActionError] = useState<string | null>(null);
  const [lastPayload, setLastPayload] = useState<OnlineKnowledgeSearchRequest | null>(null);
  const [selectedIndex, setSelectedIndex] = useState<number | null>(null);
  const [drawerView, setDrawerView] = useState<DrawerView>("detail");
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [drawerWidth, setDrawerWidth] = useState(380);
  const [confirmClearHistory, setConfirmClearHistory] = useState(false);
  const previousDataRef = useRef<OnlineKnowledgeSearchResponse | null>(null);

  const hasModelAccess = hasServerApiKey && Boolean(baseUrl.trim()) && Boolean(model.trim());
  const canSearch = Boolean(material.trim()) && hasModelAccess && maxPapers >= 1 && maxPapers <= 2000 && !searchState.isLoading;
  const data = searchState.data;
  const resultCount = data ? (data.mode === "property" ? data.propertyPoints.length : data.syntheses.length) : 0;
  const selectedProperty = data?.mode === "property" && selectedIndex !== null ? data.propertyPoints[selectedIndex] : null;
  const selectedSynthesis = data?.mode === "synthesis" && selectedIndex !== null ? data.syntheses[selectedIndex] : null;

  useEffect(() => {
    const value = initialMaterial.trim();
    if (value) setMaterial(value);
  }, [initialMaterial]);

  useEffect(() => {
    void loadDefaultConfig();
    void searchState.loadHistory();
    // First mount is the lazy-entry point for online configuration and shared history.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!data || data === previousDataRef.current) return;
    previousDataRef.current = data;
    const count = data.mode === "property" ? data.propertyPoints.length : data.syntheses.length;
    if (!count) {
      setSelectedIndex(null);
      setDrawerOpen(false);
      return;
    }
    setSelectedIndex(0);
    setDrawerView("detail");
    if (isDesktopViewport()) setDrawerOpen(true);
  }, [data]);

  async function loadDefaultConfig() {
    setConfigError(null);
    try {
      const config = await fetchOnlineKnowledgeDefaultConfig();
      setBaseUrl(config.base_url);
      setModel(config.model);
      setHasServerApiKey(config.has_server_api_key);
      if (!config.has_server_api_key) {
        setConfigError("服务端模型 API Key 尚未配置，在线检索暂不可运行。");
      }
    } catch (error) {
      setHasServerApiKey(false);
      setConfigError(error instanceof Error ? error.message : "无法读取在线检索配置");
    }
  }

  function buildPayload(nextMaterial: string, nextMode: OnlineKnowledgeMode, nextMaxPapers: number): OnlineKnowledgeSearchRequest {
    return {
      material: nextMaterial,
      api_key: null,
      base_url: baseUrl.trim(),
      model: model.trim(),
      mode: nextMode,
      max_papers: nextMaxPapers,
      extraction_delay_seconds: 0.5,
      use_server_default: true
    };
  }

  async function submitPayload(payload: OnlineKnowledgeSearchRequest) {
    if (!hasModelAccess || searchState.isLoading) return;
    setLastPayload(payload);
    setSelectedIndex(null);
    setDrawerOpen(false);
    setCsvError(null);
    await searchState.submit(payload);
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canSearch) return;
    await submitPayload(buildPayload(material.trim(), mode, maxPapers));
  }

  async function handleExportCsv() {
    if (!data?.dataframe.length) return;
    setCsvError(null);
    try {
      const response = await exportOnlineKnowledgeCsv(
        data.dataframe,
        `${data.material.replace(/\s+/g, "_")}_${data.mode}_results.csv`
      );
      const blob = new Blob([response.csv_content], { type: "text/csv;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = response.filename;
      link.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 0);
    } catch (error) {
      setCsvError(error instanceof Error ? error.message : "CSV 导出失败");
    }
  }

  function restoreHistory(item: OnlineKnowledgeHistoryItem) {
    setMaterial(item.material);
    setMode(item.mode);
    setMaxPapers(item.max_papers || ONLINE_KNOWLEDGE_DEFAULT_MAX_PAPERS);
    searchState.restoreFromHistory(item);
    setDrawerView("detail");
  }

  async function replayHistory(item: OnlineKnowledgeHistoryItem) {
    setMaterial(item.material);
    setMode(item.mode);
    setMaxPapers(item.max_papers || ONLINE_KNOWLEDGE_DEFAULT_MAX_PAPERS);
    setDrawerOpen(false);
    await submitPayload(buildPayload(item.material, item.mode, item.max_papers || ONLINE_KNOWLEDGE_DEFAULT_MAX_PAPERS));
  }

  async function deleteHistory(historyId: number) {
    setHistoryActionError(null);
    try {
      await searchState.deleteHistoryItem(historyId);
    } catch (error) {
      setHistoryActionError(error instanceof Error ? error.message : "删除历史失败");
    }
  }

  async function clearHistory() {
    setHistoryActionError(null);
    try {
      await searchState.clearHistory();
      setConfirmClearHistory(false);
    } catch (error) {
      setHistoryActionError(error instanceof Error ? error.message : "清空历史失败");
    }
  }

  const detailTabs = useMemo(() => {
    if (!data) return [];
    if (selectedProperty) return buildPropertyTabs(selectedProperty, data);
    if (selectedSynthesis) return buildSynthesisTabs(selectedSynthesis, data);
    return [];
  }, [data, selectedProperty, selectedSynthesis]);

  const drawerTitle = drawerView === "history"
    ? "在线检索历史"
    : selectedProperty
      ? "性质关系详情"
      : selectedSynthesis ? "合成记录详情" : "在线结果详情";
  const drawerSubtitle = drawerView === "history"
    ? `${searchState.history.length} 条已完成记录 · 当前服务实例共享`
    : data
      ? `${data.material} · 在线文献`
      : "选择结果后查看";

  return (
    <div className={`ks-panel-layout${drawerOpen ? " is-drawer-open" : ""}`} style={{ "--ks-drawer-width": `${drawerWidth}px` } as CSSProperties}>
      <div className="ks-panel-scroll">
        <div className="ks-workbench-column">
          <div className="ks-module-toolbar">
            <span className="ks-toolbar-status"><i />准备就绪</span>
          </div>

          <section className="ks-surface ks-search-surface">
            <header className="ks-surface-header">
              <div className="ks-surface-heading">
                <span className="ks-surface-mark"><Globe2 aria-hidden="true" /></span>
                <div className="ks-surface-copy"><h2>在线文献检索</h2><p>聚合论文摘要并抽取结构化合成或性质关系</p></div>
              </div>
              {modeNavigation}
            </header>

            <div className="ks-form-zone">
              <form className="ks-search-form ks-online-form" onSubmit={handleSubmit}>
                <label className="ks-field"><span>材料名称 <small>Material</small></span><span className="ks-input-with-icon"><Search aria-hidden="true" /><input value={material} onChange={(event) => setMaterial(event.target.value)} placeholder="例如 PLA、polyimide" aria-label="在线检索材料名称" autoComplete="off" /></span></label>
                <label className="ks-field"><span>抽取模式</span><select value={mode} onChange={(event) => setMode(event.target.value as OnlineKnowledgeMode)} aria-label="在线检索抽取模式"><option value="property">性质–条件关系</option><option value="synthesis">合成方法</option></select></label>
                <label className="ks-field"><span>论文上限</span><input type="number" min={1} max={2000} value={maxPapers} onChange={(event) => setMaxPapers(Number(event.target.value))} aria-label="在线检索论文上限" /></label>
                <button className="ks-button is-primary" type="submit" disabled={!canSearch}>{searchState.isLoading ? <LoaderCircle className="is-spinning" aria-hidden="true" /> : <Search aria-hidden="true" />}开始检索</button>
              </form>

              <div className="ks-meta-row">
                <span className={hasModelAccess ? "is-ready" : "is-warning"}><KeyRound aria-hidden="true" />{hasModelAccess ? "服务端模型配置可用" : "服务端模型配置不可用"}</span>
                <span><Globe2 aria-hidden="true" />Semantic Scholar / PubMed / OpenAlex / arXiv / Crossref</span>
                <span>异步任务 · 仅成功任务写入共享历史</span>
              </div>
              {configError ? <div className="ks-inline-alert is-error" role="alert"><AlertTriangle aria-hidden="true" /><span>{configError}</span><button type="button" onClick={() => void loadDefaultConfig()}>重新检查</button></div> : null}
            </div>
          </section>

          <section className="ks-surface ks-results-surface" aria-busy={searchState.isLoading}>
            <header className="ks-results-header">
              <div className="ks-results-summary"><h2>{data?.mode === "synthesis" ? "在线合成记录" : "在线性质关系"}</h2><p>{searchState.isLoading ? "异步任务运行中，切换知识模式后状态仍会保留" : data ? `${data.totalPapers} 篇处理论文 · ${resultCount} 条展示记录` : "外部文献聚合与模型结构化抽取"}</p></div>
              <div className="ks-results-actions">
                <button className="ks-button" type="button" onClick={() => { setDrawerView("history"); setDrawerOpen(true); }}><History aria-hidden="true" />检索历史 <b>{searchState.history.length}</b></button>
                <button className="ks-button" type="button" disabled={!data?.dataframe.length} onClick={() => void handleExportCsv()}><Download aria-hidden="true" />导出 CSV</button>
              </div>
            </header>

            <div className="ks-results-body">
              {searchState.isLoading ? <OnlineProgress state={searchState} /> : null}
              {!searchState.isLoading && searchState.error ? (
                <div className="ks-state is-error" role="alert"><span><AlertTriangle aria-hidden="true" /></span><h3>在线检索任务失败</h3><p>{searchState.error}</p>{lastPayload ? <button className="ks-button" type="button" onClick={() => void submitPayload(lastPayload)}><RefreshCw aria-hidden="true" />重试本次任务</button> : null}</div>
              ) : null}
              {!searchState.isLoading && !searchState.error && !data ? (
                <div className="ks-state"><span><Globe2 aria-hidden="true" /></span><h3>输入材料名称开始在线检索</h3><p>任务会聚合可用摘要，再按当前模式抽取结构化记录。</p></div>
              ) : null}
              {!searchState.isLoading && data && resultCount === 0 ? (
                <div className="ks-state"><span><SearchX aria-hidden="true" /></span><h3>没有提取到结构化记录</h3><p>已处理论文，但可用摘要中没有形成满足当前模式的数据点。</p></div>
              ) : null}
              {!searchState.isLoading && data && resultCount > 0 ? (
                <div className="ks-online-results">
                  {data.exampleUsed ? <div className="ks-inline-alert is-warning" role="status"><AlertTriangle aria-hidden="true" /><span>在线来源没有可用摘要，本次结果使用了服务端内置示例，不能视为真实文献检索结论。</span></div> : null}
                  {csvError ? <div className="ks-inline-alert is-error" role="alert"><AlertTriangle aria-hidden="true" /><span>{csvError}</span><button type="button" onClick={() => void handleExportCsv()}>重试导出</button></div> : null}
                  <div className="ks-metric-strip">
                    <Metric icon={<Globe2 aria-hidden="true" />} label="处理论文" value={`${data.totalPapers} 篇`} />
                    <Metric icon={<FlaskConical aria-hidden="true" />} label={data.mode === "property" ? "性质关系" : "合成反应"} value={`${resultCount} 条`} />
                    <Metric icon={<Clock3 aria-hidden="true" />} label="查询耗时" value={`${(data.query_time_ms / 1000).toFixed(1)} s`} />
                    <Metric icon={<FileClock aria-hidden="true" />} label="论文上限" value={`${data.max_papers} 篇`} />
                  </div>
                  <div className="ks-distribution-grid">
                    {data.mode === "property" ? <><DistributionPanel title="条件分布" items={data.conditionDistribution} /><DistributionPanel title="关系类型" items={data.relationshipDistribution} /></> : <><DistributionPanel title="反应类型" items={data.reactionTypeTable} /><DistributionPanel title="催化剂" items={data.catalystTable} /></>}
                  </div>
                  {data.conditionSummary.length ? <div className="ks-summary-row"><BarChart3 aria-hidden="true" /><strong>条件摘要</strong>{data.conditionSummary.slice(0, 8).map((item) => <span className="ks-chip" key={item}>{item}</span>)}</div> : null}
                  <div className="ks-result-list">
                    {data.mode === "property" ? data.propertyPoints.map((item, index) => (
                      <button className={`ks-result-card${selectedIndex === index ? " is-selected" : ""}`} type="button" aria-pressed={selectedIndex === index} key={`${item.polymer_name}-${item.property_name}-${index}`} onClick={() => { setSelectedIndex(index); setDrawerView("detail"); setDrawerOpen(true); }}>
                        <span className="ks-card-topline"><span className="ks-card-heading"><strong>{item.property_name} · {item.property_value}</strong><small>{item.polymer_name} · {item.polymer_type}</small></span><span className="ks-card-status-group">{selectedIndex === index ? <span className="ks-selected-indicator"><Check aria-hidden="true" />已选中</span> : null}<span className="ks-relation-chip">{item.relationship}</span></span></span>
                        <span className="ks-card-snippet"><b>条件：</b>{item.condition_name} = {item.condition_value}</span>
                        {item.paper_title ? <span className="ks-chip-row"><span className="ks-chip"><BookMarked aria-hidden="true" />{item.paper_title}</span></span> : null}
                      </button>
                    )) : data.syntheses.map((item, index) => (
                      <button className={`ks-result-card${selectedIndex === index ? " is-selected" : ""}`} type="button" aria-pressed={selectedIndex === index} key={`${item.product_name}-${index}`} onClick={() => { setSelectedIndex(index); setDrawerView("detail"); setDrawerOpen(true); }}>
                        <span className="ks-card-topline"><span className="ks-card-heading"><strong>{item.method || "未命名合成方法"}</strong><small>{item.product_name} · {item.product_abbreviation}</small></span><span className="ks-card-status-group">{selectedIndex === index ? <span className="ks-selected-indicator"><Check aria-hidden="true" />已选中</span> : null}<span className="ks-chip is-match">{item.reaction_type}</span></span></span>
                        <span className="ks-card-snippet"><b>Reactants：</b>{optional(item.reactants)} · <b>Catalyst：</b>{optional(item.catalyst)}</span>
                        <span className="ks-chip-row"><span className="ks-chip is-warning"><AlertTriangle aria-hidden="true" />接口未返回论文题名</span></span>
                      </button>
                    ))}
                  </div>
                </div>
              ) : null}
            </div>
          </section>
        </div>
      </div>

      <KnowledgeDetailDrawer
        id="online-knowledge-detail"
        open={drawerOpen && (drawerView === "history" || detailTabs.length > 0)}
        width={drawerWidth}
        contentKey={drawerView === "history" ? "online-history" : `online-detail-${data?.mode}-${selectedIndex}`}
        title={drawerTitle}
        subtitle={drawerSubtitle}
        icon={drawerView === "history" ? <History aria-hidden="true" /> : <Globe2 aria-hidden="true" />}
        tabs={drawerView === "detail" ? detailTabs : undefined}
        footer={drawerView === "detail" && data ? <><span>处理论文 {data.totalPapers} 篇</span><span>{data.exampleUsed ? "示例回退" : "在线抽取"}</span></> : undefined}
        reopenLabel={drawerView === "history" ? "查看检索历史" : "查看记录详情"}
        verticalReopen={drawerView === "detail"}
        showReopen={drawerView === "history" || detailTabs.length > 0}
        onWidthChange={setDrawerWidth}
        onClose={() => setDrawerOpen(false)}
        onOpen={() => setDrawerOpen(true)}
      >
        {drawerView === "history" ? (
          <div className="ks-drawer-stack">
            <div className="ks-drawer-callout"><History aria-hidden="true" /><span>这里展示当前数据库服务实例最近 100 条已完成记录，不是按用户隔离的个人历史。</span></div>
            <div className="ks-history-toolbar">
              <button className="ks-button" type="button" onClick={() => void searchState.loadHistory()} disabled={searchState.isHistoryLoading}><RefreshCw className={searchState.isHistoryLoading ? "is-spinning" : ""} aria-hidden="true" />刷新</button>
              {!confirmClearHistory ? <button className="ks-button is-danger" type="button" disabled={!searchState.history.length} onClick={() => setConfirmClearHistory(true)}><Trash2 aria-hidden="true" />清空全部</button> : <div className="ks-confirm-row"><span>确认清空服务实例全部历史？</span><button type="button" onClick={() => void clearHistory()}>确认</button><button type="button" onClick={() => setConfirmClearHistory(false)}>取消</button></div>}
            </div>
            {searchState.historyError || historyActionError ? <div className="ks-inline-alert is-error" role="alert"><AlertTriangle aria-hidden="true" /><span>{historyActionError || searchState.historyError}</span></div> : null}
            <div className="ks-history-list">
              {searchState.history.length ? searchState.history.map((item) => (
                <article className="ks-history-item" key={item.history_id}>
                  <div><strong>{item.material} · {item.mode === "property" ? "性质" : "合成"}</strong><span>{item.timestamp} · {item.papers_found} 篇 · {item.reactions_extracted} 条展示记录</span></div>
                  <div><button type="button" onClick={() => restoreHistory(item)}>恢复</button><button type="button" disabled={!hasModelAccess || searchState.isLoading} onClick={() => void replayHistory(item)}>重新检索</button><button className="is-danger" type="button" onClick={() => void deleteHistory(item.history_id)} aria-label={`删除 ${item.material} 历史`}><Trash2 aria-hidden="true" /></button></div>
                </article>
              )) : <div className="ks-state is-compact"><span><FileClock aria-hidden="true" /></span><h3>暂无已完成历史</h3><p>成功完成的在线任务会显示在这里。</p></div>}
            </div>
          </div>
        ) : null}
      </KnowledgeDetailDrawer>
    </div>
  );
}
