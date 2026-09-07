import {
  ChevronDown,
  ChevronUp,
  Clock3,
  Database,
  FlaskConical,
  LoaderCircle,
  PanelRightOpen,
  Search,
  Table2,
  TriangleAlert,
  X
} from "lucide-react";
import { useId, useMemo, useState } from "react";
import type {
  SmilesLookupResponse,
  SmilesLookupResult,
  SmilesLookupTable
} from "../../types";
import { StructureSvg } from "../StructureSvg";
import { WorkbenchDrawerShell } from "../structure-workbench/WorkbenchDrawerShell";
import {
  DATABASE_QUERY_FIELD_LABELS,
  DATABASE_QUERY_TABLE_META,
  formatDatabaseLookupValue
} from "./config";

export type DatabaseQuerySnapshot = {
  smiles: string;
  table: SmilesLookupTable;
};

type DatabaseQueryDrawerProps = {
  open: boolean;
  hasAttempt: boolean;
  width: number;
  preparing: boolean;
  loading: boolean;
  error: string | null;
  data: SmilesLookupResponse | null;
  snapshot: DatabaseQuerySnapshot | null;
  stale: boolean;
  propertyNameFilter: string;
  onPropertyNameFilterChange: (value: string) => void;
  onWidthChange: (width: number) => void;
  onClose: () => void;
  onOpen: () => void;
  onAdjustParameters: () => void;
};

const HIDDEN_DETAIL_FIELDS = new Set([
  "rdkit_parse_ok",
  "polymer_id",
  "property_id",
  "property_value",
  "pi_id",
  "property_count"
]);

function getLookupPropertyName(result: SmilesLookupResult) {
  const value = result.fields.property_name;
  return typeof value === "string" ? value : "";
}

function SubmittedStructureSummary({ smiles }: { smiles: string }) {
  const contentId = useId();
  const collapsible = smiles.length > 72;
  const [expanded, setExpanded] = useState(!collapsible);

  return (
    <div className="np-dq-result-summary__structure">
      <div className="np-dq-result-summary__structure-header">
        <span><FlaskConical aria-hidden="true" /> 提交结构</span>
        {collapsible ? (
          <button
            type="button"
            aria-expanded={expanded}
            aria-controls={contentId}
            aria-label={expanded ? "收起完整提交结构" : "展开完整提交结构"}
            onClick={() => setExpanded((current) => !current)}
          >
            {expanded ? <ChevronUp aria-hidden="true" /> : <ChevronDown aria-hidden="true" />}
            {expanded ? "收起" : "展开"}
          </button>
        ) : null}
      </div>
      <code
        id={contentId}
        className={collapsible && !expanded ? "is-collapsed" : undefined}
        title={collapsible && !expanded ? smiles : undefined}
      >
        {smiles}
      </code>
    </div>
  );
}

function DatabaseLookupResultCard({ result }: { result: SmilesLookupResult }) {
  const displaySmiles = result.canonical_smiles || result.smiles;
  const hasDistinctCanonical = Boolean(
    result.canonical_smiles && result.canonical_smiles !== result.smiles
  );
  const propertyCount = result.fields.property_count;
  const detailFields = Object.entries(result.fields).filter(
    ([key, value]) => !HIDDEN_DETAIL_FIELDS.has(key) && value !== null && value !== ""
  );

  return (
    <article className="np-dq-result-card">
      <header>
        <span>ID {result.record_id}</span>
        <strong>{result.summary || "数据库精确匹配记录"}</strong>
      </header>
      <div className="np-dq-result-card__structure">
        {result.structure_svg ? (
          <StructureSvg
            svg={result.structure_svg}
            alt={result.summary || `${displaySmiles} 的二维结构`}
            className="np-dq-result-svg"
            imageClassName="np-dq-result-svg__image"
          />
        ) : (
          <code>{displaySmiles}</code>
        )}
      </div>
      <div className="np-dq-result-card__smiles">
        <span>{hasDistinctCanonical ? "规范化 SMILES" : "匹配 SMILES"}</span>
        <code>{displaySmiles}</code>
        {hasDistinctCanonical ? (
          <p><span>原始匹配</span><code>{result.smiles}</code></p>
        ) : null}
      </div>
      {propertyCount !== null && propertyCount !== undefined && propertyCount !== "" ? (
        <dl className="np-dq-result-card__count">
          <div><dt>性能条目</dt><dd>{formatDatabaseLookupValue(propertyCount)}</dd></div>
        </dl>
      ) : null}
      {detailFields.length ? (
        <dl className="np-dq-result-card__fields">
          {detailFields.map(([key, value]) => (
            <div key={key}>
              <dt>{DATABASE_QUERY_FIELD_LABELS[key] ?? key}</dt>
              <dd title={formatDatabaseLookupValue(value)}>{formatDatabaseLookupValue(value)}</dd>
            </div>
          ))}
        </dl>
      ) : null}
    </article>
  );
}

function EmptyResult({
  title,
  description,
  actionLabel,
  onAction
}: {
  title: string;
  description: string;
  actionLabel?: string;
  onAction?: () => void;
}) {
  return (
    <div className="np-sw-result-state">
      <span><Database aria-hidden="true" /></span>
      <strong>{title}</strong>
      <p>{description}</p>
      {actionLabel && onAction ? (
        <button type="button" className="np-sw-secondary-button" onClick={onAction}>{actionLabel}</button>
      ) : null}
    </div>
  );
}

export function DatabaseQueryDrawer({
  open,
  hasAttempt,
  width,
  preparing,
  loading,
  error,
  data,
  snapshot,
  stale,
  propertyNameFilter,
  onPropertyNameFilterChange,
  onWidthChange,
  onClose,
  onOpen,
  onAdjustParameters
}: DatabaseQueryDrawerProps) {
  const filteredResults = useMemo(() => {
    const results = data?.results ?? [];
    const normalized = propertyNameFilter.trim().toLowerCase();
    if (snapshot?.table !== "properties" || !normalized) return results;
    return results.filter((result) =>
      getLookupPropertyName(result).trim().toLowerCase().startsWith(normalized)
    );
  }, [data?.results, propertyNameFilter, snapshot?.table]);
  const showPropertyFilter = snapshot?.table === "properties" && Boolean(data?.results.length);
  const tableMeta = snapshot ? DATABASE_QUERY_TABLE_META[snapshot.table] : null;
  const status = preparing
    ? "正在准备查询结构"
    : loading
      ? "数据库查询运行中"
      : error
        ? "数据库查询失败"
        : data
          ? data.exists ? `${data.total} 条精确匹配` : "未找到精确匹配"
          : "等待数据库查询";

  return (
    <WorkbenchDrawerShell
      open={open}
      hasRun={hasAttempt}
      width={width}
      title="数据库查询结果"
      status={status}
      headerIcon={<Database aria-hidden="true" />}
      reopenIcon={<PanelRightOpen aria-hidden="true" />}
      reopenLabel="展开数据库查询结果"
      reopenVariant="side-handle"
      closeLabel="关闭数据库查询结果"
      resizeLabel="调整数据库查询结果抽屉宽度"
      onWidthChange={onWidthChange}
      onClose={onClose}
      onOpen={onOpen}
    >
      {preparing ? (
        <div className="np-sw-result-state">
          <span><LoaderCircle className="np-sw-spin" /></span>
          <strong>正在准备查询结构</strong>
          <p>正在同步并标准化当前画板结构。</p>
        </div>
      ) : loading ? (
        <div className="np-dq-loading-list" aria-label="数据库查询运行中">
          {[0, 1, 2].map((item) => <span key={item} />)}
        </div>
      ) : error ? (
        <div className="np-sw-result-state is-danger">
          <span><TriangleAlert aria-hidden="true" /></span>
          <strong>数据库查询失败</strong>
          <p>{error}</p>
          <button type="button" className="np-sw-secondary-button" onClick={onAdjustParameters}>检查查询参数</button>
        </div>
      ) : data && snapshot ? (
        <div className="np-dq-results">
          {stale ? (
            <div className="np-dq-stale-notice" role="status">
              当前结构或目标数据表已变化；下列结果对应上次提交参数。
            </div>
          ) : null}
          <div className="np-dq-result-summary">
            <SubmittedStructureSummary key={snapshot.smiles} smiles={snapshot.smiles} />
            <div className="np-dq-result-summary__metric">
              <span><Clock3 aria-hidden="true" /> 查询耗时</span>
              <strong>{data.query_time_ms.toFixed(1)} ms</strong>
            </div>
            <div className="np-dq-result-summary__metric">
              <span><Table2 aria-hidden="true" /> 查询数据表</span>
              <strong>{tableMeta?.shortLabel}</strong>
            </div>
            <div className={`np-dq-match-status${data.exists ? " is-success" : ""}`}>
              <span><Search aria-hidden="true" /> 精确匹配</span>
              <strong>{data.exists ? `找到 ${data.total} 条记录` : "未找到记录"}</strong>
            </div>
          </div>

          {showPropertyFilter ? (
            <section className="np-dq-property-filter" aria-label="性能名称筛选">
              <header>
                <span>性能名称前缀</span>
                <em>显示 {filteredResults.length} / {data.total} 条</em>
              </header>
              <label>
                <Search aria-hidden="true" />
                <input
                  type="search"
                  value={propertyNameFilter}
                  placeholder="例如 Dynamic mechanical"
                  aria-label="筛选性能名称"
                  onChange={(event) => onPropertyNameFilterChange(event.currentTarget.value)}
                />
                {propertyNameFilter ? (
                  <button type="button" aria-label="清空性能名称筛选" onClick={() => onPropertyNameFilterChange("")}>
                    <X aria-hidden="true" />
                  </button>
                ) : null}
              </label>
            </section>
          ) : null}

          {filteredResults.length ? (
            <div className="np-dq-result-list" aria-label="数据库精确匹配结果">
              {filteredResults.map((result) => (
                <DatabaseLookupResultCard
                  key={`${result.source_column}-${result.record_id}`}
                  result={result}
                />
              ))}
            </div>
          ) : data.results.length ? (
            <EmptyResult
              title="没有匹配的性能名称"
              description="清空筛选词，或输入当前结果中已有的性能名前缀。"
              actionLabel="清空筛选"
              onAction={() => onPropertyNameFilterChange("")}
            />
          ) : (
            <EmptyResult
              title="未找到精确匹配"
              description="所选数据表中没有与 canonical SMILES 完全一致的记录。"
              actionLabel="调整查询参数"
              onAction={onAdjustParameters}
            />
          )}
        </div>
      ) : (
        <EmptyResult
          title="等待数据库查询"
          description="打开查询参数，选择数据表后运行当前结构的精确匹配。"
        />
      )}
    </WorkbenchDrawerShell>
  );
}
