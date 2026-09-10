import { ArrowLeft, BadgeInfo, Check, CheckCircle2, ChevronRight, Circle, Layers3, LoaderCircle, Star, Target, TriangleAlert } from "lucide-react";
import { type CSSProperties, type ReactNode, useId, useMemo, useRef } from "react";
import type { HighThroughputTarget, HighThroughputTargetKey } from "../../constants/highThroughputDemoScenario";
import { cn } from "../../lib/utils";
import { AgentCardShell, AgentDockLayout, AGENT_PROPERTY_ICONS, TargetTabs } from "./PriorWorkbench";
import { buildPriorHotspotSummary, displayTargetUnit, formatPriorValue } from "./prior-hotspot-model";
import type { PriorDataUploadsState, RecommendationSelection, RecommendationValidationValues } from "./types";
import { CandidateStructureDetails, RecommendationValidationCards } from "./RecommendationValidation";
import "./prior-hotspot-workspace.css";

type PriorHotspotWorkspaceProps = {
  targets: HighThroughputTarget[];
  candidateTotal: number;
  materialType: string;
  representation: string;
  uploads: PriorDataUploadsState;
  activeTargetKey: HighThroughputTargetKey;
  onSelectTarget: (key: HighThroughputTargetKey) => void;
  selectedRecommendation: RecommendationSelection | null;
  onSelectRecommendation: (selection: RecommendationSelection) => void;
  validationValues: RecommendationValidationValues;
  onValidationValueChange: (key: HighThroughputTargetKey, id: string, value: string) => void;
  validationConfirmed: boolean;
  onConfirm: () => void;
  onBack: () => void;
  onNext: () => void;
  canAdvance: boolean;
  transitionMessage: string | null;
  renderCandidateMap: (onSelect: (selection: RecommendationSelection) => void) => ReactNode;
};

export function PriorHotspotWorkspace({ targets, candidateTotal, materialType, representation, uploads,
  activeTargetKey, onSelectTarget, selectedRecommendation, onSelectRecommendation, validationValues,
  onValidationValueChange, validationConfirmed, onConfirm, onBack, onNext, canAdvance,
  transitionMessage, renderCandidateMap }: PriorHotspotWorkspaceProps) {
  const mapRef = useRef<HTMLDivElement>(null);
  const footerRef = useRef<HTMLElement>(null);
  const cardRefs = useRef(new Map<string, HTMLElement>());
  const id = useId();
  const summaries = useMemo(() => buildPriorHotspotSummary(targets, validationValues), [targets, validationValues]);
  const active = summaries.find((summary) => summary.target.key === activeTargetKey) ?? summaries[0];
  const { target, recommendations, best, meetsTarget } = active;
  const PropertyIcon = AGENT_PROPERTY_ICONS[target.key];
  const readyCount = targets.filter((item) => uploads[item.key] && !uploads[item.key]?.isLoading && !uploads[item.key]?.errorMessage).length;
  const requiredCount = summaries.reduce((total, summary) => total + summary.recommendations.length, 0);
  const filledCount = summaries.reduce((total, summary) => total + summary.filledCount, 0);
  const missingCount = requiredCount - filledCount;
  const missingTargets = summaries.filter((summary) => summary.filledCount < summary.recommendations.length)
    .map((summary) => `${summary.target.shortLabel} ${summary.recommendations.length - summary.filledCount} 项`).join("、");
  const selected = recommendations.find((candidate) => selectedRecommendation?.targetKey === target.key && selectedRecommendation.candidateId === candidate.id) ?? recommendations[0];
  const isBusy = Boolean(transitionMessage);
  const statusText = missingCount > 0 ? `待补 ${missingCount} 项：${missingTargets}`
    : validationConfirmed ? "本批已确认，可进入 S3" : "8 项演示值已填，尚未确认本批";

  function selectFromMap(selection: RecommendationSelection) {
    if (isBusy) return;
    onSelectRecommendation(selection);
    const card = cardRefs.current.get(selection.candidateId);
    const scroll = mapRef.current?.closest<HTMLElement>(".ht-scroll-region");
    if (!card || !scroll) return;
    const region = scroll.getBoundingClientRect();
    const bounds = card.getBoundingClientRect();
    const visibleBottom = Math.min(region.bottom, footerRef.current?.getBoundingClientRect().top ?? region.bottom);
    if (bounds.top < region.top + 12 || bounds.bottom > visibleBottom - 12) {
      // Scroll only this workbench, never the AppShell or the document.
      scroll.scrollTo({ top: scroll.scrollTop + bounds.top - region.top - 12,
        behavior: window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
    }
  }

  return (
    <div className="ht-s1-stage ht-s2-stage" aria-busy={isBusy}>
      <div className="ht-s1-content">
        <div className="ht-workbench-demo-note">
          <BadgeInfo aria-hidden="true" size={16} />
          <span><strong>演示说明：</strong>当前展示预设 DOE 先验、热点与推荐批次；验证值可编辑并用于演示回流记录，不重训模型或改变预设搜索路径。</span>
        </div>
        <div className="ht-s1-summary">
          <div className="ht-s1-scenario-facts"><Layers3 aria-hidden="true" /><span>{materialType}</span>
            <span><b>{candidateTotal.toLocaleString("en-US")}</b> 候选</span><span>{representation} · 2D</span></div>
          <div className={cn("ht-s2-summary-status", validationConfirmed && "confirmed")} role="status" aria-live="polite">
            <span><CheckCircle2 aria-hidden="true" />先验就绪 <b>{readyCount}/4</b></span>
            <span>{validationConfirmed ? <CheckCircle2 aria-hidden="true" /> : <Circle aria-hidden="true" />}
              验证值已填 <b>{filledCount}/{requiredCount}</b> · {validationConfirmed ? "已确认" : "待确认"}</span>
          </div>
        </div>

        <AgentDockLayout targets={targets} activeTargetKey={activeTargetKey} onSelectTarget={onSelectTarget}
          mapRef={mapRef} disabled={isBusy} renderAgent={(props) => {
            const summary = summaries.find((item) => item.target.key === props.target.key)!;
            return <AgentCardShell {...props} stageLabel="先验热点">
              <div className="ht-s2-agent-analysis" data-agent-inset="true">
                <div className="ht-s2-agent-counts"><span>DOE <b>{summary.doeCount}</b></span><span>推荐 <b>{summary.recommendations.length}</b></span></div>
                <span>初始最优</span><strong>{summary.best?.id ?? "—"}</strong>
                <p>{summary.best ? formatPriorValue(props.target, summary.best.scores[props.target.key]) : "—"} <span>{displayTargetUnit(props.target)}</span></p>
                <small>{summary.meetsTarget ? "已达到场景阈值" : "尚未达到场景阈值"}</small>
              </div>
              <div className={cn("ht-s1-agent-status", validationConfirmed && "ready")}>
                {validationConfirmed ? <CheckCircle2 aria-hidden="true" /> : <Circle aria-hidden="true" />}
                <span>已填 {summary.filledCount}/{summary.recommendations.length} · {validationConfirmed ? "本批已确认" : "本批待确认"}</span>
              </div>
            </AgentCardShell>;
          }}>
          <section className="ht-s1-space-panel" aria-labelledby={`${id}-space-heading`}>
            <header className="ht-s1-space-header">
              <Layers3 className="ht-s1-space-watermark" aria-hidden="true" />
              <div className="ht-s1-section-title"><span className="ht-s1-section-index">01</span><div>
                <h3 id={`${id}-space-heading`}>先验热点与候选空间</h3><p>切换目标，查看初始热点并选择推荐验证点</p>
              </div></div>
              <div className="ht-s1-space-focus" style={{ "--target-color": target.color } as CSSProperties}>
                <PropertyIcon aria-hidden="true" /><span>当前目标 <strong>{target.shortLabel}</strong></span>
              </div>
            </header>
            <div className="ht-s1-space-body">
              <TargetTabs targets={targets} activeTargetKey={activeTargetKey} onSelectTarget={onSelectTarget}
                disabled={isBusy} tabId={id} panelId={`${id}-space`} label="切换先验热点目标"
                renderIcon={(item) => { const Icon = AGENT_PROPERTY_ICONS[item.key]; return <Icon aria-hidden="true" />; }} />
              <div className="ht-s1-map ht-s2-map" id={`${id}-space`} role="tabpanel" aria-labelledby={`${id}-${activeTargetKey}`} tabIndex={0}>
                <span className="ht-s2-axis-label">{representation} 投影维度 Y</span>
                <div ref={mapRef}>{renderCandidateMap(selectFromMap)}</div>
                <span className="ht-s2-axis-label horizontal">{representation} 投影维度 X</span>
              </div>
              <div className="ht-s1-map-legend" style={{ "--target-color": target.color } as CSSProperties}>
                <span><i className="candidate" aria-hidden="true" />候选点</span>
                <span><i className="doe" aria-hidden="true" />DOE <b>{active.doeCount}</b></span>
                <span><i className="recommended" aria-hidden="true" />推荐 <b>{recommendations.length}</b></span>
                <span><Star aria-hidden="true" />初始最优</span>
                <span><i className="hotspot" aria-hidden="true" />预设先验热点</span>
                <span className="ht-s2-heat-note">浓淡仅作区域示意，非预测值或置信概率</span>
              </div>
              <div className="ht-s2-best-summary" style={{ "--target-color": target.color } as CSSProperties}>
                <span><Star aria-hidden="true" />初始最优 <strong>{best?.id ?? "—"}</strong></span>
                <span><b>{best ? formatPriorValue(target, best.scores[target.key]) : "—"}</b> {displayTargetUnit(target)}</span>
                <span>目标 {target.direction === "higher" ? "≥" : "≤"} <b>{formatPriorValue(target, target.target)}</b> {displayTargetUnit(target)}</span>
                <span className={cn("ht-s2-threshold-state", meetsTarget && "met")}>
                  {meetsTarget ? <CheckCircle2 aria-hidden="true" /> : <Target aria-hidden="true" />}{meetsTarget ? "已达到场景阈值" : "尚未达到场景阈值"}
                </span>
              </div>
            </div>
          </section>
        </AgentDockLayout>

        <section className="ht-s1-preview ht-s2-validation ht-validation-workspace" aria-labelledby={`${id}-validation-heading`} style={{ "--target-color": target.color } as CSSProperties}>
          <header className="ht-s1-preview-header">
            <div className="ht-s1-section-title"><span className="ht-s1-section-index">02</span><div>
              <h3 id={`${id}-validation-heading`}>{target.shortLabel} 推荐点验证</h3><p>已预填示例值，可修改；每目标 2 项，共 8 项，需整批确认</p>
            </div></div>
            <span className="ht-s2-validation-threshold">目标 {target.direction === "higher" ? "≥" : "≤"} <b>{formatPriorValue(target, target.target)}</b> {displayTargetUnit(target)}</span>
          </header>
          <div className="ht-s2-validation-body">
            <RecommendationValidationCards target={target} recommendations={recommendations} selectedId={selected?.id}
              values={validationValues} disabled={isBusy} cardRefs={cardRefs}
              onSelect={(candidateId) => onSelectRecommendation({ targetKey: target.key, candidateId })}
              onChange={(candidateId, value) => onValidationValueChange(target.key, candidateId, value)} />
            {selected ? <CandidateStructureDetails candidate={selected} sourceLabel={`S2 ${target.shortLabel} 预设推荐批次`} disabled={isBusy} /> : null}
          </div>
        </section>
      </div>
      <footer ref={footerRef} className="ht-s1-footer ht-s2-footer">
        <button type="button" className="ht-s1-secondary-button ht-s2-back" onClick={onBack} disabled={isBusy}><ArrowLeft aria-hidden="true" />返回 S1 先验导入</button>
        <p className={cn("ht-s2-batch-status", validationConfirmed && "confirmed")} role="status" aria-live="polite">
          {isBusy ? <LoaderCircle className="ht-s1-spinner" aria-hidden="true" /> : validationConfirmed ? <CheckCircle2 aria-hidden="true" /> : missingCount ? <TriangleAlert aria-hidden="true" /> : <Circle aria-hidden="true" />}
          <span>{transitionMessage || statusText}</span>
        </p>
        <div className="ht-s2-batch-actions">
          <button type="button" className="ht-s1-secondary-button" onClick={onConfirm} disabled={missingCount > 0 || validationConfirmed || isBusy}>
            {validationConfirmed ? <Check aria-hidden="true" /> : null}{validationConfirmed ? "本批已确认" : `确认本批 ${requiredCount} 项验证值`}
          </button>
          <button type="button" className="ht-s1-primary-button" onClick={onNext} disabled={!canAdvance || isBusy}>
            {isBusy ? <LoaderCircle className="ht-s1-spinner" aria-hidden="true" /> : null}{isBusy ? "演示回流中" : "进入 S3"}{!isBusy ? <ChevronRight aria-hidden="true" /> : null}
          </button>
        </div>
      </footer>
    </div>
  );
}
