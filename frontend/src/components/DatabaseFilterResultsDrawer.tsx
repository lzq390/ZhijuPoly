import {
  AlertTriangle,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Copy,
  Database,
  LoaderCircle,
  PanelRightOpen,
  RefreshCw,
  SearchX
} from "lucide-react";
import {
  memo,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState
} from "react";
import type {
  PropertyFilterRecord,
  PropertyFilterSearchResponse,
  PropertyFilterSearchResult
} from "../types";
import type { SubmittedPropertyFilter } from "../hooks/usePropertyFilter";
import { usePropertyFilterObservation } from "../hooks/usePropertyFilterObservation";
import { WorkbenchDrawerShell } from "./structure-workbench/WorkbenchDrawerShell";

export type DatabaseFilterDrawerProfile = {
  defaultWidth: number;
  minWidth: number;
  maxWidth: number;
  keyboardStep: number;
  overlayContainerWidth: number;
};

const STANDARD_DRAWER_PROFILE: DatabaseFilterDrawerProfile = {
  defaultWidth: 380,
  minWidth: 320,
  maxWidth: 560,
  keyboardStep: 10,
  overlayContainerWidth: 1360
};

const TWO_K_DRAWER_PROFILE: DatabaseFilterDrawerProfile = {
  defaultWidth: 540,
  minWidth: 480,
  maxWidth: 720,
  keyboardStep: 15,
  overlayContainerWidth: 2050
};

const TWO_K_QUERY = "(min-width: 2000px) and (min-height: 1120px)";

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}

function mapDrawerWidth(
  width: number,
  from: DatabaseFilterDrawerProfile,
  to: DatabaseFilterDrawerProfile
) {
  const ratio = (width - from.minWidth) / Math.max(1, from.maxWidth - from.minWidth);
  return Math.round(to.minWidth + clamp(ratio, 0, 1) * (to.maxWidth - to.minWidth));
}

export function useDatabaseFilterDrawerSizing() {
  const initialProfile = typeof window !== "undefined"
    && typeof window.matchMedia === "function"
    && window.matchMedia(TWO_K_QUERY).matches
    ? TWO_K_DRAWER_PROFILE
    : STANDARD_DRAWER_PROFILE;
  const [sizing, setSizing] = useState(() => ({
    profile: initialProfile,
    width: initialProfile.defaultWidth
  }));
  const setWidth = useCallback((width: number) => {
    setSizing((current) => current.width === width ? current : { ...current, width });
  }, []);

  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const media = window.matchMedia(TWO_K_QUERY);
    const update = () => {
      const nextProfile = media.matches ? TWO_K_DRAWER_PROFILE : STANDARD_DRAWER_PROFILE;
      setSizing((current) => current.profile === nextProfile
        ? current
        : {
            profile: nextProfile,
            width: mapDrawerWidth(current.width, current.profile, nextProfile)
          });
    };
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);

  return { width: sizing.width, setWidth, profile: sizing.profile };
}

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
  profile: DatabaseFilterDrawerProfile;
  onWidthChange: (width: number) => void;
  onClose: () => void;
  onOpen: (trigger?: HTMLElement) => void;
  onRetry: () => void;
  onPageChange: (page: number) => void;
};

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
    <button className="dbf-copy-button" type="button" onClick={handleCopy} aria-label={`复制 ${label}`}>
      {copied ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
      <span>{copied ? "已复制" : "复制"}</span>
    </button>
  );
});

const SmilesDetails = memo(function SmilesDetails({
  value,
  label,
  onOpen,
  secondary = false
}: {
  value: string;
  label: string;
  onOpen: () => void;
  secondary?: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const wasExpanded = useRef(false);

  return (
    <details
      className={`dbf-smiles-details${secondary ? " is-secondary" : ""}`}
      onToggle={(event) => {
        const open = event.currentTarget.open;
        setExpanded(open);
        if (open && !wasExpanded.current) onOpen();
        wasExpanded.current = open;
      }}
    >
      <summary>
        <span className="dbf-smiles-summary-label">
          <strong>{label}</strong>
          {!expanded ? <code className="dbf-smiles-preview">{value}</code> : null}
        </span>
        <span className="dbf-smiles-toggle">
          {expanded ? "收起" : "展开"}
          <ChevronDown aria-hidden="true" />
        </span>
      </summary>
      {expanded ? (
        <div className="dbf-smiles-content">
          <code>{value}</code>
          <CopyButton value={value} label={label} />
        </div>
      ) : null}
    </details>
  );
});

const ResultTitle = memo(function ResultTitle({ value }: { value: string }) {
  const titleRef = useRef<HTMLHeadingElement>(null);
  const [expanded, setExpanded] = useState(false);
  const [canExpand, setCanExpand] = useState(value.length > 48);

  useEffect(() => {
    if (expanded) return;
    const title = titleRef.current;
    if (!title) return;
    const update = () => {
      setCanExpand(title.scrollWidth > title.clientWidth || value.length > 48);
    };
    update();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(update);
    observer.observe(title);
    return () => observer.disconnect();
  }, [expanded, value]);

  return (
    <div className={`dbf-result-title${expanded ? " is-expanded" : ""}`}>
      <h3 ref={titleRef}>{value}</h3>
      {canExpand ? (
        <button
          type="button"
          aria-expanded={expanded}
          aria-label={expanded ? "收起完整标题" : "展开完整标题"}
          onClick={() => setExpanded((current) => !current)}
        >
          <span>{expanded ? "收起" : "展开"}</span>
          <ChevronDown aria-hidden="true" />
        </button>
      ) : null}
    </div>
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
        <dt>原始记录序号</dt>
        <dd>#{record.source_row_number.toLocaleString("zh-CN")}</dd>
      </div>
    </dl>
  );
});

function MeasurementDetails({ records, onOpen }: { records: PropertyFilterRecord[]; onOpen: () => void }) {
  const [expanded, setExpanded] = useState(false);
  const wasExpanded = useRef(false);
  return (
    <details onToggle={(event) => {
      const open = event.currentTarget.open;
      setExpanded(open);
      if (open && !wasExpanded.current) onOpen();
      wasExpanded.current = open;
    }}>
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
  searchId,
  resultIndex,
  submitted
}: {
  result: PropertyFilterSearchResult;
  rank: number;
  searchId?: string;
  resultIndex: number;
  submitted: SubmittedPropertyFilter;
}) {
  const observe = usePropertyFilterObservation(searchId, resultIndex);
  const groupedRecords = useMemo(() => groupRecords(result), [result]);
  const primarySmiles = result.canonical_smiles || result.smiles || "";
  const sameSmiles = Boolean(result.smiles && result.canonical_smiles && result.smiles === result.canonical_smiles);

  return (
    <article className="dbf-result-card">
      <div className="dbf-result-card-head">
        <span className="dbf-result-rank">#{rank}</span>
        <span className="dbf-result-match">
          {result.matched_filters}/{submitted.conditions.length} 条件满足
        </span>
      </div>
      <ResultTitle value={result.polymer_name || "未命名聚合物"} />

      {primarySmiles ? (
        <SmilesDetails
          value={primarySmiles}
          onOpen={() => observe({ source: "smiles", smiles_field: result.canonical_smiles ? "canonical_smiles" : "smiles" })}
          label={sameSmiles ? "SMILES / canonical SMILES" : result.canonical_smiles ? "canonical SMILES" : "SMILES"}
        />
      ) : (
        <p className="dbf-result-muted">该记录未提供 SMILES。</p>
      )}

      {result.smiles && result.canonical_smiles && !sameSmiles ? (
        <SmilesDetails value={result.smiles} label="SMILES" secondary
          onOpen={() => observe({ source: "smiles", smiles_field: "smiles" })} />
      ) : null}

      <div className="dbf-condition-values">
        {submitted.conditions.map((condition, conditionIndex) => {
          const records = groupedRecords.get(conditionIndex) ?? [];
          const primaryRecord = records[0];
          return (
            <div className="dbf-condition-value" key={`${condition.optionKey}-${conditionIndex}`}>
              <div className="dbf-condition-value-main">
                <span>{condition.expression}</span>
                <strong>{primaryRecord ? displayRecordValue(primaryRecord) : "暂无匹配值"}</strong>
              </div>
              {primaryRecord ? <MeasurementDetails records={records}
                onOpen={() => observe({ source: "measurement_details", filter_index: conditionIndex })} /> : null}
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
          key={`${data.search_id ?? ""}-${result.canonical_smiles || result.smiles || "record"}-${index}`}
          result={result}
          searchId={data.search_id}
          resultIndex={index}
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
  profile,
  onWidthChange,
  onClose,
  onOpen,
  onRetry,
  onPageChange
}: DatabaseFilterResultsDrawerProps) {
  const hasResults = Boolean(data && data.results.length > 0);
  const status = loading
    ? "正在筛选…"
    : error
      ? "筛选未完成"
      : data
        ? `${matchedRecords.toLocaleString("zh-CN")} 个聚合物`
        : "尚未筛选";

  return (
    <WorkbenchDrawerShell
      open={open}
      hasRun={Boolean(submitted)}
      width={width}
      minWidth={profile.minWidth}
      maxWidth={profile.maxWidth}
      keyboardStep={profile.keyboardStep}
      overlayContainerWidth={profile.overlayContainerWidth + Math.max(0, width - profile.defaultWidth)}
      title="筛选结果"
      status={status}
      headerIcon={<Database aria-hidden="true" />}
      reopenIcon={<PanelRightOpen aria-hidden="true" />}
      reopenLabel="查看结果"
      reopenVariant="side-handle"
      closeLabel="关闭筛选结果"
      resizeLabel="调整筛选结果区域宽度"
      onWidthChange={onWidthChange}
      onClose={onClose}
      onOpen={onOpen}
    >
      <div className="dbf-drawer-content">
        {submitted ? (
          <div className="dbf-result-context">
            <span>本次筛选条件</span>
            <strong>{submitted.expression}</strong>
          </div>
        ) : null}

        <div className="dbf-drawer-body">
          {loading ? <DrawerSkeleton /> : null}

          {!loading && error ? (
            <div className="dbf-drawer-state is-error" role="alert">
              <span><AlertTriangle aria-hidden="true" /></span>
              <h3>暂时无法完成筛选</h3>
              <p>{error}</p>
              <button type="button" onClick={onRetry}>
                <RefreshCw aria-hidden="true" />
                重新筛选
              </button>
            </div>
          ) : null}

          {!loading && !error && data && !hasResults ? (
            <div className="dbf-drawer-state">
              <span><SearchX aria-hidden="true" /></span>
              <h3>没有找到匹配记录</h3>
              <p>可以放宽筛选范围，或清除关键词后重新运行。</p>
            </div>
          ) : null}

          {!loading && !error && data && hasResults && submitted ? (
            <ResultList data={data} page={page} pageSize={pageSize} submitted={submitted} />
          ) : null}

          {!loading && !error && !data ? (
            <div className="dbf-drawer-state">
              <span><LoaderCircle aria-hidden="true" /></span>
              <h3>等待筛选条件</h3>
              <p>填写最小值或最大值并运行筛选后，结果会显示在这里。</p>
            </div>
          ) : null}
        </div>

        {!loading && !error && data && hasResults ? (
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
      </div>
    </WorkbenchDrawerShell>
  );
}
