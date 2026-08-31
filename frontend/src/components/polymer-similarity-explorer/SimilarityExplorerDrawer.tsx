import {
  ArrowDownAZ,
  ArrowUpAZ,
  ChevronDown,
  ChevronUp,
  Clock3,
  Database,
  FlaskConical,
  LoaderCircle,
  PanelRightOpen,
  Search,
  TriangleAlert
} from "lucide-react";
import { useMemo, useState } from "react";
import { PREDICT_PROPERTY_META } from "../../constants/predictableProperties";
import type {
  MatchMode,
  PolymerResult,
  PredictableProperty,
  SmilesQueryResponse
} from "../../types";
import { StructureSvg } from "../StructureSvg";
import { WorkbenchDrawerShell } from "../structure-workbench/WorkbenchDrawerShell";

export type SimilarityExplorerSnapshot = {
  smiles: string;
  mode: MatchMode;
  similarityThreshold: number;
  topK: number;
  property: PredictableProperty | null;
};

type SimilarityExplorerDrawerProps = {
  open: boolean;
  hasAttempt: boolean;
  width: number;
  preparing: boolean;
  loading: boolean;
  error: string | null;
  data: SmilesQueryResponse | null;
  snapshot: SimilarityExplorerSnapshot | null;
  stale: boolean;
  onWidthChange: (width: number) => void;
  onClose: () => void;
  onOpen: () => void;
  onAdjustParameters: () => void;
};

type SortOrder = "desc" | "asc";

function formatOrigin(value: string | null | undefined) {
  const normalized = value?.trim().toLowerCase();
  if (!normalized || normalized === "n/a" || normalized === "na") return "未标注";
  if (normalized === "exp" || normalized === "experimental") return "实验";
  if (normalized === "sim" || normalized === "simulated") return "模拟";
  return value?.trim() || "未标注";
}

function resultOrigin(result: PolymerResult, mode: MatchMode) {
  if (mode === "property") return formatOrigin(result.matched_property_source);
  const sources = Object.values(result.properties)
    .flat()
    .map((property) => formatOrigin(property.label_source))
    .filter((source) => source !== "未标注");
  return Array.from(new Set(sources)).join(" / ") || "未标注";
}

function formatScore(value: number | null | undefined) {
  return value === null || value === undefined || !Number.isFinite(value) ? "暂无" : value.toFixed(3);
}

function SubmittedStructureSummary({ smiles }: { smiles: string }) {
  const collapsible = smiles.length > 72;
  const [expanded, setExpanded] = useState(!collapsible);

  return (
    <div className="np-se-result-summary__structure">
      <div className="np-se-result-summary__structure-header">
        <span><FlaskConical aria-hidden="true" /> 提交结构</span>
        {collapsible ? (
          <button
            type="button"
            aria-expanded={expanded}
            aria-controls="similarity-submitted-smiles"
            aria-label={expanded ? "收起完整提交结构" : "展开完整提交结构"}
            onClick={() => setExpanded((current) => !current)}
          >
            {expanded ? <ChevronUp aria-hidden="true" /> : <ChevronDown aria-hidden="true" />}
            {expanded ? "收起" : "展开"}
          </button>
        ) : null}
      </div>
      <code
        id="similarity-submitted-smiles"
        className={collapsible && !expanded ? "is-collapsed" : undefined}
        title={collapsible && !expanded ? smiles : undefined}
      >
        {smiles}
      </code>
    </div>
  );
}

function SimilarityResultCard({
  result,
  snapshot,
  rank
}: {
  result: PolymerResult;
  snapshot: SimilarityExplorerSnapshot;
  rank: number;
}) {
  const displaySmiles = result.canonical_smiles || result.smiles;
  const propertyMeta = snapshot.property ? PREDICT_PROPERTY_META[snapshot.property] : null;
  const propertyValue =
    result.matched_property_value === null || result.matched_property_value === undefined
      ? "暂无"
      : `${Number(result.matched_property_value).toPrecision(6)} ${result.matched_property_unit || propertyMeta?.unit || ""}`.trim();

  return (
    <article className="np-se-result-card">
      <header>
        <div>
          <span>#{rank}</span>
          <strong>{result.polymer_name || `聚合物 ${result.polymer_id}`}</strong>
        </div>
        <span className="np-se-score">匹配度 {formatScore(result.similarity_score)}</span>
      </header>
      <div className="np-se-result-card__structure">
        {result.structure_svg ? (
          <StructureSvg
            svg={result.structure_svg}
            alt={`${displaySmiles} 的二维结构`}
            className="np-se-result-svg"
            imageClassName="np-se-result-svg__image"
          />
        ) : (
          <code>{displaySmiles}</code>
        )}
      </div>
      <code className="np-se-result-card__smiles">{displaySmiles}</code>
      <dl>
        <div>
          <dt>{snapshot.mode === "property" ? propertyMeta?.label || "所选性能" : "结构相似度"}</dt>
          <dd>{snapshot.mode === "property" ? propertyValue : formatScore(result.similarity_score)}</dd>
        </div>
        <div>
          <dt>数据来源</dt>
          <dd>{resultOrigin(result, snapshot.mode)}</dd>
        </div>
      </dl>
    </article>
  );
}

export function SimilarityExplorerDrawer({
  open,
  hasAttempt,
  width,
  preparing,
  loading,
  error,
  data,
  snapshot,
  stale,
  onWidthChange,
  onClose,
  onOpen,
  onAdjustParameters
}: SimilarityExplorerDrawerProps) {
  const [sortOrder, setSortOrder] = useState<SortOrder>("desc");
  const sortedResults = useMemo(() => {
    const results = data?.results ?? [];
    return [...results].sort((left, right) => {
      const leftScore = left.similarity_score ?? -1;
      const rightScore = right.similarity_score ?? -1;
      return sortOrder === "desc" ? rightScore - leftScore : leftScore - rightScore;
    });
  }, [data?.results, sortOrder]);
  const status = preparing
    ? "正在准备检索结构"
    : loading
      ? "相似性检索运行中"
      : error
        ? "相似性检索失败"
        : data
          ? `${data.total} 条匹配结果`
          : "等待相似性检索";
  const propertyMeta = snapshot?.property ? PREDICT_PROPERTY_META[snapshot.property] : null;

  return (
    <WorkbenchDrawerShell
      open={open}
      hasRun={hasAttempt}
      width={width}
      title="相似性探索结果"
      status={status}
      headerIcon={<Search aria-hidden="true" />}
      reopenIcon={<PanelRightOpen aria-hidden="true" />}
      reopenLabel="展开相似性探索结果"
      reopenVariant="side-handle"
      closeLabel="关闭相似性探索结果"
      resizeLabel="调整相似性探索结果抽屉宽度"
      onWidthChange={onWidthChange}
      onClose={onClose}
      onOpen={onOpen}
    >
      {preparing ? (
        <div className="np-sw-result-state">
          <span><LoaderCircle className="np-sw-spin" /></span>
          <strong>正在准备检索结构</strong>
          <p>正在同步并标准化当前画板结构。</p>
        </div>
      ) : loading ? (
        <div className="np-sw-result-state">
          <span><LoaderCircle className="np-sw-spin" /></span>
          <strong>正在检索相似聚合物</strong>
          <p>正在计算当前结构与数据库记录的匹配程度。</p>
        </div>
      ) : error ? (
        <div className="np-sw-result-state is-danger">
          <span><TriangleAlert /></span>
          <strong>相似性检索失败</strong>
          <p>{error}</p>
          <button type="button" className="np-sw-secondary-button" onClick={onAdjustParameters}>检查探索参数</button>
        </div>
      ) : data && snapshot ? (
        <div className="np-se-results">
          {stale ? (
            <div className="np-se-stale-notice" role="status">
              当前结构或探索参数已变化；下列结果对应上次提交参数。
            </div>
          ) : null}
          <div className="np-se-result-summary">
            <SubmittedStructureSummary key={snapshot.smiles} smiles={snapshot.smiles} />
            <div className="np-se-result-summary__metric">
              <span><Clock3 aria-hidden="true" /> 查询耗时</span>
              <strong>{data.query_time_ms.toFixed(1)} ms</strong>
            </div>
            <div className="np-se-result-summary__metric">
              <span><Database aria-hidden="true" /> 探索口径</span>
              <strong>{snapshot.mode === "structure" ? "结构相似" : propertyMeta?.label || "性能相似"}</strong>
            </div>
            {snapshot.mode === "property" && data.predicted_property_value != null ? (
              <div className="np-se-result-summary__prediction">
                <span><Search aria-hidden="true" /> 查询结构预测值</span>
                <strong>
                  {Number(data.predicted_property_value).toPrecision(6)} {data.predicted_property_unit || propertyMeta?.unit || ""}
                </strong>
              </div>
            ) : null}
          </div>
          <div className="np-se-results-toolbar">
            <span>阈值 {snapshot.similarityThreshold.toFixed(2)} · 搜索 {snapshot.topK} 条</span>
            {sortedResults.length > 1 ? (
              <button
                type="button"
                onClick={() => setSortOrder((current) => current === "desc" ? "asc" : "desc")}
                aria-label={`按匹配度${sortOrder === "desc" ? "升序" : "降序"}排列`}
              >
                {sortOrder === "desc" ? <ArrowDownAZ aria-hidden="true" /> : <ArrowUpAZ aria-hidden="true" />}
                匹配度{sortOrder === "desc" ? "降序" : "升序"}
              </button>
            ) : null}
          </div>
          {sortedResults.length ? (
            <div className="np-se-result-list" aria-label="相似性探索匹配结果">
              {sortedResults.map((result, index) => (
                <SimilarityResultCard
                  key={result.polymer_id}
                  result={result}
                  snapshot={snapshot}
                  rank={index + 1}
                />
              ))}
            </div>
          ) : (
            <div className="np-sw-result-state">
              <span><Search /></span>
              <strong>未找到匹配结果</strong>
              <p>可以降低相似度阈值、增加搜索数量，或调整当前结构后重试。</p>
              <button type="button" className="np-sw-secondary-button" onClick={onAdjustParameters}>调整探索参数</button>
            </div>
          )}
        </div>
      ) : (
        <div className="np-sw-result-state">
          <span><Search /></span>
          <strong>等待相似性探索</strong>
          <p>打开探索参数，选择匹配方式后运行当前结构的检索。</p>
        </div>
      )}
    </WorkbenchDrawerShell>
  );
}
