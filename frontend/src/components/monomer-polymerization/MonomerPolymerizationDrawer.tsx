import {
  CircleAlert,
  Clock3,
  FlaskConical,
  LoaderCircle,
  PanelRightOpen,
  SearchX,
  Trash2,
  TriangleAlert
} from "lucide-react";
import type {
  MonomerPolymerizationRequest,
  MonomerPolymerizationResponse
} from "../../types";
import { WorkbenchDrawerShell } from "../structure-workbench/WorkbenchDrawerShell";
import { localizeSmipolyWarning, TARGET_CLASS_LABELS } from "./config";
import { MonomerPolymerizationCandidateCard } from "./MonomerPolymerizationCandidateCard";

export type MonomerPolymerizationSnapshot = MonomerPolymerizationRequest;

type MonomerPolymerizationDrawerProps = {
  open: boolean;
  hasAttempt: boolean;
  width: number;
  minWidth: number;
  maxWidth: number;
  keyboardStep: number;
  loading: boolean;
  error: string | null;
  data: MonomerPolymerizationResponse | null;
  snapshot: MonomerPolymerizationSnapshot | null;
  stale: boolean;
  onWidthChange: (width: number) => void;
  onClose: () => void;
  onOpen: () => void;
  onClear: () => void;
};

function formatQueryTime(value: number) {
  if (!Number.isFinite(value)) return "--";
  return value >= 1000 ? `${(value / 1000).toFixed(2)} s` : `${value.toFixed(1)} ms`;
}
export function MonomerPolymerizationDrawer({
  open,
  hasAttempt,
  width,
  minWidth,
  maxWidth,
  keyboardStep,
  loading,
  error,
  data,
  snapshot,
  stale,
  onWidthChange,
  onClose,
  onOpen,
  onClear
}: MonomerPolymerizationDrawerProps) {
  const status = loading
    ? "聚合运行中"
    : error
      ? "运行失败"
      : data
        ? data.results.length
          ? `${data.results.length} / ${data.total} 个候选已返回`
          : "未找到候选"
        : "等待运行";

  return (
    <WorkbenchDrawerShell
      open={open}
      hasRun={hasAttempt}
      width={width}
      minWidth={minWidth}
      maxWidth={maxWidth}
      keyboardStep={keyboardStep}
      title="正向聚合结果"
      status={status}
      headerIcon={<FlaskConical aria-hidden="true" />}
      reopenIcon={<PanelRightOpen aria-hidden="true" />}
      reopenLabel="展开聚合结果"
      reopenVariant="side-handle"
      closeLabel="关闭正向聚合结果"
      resizeLabel="调整正向聚合结果抽屉宽度"
      onWidthChange={onWidthChange}
      onClose={onClose}
      onOpen={onOpen}
    >
      <div className="np-mp-drawer-toolbar">
        <span>{snapshot ? TARGET_CLASS_LABELS[snapshot.target_class] : "SMiPoly"}</span>
        <button type="button" onClick={onClear}>
          <Trash2 aria-hidden="true" />
          清空结果
        </button>
      </div>

      {loading ? (
        <div className="np-mp-loading-results" aria-label="正在调用 SMiPoly 规则生成">
          <div className="np-sw-result-state">
            <span><LoaderCircle className="np-sw-spin" /></span>
            <strong>正在生成聚合物候选</strong>
            <p>SMiPoly 正在匹配规则并整理候选结构。</p>
          </div>
          <div className="np-mp-skeleton-list" aria-hidden="true">
            <i /><i /><i />
          </div>
        </div>
      ) : error ? (
        <div className="np-sw-result-state is-danger">
          <span><TriangleAlert /></span>
          <strong>正向聚合运行失败</strong>
          <p>{error}</p>
          <button type="button" className="np-sw-secondary-button" onClick={onClose}>返回检查输入</button>
        </div>
      ) : data && snapshot ? (
        <div className="np-mp-results">
          {stale ? (
            <div className="np-mp-stale-notice" role="status">
              <CircleAlert aria-hidden="true" />
              <span>结果对应上次提交的输入；当前参数已发生变化。</span>
            </div>
          ) : null}

          <div className="np-mp-result-summary">
            <div>
              <span>返回 / 总命中</span>
              <strong>{data.results.length} / {data.total}</strong>
            </div>
            <div>
              <span>目标类型</span>
              <strong>{TARGET_CLASS_LABELS[data.target_class]}</strong>
            </div>
            <div>
              <span><Clock3 aria-hidden="true" /> 查询耗时</span>
              <strong>{formatQueryTime(data.query_time_ms)}</strong>
            </div>
          </div>

          <section className="np-mp-canonical-inputs" aria-label="后端规范化单体">
            <h3>后端识别单体</h3>
            {data.input_monomers.map((monomer) => (
              <div key={monomer.role}>
                <span>{monomer.role === "monomer_a" ? "MONOMER A" : "MONOMER B"}</span>
                <dl>
                  <div><dt>原始</dt><dd>{monomer.input_smiles}</dd></div>
                  <div><dt>Canonical</dt><dd>{monomer.canonical_smiles}</dd></div>
                </dl>
              </div>
            ))}
          </section>

          {data.warnings.length ? (
            <section className="np-mp-warning-list" aria-label="SMiPoly 提示">
              {data.warnings.map((warning, index) => {
                const localized = localizeSmipolyWarning(warning);
                return (
                  <div key={`${index}-${warning}`}>
                    <TriangleAlert aria-hidden="true" />
                    <span>
                      {!localized.translated ? <strong>SMiPoly 信息：</strong> : null}
                      {localized.text}
                    </span>
                  </div>
                );
              })}
            </section>
          ) : null}

          {data.results.length ? (
            <section className="np-mp-candidate-list" aria-label="聚合物候选">
              {data.results
                .slice()
                .sort((left, right) => left.rank - right.rank)
                .map((candidate) => (
                  <MonomerPolymerizationCandidateCard
                    key={`${candidate.rank}-${candidate.polymer_smiles}`}
                    candidate={candidate}
                  />
                ))}
            </section>
          ) : (
            <div className="np-sw-result-state np-mp-empty-state">
              <span><SearchX /></span>
              <strong>本次没有生成候选</strong>
              <p>可检查单体互补性，或切换目标聚合物类型后重新运行。</p>
            </div>
          )}
        </div>
      ) : (
        <div className="np-sw-result-state">
          <span><FlaskConical /></span>
          <strong>等待正向聚合</strong>
          <p>完成输入并运行后，候选会在这里纵向展示。</p>
        </div>
      )}
    </WorkbenchDrawerShell>
  );
}
