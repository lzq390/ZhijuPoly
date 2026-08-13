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
import { type CSSProperties, type FormEvent, type ReactNode, useEffect, useMemo, useState } from "react";
import { useKnowledgeSearch } from "../../hooks/useKnowledgeSearch";
import type { KnowledgeDocumentResult } from "../../types";
import { KnowledgeDetailDrawer, type KnowledgeDrawerTab } from "./KnowledgeDetailDrawer";

type LocalKnowledgePanelProps = {
  initialQuery?: string;
  initialTerms?: string[];
  modeNavigation: ReactNode;
};

const PAGE_SIZES = [20, 50, 100] as const;

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

export function LocalKnowledgePanel({ initialQuery = "", initialTerms = [], modeNavigation }: LocalKnowledgePanelProps) {
  const searchState = useKnowledgeSearch();
  const [query, setQuery] = useState(initialQuery);
  const [activeTerms, setActiveTerms] = useState<string[]>(() => normalizeTerms(initialTerms));
  const [pageSize, setPageSize] = useState<(typeof PAGE_SIZES)[number]>(20);
  const [page, setPage] = useState(1);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [drawerWidth, setDrawerWidth] = useState(380);
  const selectedRecord = searchState.data?.results.find((record) => record.knowledge_id === selectedId) ?? null;
  const initialTermsKey = initialTerms.join("\u0000");
  const totalPages = Math.max(1, Math.ceil((searchState.data?.total ?? 0) / pageSize));
  const highlightTerms = searchState.data?.terms.length
    ? searchState.data.terms
    : activeTerms.length
      ? activeTerms
      : [searchState.data?.query || query];

  async function executeSearch(nextQuery: string, nextPage: number, nextPageSize: number, terms = activeTerms) {
    const normalizedQuery = nextQuery.trim() || terms.join(" OR ");
    if (!normalizedQuery) return;
    setPage(nextPage);
    setSelectedId(null);
    setDrawerOpen(false);
    const response = await searchState.submit(normalizedQuery, nextPageSize, terms, nextPage, nextPageSize);
    if (!response?.results.length) return;
    setSelectedId(response.results[0].knowledge_id);
    if (isDesktopViewport()) setDrawerOpen(true);
  }

  useEffect(() => {
    const terms = normalizeTerms(initialTerms);
    const nextQuery = initialQuery.trim() || terms.join(" OR ");
    if (!nextQuery) return;
    setQuery(nextQuery);
    setActiveTerms(terms);
    void executeSearch(nextQuery, 1, 20, terms);
    // Initial route parameters are the trigger; executeSearch intentionally reads no mutable form state here.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialQuery, initialTermsKey]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!query.trim() || searchState.isLoading) return;
    await executeSearch(query, 1, pageSize);
  }

  async function handlePageSizeChange(value: number) {
    const nextPageSize = PAGE_SIZES.includes(value as (typeof PAGE_SIZES)[number])
      ? (value as (typeof PAGE_SIZES)[number])
      : 20;
    setPageSize(nextPageSize);
    setPage(1);
    if (searchState.data || searchState.error) {
      await executeSearch(query, 1, nextPageSize);
    }
  }

  const resultCountLabel = searchState.data
    ? `${searchState.data.total.toLocaleString("zh-CN")} 条记录 · ${searchState.data.query_time_ms.toFixed(1)} ms · 相关性排序`
    : "支持关键词、IUPAC 名称与配方术语";
  const drawerTabs = useMemo(() => (selectedRecord ? buildLocalTabs(selectedRecord) : []), [selectedRecord]);

  return (
    <div
      className={`ks-panel-layout${drawerOpen ? " is-drawer-open" : ""}`}
      style={{ "--ks-drawer-width": `${drawerWidth}px` } as CSSProperties}
    >
      <div className="ks-panel-scroll">
        <div className="ks-workbench-column">
          <div className="ks-module-toolbar">
            <span className="ks-toolbar-status"><i />准备就绪</span>
          </div>

          <section className="ks-surface ks-search-surface">
            <header className="ks-surface-header">
              <div className="ks-surface-heading">
                <span className="ks-surface-mark"><Database aria-hidden="true" /></span>
                <div className="ks-surface-copy">
                  <h2>本地知识库检索</h2>
                  <p>检索聚合物、配方、标题、权利要求与摘要中的知识记录</p>
                </div>
              </div>
              {modeNavigation}
            </header>

            <div className="ks-form-zone">
              <form className="ks-search-form ks-local-form" onSubmit={handleSubmit}>
                <label className="ks-field">
                  <span>检索词 <small>关键词 / IUPAC / 配方</small></span>
                  <span className="ks-input-with-icon">
                    <Search aria-hidden="true" />
                    <input
                      type="search"
                      value={query}
                      onChange={(event) => {
                        setQuery(event.target.value);
                        setActiveTerms([]);
                      }}
                      placeholder="例如 polyimide、IUPAC 名称或配方术语"
                      aria-label="本地知识库检索词"
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
                <button className="ks-button is-primary" type="submit" disabled={!query.trim() || searchState.isLoading}>
                  {searchState.isLoading ? <LoaderCircle className="is-spinning" aria-hidden="true" /> : <Search aria-hidden="true" />}
                  运行检索
                </button>
              </form>

              <div className="ks-meta-row">
                <span>范围：Polymer / Formulation / 中英文标题 / Claim / Abstract</span>
                <span>排序：命中术语数 → 字段权重 → 记录 ID</span>
              </div>
            </div>
          </section>

          <section className="ks-surface ks-results-surface" aria-busy={searchState.isLoading}>
            <header className="ks-results-header">
              <div className="ks-results-summary">
                <h2>{searchState.data ? "本地检索结果" : "准备检索本地知识库"}</h2>
                <p>{searchState.isLoading ? "正在查询 PostgreSQL 知识库…" : resultCountLabel}</p>
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
                  <h3>本地知识库暂时不可用</h3>
                  <p>{searchState.error}</p>
                  <button className="ks-button" type="button" onClick={() => void executeSearch(query, page, pageSize)}>
                    <RefreshCw aria-hidden="true" />重试检索
                  </button>
                </div>
              ) : null}

              {!searchState.isLoading && !searchState.error && !searchState.data ? (
                <div className="ks-state">
                  <span><Search aria-hidden="true" /></span>
                  <h3>输入检索词开始查询</h3>
                  <p>可检索聚合物名称、IUPAC 名称、配方、标题、权利要求和摘要。</p>
                </div>
              ) : null}

              {!searchState.isLoading && searchState.data?.results.length === 0 ? (
                <div className="ks-state">
                  <span><SearchX aria-hidden="true" /></span>
                  <h3>没有匹配的知识记录</h3>
                  <p>请尝试更短的材料名称、IUPAC 片段或配方术语。</p>
                </div>
              ) : null}

              {!searchState.isLoading && searchState.data?.results.length ? (
                <>
                  <div className="ks-result-list">
                    {searchState.data.results.map((record) => {
                      const selected = record.knowledge_id === selectedId;
                      return (
                        <button
                          key={record.knowledge_id}
                          className={`ks-result-card${selected ? " is-selected" : ""}`}
                          type="button"
                          aria-pressed={selected}
                          onClick={() => {
                            setSelectedId(record.knowledge_id);
                            setDrawerOpen(true);
                          }}
                        >
                          <span className="ks-card-topline">
                            <span className="ks-card-heading">
                              <strong><HighlightedText text={record.title_zh || record.title_en || `知识记录 #${record.knowledge_id}`} terms={highlightTerms} /></strong>
                              {record.title_zh && record.title_en ? <small><HighlightedText text={record.title_en} terms={highlightTerms} /></small> : null}
                            </span>
                            <span className="ks-card-status-group">
                              {selected ? <span className="ks-selected-indicator"><Check aria-hidden="true" />已选中</span> : null}
                              <span className="ks-record-id">#{record.knowledge_id}</span>
                            </span>
                          </span>
                          <span className="ks-card-snippet"><HighlightedText text={record.abstract_snippet} terms={highlightTerms} /></span>
                          <span className="ks-chip-row">
                            {record.matched_terms.map((term) => <span className="ks-chip is-match" key={`term-${term}`}>{term}</span>)}
                            {record.matched_fields.map((field) => <span className="ks-chip" key={`field-${field}`}>{field}</span>)}
                          </span>
                          <span className="ks-fact-grid">
                            <span><small>Polymer</small><b>{optional(record.polymer_iupac)}</b></span>
                            <span><small>Temperature</small><b>{optional(record.temperature)}</b></span>
                            <span><small>Time</small><b>{optional(record.reaction_time)}</b></span>
                            <span><small>Solvent</small><b>{optional(record.solvent)}</b></span>
                          </span>
                        </button>
                      );
                    })}
                  </div>

                  <div className="ks-pagination">
                    <span>第 {page} / {totalPages.toLocaleString("zh-CN")} 页 · 共 {searchState.data.total.toLocaleString("zh-CN")} 条</span>
                    <div>
                      <button className="ks-button" type="button" disabled={page <= 1 || searchState.isLoading} onClick={() => void executeSearch(query, page - 1, pageSize)}>
                        <ChevronLeft aria-hidden="true" />上一页
                      </button>
                      <button className="ks-button" type="button" disabled={page >= totalPages || searchState.isLoading} onClick={() => void executeSearch(query, page + 1, pageSize)}>
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

      <KnowledgeDetailDrawer
        id="local-knowledge-detail"
        open={drawerOpen && Boolean(selectedRecord)}
        width={drawerWidth}
        contentKey={selectedRecord ? `local-${selectedRecord.knowledge_id}` : "local-empty"}
        title="知识记录详情"
        subtitle={selectedRecord ? `#${selectedRecord.knowledge_id} · 本地知识库` : "选择结果后查看"}
        icon={<BookOpenText aria-hidden="true" />}
        tabs={drawerTabs}
        footer={selectedRecord ? (
          <><span><Clock3 aria-hidden="true" />记录 {selectedRecord.knowledge_id} · 可追溯字段完整</span><button className="ks-button" type="button" onClick={() => void navigator.clipboard?.writeText(`${selectedRecord.source_file}:${selectedRecord.source_row_number}`)}><Copy aria-hidden="true" />复制定位</button></>
        ) : undefined}
        reopenLabel="查看记录详情"
        verticalReopen
        showReopen={Boolean(selectedRecord)}
        onWidthChange={setDrawerWidth}
        onClose={() => setDrawerOpen(false)}
        onOpen={() => setDrawerOpen(true)}
      />
    </div>
  );
}
