import { useAuth } from "../../auth/AuthProvider";
import { requestServiceAccess } from "../../auth/guestAccess";
import {
  AlertTriangle,
  BookOpenText,
  ChevronLeft,
  ChevronRight,
  Clock3,
  Check,
  Copy,
  Database,
  FileText,
  FileSearch,
  FlaskConical,
  Info,
  LoaderCircle,
  RefreshCw,
  Search,
  SearchX,
  SlidersHorizontal
} from "lucide-react";
import { type CSSProperties, type FormEvent, type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { ModalFocusSuspendedContext } from "../../hooks/useModalFocus";
import { DEFAULT_KNOWLEDGE_CONDITIONS, KnowledgeConditionsDialog, type KnowledgeConditionSettings } from "./KnowledgeConditionsDialog";
import { useKnowledgeSearch } from "../../hooks/useKnowledgeSearch";
import { useKnowledgeObservation } from "../../hooks/useKnowledgeObservation";
import {
  knowledgeSearchGroupsFromTerms,
  normalizeKnowledgeSearchGroups,
  parseKnowledgeSearchExpression,
  serializeKnowledgeSearchGroups
} from "../../lib/knowledgeSearchExpression";
import { fetchKnowledgeSources } from "../../services/api";
import type { KnowledgeDataSource, KnowledgeDocumentResult, KnowledgeSearchGroup, KnowledgeSearchResult, KnowledgeSourcesResponse, LiteratureFilters } from "../../types";
import { LiteratureDetail } from "./LiteratureDetail";
import {
  KnowledgeDetailDrawer,
  type KnowledgeDrawerTab,
  useKnowledgeDrawerSizing
} from "./KnowledgeDetailDrawer";

type LocalKnowledgePanelProps = {
  initialQuery?: string;
  initialTerms?: string[];
  modeNavigation: ReactNode;
  toolbarActions?: ReactNode;
};

const SOURCE_LABELS: Record<KnowledgeDataSource, string> = { legacy: "现有专利知识库", extracted_literature: "提取文献库" };

type SubmittedExpression = {
  query: string;
  groups: KnowledgeSearchGroup[];
  data_source?: KnowledgeDataSource;
  filters?: LiteratureFilters;
  generation_id?: string;
};

function literatureFilters(settings: KnowledgeConditionSettings): LiteratureFilters {
  return {
    ...(settings.filters.year_from ? { year_from: Number(settings.filters.year_from) } : {}),
    ...(settings.filters.year_to ? { year_to: Number(settings.filters.year_to) } : {}),
    ...(settings.filters.has_properties ? { has_properties: true } : {}),
    ...(settings.filters.has_processes ? { has_processes: true } : {}),
    ...(settings.filters.has_original ? { has_original: true } : {})
  };
}

function initialConditions(): KnowledgeConditionSettings {
  return { ...DEFAULT_KNOWLEDGE_CONDITIONS, sources: [...DEFAULT_KNOWLEDGE_CONDITIONS.sources], filters: { ...DEFAULT_KNOWLEDGE_CONDITIONS.filters } };
}

function recordKey(record: KnowledgeSearchResult) {
  return record.data_source === "extracted_literature" ? `literature:${record.document_uid}` : `legacy:${record.knowledge_id}`;
}

function parseExpression(query: string, source: KnowledgeDataSource) {
  // Semicolons are part of many valid DOI suffixes, rather than an AND operator.
  if (source === "extracted_literature" && /^(?:https?:\/\/(?:dx\.)?doi\.org\/|doi:\s*)?10\.\d{4,9}\//i.test(query.trim())) {
    return { groups: [{ terms: [query.trim()] }], error: null };
  }
  return parseKnowledgeSearchExpression(query);
}

function normalizeTerms(terms: string[]) {
  const values: string[] = [];
  const seen = new Set<string>();
  terms.forEach((term) => {
    const value = term.trim();
    const key = value.toLocaleLowerCase();
    if (value && !seen.has(key)) {
      seen.add(key);
      values.push(value);
    }
  });
  return values;
}

function escapeRegExp(value: string) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function HighlightedText({ text, terms }: { text: string; terms: string[] }) {
  const normalized = normalizeTerms(terms).sort((first, second) => second.length - first.length);
  if (!text || normalized.length === 0) return <>{text}</>;
  const parts = text.split(new RegExp(`(${normalized.map(escapeRegExp).join("|")})`, "ig"));
  return (
    <>
      {parts.map((part, index) =>
        normalized.some((term) => part.toLocaleLowerCase() === term.toLocaleLowerCase()) ? (
          <mark key={`${part}-${index}`}>{part}</mark>
        ) : (
          <span key={`${part}-${index}`}>{part}</span>
        )
      )}
    </>
  );
}

function isDesktopViewport() {
  return typeof window === "undefined" || typeof window.matchMedia !== "function"
    ? true
    : !window.matchMedia("(max-width: 899px)").matches;
}

function optional(value: string | number | null | undefined) {
  if (value === null || value === undefined || value === "") return "—";
  return String(value);
}

function DetailGrid({ fields }: { fields: Array<{ label: string; value: string | number | null | undefined }> }) {
  return (
    <dl className="ks-detail-grid">
      {fields.map((field) => (
        <div key={field.label}>
          <dt>{field.label}</dt>
          <dd>{optional(field.value)}</dd>
        </div>
      ))}
    </dl>
  );
}

function DetailText({ label, value }: { label: string; value: string | null | undefined }) {
  return (
    <section className="ks-drawer-section">
      <h3>{label}</h3>
      <p className="ks-long-copy">{optional(value)}</p>
    </section>
  );
}

function buildLocalTabs(record: KnowledgeDocumentResult): KnowledgeDrawerTab[] {
  return [
    {
      id: "overview",
      label: "记录概览",
      content: (
        <div className="ks-drawer-stack">
          <section className="ks-drawer-section">
            <p className="ks-drawer-title-main">{record.title_zh || "未提供中文标题"}</p>
            {record.title_en ? <p className="ks-drawer-title-secondary">{record.title_en}</p> : null}
            <div className="ks-chip-row">
              {record.matched_terms.map((term) => <span className="ks-chip is-match" key={`detail-term-${term}`}>{term}</span>)}
              {record.matched_fields.map((field) => <span className="ks-chip" key={`detail-field-${field}`}>{field}</span>)}
            </div>
          </section>
          <section className="ks-drawer-section">
            <h3><FlaskConical aria-hidden="true" />核心字段</h3>
            <DetailGrid
              fields={[
                { label: "Polymer IUPAC", value: record.polymer_iupac },
                { label: "合成判断", value: record.is_polymer_synthesis || "未标注" },
                { label: "温度", value: record.temperature },
                { label: "反应时间", value: record.reaction_time }
              ]}
            />
          </section>
          <section className="ks-drawer-section">
            <h3><FileText aria-hidden="true" />摘要</h3>
            <p className="ks-long-copy">{optional(record.abstract)}</p>
          </section>
        </div>
      )
    },
    {
      id: "reaction",
      label: "反应信息",
      content: (
        <div className="ks-drawer-stack">
          <section className="ks-drawer-section">
            <h3><FlaskConical aria-hidden="true" />配方与条件</h3>
            <DetailGrid
              fields={[
                { label: "聚合物", value: record.polymer_iupac },
                { label: "配方", value: record.formulation },
                { label: "催化剂", value: record.catalyst },
                { label: "溶剂", value: record.solvent },
                { label: "温度", value: record.temperature },
                { label: "时间", value: record.reaction_time }
              ]}
            />
          </section>
          <DetailText label="模型分析" value={record.analysis} />
          <DetailText label="判断理由" value={record.judgement_reason} />
        </div>
      )
    },
    {
      id: "source",
      label: "原文与溯源",
      content: (
        <div className="ks-drawer-stack">
          <section className="ks-drawer-section">
            <h3><FileSearch aria-hidden="true" />来源定位</h3>
            <DetailGrid
              fields={[
                { label: "Knowledge ID", value: record.knowledge_id },
                { label: "来源文件", value: record.source_file },
                { label: "原始行号", value: record.source_row_number },
                { label: "来源序号", value: record.source_sequence }
              ]}
            />
          </section>
          <DetailText label="完整权利要求" value={record.claim} />
          <section className="ks-drawer-section">
            <div className="ks-drawer-callout"><Info aria-hidden="true" /><span>该定位信息来自当前本地知识接口，可用于回到原始导入文件核验；页面不会打开或修改源文件。</span></div>
          </section>
        </div>
      )
    }
  ];
}

function ResultSkeletons() {
  return (
    <div className="ks-skeleton-stack" role="status" aria-label="正在加载本地知识记录">
      {[0, 1, 2].map((item) => (
        <div className="ks-skeleton-card" key={item}>
          <span className="ks-skeleton is-title" />
          <span className="ks-skeleton" />
          <span className="ks-skeleton is-short" />
        </div>
      ))}
    </div>
  );
}

export function LocalKnowledgePanel({ initialQuery = "", initialTerms = [], modeNavigation, toolbarActions }: LocalKnowledgePanelProps) {
  const guest = useAuth()?.status === "guest";
  const legacySearch = useKnowledgeSearch();
  const literatureSearch = useKnowledgeSearch();
  const [settings, setSettings] = useState<KnowledgeConditionSettings>(initialConditions);
  const [conditionsOpen, setConditionsOpen] = useState(false);
  const [dataSource, setDataSource] = useState<KnowledgeDataSource>("legacy");
  const searchState = dataSource === "legacy" ? legacySearch : literatureSearch;
  const observeKnowledge = useKnowledgeObservation(searchState.data?.search_id);
  const [sources, setSources] = useState<KnowledgeSourcesResponse | null>(null);
  const [sourceError, setSourceError] = useState<string | null>(null);
  const [sourceRetry, setSourceRetry] = useState(0);
  const [query, setQuery] = useState(() => {
    const groups = knowledgeSearchGroupsFromTerms(initialTerms);
    return groups.length ? serializeKnowledgeSearchGroups(groups) : initialQuery;
  });
  const [submittedExpressions, setSubmittedExpressions] = useState<Partial<Record<KnowledgeDataSource, SubmittedExpression>>>({});
  const submittedExpression = submittedExpressions[dataSource] ?? null;
  const [pages, setPages] = useState<Record<KnowledgeDataSource, number>>({ legacy: 1, extracted_literature: 1 });
  const page = pages[dataSource];
  const pageSize = settings.pageSize;
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const searchEpoch = useRef(0);
  const selectionEpoch = useRef(0);
  const sizing = useKnowledgeDrawerSizing();
  const { drawerWidth, setDrawerWidth, widthProfile } = sizing;
  const selectedRecord = searchState.data?.results.find((record) => recordKey(record) === selectedId) ?? null;
  const initialTermsKey = initialTerms.join("\u0000");
  const totalPages = Math.max(1, Math.ceil((searchState.data?.total ?? 0) / pageSize));
  const includesLiterature = settings.sources.includes("extracted_literature");
  const literatureOnly = includesLiterature && settings.sources.length === 1;
  const parsedExpression = useMemo(() => parseExpression(query, includesLiterature ? "extracted_literature" : "legacy"), [query, includesLiterature]);
  const literature = dataSource === "extracted_literature";
  const sourceInfo = sources?.sources.find((source) => source.data_source === "extracted_literature");
  const unavailableSources = settings.sources.filter((source) => {
    const info = sources?.sources.find((item) => item.data_source === source);
    return info && (!info.enabled || !info.ready);
  });
  const allSourcesUnavailable = unavailableSources.length === settings.sources.length;
  const searching = legacySearch.isLoading || literatureSearch.isLoading;
  const hasSubmittedSearch = Object.keys(submittedExpressions).length > 0;

  useEffect(() => {
    if (guest) return;
    const controller = new AbortController();
    setSourceError(null);
    void fetchKnowledgeSources(controller.signal).then((value) => {
      if (!controller.signal.aborted) setSources(value);
    }).catch((failure: unknown) => {
      if (!controller.signal.aborted) setSourceError(failure instanceof Error ? failure.message : "无法读取知识库状态");
    });
    return () => controller.abort();
  }, [guest, sourceRetry]);

  function applyConditions(next: KnowledgeConditionSettings) {
    searchEpoch.current += 1;
    selectionEpoch.current += 1;
    legacySearch.reset();
    literatureSearch.reset();
    setSettings({ ...next, sources: [...next.sources], filters: { ...next.filters } });
    setDataSource(next.sources.includes(dataSource) ? dataSource : next.sources[0]);
    setSubmittedExpressions({});
    setPages({ legacy: 1, extracted_literature: 1 });
    setSelectedId(null);
    setDrawerOpen(false);
    setConditionsOpen(false);
  }

  function showSource(next: KnowledgeDataSource) {
    selectionEpoch.current += 1;
    setDataSource(next);
    const state = next === "legacy" ? legacySearch : literatureSearch;
    setSelectedId(state.data?.results[0] ? recordKey(state.data.results[0]) : null);
    setDrawerOpen(false);
  }
  const highlightTerms = searchState.data?.terms.length
    ? searchState.data.terms
    : submittedExpression?.groups.flatMap((group) => group.terms) ?? [searchState.data?.query || query];

  async function searchSource(
    source: KnowledgeDataSource,
    nextQuery: string,
    nextPage: number,
    nextPageSize: number,
    suppliedGroups?: KnowledgeSearchGroup[],
    context?: SubmittedExpression
  ) {
    const epoch = searchEpoch.current;
    const parsed = suppliedGroups
      ? { groups: normalizeKnowledgeSearchGroups(suppliedGroups), error: null }
      : parseExpression(nextQuery, source);
    if (parsed.error || parsed.groups.length === 0) return null;
    const normalizedQuery = nextQuery.trim() || serializeKnowledgeSearchGroups(parsed.groups);
    const expression: SubmittedExpression = {
      query: normalizedQuery, groups: parsed.groups, data_source: source,
      ...(source === "extracted_literature" ? { filters: context?.filters ?? literatureFilters(settings), generation_id: context?.generation_id } : {})
    };
    setSubmittedExpressions((current) => ({ ...current, [source]: expression }));
    setPages((current) => ({ ...current, [source]: nextPage }));
    const state = source === "legacy" ? legacySearch : literatureSearch;
    const response = await state.submit(
      normalizedQuery, nextPageSize, expression.groups, nextPage, nextPageSize,
      source === "extracted_literature" ? { data_source: source, filters: expression.filters, ...(expression.generation_id ? { generation_id: expression.generation_id } : {}) } : undefined
    );
    if (!response || searchEpoch.current !== epoch) return null;
    setSubmittedExpressions((current) => ({ ...current, [source]: { ...expression, ...(response.generation_id ? { generation_id: response.generation_id } : {}) } }));
    return response;
  }

  async function executeSearch(
    nextQuery: string, nextPage: number, nextPageSize: number,
    suppliedGroups?: KnowledgeSearchGroup[], context?: SubmittedExpression
  ) {
    const source = context?.data_source ?? dataSource;
    const selection = ++selectionEpoch.current;
    setSelectedId(null);
    setDrawerOpen(false);
    const response = await searchSource(source, nextQuery, nextPage, nextPageSize, suppliedGroups, context);
    if (!response?.results.length || selection !== selectionEpoch.current) return;
    setDataSource(source);
    setSelectedId(recordKey(response.results[0]));
    if (isDesktopViewport()) setDrawerOpen(true);
  }

  useEffect(() => {
    const groups = knowledgeSearchGroupsFromTerms(initialTerms);
    const nextQuery = groups.length ? serializeKnowledgeSearchGroups(groups) : initialQuery.trim();
    if (!nextQuery || guest) return;
    searchEpoch.current += 1;
    selectionEpoch.current += 1;
    legacySearch.reset();
    literatureSearch.reset();
    setSettings(initialConditions());
    setConditionsOpen(false);
    setDataSource("legacy");
    setSubmittedExpressions({});
    setPages({ legacy: 1, extracted_literature: 1 });
    setQuery(nextQuery);
    void executeSearch(nextQuery, 1, 20, groups.length ? groups : undefined, { query: nextQuery, groups, data_source: "legacy" });
    // Route parameters deliberately reset conditions and always query patents.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialQuery, initialTermsKey]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!requestServiceAccess()) return;
    if (!query.trim() || parsedExpression.error || allSourcesUnavailable || parsedExpression.groups.length === 0 || searching) return;
    const epoch = ++searchEpoch.current;
    const selection = ++selectionEpoch.current;
    legacySearch.reset();
    literatureSearch.reset();
    setSubmittedExpressions({});
    setSelectedId(null);
    setDrawerOpen(false);
    const responses = await Promise.all(settings.sources.map((source) => searchSource(source, query, 1, pageSize, parsedExpression.groups)));
    if (searchEpoch.current !== epoch || selectionEpoch.current !== selection) return;
    const preferred = settings.sources.indexOf(dataSource);
    const nextIndex = preferred >= 0 && responses[preferred]?.results.length ? preferred : responses.findIndex((response) => response?.results.length);
    const source = settings.sources[nextIndex >= 0 ? nextIndex : 0];
    const record = nextIndex >= 0 ? responses[nextIndex]?.results[0] : null;
    setDataSource(source);
    setSelectedId(record ? recordKey(record) : null);
    if (record && isDesktopViewport()) setDrawerOpen(true);
  }

  const resultCountLabel = searchState.data
    ? `${searchState.data.total.toLocaleString("zh-CN")} ${literature ? "篇文献" : "条记录"} · ${searchState.data.query_time_ms.toFixed(1)} ms · 相关性排序`
    : literature ? "支持标题、摘要关键词与 DOI" : "支持关键词、IUPAC 名称与配方术语";
  const legacyRecord = selectedRecord?.data_source === "extracted_literature" ? null : selectedRecord;
  const drawerTabs = useMemo(() => (legacyRecord ? buildLocalTabs(legacyRecord) : []), [legacyRecord]);

  return (
    <div
      className={`ks-panel-layout${drawerOpen ? " is-drawer-open" : ""}`}
      style={{ "--ks-drawer-width": `${drawerWidth}px` } as CSSProperties}
    >
      <div className="ks-panel-scroll">
        <div className="ks-workbench-column">
          <div className="ks-module-toolbar" data-recording-header>
            <span className="ks-toolbar-status"><i />准备就绪</span>
            {toolbarActions}
          </div>

          <section className="ks-surface ks-search-surface">
            <header className="ks-surface-header">
              <div className="ks-surface-heading">
                <span className="ks-surface-mark"><Database aria-hidden="true" /></span>
                <div className="ks-surface-copy">
                  <h2>本地知识库检索</h2>
                  <p>{literatureOnly ? "检索提取文献的标题、摘要和 DOI，查看样品、性质与工艺" : "检索聚合物、配方、标题、权利要求与摘要中的知识记录"}</p>
                </div>
              </div>
              {modeNavigation}
            </header>

            <div className="ks-form-zone">
              {includesLiterature && (unavailableSources.includes("extracted_literature") || sourceError) ? <div className="ks-inline-alert is-warning" role="status">
                <span>{sourceError || sourceInfo?.reason || "提取文献库暂不可用；现有专利知识库仍可使用。"}</span>
                <button type="button" onClick={() => setSourceRetry((value) => value + 1)}>重新检查</button>
              </div> : null}
              <form className="ks-search-form ks-local-form" onSubmit={handleSubmit}>
                <label className="ks-field">
                  <span>检索词 <small>{literatureOnly ? "标题 / 摘要 / DOI" : includesLiterature ? "关键词 / IUPAC / DOI" : "关键词 / IUPAC / 配方"}</small></span>
                  <span className="ks-input-with-icon">
                    <Search aria-hidden="true" />
                    <input
                      type="search"
                      value={query}
                      onChange={(event) => setQuery(event.target.value)}
                      placeholder={literatureOnly ? "例如 polyimide；或粘贴完整 DOI" : "例如 polyimide；NMP | N-methyl-2-pyrrolidone"}
                      aria-label="本地知识库检索词"
                      aria-describedby="local-knowledge-expression-help"
                      aria-invalid={Boolean(query.trim() && parsedExpression.error)}
                      autoComplete="off"
                    />
                  </span>
                </label>
                <div className="ks-field ks-condition-field">
                  <span>检索条件</span>
                  <button className="ks-button ks-conditions-trigger" type="button" aria-label="条件设置" aria-haspopup="dialog" aria-expanded={conditionsOpen} onClick={() => setConditionsOpen(true)}>
                    <SlidersHorizontal aria-hidden="true" />条件设置
                  </button>
                </div>
                <button
                  className="ks-button is-primary"
                  type="submit"
                  aria-busy={searching}
                  disabled={
                    !query.trim() ||
                    Boolean(parsedExpression.error) ||
                    allSourcesUnavailable ||
                    parsedExpression.groups.length === 0 ||
                    searching
                  }
                >
                  {searching ? <LoaderCircle className="is-spinning" aria-hidden="true" /> : <Search aria-hidden="true" />}
                  {searching ? "检索中" : "运行检索"}
                </button>
              </form>

              <div
                id="local-knowledge-expression-help"
                className={`ks-expression-help${parsedExpression.error ? " is-error" : ""}`}
              >
                <span className="ks-expression-guidance">多个条件用“；”分隔，同义词用“|”分隔</span>
                {query.trim() && parsedExpression.error ? (
                  <span className="ks-expression-error" role="alert"><AlertTriangle aria-hidden="true" />{parsedExpression.error}</span>
                ) : null}
                {query.trim() && !parsedExpression.error && parsedExpression.groups.length ? (
                  <span
                    className="ks-expression-preview"
                    aria-label={`检索逻辑：${serializeKnowledgeSearchGroups(parsedExpression.groups)}`}
                  >
                    <small>逻辑预览</small>
                    {parsedExpression.groups.map((group, groupIndex) => (
                      <span className="ks-expression-segment" key={`query-group-${groupIndex}`}>
                        {groupIndex > 0 ? <b className="is-and">AND</b> : null}
                        <span className="ks-expression-group">
                          <i aria-hidden="true">[</i>
                          {group.terms.map((term, termIndex) => (
                            <span className="ks-expression-term" key={`${term}-${termIndex}`}>
                              {termIndex > 0 ? <b className="is-or">OR</b> : null}
                              <span>{term}</span>
                            </span>
                          ))}
                          <i aria-hidden="true">]</i>
                        </span>
                      </span>
                    ))}
                  </span>
                ) : null}
              </div>

              <div className="ks-meta-row">
                <span>{literatureOnly ? "范围：标题 / 摘要 / DOI" : "范围：Polymer / Formulation / 中英文标题 / Claim / Abstract"}</span>
                <span>{settings.sources.length > 1 ? "两个知识库分别排序、分页" : literatureOnly ? "排序：DOI 精确命中 → 字段相关性 → 文献 ID" : "排序：全部条件命中 → 字段相关性 → 记录 ID"}</span>
                {includesLiterature ? <span>来源：{settings.sources.map((source) => SOURCE_LABELS[source]).join(" + ")}</span> : null}
              </div>
            </div>
          </section>

          <section className="ks-surface ks-results-surface" aria-busy={searchState.isLoading}>
            {settings.sources.length > 1 && hasSubmittedSearch ? <div className="ks-result-source-tabs" role="group" aria-label="检索结果知识库">
              {settings.sources.map((source) => {
                const state = source === "legacy" ? legacySearch : literatureSearch;
                return <button type="button" key={source} aria-pressed={dataSource === source} className={dataSource === source ? "is-active" : ""} onClick={() => showSource(source)}>
                  {source === "legacy" ? <Database aria-hidden="true" /> : <BookOpenText aria-hidden="true" />}
                  {SOURCE_LABELS[source]}<span>{state.isLoading ? "检索中" : state.error ? "暂不可用" : state.data ? state.data.total.toLocaleString("zh-CN") : "未检索"}</span>
                </button>;
              })}
            </div> : null}
            <header className="ks-results-header">
              <div className="ks-results-summary">
                <h2>{searchState.data ? literature ? "文献检索结果" : "本地检索结果" : literature ? "准备检索已提取文献" : "准备检索本地知识库"}</h2>
                <p role="status" aria-live="polite" aria-atomic="true">{searchState.isLoading ? "正在查询知识库…" : resultCountLabel}</p>
              </div>
              {searchState.data ? (
                <button className="ks-button" type="button" onClick={() => void navigator.clipboard?.writeText(searchState.data?.query || "")}>
                  <Copy aria-hidden="true" />复制检索词
                </button>
              ) : null}
            </header>

            <div className="ks-results-body">
              {searchState.isLoading ? <ResultSkeletons /> : null}

              {!searchState.isLoading && searchState.error ? (
                <div className="ks-state is-error" role="alert">
                  <span><AlertTriangle aria-hidden="true" /></span>
                  <h3>{literature ? "文献检索暂时不可用" : "本地知识库暂时不可用"}</h3>
                  <p>{searchState.error}</p>
                  <button
                    className="ks-button"
                    type="button"
                    onClick={() => {
                      const expression = submittedExpression ?? { query, groups: parsedExpression.groups };
                      void executeSearch(expression.query, page, pageSize, expression.groups, expression);
                    }}
                  >
                    <RefreshCw aria-hidden="true" />重试检索
                  </button>
                </div>
              ) : null}

              {!searchState.isLoading && !searchState.error && !searchState.data ? (
                <div className="ks-state">
                  <span><Search aria-hidden="true" /></span>
                  <h3>输入检索词开始查询</h3>
                  <p>{literature ? "输入标题、摘要关键词或 DOI；筛选条件在运行检索后生效。" : "可检索聚合物名称、IUPAC 名称、配方、标题、权利要求和摘要。"}</p>
                </div>
              ) : null}

              {!searchState.isLoading && searchState.data?.results.length === 0 ? (
                <div className="ks-state">
                  <span><SearchX aria-hidden="true" /></span>
                  <h3>{literature ? "没有匹配的文献" : "没有匹配的知识记录"}</h3>
                  <p>{literature ? "请尝试其他标题关键词、DOI，或放宽年份和信息筛选。" : "请尝试更短的材料名称、IUPAC 片段或配方术语。"}</p>
                </div>
              ) : null}

              {!searchState.isLoading && searchState.data?.results.length ? (
                <>
                  <div className="ks-result-list">
                    {searchState.data.results.map((record) => {
                      const selected = recordKey(record) === selectedId;
                      const isLiterature = record.data_source === "extracted_literature";
                      return (
                        <button
                          key={recordKey(record)}
                          className={`ks-result-card${selected ? " is-selected" : ""}`}
                          type="button"
                          aria-pressed={selected}
                          onClick={() => {
                            selectionEpoch.current += 1;
                            setSelectedId(recordKey(record));
                            setDrawerOpen(true);
                            if (record.data_source === "extracted_literature") observeKnowledge(record, "literature_detail");
                            else observeKnowledge(record.knowledge_id, "result_card");
                          }}
                        >
                          <span className="ks-card-topline">
                            <span className="ks-card-heading">
                              <strong><HighlightedText text={record.data_source === "extracted_literature" ? record.title : record.title_zh || record.title_en || `知识记录 #${record.knowledge_id}`} terms={highlightTerms} /></strong>
                              {record.title_zh && record.title_en ? <small><HighlightedText text={record.title_en} terms={highlightTerms} /></small> : null}
                            </span>
                            <span className="ks-card-status-group">
                              {selected ? <span className="ks-selected-indicator"><Check aria-hidden="true" />已选中</span> : null}
                              <span className="ks-record-id">{isLiterature ? "已提取文献" : `#${record.knowledge_id}`}</span>
                            </span>
                          </span>
                          <span className="ks-card-snippet"><HighlightedText text={record.abstract_snippet} terms={highlightTerms} /></span>
                          <span className="ks-chip-row">
                            {record.matched_terms.map((term) => <span className="ks-chip is-match" key={`term-${term}`}>{term}</span>)}
                            {record.matched_fields.map((field) => <span className="ks-chip" key={`field-${field}`}>{field}</span>)}
                          </span>
                          {record.data_source === "extracted_literature" ? <>
                            <span className="ks-literature-citation">{[record.authors.slice(0, 3).join("、"), record.year, record.journal].filter(Boolean).join(" · ")}</span>
                            {record.doi ? <span className="ks-literature-citation">DOI：{record.doi}</span> : null}
                            <span className="ks-chip-row">
                              <span className="ks-chip">样品 {record.counts.samples ?? 0}</span>
                              <span className="ks-chip">性质 {record.counts.properties ?? 0}</span>
                              <span className="ks-chip">工艺 {record.counts.processes ?? 0}</span>
                              {record.capabilities.has_fulltext ? <span className="ks-chip">全文</span> : null}
                              {record.capabilities.has_pdf ? <span className="ks-chip">PDF</span> : null}
                            </span>
                          </> : <span className="ks-fact-grid">
                            <span><small>Polymer</small><b>{optional(record.polymer_iupac)}</b></span>
                            <span><small>Temperature</small><b>{optional(record.temperature)}</b></span>
                            <span><small>Time</small><b>{optional(record.reaction_time)}</b></span>
                            <span><small>Solvent</small><b>{optional(record.solvent)}</b></span>
                          </span>}
                        </button>
                      );
                    })}
                  </div>

                  <div className="ks-pagination">
                    <span>第 {page} / {totalPages.toLocaleString("zh-CN")} 页 · 共 {searchState.data.total.toLocaleString("zh-CN")} 条</span>
                    <div>
                      <button
                        className="ks-button"
                        type="button"
                        disabled={page <= 1 || searchState.isLoading || !submittedExpression}
                        onClick={() => submittedExpression && void executeSearch(
                          submittedExpression.query,
                          page - 1,
                          pageSize,
                          submittedExpression.groups,
                          submittedExpression
                        )}
                      >
                        <ChevronLeft aria-hidden="true" />上一页
                      </button>
                      <button
                        className="ks-button"
                        type="button"
                        disabled={page >= totalPages || searchState.isLoading || !submittedExpression}
                        onClick={() => submittedExpression && void executeSearch(
                          submittedExpression.query,
                          page + 1,
                          pageSize,
                          submittedExpression.groups,
                          submittedExpression
                        )}
                      >
                        下一页<ChevronRight aria-hidden="true" />
                      </button>
                    </div>
                  </div>
                </>
              ) : null}
            </div>
          </section>
        </div>
      </div>

      <ModalFocusSuspendedContext.Provider value={conditionsOpen}>
      {legacyRecord ? <KnowledgeDetailDrawer
        id="local-knowledge-detail"
        open={drawerOpen && Boolean(legacyRecord)}
        width={drawerWidth}
        contentKey={legacyRecord ? `local-${legacyRecord.knowledge_id}` : "local-empty"}
        title="知识记录详情"
        subtitle={legacyRecord ? `#${legacyRecord.knowledge_id} · 本地知识库` : "选择结果后查看"}
        icon={<BookOpenText aria-hidden="true" />}
        tabs={drawerTabs}
        onTabChange={(tabId) => {
          if (tabId === "reaction" && legacyRecord) {
            observeKnowledge(legacyRecord.knowledge_id, "reaction_tab");
          }
        }}
        footer={legacyRecord ? (
          <><span><Clock3 aria-hidden="true" />记录 {legacyRecord.knowledge_id} · 可追溯字段完整</span><button className="ks-button" type="button" onClick={() => void navigator.clipboard?.writeText(`${legacyRecord.source_file}:${legacyRecord.source_row_number}`)}><Copy aria-hidden="true" />复制定位</button></>
        ) : undefined}
        reopenLabel="查看记录详情"
        verticalReopen
        showReopen={Boolean(legacyRecord)}
        widthProfile={widthProfile}
        onWidthChange={setDrawerWidth}
        onClose={() => setDrawerOpen(false)}
        onOpen={() => {
          setDrawerOpen(true);
          if (legacyRecord) observeKnowledge(legacyRecord.knowledge_id, "drawer_reopen");
        }}
      /> : null}
      {selectedRecord?.data_source === "extracted_literature" ? <LiteratureDetail
        key={`${selectedRecord.generation_id}:${selectedRecord.document_uid}:${selectedRecord.snapshot_uid}`}
        record={selectedRecord} open={drawerOpen} sizing={sizing}
        onClose={() => setDrawerOpen(false)}
        onOpen={() => { setDrawerOpen(true); observeKnowledge(selectedRecord, "drawer_reopen"); }}
        onObserve={(source) => observeKnowledge(selectedRecord, source)}
      /> : null}
      </ModalFocusSuspendedContext.Provider>
      {conditionsOpen ? <KnowledgeConditionsDialog settings={settings} onApply={applyConditions} onClose={() => setConditionsOpen(false)} sources={sources} sourceError={sourceError} /> : null}
    </div>
  );
}
