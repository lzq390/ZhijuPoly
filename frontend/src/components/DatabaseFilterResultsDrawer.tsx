import {
  AlertTriangle,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Copy,
  Database,
  LoaderCircle,
  RefreshCw,
  SearchX,
  X
} from "lucide-react";
import {
  memo,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  type Ref
} from "react";
import type {
  PropertyFilterRecord,
  PropertyFilterSearchResponse,
  PropertyFilterSearchResult
} from "../types";
import type { SubmittedPropertyFilter } from "../hooks/usePropertyFilter";

type DatabaseFilterResultsDrawerProps = {
  open: boolean;
  submitted: SubmittedPropertyFilter | null;
  data: PropertyFilterSearchResponse | null;
  loading: boolean;
  error: string | null;
  page: number;
  pageSize: number;
  matchedRecords: number;
  totalPages: number;
  width: number;
  onWidthChange: (width: number) => void;
  reopenButtonRef?: Ref<HTMLButtonElement>;
  onClose: () => void;
  onOpen: () => void;
  onRetry: () => void;
  onPageChange: (page: number) => void;
};

const MIN_DRAWER_WIDTH = 320;
const MAX_DRAWER_WIDTH = 560;
function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}

function formatNumber(value: number | null | undefined, maximumFractionDigits = 5) {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return value.toLocaleString("zh-CN", { maximumFractionDigits });
}

function displayRecordValue(record: PropertyFilterRecord) {
  if (record.canonical_value !== null) {
    return `${formatNumber(record.canonical_value)}${record.canonical_unit ? ` ${record.canonical_unit}` : ""}`;
  }
  if (record.property_value_num !== null) {
    const unit = record.property_unit_clean || record.property_unit_raw || "";
    return `${formatNumber(record.property_value_num)}${unit ? ` ${unit}` : ""}`;
  }
  return `${record.property_value || "—"}${record.property_unit_raw ? ` ${record.property_unit_raw}` : ""}`;
}

function recordTitle(record: PropertyFilterRecord) {
  return record.property_label || record.property_key || record.property_name || `条件 ${record.filter_index + 1}`;
}

function groupRecords(result: PropertyFilterSearchResult) {
  const grouped = new Map<number, PropertyFilterRecord[]>();
  result.records.forEach((record) => {
    const records = grouped.get(record.filter_index) ?? [];
    records.push(record);
    grouped.set(record.filter_index, records);
  });
  return grouped;
}

async function copyText(value: string) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(value);
    return;
  }
  const textarea = document.createElement("textarea");
  textarea.value = value;
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  textarea.select();
  document.execCommand("copy");
  textarea.remove();
}

const CopyButton = memo(function CopyButton({ value, label }: { value: string; label: string }) {
  const [copied, setCopied] = useState(false);
  const timerRef = useRef<number | null>(null);

  useEffect(
    () => () => {
      if (timerRef.current !== null) window.clearTimeout(timerRef.current);
    },
    []
  );

  async function handleCopy() {
    try {
      await copyText(value);
      setCopied(true);
      if (timerRef.current !== null) window.clearTimeout(timerRef.current);
      timerRef.current = window.setTimeout(() => setCopied(false), 1600);
    } catch {
      setCopied(false);
    }
  }

  return (
    <button className="dbf-copy-button" type="button" onClick={handleCopy} aria-label={`复制${label}`}>
      {copied ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
      <span>{copied ? "已复制" : "复制"}</span>
    </button>
  );
});

const RecordMetadata = memo(function RecordMetadata({ record }: { record: PropertyFilterRecord }) {
  return (
    <dl className="dbf-record-metadata">
      <div>
        <dt>原始测量</dt>
        <dd>{record.property_value || "—"}{record.property_unit_raw ? ` ${record.property_unit_raw}` : ""}</dd>
      </div>
      <div>
        <dt>数值来源</dt>
        <dd>{record.value_origin || "—"}</dd>
      </div>
      <div>
        <dt>可靠度</dt>
        <dd>{formatNumber(record.reliable_score, 3)}</dd>
      </div>
      <div>
        <dt>单位转换</dt>
        <dd>{record.unit_conversion_status || "—"}</dd>
      </div>
      <div>
        <dt>性质分类</dt>
        <dd>{record.property_category || "—"}</dd>
      </div>
      <div>
        <dt>标签来源</dt>
        <dd>{record.label_source || "—"}</dd>
      </div>
      <div>
        <dt>质量标记</dt>
        <dd>{record.soft_quality_flags || "无"}</dd>
      </div>
      <div>
        <dt>重复标记</dt>
        <dd>{record.duplicate_flag || "无"}</dd>
      </div>
      <div>
        <dt>来源行</dt>
        <dd>#{record.source_row_number.toLocaleString("zh-CN")}</dd>
      </div>
    </dl>
  );
});

function MeasurementDetails({ records }: { records: PropertyFilterRecord[] }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <details onToggle={(event) => setExpanded(event.currentTarget.open)}>
      <summary>
        记录详情{records.length > 1 ? ` · ${records.length} 条测量` : ""}
        <ChevronDown aria-hidden="true" />
      </summary>
      {expanded ? (
        <div className="dbf-measurement-list">
          {records.map((record, recordIndex) => (
            <section key={record.filter_record_id}>
              <header>
                <span>{recordIndex === 0 ? "主值" : `补充测量 ${recordIndex}`}</span>
                <strong>{recordTitle(record)} · {displayRecordValue(record)}</strong>
              </header>
              <RecordMetadata record={record} />
            </section>
          ))}
        </div>
      ) : null}
    </details>
  );
}

const ResultCard = memo(function ResultCard({
  result,
  rank,
  submitted
}: {
  result: PropertyFilterSearchResult;
  rank: number;
  submitted: SubmittedPropertyFilter;
}) {
  const groupedRecords = useMemo(() => groupRecords(result), [result]);
  const primarySmiles = result.canonical_smiles || result.smiles || "";
  const sameSmiles = Boolean(result.smiles && result.canonical_smiles && result.smiles === result.canonical_smiles);

  return (
    <article className="dbf-result-card">
      <div className="dbf-result-card-head">
        <span className="dbf-result-rank">#{rank}</span>
        <span className="dbf-result-match">
          {result.matched_filters}/{submitted.conditions.length} 条件命中
        </span>
      </div>
      <h3>{result.polymer_name || "未命名聚合物"}</h3>

      {primarySmiles ? (
        <div className="dbf-smiles-block">
          <div>
            <span>{sameSmiles ? "SMILES / canonical SMILES" : result.canonical_smiles ? "canonical SMILES" : "SMILES"}</span>
            <code title={primarySmiles}>{primarySmiles}</code>
          </div>
          <CopyButton value={primarySmiles} label="SMILES" />
        </div>
      ) : (
        <p className="dbf-result-muted">该记录未提供 SMILES。</p>
      )}

      {result.smiles && result.canonical_smiles && !sameSmiles ? (
        <div className="dbf-smiles-block is-secondary">
          <div>
            <span>SMILES</span>
            <code title={result.smiles}>{result.smiles}</code>
          </div>
          <CopyButton value={result.smiles} label="SMILES" />
        </div>
      ) : null}

      <div className="dbf-condition-values">
        {submitted.conditions.map((condition, conditionIndex) => {
          const records = groupedRecords.get(conditionIndex) ?? [];
          const primaryRecord = records[0];
          return (
            <div className="dbf-condition-value" key={`${condition.optionKey}-${conditionIndex}`}>
              <div className="dbf-condition-value-main">
                <span>{condition.expression}</span>
                <strong>{primaryRecord ? displayRecordValue(primaryRecord) : "未返回记录"}</strong>
              </div>
              {primaryRecord ? <MeasurementDetails records={records} /> : null}
            </div>
          );
        })}
      </div>
    </article>
  );
});

const ResultList = memo(function ResultList({
  data,
  page,
  pageSize,
  submitted
}: {
  data: PropertyFilterSearchResponse;
  page: number;
  pageSize: number;
  submitted: SubmittedPropertyFilter;
}) {
  return (
    <div className="dbf-result-list">
      {data.results.map((result, index) => (
        <ResultCard
          key={`${result.canonical_smiles || result.smiles || "record"}-${index}`}
          result={result}
          rank={(page - 1) * pageSize + index + 1}
          submitted={submitted}
        />
      ))}
    </div>
  );
});

function DrawerSkeleton() {
  return (
    <div className="dbf-result-skeletons" aria-label="正在加载筛选结果">
      {[0, 1, 2].map((index) => (
        <div className="dbf-result-skeleton" key={index}>
          <i />
          <i />
          <i />
          <i />
        </div>
      ))}
    </div>
  );
}

export function DatabaseFilterResultsDrawer({
  open,
  submitted,
  data,
  loading,
  error,
  page,
  pageSize,
  matchedRecords,
  totalPages,
  width,
  onWidthChange,
  reopenButtonRef,
  onClose,
  onOpen,
  onRetry,
  onPageChange
}: DatabaseFilterResultsDrawerProps) {
  const [resizing, setResizing] = useState(false);
  const drawerRef = useRef<HTMLElement | null>(null);
  const dragState = useRef<{
    startX: number;
    startWidth: number;
    root: HTMLElement | null;
  } | null>(null);
  const pendingWidth = useRef(width);
  const resizeFrame = useRef<number | null>(null);

  useEffect(() => {
    if (!resizing) return;

    function handlePointerMove(event: PointerEvent) {
      if (!dragState.current) return;
      const delta = dragState.current.startX - event.clientX;
      pendingWidth.current = clamp(
        dragState.current.startWidth + delta,
        MIN_DRAWER_WIDTH,
        MAX_DRAWER_WIDTH
      );
      if (resizeFrame.current !== null) return;
      resizeFrame.current = window.requestAnimationFrame(() => {
        resizeFrame.current = null;
        const value = `${pendingWidth.current}px`;
        dragState.current?.root?.style.setProperty("--dbf-drawer-width", value);
        drawerRef.current?.style.setProperty("--dbf-drawer-width", value);
      });
    }

    function stopResize() {
      if (resizeFrame.current !== null) {
        window.cancelAnimationFrame(resizeFrame.current);
        resizeFrame.current = null;
      }
      const finalWidth = pendingWidth.current;
      dragState.current?.root?.classList.remove("dbf-is-resizing");
      dragState.current = null;
      setResizing(false);
      onWidthChange(finalWidth);
    }

    window.addEventListener("pointermove", handlePointerMove);
    window.addEventListener("pointerup", stopResize);
    window.addEventListener("pointercancel", stopResize);
    return () => {
      window.removeEventListener("pointermove", handlePointerMove);
      window.removeEventListener("pointerup", stopResize);
      window.removeEventListener("pointercancel", stopResize);
      if (resizeFrame.current !== null) window.cancelAnimationFrame(resizeFrame.current);
      dragState.current?.root?.classList.remove("dbf-is-resizing");
    };
  }, [onWidthChange, resizing]);

  useEffect(() => {
    function handleEscape(event: KeyboardEvent) {
      if (event.key === "Escape" && open) onClose();
    }
    window.addEventListener("keydown", handleEscape);
    return () => window.removeEventListener("keydown", handleEscape);
  }, [onClose, open]);

  function startResize(event: ReactPointerEvent<HTMLDivElement>) {
    event.preventDefault();
    const root = event.currentTarget.closest<HTMLElement>(".database-filter-page");
    pendingWidth.current = width;
    dragState.current = { startX: event.clientX, startWidth: width, root };
    root?.classList.add("dbf-is-resizing");
    setResizing(true);
  }

  function handleResizeKey(event: ReactKeyboardEvent<HTMLDivElement>) {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    const amount = event.shiftKey ? 40 : 10;
    onWidthChange(clamp(width + (event.key === "ArrowLeft" ? amount : -amount), MIN_DRAWER_WIDTH, MAX_DRAWER_WIDTH));
  }

  const hasResults = Boolean(data && data.results.length > 0);
  const status = loading
    ? "正在查询 PostgreSQL…"
    : error
      ? "查询失败"
      : data
        ? `${matchedRecords.toLocaleString("zh-CN")} 个聚合物 · ${formatNumber(data.query_time_ms, 1)} ms`
        : "等待运行筛选";

  return (
    <>
      {submitted && !open ? (
        <button
          ref={reopenButtonRef}
          className="dbf-drawer-reopen"
          type="button"
          onClick={onOpen}
          aria-expanded="false"
          aria-controls="database-filter-results"
        >
          <Database aria-hidden="true" />
          <span>查看结果</span>
          {data ? <b>{matchedRecords.toLocaleString("zh-CN")}</b> : null}
        </button>
      ) : null}

      <aside
        ref={drawerRef}
        id="database-filter-results"
        className={`dbf-results-drawer${open ? " is-open" : ""}${resizing ? " is-resizing" : ""}`}
        style={{ "--dbf-drawer-width": `${width}px` } as CSSProperties}
        aria-labelledby="database-filter-results-title"
        aria-hidden={!open}
        inert={!open}
      >
        <div
          className="dbf-drawer-resizer"
          role="separator"
          tabIndex={open ? 0 : -1}
          aria-label="调整结果抽屉宽度"
          aria-orientation="vertical"
          aria-valuemin={MIN_DRAWER_WIDTH}
          aria-valuemax={MAX_DRAWER_WIDTH}
          aria-valuenow={Math.round(width)}
          onPointerDown={startResize}
          onKeyDown={handleResizeKey}
        />

        <header className="dbf-drawer-header">
          <div className="dbf-drawer-title">
            <span><Database aria-hidden="true" /></span>
            <div>
              <h2 id="database-filter-results-title">筛选结果</h2>
              <p>{status}</p>
            </div>
          </div>
          <button className="dbf-icon-button" type="button" onClick={onClose} aria-label="关闭筛选结果">
            <X aria-hidden="true" />
          </button>
        </header>

        {open && submitted ? (
          <div className="dbf-result-context">
            <span>已提交条件</span>
            <strong>{submitted.expression}</strong>
          </div>
        ) : null}

        {open ? <div className="dbf-drawer-body">
          {loading ? <DrawerSkeleton /> : null}

          {!loading && error ? (
            <div className="dbf-drawer-state is-error" role="alert">
              <span><AlertTriangle aria-hidden="true" /></span>
              <h3>暂时无法完成筛选</h3>
              <p>{error}</p>
              <button type="button" onClick={onRetry}>
                <RefreshCw aria-hidden="true" />
                重试本次查询
              </button>
            </div>
          ) : null}

          {!loading && !error && data && !hasResults ? (
            <div className="dbf-drawer-state">
              <span><SearchX aria-hidden="true" /></span>
              <h3>没有找到匹配记录</h3>
              <p>可以放宽阈值区间，或清除关键词后重新运行。</p>
            </div>
          ) : null}

          {!loading && !error && data && hasResults && submitted ? (
            <ResultList data={data} page={page} pageSize={pageSize} submitted={submitted} />
          ) : null}

          {!loading && !error && !data ? (
            <div className="dbf-drawer-state">
              <span><LoaderCircle aria-hidden="true" /></span>
              <h3>等待筛选条件</h3>
              <p>填写至少一个阈值并运行筛选后，结果会显示在这里。</p>
            </div>
          ) : null}
        </div> : null}

        {open && !loading && !error && data && hasResults ? (
          <footer className="dbf-drawer-pagination">
            <button
              type="button"
              onClick={() => onPageChange(page - 1)}
              disabled={page <= 1}
              aria-label="上一页"
            >
              <ChevronLeft aria-hidden="true" />
            </button>
            <span>第 <strong>{page}</strong> / {totalPages.toLocaleString("zh-CN")} 页</span>
            <button
              type="button"
              onClick={() => onPageChange(page + 1)}
              disabled={page >= totalPages}
              aria-label="下一页"
            >
              <ChevronRight aria-hidden="true" />
            </button>
          </footer>
        ) : null}
      </aside>
    </>
  );
}
