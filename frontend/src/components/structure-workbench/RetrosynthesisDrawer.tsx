import {
  ChevronLeft,
  ChevronRight,
  Copy,
  LoaderCircle,
  MessageSquareText,
  Network,
  Route,
  Search,
  TriangleAlert,
  X
} from "lucide-react";
import { useEffect, useRef, type CSSProperties, type PointerEvent as ReactPointerEvent } from "react";
import type { MonomerRetrosynthesisResponse, MonomerRetrosynthesisTargetRole } from "../../types";

const DRAWER_MIN_WIDTH = 320;
const DRAWER_MAX_WIDTH = 560;

const TARGET_ROLE_LABEL: Record<MonomerRetrosynthesisTargetRole, string> = {
  auto: "自动",
  diamine: "二胺提示",
  dianhydride: "二酐提示",
  other: "通用单体"
};

function clamp(value: number) {
  return Math.min(DRAWER_MAX_WIDTH, Math.max(DRAWER_MIN_WIDTH, value));
}

type RetrosynthesisDrawerProps = {
  open: boolean;
  hasRun: boolean;
  width: number;
  loading: boolean;
  error: string | null;
  data: MonomerRetrosynthesisResponse | null;
  selectedCandidateIndex: number;
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
  onWidthChange,
  onSelectedCandidateIndexChange,
  onClose,
  onOpen,
  onAdjustParameters
}: RetrosynthesisDrawerProps) {
  const resizeStateRef = useRef<{ startX: number; startWidth: number } | null>(null);
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

  useEffect(() => {
    function handlePointerMove(event: PointerEvent) {
      const state = resizeStateRef.current;
      if (!state) return;
      onWidthChange(clamp(state.startWidth + state.startX - event.clientX));
    }

    function stopResize() {
      resizeStateRef.current = null;
    }

    document.addEventListener("pointermove", handlePointerMove);
    document.addEventListener("pointerup", stopResize);
    document.addEventListener("pointercancel", stopResize);
    return () => {
      document.removeEventListener("pointermove", handlePointerMove);
      document.removeEventListener("pointerup", stopResize);
      document.removeEventListener("pointercancel", stopResize);
      resizeStateRef.current = null;
    };
  }, [onWidthChange]);

  function startResize(event: ReactPointerEvent<HTMLDivElement>) {
    event.preventDefault();
    resizeStateRef.current = { startX: event.clientX, startWidth: width };
  }

  function moveCandidate(delta: number) {
    if (!candidates.length) return;
    onSelectedCandidateIndexChange(
      (clampedCandidateIndex + delta + candidates.length) % candidates.length
    );
  }

  function copyText(value: string | null | undefined) {
    if (value) void navigator.clipboard?.writeText(value);
  }

  const style = { "--np-sw-drawer-width": `${width}px` } as CSSProperties;

  return (
    <>
      <div
        className={`np-sw-drawer-layer${open ? " is-open" : ""}`}
        style={style}
        aria-hidden={!open}
      >
        <button
          type="button"
          className="np-sw-drawer-backdrop"
          aria-label="关闭单体反推结果背景"
          tabIndex={open ? 0 : -1}
          onClick={onClose}
        />
        <aside
          className="np-sw-drawer"
          aria-labelledby="structure-retro-results-title"
          inert={!open}
        >
          <div
            className="np-sw-drawer__resizer"
            role="separator"
            tabIndex={open ? 0 : -1}
            aria-label="调整单体反推结果抽屉宽度"
            aria-orientation="vertical"
            aria-valuemin={DRAWER_MIN_WIDTH}
            aria-valuemax={DRAWER_MAX_WIDTH}
            aria-valuenow={width}
            onPointerDown={startResize}
            onKeyDown={(event) => {
              if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
              event.preventDefault();
              const amount = event.shiftKey ? 40 : 16;
              onWidthChange(
                clamp(width + (event.key === "ArrowLeft" ? amount : -amount))
              );
            }}
          />
          <header className="np-sw-drawer__header">
            <div>
              <span><MessageSquareText aria-hidden="true" /></span>
              <div>
                <h2 id="structure-retro-results-title">单体反推结果</h2>
                <p>{resultStatus}</p>
              </div>
            </div>
            <button type="button" className="np-sw-icon-button" aria-label="关闭单体反推结果" onClick={onClose}>
              <X aria-hidden="true" />
            </button>
          </header>
          <div className="np-sw-drawer__body" aria-live="polite">
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
                <p>从功能参数中选择“单体逆合成反推”，输入目标单体后运行。</p>
              </div>
            ) : !candidates.length ? (
              <div className="np-sw-result-state">
                <span><Search /></span>
                <strong>未找到可展示候选</strong>
                <p>可调整目标结构、结构提示或候选数后重新运行。</p>
                <button type="button" className="np-sw-secondary-button" onClick={onAdjustParameters}>调整反推参数</button>
              </div>
            ) : (
              <div className="np-sw-retro-results">
                <div className="np-sw-result-summary">
                  <div><span>识别类型</span><strong>{TARGET_ROLE_LABEL[data.inferred_target_role]}</strong></div>
                  <div><span>合法候选</span><strong>{validCandidateCount}/{data.total}</strong></div>
                </div>
                <div className="np-sw-target-card">
                  <div><span>目标结构</span><code>{data.canonical_smiles}</code></div>
                  <button type="button" className="np-sw-icon-button" onClick={() => copyText(data.canonical_smiles)} aria-label="复制目标 SMILES"><Copy /></button>
                </div>
                {selectedCandidate ? (
                  <article className="np-sw-candidate-card">
                    <header>
                      <div>
                        <span className={`np-sw-result-badge${selectedCandidate.valid_smiles ? " is-valid" : " is-warning"}`}>
                          候选 {selectedCandidate.rank} · {selectedCandidate.valid_smiles ? "合法 SMILES" : "需人工校验"}
                        </span>
                        <h3>{selectedCandidate.reaction_hint}</h3>
                      </div>
                      <div className="np-sw-candidate-pager">
                        <button type="button" onClick={() => moveCandidate(-1)} disabled={candidates.length <= 1} aria-label="查看上一个候选"><ChevronLeft /></button>
                        <span>{clampedCandidateIndex + 1}/{candidates.length}</span>
                        <button type="button" onClick={() => moveCandidate(1)} disabled={candidates.length <= 1} aria-label="查看下一个候选"><ChevronRight /></button>
                      </div>
                    </header>
                    <div className="np-sw-reactant-list">
                      {selectedCandidate.reactants.map((reactant, index) => {
                        const value = reactant.canonical_smiles ?? reactant.input_smiles;
                        return (
                          <div key={`${selectedCandidate.rank}-${index}-${reactant.input_smiles}`} className="np-sw-reactant">
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
          </div>
        </aside>
      </div>

      {hasRun && !open ? (
        <button type="button" className="np-sw-drawer-reopen" onClick={onOpen} aria-label="展开单体反推结果" title="展开单体反推结果">
          <Route aria-hidden="true" />
          <span>反推结果</span>
        </button>
      ) : null}
    </>
  );
}
