import {
  BarChart3,
  Clock3,
  FlaskConical,
  LoaderCircle,
  PanelRightOpen,
  TriangleAlert
} from "lucide-react";
import { PREDICT_PROPERTY_META } from "../../constants/predictableProperties";
import type { PredictableProperty, PredictResponse } from "../../types";
import { WorkbenchDrawerShell } from "../structure-workbench/WorkbenchDrawerShell";

export type HomopolymerPredictionSnapshot = {
  smiles: string;
  properties: PredictableProperty[];
};

type HomopolymerPredictionDrawerProps = {
  open: boolean;
  hasAttempt: boolean;
  width: number;
  loading: boolean;
  error: string | null;
  data: PredictResponse | null;
  snapshot: HomopolymerPredictionSnapshot | null;
  stale: boolean;
  onWidthChange: (width: number) => void;
  onClose: () => void;
  onOpen: () => void;
  onAdjustParameters: () => void;
};

function formatValue(value: number | undefined) {
  return value !== undefined && Number.isFinite(value) ? value.toFixed(2) : null;
}

export function HomopolymerPredictionDrawer({
  open,
  hasAttempt,
  width,
  loading,
  error,
  data,
  snapshot,
  stale,
  onWidthChange,
  onClose,
  onOpen,
  onAdjustParameters
}: HomopolymerPredictionDrawerProps) {
  const returnedCount = snapshot && data
    ? snapshot.properties.filter((property) => formatValue(data.predictions[property]) !== null).length
    : 0;
  const status = loading
    ? "预测运行中"
    : error
      ? "预测失败"
      : data && snapshot
        ? `${returnedCount} / ${snapshot.properties.length} 项已返回`
        : "等待预测";

  return (
    <WorkbenchDrawerShell
      open={open}
      hasRun={hasAttempt}
      width={width}
      title="性质预测结果"
      status={status}
      headerIcon={<BarChart3 aria-hidden="true" />}
      reopenIcon={<PanelRightOpen aria-hidden="true" />}
      reopenLabel="展开预测结果"
      reopenVariant="side-handle"
      closeLabel="关闭性质预测结果"
      resizeLabel="调整性质预测结果抽屉宽度"
      onWidthChange={onWidthChange}
      onClose={onClose}
      onOpen={onOpen}
    >
      {loading ? (
        <div className="np-sw-result-state">
          <span><LoaderCircle className="np-sw-spin" /></span>
          <strong>正在计算所选性质</strong>
          <p>模型正在生成当前结构的性质预测，请稍候。</p>
        </div>
      ) : error ? (
        <div className="np-sw-result-state is-danger">
          <span><TriangleAlert /></span>
          <strong>性质预测失败</strong>
          <p>{error}</p>
          <button type="button" className="np-sw-secondary-button" onClick={onAdjustParameters}>检查预测参数</button>
        </div>
      ) : data && snapshot ? (
        <div className="np-hp-results">
          {stale ? (
            <div className="np-hp-stale-notice" role="status">
              当前结构或性质选择已变化；下列结果对应上次提交参数。
            </div>
          ) : null}
          <div className="np-hp-result-summary">
            <div>
              <span><FlaskConical aria-hidden="true" /> 提交结构</span>
              <code>{snapshot.smiles}</code>
            </div>
            <div>
              <span><Clock3 aria-hidden="true" /> 查询耗时</span>
              <strong>{data.query_time_ms.toFixed(1)} ms</strong>
            </div>
          </div>
          <div className="np-hp-result-list" aria-label="性质预测值">
            {snapshot.properties.map((property) => {
              const meta = PREDICT_PROPERTY_META[property];
              const value = formatValue(data.predictions[property]);
              return (
                <article key={property} className={`np-hp-result-card${value === null ? " is-missing" : ""}`}>
                  <header>
                    <span>{meta.shortLabel}</span>
                  </header>
                  <h3>{meta.label}</h3>
                  <div>
                    <strong>{value ?? "未返回"}</strong>
                    {value !== null ? <span>{meta.unit}</span> : null}
                  </div>
                </article>
              );
            })}
          </div>
        </div>
      ) : (
        <div className="np-sw-result-state">
          <span><BarChart3 /></span>
          <strong>等待性质预测</strong>
          <p>打开预测参数，选择性质后运行当前结构的预测。</p>
        </div>
      )}
    </WorkbenchDrawerShell>
  );
}
