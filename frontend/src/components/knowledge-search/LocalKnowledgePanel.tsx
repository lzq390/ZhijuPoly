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
  SearchX
} from "lucide-react";
import { type CSSProperties, type FormEvent, type ReactNode, useEffect, useMemo, useRef, useState } from "react";
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

const PAGE_SIZES = [20, 50, 100] as const;

type SubmittedExpression = {
  query: string;
  groups: KnowledgeSearchGroup[];
  data_source?: KnowledgeDataSource;
  filters?: LiteratureFilters;
  generation_id?: string;
};

type FilterDraft = { year_from: string; year_to: string; has_properties: boolean; has_processes: boolean; has_original: boolean };
const EMPTY_FILTERS: FilterDraft = { year_from: "", year_to: "", has_properties: false, has_processes: false, has_original: false };

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
  const searchState = useKnowledgeSearch();
  const observeKnowledge = useKnowledgeObservation(searchState.data?.search_id);
  const [dataSource, setDataSource] = useState<KnowledgeDataSource>("legacy");
  const [filters, setFilters] = useState<FilterDraft>({ ...EMPTY_FILTERS });
  const [sources, setSources] = useState<KnowledgeSourcesResponse | null>(null);
  const [sourceError, setSourceError] = useState<string | null>(null);
  const [sourceRetry, setSourceRetry] = useState(0);
  const [query, setQuery] = useState(() => {
    const groups = knowledgeSearchGroupsFromTerms(initialTerms);
    return groups.length ? serializeKnowledgeSearchGroups(groups) : initialQuery;
  });
  const [submittedExpression, setSubmittedExpression] = useState<SubmittedExpression | null>(null);
  const [pageSize, setPageSize] = useState<(typeof PAGE_SIZES)[number]>(20);
  const [page, setPage] = useState(1);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const sizing = useKnowledgeDrawerSizing();
  const { drawerWidth, setDrawerWidth, widthProfile } = sizing;
  const selectedRecord = searchState.data?.results.find((record) => recordKey(record) === selectedId) ?? null;
  const savedForms = useRef<Partial<Record<KnowledgeDataSource, { query: string; filters: FilterDraft; pageSize: typeof pageSize; submitted: SubmittedExpression | null }>>>({});
  const initialTermsKey = initialTerms.join("\u0000");
  const totalPages = Math.max(1, Math.ceil((searchState.data?.total ?? 0) / pageSize));
  const parsedExpression = useMemo(() => parseExpression(query, dataSource), [query, dataSource]);
  const literature = dataSource === "extracted_literature";
  const sourceInfo = sources?.sources.find((source) => source.data_source === "extracted_literature");
  const sourceUnavailable = literature && sourceInfo && (!sourceInfo.enabled || !sourceInfo.ready);
  const yearError = literature && (
    (filters.year_from && !/^\d{4}$/.test(filters.year_from)) ||
    (filters.year_to && !/^\d{4}$/.test(filters.year_to))
  ) ? "年份须为四位数字" : literature && filters.year_from && filters.year_to && Number(filters.year_from) > Number(filters.year_to) ? "起始年份不能晚于结束年份" : null;
  const submittedFilters: LiteratureFilters = {
    ...(filters.year_from ? { year_from: Number(filters.year_from) } : {}),
    ...(filters.year_to ? { year_to: Number(filters.year_to) } : {}),
    ...(filters.has_properties ? { has_properties: true } : {}),
    ...(filters.has_processes ? { has_processes: true } : {}),
    ...(filters.has_original ? { has_original: true } : {})
  };

  useEffect(() => {
    if (!literature || guest) return;
    const controller = new AbortController();
    setSourceError(null);
    void fetchKnowledgeSources(controller.signal).then((value) => {
      if (!controller.signal.aborted) setSources(value);
    }).catch((failure: unknown) => {
      if (!controller.signal.aborted) setSourceError(failure instanceof Error ? failure.message : "无法读取文献服务状态");
    });
    return () => controller.abort();
  }, [literature, guest, sourceRetry]);

  function changeSource(nextSource: KnowledgeDataSource) {
    savedForms.current[dataSource] = { query, filters, pageSize, submitted: submittedExpression };
    const saved = savedForms.current[nextSource];
    searchState.reset();
    setDataSource(nextSource);
    setQuery(saved?.query ?? "");
    setFilters(saved?.filters ?? { ...EMPTY_FILTERS });
    setPageSize(saved?.pageSize ?? 20);
    setSubmittedExpression(saved?.submitted ?? null);
    setPage(1);
    setSelectedId(null);
    setDrawerOpen(false);
  }

  function resetConditions() {
    searchState.reset();
    setQuery("");
    setFilters({ ...EMPTY_FILTERS });
    setSubmittedExpression(null);
    setPage(1);
    setSelectedId(null);
    setDrawerOpen(false);
  }
  const highlightTerms = searchState.data?.terms.length
    ? searchState.data.terms
    : submittedExpression?.groups.flatMap((group) => group.terms) ?? [searchState.data?.query || query];

  async function executeSearch(
    nextQuery: string,
    nextPage: number,
    nextPageSize: number,
    suppliedGroups?: KnowledgeSearchGroup[],
    context?: SubmittedExpression
  ) {
    const source = context?.data_source ?? dataSource;
    const parsed = suppliedGroups
      ? { groups: normalizeKnowledgeSearchGroups(suppliedGroups), error: null }
      : parseExpression(nextQuery, source);
    if (parsed.error || parsed.groups.length === 0) return;
    const normalizedQuery = nextQuery.trim() || serializeKnowledgeSearchGroups(parsed.groups);
    const expression: SubmittedExpression = {
      query: normalizedQuery, groups: parsed.groups, data_source: source,
      ...(source === "extracted_literature" ? { filters: context?.filters ?? submittedFilters, generation_id: context?.generation_id } : {})
    };
    setSubmittedExpression(expression);
    setPage(nextPage);
    setSelectedId(null);
    setDrawerOpen(false);
    const response = await searchState.submit(
      normalizedQuery,
      nextPageSize,
      expression.groups,
      nextPage,
      nextPageSize,
      source === "extracted_literature" ? { data_source: source, filters: expression.filters, ...(expression.generation_id ? { generation_id: expression.generation_id } : {}) } : undefined
    );
    if (!response?.results.length) return;
    setSubmittedExpression({ ...expression, ...(response.generation_id ? { generation_id: response.generation_id } : {}) });
    setSelectedId(recordKey(response.results[0]));
    if (isDesktopViewport()) setDrawerOpen(true);
  }

  useEffect(() => {
    const groups = knowledgeSearchGroupsFromTerms(initialTerms);
    const nextQuery = groups.length ? serializeKnowledgeSearchGroups(groups) : initialQuery.trim();
    if (!nextQuery || guest) return;
    searchState.reset();
    setDataSource("legacy");
    setFilters({ ...EMPTY_FILTERS });
    setPageSize(20);
    setQuery(nextQuery);
    void executeSearch(nextQuery, 1, 20, groups.length ? groups : undefined, { query: nextQuery, groups, data_source: "legacy" });
    // Initial route parameters are the trigger; executeSearch intentionally reads no mutable form state here.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialQuery, initialTermsKey]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!requestServiceAccess()) return;
    if (!query.trim() || parsedExpression.error || yearError || sourceUnavailable || parsedExpression.groups.length === 0 || searchState.isLoading) return;
    await executeSearch(query, 1, pageSize, parsedExpression.groups);
  }

  async function handlePageSizeChange(value: number) {
    const nextPageSize = PAGE_SIZES.includes(value as (typeof PAGE_SIZES)[number])
      ? (value as (typeof PAGE_SIZES)[number])
      : 20;
    setPageSize(nextPageSize);
    setPage(1);
    if (searchState.data || searchState.error) {
      const expression = submittedExpression ?? {
        query,
        groups: parsedExpression.groups
      };
      await executeSearch(expression.query, 1, nextPageSize, expression.groups, expression);
    }
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
                  <p>{literature ? "检索已提取文献的标题、摘要和 DOI，查看样品、性质与工艺" : "检索聚合物、配方、标题、权利要求与摘要中的知识记录"}</p>
                </div>
              </div>
              {modeNavigation}
            </header>

            <div className="ks-form-zone">
              <div className="ks-source-controls">
                <label className="ks-field"><span>数据来源</span><select aria-label="知识库数据来源" value={dataSource} onChange={(event) => changeSource(event.target.value as KnowledgeDataSource)}>
                  <option value="legacy">现有知识库</option><option value="extracted_literature">已提取文献</option>
                </select></label>
                <button className="ks-button" type="button" onClick={resetConditions}>重置条件</button>
              </div>
              {literature && (sourceUnavailable || sourceError) ? <div className="ks-inline-alert is-warning" role="status">
                <span>{sourceError || sourceInfo?.reason || "已提取文献尚未就绪；现有知识库仍可使用。"}</span>
                <button type="button" onClick={() => setSourceRetry((value) => value + 1)}>重新检查</button>
              </div> : null}
              {literature ? <div className="ks-literature-filters">
                <label className="ks-field"><span>起始年份</span><input aria-label="文献起始年份" inputMode="numeric" maxLength={4} value={filters.year_from} onChange={(event) => setFilters((value) => ({ ...value, year_from: event.target.value }))} /></label>
                <label className="ks-field"><span>结束年份</span><input aria-label="文献结束年份" inputMode="numeric" maxLength={4} value={filters.year_to} onChange={(event) => setFilters((value) => ({ ...value, year_to: event.target.value }))} /></label>
                <label className="ks-filter-check"><input type="checkbox" checked={filters.has_properties} onChange={(event) => setFilters((value) => ({ ...value, has_properties: event.target.checked }))} />有性质信息</label>
                <label className="ks-filter-check"><input type="checkbox" checked={filters.has_processes} onChange={(event) => setFilters((value) => ({ ...value, has_processes: event.target.checked }))} />有工艺信息</label>
                <label className="ks-filter-check"><input type="checkbox" checked={filters.has_original} onChange={(event) => setFilters((value) => ({ ...value, has_original: event.target.checked }))} />有可用原文</label>
                {yearError ? <p className="ks-filter-error" role="alert">{yearError}</p> : null}
              </div> : null}
              <form className="ks-search-form ks-local-form" onSubmit={handleSubmit}>
                <label className="ks-field">
                  <span>检索词 <small>{literature ? "标题 / 摘要 / DOI" : "关键词 / IUPAC / 配方"}</small></span>
                  <span className="ks-input-with-icon">
                    <Search aria-hidden="true" />
                    <input
                      type="search"
                      value={query}
                      onChange={(event) => setQuery(event.target.value)}
                      placeholder={literature ? "例如 polyimide；或粘贴完整 DOI" : "例如 polyimide；NMP | N-methyl-2-pyrrolidone"}
                      aria-label="本地知识库检索词"
                      aria-describedby="local-knowledge-expression-help"
                      aria-invalid={Boolean(query.trim() && parsedExpression.error)}
                      autoComplete="off"
                    />
                  </span>
                </label>
                <label className="ks-field">
                  <span>每页数量</span>
                  <select
                    value={pageSize}
                    onChange={(event) => void handlePageSizeChange(Number(event.target.value))}
                    aria-label="本地知识库每页数量"
                    disabled={searchState.isLoading}
                  >
                    {PAGE_SIZES.map((size) => <option key={size} value={size}>{size} 条</option>)}
                  </select>
                </label>
                <button
                  className="ks-button is-primary"
                  type="submit"
                  aria-busy={searchState.isLoading}
                  disabled={
                    !query.trim() ||
                    Boolean(parsedExpression.error) ||
                    Boolean(yearError) ||
                    Boolean(sourceUnavailable) ||
                    parsedExpression.groups.length === 0 ||
                    searchState.isLoading
                  }
                >
                  {searchState.isLoading ? <LoaderCircle className="is-spinning" aria-hidden="true" /> : <Search aria-hidden="true" />}
                  {searchState.isLoading ? "检索中" : "运行检索"}
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
                <span>{literature ? "范围：标题 / 摘要 / DOI；只查询已提取文献" : "范围：Polymer / Formulation / 中英文标题 / Claim / Abstract"}</span>
                <span>{literature ? "排序：DOI 精确命中 → 字段相关性 → 文献 ID" : "排序：全部条件命中 → 字段相关性 → 记录 ID"}</span>
              </div>
            </div>
          </section>

          <section className="ks-surface ks-results-surface" aria-busy={searchState.isLoading}>
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
    </div>
  );
}
