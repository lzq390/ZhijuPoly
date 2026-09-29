import {
  ChevronLeft,
  ChevronRight,
  Copy,
  LoaderCircle,
  Network,
  Route,
  Search,
  TriangleAlert
} from "lucide-react";
import type { MonomerRetrosynthesisResponse, MonomerRetrosynthesisTargetRole } from "../../types";
import { useState } from "react";
import { WorkbenchDrawerShell } from "../structure-workbench/WorkbenchDrawerShell";

const TARGET_ROLE_LABEL: Record<MonomerRetrosynthesisTargetRole, string> = {
  auto: "自动",
  diamine: "二胺",
  dianhydride: "二酐",
  other: "通用单体"
};

type RetrosynthesisDrawerProps = {
  open: boolean;
  hasRun: boolean;
  width: number;
  loading: boolean;
  error: string | null;
  data: MonomerRetrosynthesisResponse | null;
  selectedCandidateIndex: number;
  stale?: boolean;
  restoreFocusTarget?: HTMLElement | null;
  onWidthChange: (width: number) => void;
  onSelectedCandidateIndexChange: (index: number) => void;
  onClose: () => void;
  onOpen: () => void;
  onAdjustParameters: () => void;
};

export function RetrosynthesisDrawer({
  open,
  hasRun,
  width,
  loading,
  error,
  data,
  selectedCandidateIndex,
  stale = false,
  restoreFocusTarget,
  onWidthChange,
  onSelectedCandidateIndexChange,
  onClose,
  onOpen,
  onAdjustParameters
}: RetrosynthesisDrawerProps) {
  const [copyNotice, setCopyNotice] = useState<string | null>(null);
  const candidates = data?.candidates ?? [];
  const clampedCandidateIndex = candidates.length
    ? Math.min(selectedCandidateIndex, candidates.length - 1)
    : 0;
  const selectedCandidate = candidates[clampedCandidateIndex] ?? null;
  const validCandidateCount = candidates.filter((candidate) => candidate.valid_smiles).length;
  const resultStatus = loading
    ? "运行中"
    : error
      ? "运行失败"
      : data
        ? candidates.length
          ? `${data.total} 个候选`
          : "未找到候选"
        : "等待运行";

  function moveCandidate(delta: number) {
    if (!candidates.length) return;
    onSelectedCandidateIndexChange(
      (clampedCandidateIndex + delta + candidates.length) % candidates.length
    );
  }

  async function copyText(value: string | null | undefined) {
    if (!value) return;
    try {
      await navigator.clipboard.writeText(value);
      setCopyNotice("SMILES 已复制。");
    } catch { setCopyNotice("复制失败，请手动选择 SMILES。"); }
  }

  return (
    <WorkbenchDrawerShell
      open={open}
      hasRun={hasRun}
      width={width}
      title="单体反推结果"
      status={resultStatus}
      headerIcon={<Route aria-hidden="true" />}
      reopenIcon={<Route aria-hidden="true" />}
      reopenLabel="展开反推结果"
      reopenVariant="side-handle"
      restoreFocusTarget={restoreFocusTarget}
      closeLabel="关闭单体反推结果"
      resizeLabel="调整单体反推结果抽屉宽度"
      onWidthChange={onWidthChange}
      onClose={onClose}
      onOpen={onOpen}
    >
      {copyNotice ? <p className="np-mr-notice" role="status">{copyNotice}</p> : null}
      {stale ? <p className="np-mr-stale" role="status">参数已修改，当前结果对应上次提交的参数。</p> : null}
      {loading ? (
        <div className="np-sw-result-state">
          <span><LoaderCircle className="np-sw-spin" /></span>
          <strong>正在生成逆合成候选</strong>
          <p>模型正在分析目标单体并整理可能的前体组合。</p>
        </div>
      ) : error ? (
        <div className="np-sw-result-state is-danger">
          <span><TriangleAlert /></span>
          <strong>反推运行失败</strong>
          <p>{error}</p>
          <button type="button" className="np-sw-secondary-button" onClick={onAdjustParameters}>调整反推参数</button>
        </div>
      ) : !data ? (
        <div className="np-sw-result-state">
          <span><Network /></span>
          <strong>等待反推</strong>
          <p>输入目标单体并设置候选数，然后运行反推。</p>
        </div>
      ) : !candidates.length ? (
        <div className="np-sw-result-state">
          <span><Search /></span>
          <strong>未找到可展示候选</strong>
          <p>可调整目标结构或候选数后重新运行。</p>
          <button type="button" className="np-sw-secondary-button" onClick={onAdjustParameters}>调整反推参数</button>
        </div>
      ) : (
        <div className="np-mr-results">
          <div className="np-mr-result-summary">
            <div><span>{data.target_role === "auto" ? "自动识别类型" : "用户指定类型"}</span><strong>{TARGET_ROLE_LABEL[data.inferred_target_role]}</strong></div>
            <div><span>合法候选</span><strong>{validCandidateCount}/{data.total}</strong></div>
          </div>
          <div className="np-mr-target-card">
            <div><span>目标结构</span><code>{data.canonical_smiles}</code></div>
            <button type="button" className="np-sw-icon-button" onClick={() => copyText(data.canonical_smiles)} aria-label="复制目标 SMILES"><Copy /></button>
          </div>
          {selectedCandidate ? (
            <article className="np-mr-candidate-card">
              <header>
                <div>
                  <span className={`np-mr-result-badge${selectedCandidate.valid_smiles ? " is-valid" : " is-warning"}`}>
                    候选 {selectedCandidate.rank} · {selectedCandidate.valid_smiles ? "合法 SMILES" : "需人工校验"}
                  </span>
                  <h3>{selectedCandidate.reaction_hint}</h3>
                </div>
                <div className="np-mr-candidate-pager">
                  <button type="button" onClick={() => moveCandidate(-1)} disabled={candidates.length <= 1} aria-label="查看上一个候选"><ChevronLeft /></button>
                  <span>{clampedCandidateIndex + 1}/{candidates.length}</span>
                  <button type="button" onClick={() => moveCandidate(1)} disabled={candidates.length <= 1} aria-label="查看下一个候选"><ChevronRight /></button>
                </div>
              </header>
              <div className="np-mr-reactant-list">
                {selectedCandidate.reactants.map((reactant, index) => {
                  const value = reactant.canonical_smiles ?? reactant.input_smiles;
                  return (
                    <div key={`${selectedCandidate.rank}-${index}-${reactant.input_smiles}`} className="np-mr-reactant">
                      <div><span>前体 {index + 1}</span><code>{value}</code></div>
                      <button type="button" className="np-sw-icon-button" onClick={() => copyText(value)} aria-label={`复制前体 ${index + 1} SMILES`}><Copy /></button>
                    </div>
                  );
                })}
              </div>
            </article>
          ) : null}
        </div>
      )}
    </WorkbenchDrawerShell>
  );
}
