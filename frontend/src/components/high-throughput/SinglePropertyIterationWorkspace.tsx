import { ArrowLeft, BadgeInfo, Check, CheckCircle2, ChevronRight, Circle, GitCompareArrows, Layers3, LoaderCircle, LockKeyhole, Star, Target, TriangleAlert } from "lucide-react";
import { type CSSProperties, type KeyboardEvent, type ReactNode, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { HighThroughputTarget, HighThroughputTargetKey } from "../../constants/highThroughputDemoScenario";
import { cn } from "../../lib/utils";
import { AgentCardShell, AgentDockLayout, AGENT_PROPERTY_ICONS, TargetTabs } from "./PriorWorkbench";
import { CandidateStructureDetails, RecommendationValidationCards } from "./RecommendationValidation";
import { buildIterationSummary, formatIterationValue as formatPriorValue, ITERATION_ROUNDS, type IterationTargetSummary } from "./iteration-model";
import { displayTargetUnit } from "./prior-hotspot-model";
import type { RecommendationSelection, RecommendationValidationValues } from "./types";
import "./single-property-iteration-workspace.css";

type Props = {
  targets: HighThroughputTarget[];
  candidateTotal: number;
  materialType: string;
  representation: string;
  activeTargetKey: HighThroughputTargetKey;
  onSelectTarget: (key: HighThroughputTargetKey) => void;
  progressRound: number;
  viewRound: number;
  onViewRound: (round: number) => void;
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
  renderCandidateMap: (summary: IterationTargetSummary, onSelect: (selection: RecommendationSelection) => void) => ReactNode;
};

export function SinglePropertyIterationWorkspace({ targets, candidateTotal, materialType, representation,
  activeTargetKey, onSelectTarget, progressRound, viewRound, onViewRound, selectedRecommendation,
  onSelectRecommendation, validationValues, onValidationValueChange, validationConfirmed, onConfirm,
  onBack, onNext, canAdvance, transitionMessage, renderCandidateMap }: Props) {
  const id = useId();
  const mapRef = useRef<HTMLDivElement>(null);
  const footerRef = useRef<HTMLElement>(null);
  const roundNavRef = useRef<HTMLElement>(null);
  const previousRound = useRef(viewRound);
  const cardRefs = useRef(new Map<string, HTMLElement>());
  const [comparisonSelection, setComparisonSelection] = useState<"record" | "script">("script");
  const summaries = useMemo(() => targets.map((target) => buildIterationSummary(target, viewRound, validationValues)), [targets, viewRound, validationValues]);
  const active = summaries.find((summary) => summary.target.key === activeTargetKey) ?? summaries[0];
  const { target, recommendations, recordBest, meetsTarget, scriptCandidate, outputCandidate, outputComponent } = active;
  const isReview = viewRound < progressRound;
  const isConverged = viewRound === 2;
  const isBusy = Boolean(transitionMessage);
  const round = ITERATION_ROUNDS[viewRound];
  const PropertyIcon = AGENT_PROPERTY_ICONS[target.key];
  const requiredCount = summaries.reduce((total, summary) => total + summary.recommendations.length, 0);
  const filledCount = summaries.reduce((total, summary) => total + summary.filledCount, 0);
  const missingCount = requiredCount - filledCount;
  const missingTargets = summaries.filter((summary) => summary.filledCount < summary.recommendations.length)
    .map((summary) => `${summary.target.shortLabel} ${summary.recommendations.length - summary.filledCount} 项`).join("、");
  const selected = recommendations.find((candidate) => selectedRecommendation?.targetKey === target.key && selectedRecommendation.candidateId === candidate.id) ?? recommendations[0];
  const detailCandidate = isConverged ? (comparisonSelection === "record" ? recordBest!.candidate : outputCandidate) : selected;
  const detailSource = isConverged ? (comparisonSelection === "record" ? `回流记录最优 · ${recordBest!.source}` : `预设输出候选 ${outputComponent.id}`)
    : `${round.label} ${target.shortLabel} 预设推荐批次`;
  const nextLabel = isConverged ? "进入 S4 候选输出" : viewRound === 0 ? "进入 R2" : "进入收敛";
  const statusText = isConverged ? "回流记录已汇总 · 后续输出保持预设" : missingCount > 0 ? `待补 ${missingCount} 项：${missingTargets}`
    : validationConfirmed ? `本批已确认，可${nextLabel}` : `${requiredCount} 项演示值已填，尚未确认本批`;

  useLayoutEffect(() => {
    const footer = footerRef.current;
    const scroll = footer?.closest<HTMLElement>(".ht-scroll-region");
    if (!footer || !scroll) return;
    const syncFooter = () => scroll.style.setProperty("--ht-s3-footer-height", `${footer.offsetHeight}px`);
    syncFooter();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(syncFooter);
    observer?.observe(footer);
    return () => {
      observer?.disconnect();
      scroll.style.removeProperty("--ht-s3-footer-height");
    };
  }, []);

  useLayoutEffect(() => {
    if (previousRound.current !== viewRound) {
      const nav = roundNavRef.current;
      const scroll = nav?.closest<HTMLElement>(".ht-scroll-region");
      if (nav && scroll) {
        const bounds = nav.getBoundingClientRect();
        const region = scroll.getBoundingClientRect();
        if (bounds.top < region.top) scroll.scrollTop += bounds.top - region.top - 12;
      }
      document.getElementById(`${id}-round-${viewRound}`)?.focus({ preventScroll: true });
    }
    previousRound.current = viewRound;
  }, [viewRound, id]);

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
      scroll.scrollTo({ top: scroll.scrollTop + bounds.top - region.top - 12,
        behavior: window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
    }
  }

  function handleRoundKey(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    if (isBusy) return;
    let next: number;
    if (event.key === "ArrowRight") next = (index + 1) % (progressRound + 1);
    else if (event.key === "ArrowLeft") next = (index + progressRound) % (progressRound + 1);
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = progressRound;
    else return;
    event.preventDefault();
    onViewRound(next);
    document.getElementById(`${id}-round-${next}`)?.focus({ preventScroll: true });
  }

  return (
    <div className="ht-s1-stage ht-s3-stage" aria-busy={isBusy} data-progress-round={progressRound} data-view-round={viewRound}>
      <div className="ht-s1-content">
        <div className="ht-workbench-demo-note"><BadgeInfo aria-hidden="true" size={16} />
          <span><strong>演示说明：</strong>当前按预设路径演示单性质迭代。验证值参与回流记录及已回流样本最优比较，不重训模型，不改变预设热点、推荐顺序和后续配方路径。</span>
        </div>
        <div className="ht-s1-summary">
          <div className="ht-s1-scenario-facts"><Layers3 aria-hidden="true" /><span>{materialType}</span>
            <span><b>{candidateTotal.toLocaleString("en-US")}</b> 候选</span><span>{representation} · 2D</span></div>
          <div className="ht-s3-summary-status" role="status" aria-live="polite">
            {isReview ? <LockKeyhole aria-hidden="true" /> : <CheckCircle2 aria-hidden="true" />}
            <span>{isReview ? `回看模式 · ${round.label} 已完成` : `当前轮次 · ${round.label}`}</span>
            <span>每目标证据 <b>{active.priorRecords.length}</b> DOE + <b>{active.returnedRecords.length}</b> 回流</span>
          </div>
        </div>
        <nav ref={roundNavRef} className="ht-s3-round-nav" aria-label="S3 迭代轮次">
          {ITERATION_ROUNDS.map((item, index) => (
            <button key={item.label} type="button" id={`${id}-round-${index}`} className={cn(viewRound === index && "selected")}
              aria-label={`${item.label} ${index < progressRound ? "已完成" : index === progressRound ? "当前" : "未开始"}`}
              aria-current={viewRound === index ? "step" : undefined} disabled={isBusy || index > progressRound}
              onClick={() => onViewRound(index)} onKeyDown={(event) => handleRoundKey(event, index)}>
              <span className={index === 2 ? "ht-s3-round-convergence" : "ht-s3-round-number"} aria-hidden="true">
                {index === 2 ? <GitCompareArrows aria-hidden="true" /> : index < progressRound ? <Check aria-hidden="true" /> : item.label}
              </span>
              <span><strong>{item.title}</strong><small>{item.description}</small></span>
              <em>{index < progressRound ? "已完成" : index === progressRound ? "当前" : "未开始"}</em>
            </button>
          ))}
        </nav>

        <AgentDockLayout targets={targets} activeTargetKey={activeTargetKey} onSelectTarget={onSelectTarget} mapRef={mapRef} disabled={isBusy}
          renderAgent={(props) => {
            const summary = summaries.find((item) => item.target.key === props.target.key)!;
            return <AgentCardShell {...props} stageLabel={`${round.label} · 迭代回流`} targetValueLabel={formatPriorValue(props.target, props.target.target)}>
              <div className="ht-s3-agent-data" data-agent-inset="true">
                <div className="ht-s3-agent-counts"><span>DOE <b>{summary.priorRecords.length}</b></span><span>回流 <b>{summary.returnedRecords.length}</b></span><span>推荐 <b>{summary.recommendations.length}</b></span></div>
                <div className="ht-s3-agent-best"><span>回流记录最优</span><strong>{summary.recordBest?.candidate.id ?? "—"}</strong>
                  <p>{summary.recordBest ? formatPriorValue(props.target, summary.recordBest.value) : "—"} <small>{displayTargetUnit(props.target)}</small></p>
                  <small>{summary.recordBest?.source} · {summary.meetsTarget ? "已达阈值" : "未达阈值"}</small></div>
                <div className="ht-s3-agent-script"><span>预设路径候选</span><strong>{summary.scriptCandidate?.id ?? "—"}</strong></div>
                <p className="ht-s3-agent-decision"><b>预设决策 · {round.label}</b>{summary.round.explanation}</p>
              </div>
              <div className={cn("ht-s1-agent-status", (isConverged || validationConfirmed) && "ready")}>
                {isConverged || validationConfirmed ? <CheckCircle2 aria-hidden="true" /> : <Circle aria-hidden="true" />}
                <span>{isConverged ? "回流已汇总 · 输出保持预设" : `已填 ${summary.filledCount}/${summary.recommendations.length} · ${validationConfirmed ? "本批已确认" : "本批待确认"}`}</span>
              </div>
            </AgentCardShell>;
          }}>
          <section className="ht-s1-space-panel" aria-labelledby={`${id}-space-heading`}>
            <header className="ht-s1-space-header"><Layers3 className="ht-s1-space-watermark" aria-hidden="true" />
              <div className="ht-s1-section-title"><span className="ht-s1-section-index">01</span><div>
                <h3 id={`${id}-space-heading`}>{round.label} 回流证据与候选空间</h3>
                <p>{isConverged ? "最终批次已计入回流记录；热点和输出候选保持预设" : `${viewRound === 0 ? "S2" : "S2 与 R1"} 验证已回流；${round.label} ${isReview ? "批次供只读回看" : "新推荐待验证"}`}</p>
              </div></div>
              <div className="ht-s1-space-focus" style={{ "--target-color": target.color } as CSSProperties}><PropertyIcon aria-hidden="true" /><span>当前目标 <strong>{target.shortLabel}</strong></span></div>
            </header>
            <div className="ht-s1-space-body">
              <TargetTabs targets={targets} activeTargetKey={activeTargetKey} onSelectTarget={onSelectTarget} disabled={isBusy}
                tabId={id} panelId={`${id}-space`} label="切换单性质迭代目标"
                renderIcon={(item) => { const Icon = AGENT_PROPERTY_ICONS[item.key]; return <Icon aria-hidden="true" />; }} />
              <div className="ht-s1-map ht-s3-map" id={`${id}-space`} role="tabpanel" aria-labelledby={`${id}-${activeTargetKey}`} tabIndex={0}>
                <span className="ht-s3-axis-label">{representation} 投影维度 Y</span>
                <div ref={mapRef}>{renderCandidateMap(active, selectFromMap)}</div>
                <span className="ht-s3-axis-label horizontal">{representation} 投影维度 X</span>
              </div>
              <div className="ht-s1-map-legend" style={{ "--target-color": target.color } as CSSProperties}>
                <span><i className="candidate" aria-hidden="true" />候选点</span><span><i className="doe" aria-hidden="true" />DOE <b>{active.priorRecords.length}</b></span>
                <span><i className="returned" aria-hidden="true" />已回流 <b>{active.returnedRecords.length}</b></span>
                <span><i className="pending" aria-hidden="true" />{isReview ? "本轮验证（回看）" : "待验证"} <b>{recommendations.length}</b></span>
                <span><Star aria-hidden="true" />记录最优</span><span><i className="hotspot" aria-hidden="true" />预设热点</span>
                <span className="ht-s3-heat-note">浓淡仅作区域示意，非预测值或置信概率；{target.shortLabel} {target.direction === "lower" ? "越低越好" : "越高越好"}</span>
              </div>
              <div className="ht-s3-result-summary" style={{ "--target-color": target.color } as CSSProperties}>
                <div className="ht-s3-record-summary"><span><Star aria-hidden="true" />回流记录最优</span><strong>{recordBest?.candidate.id ?? "—"}</strong>
                  <span><b>{recordBest ? formatPriorValue(target, recordBest.value) : "—"}</b> {displayTargetUnit(target)}</span><small>来源：{recordBest?.source}</small>
                  <span>目标 {target.direction === "lower" ? "≤" : "≥"} <b>{formatPriorValue(target, target.target)}</b> {displayTargetUnit(target)}</span>
                  <span className={cn("ht-s3-threshold-state", meetsTarget && "met")}>{meetsTarget ? <CheckCircle2 aria-hidden="true" /> : <Target aria-hidden="true" />}{meetsTarget ? "已达到场景阈值" : "尚未达到场景阈值"}</span>
                </div>
                <div className="ht-s3-script-summary"><span><GitCompareArrows aria-hidden="true" />预设路径候选</span><strong>{scriptCandidate?.id ?? "—"}</strong><small>与回流记录独立 · 不随输入改变</small></div>
              </div>
            </div>
          </section>
        </AgentDockLayout>

        <section className="ht-s1-preview ht-s3-validation ht-validation-workspace" aria-labelledby={`${id}-validation-heading`} style={{ "--target-color": target.color } as CSSProperties}>
          <header className="ht-s1-preview-header">
            <div className="ht-s1-section-title"><span className="ht-s1-section-index">02</span><div>
              <h3 id={`${id}-validation-heading`}>{target.shortLabel} {isConverged ? "收敛结果对照" : `${round.label} 推荐点验证`}</h3>
              <p>{isConverged ? "记录最优与预设输出分别展示；选择卡片比较结构" : isReview ? "只读回看：本轮验证尚未计入此轮图中证据，推进后的记录见下一轮" : `已预填示例值，可修改；每目标 ${recommendations.length} 项，共 ${requiredCount} 项，推进后计入回流`}</p>
            </div></div>
            <span className="ht-s2-validation-threshold">目标 {target.direction === "lower" ? "≤" : "≥"} <b>{formatPriorValue(target, target.target)}</b> {displayTargetUnit(target)}</span>
          </header>
          <div className="ht-s2-validation-body">
            {isConverged ? <div className="ht-s3-comparison-grid">
              <button type="button" className={cn("ht-s3-comparison-card", comparisonSelection === "record" && "selected")}
                aria-pressed={comparisonSelection === "record"} onClick={() => setComparisonSelection("record")} disabled={isBusy}>
                <span><Star aria-hidden="true" />回流记录最优</span><strong>{recordBest?.candidate.id ?? "—"}</strong>
                <b>{recordBest ? formatPriorValue(target, recordBest.value) : "—"} <small>{displayTargetUnit(target)}</small></b>
                <small>来源：{recordBest?.source} · {meetsTarget ? "已达到场景阈值" : "尚未达到场景阈值"}</small>
              </button>
              <button type="button" className={cn("ht-s3-comparison-card", comparisonSelection === "script" && "selected")}
                aria-pressed={comparisonSelection === "script"} onClick={() => setComparisonSelection("script")} disabled={isBusy}>
                <span><GitCompareArrows aria-hidden="true" />预设输出候选 <b className="ht-s3-output-id">{outputComponent.id}</b></span><strong>{outputCandidate.id}</strong>
                <b>{formatPriorValue(target, outputCandidate.scores[target.key])} <small>{displayTargetUnit(target)} · 预测/模拟</small></b>
                <small>来源：固定场景输出 · 不随验证值改变</small>
              </button>
            </div> : <RecommendationValidationCards target={target} recommendations={recommendations} selectedId={selected?.id}
              values={validationValues} disabled={isBusy} readOnly={isReview} cardRefs={cardRefs}
              onSelect={(candidateId) => onSelectRecommendation({ targetKey: target.key, candidateId })}
              onChange={(candidateId, value) => onValidationValueChange(target.key, candidateId, value)} />}
            <CandidateStructureDetails candidate={detailCandidate} sourceLabel={detailSource} disabled={isBusy} />
          </div>
        </section>
      </div>
      <footer ref={footerRef} className={cn("ht-s1-footer ht-s3-footer", isReview && "review")}>
        {isReview ? <button type="button" className="ht-s1-primary-button" onClick={() => onViewRound(progressRound)} disabled={isBusy}>
          返回当前轮次<ChevronRight aria-hidden="true" /></button> : <>
          <button type="button" className="ht-s1-secondary-button ht-s3-back" onClick={onBack} disabled={isBusy}><ArrowLeft aria-hidden="true" />返回 S2 推荐验证</button>
          <p className={cn("ht-s3-batch-status", (validationConfirmed || isConverged) && "confirmed")} role="status" aria-live="polite">
            {isBusy ? <LoaderCircle className="ht-s1-spinner" aria-hidden="true" /> : validationConfirmed || isConverged ? <CheckCircle2 aria-hidden="true" /> : missingCount ? <TriangleAlert aria-hidden="true" /> : <Circle aria-hidden="true" />}
            <span>{transitionMessage || statusText}</span>
          </p>
          <div className="ht-s3-batch-actions">
            {!isConverged ? <button type="button" className="ht-s1-secondary-button" onClick={onConfirm} disabled={missingCount > 0 || validationConfirmed || isBusy}>
              {validationConfirmed ? <Check aria-hidden="true" /> : null}{validationConfirmed ? "本批已确认" : `确认本批 ${requiredCount} 项验证值`}</button> : null}
            <button type="button" className="ht-s1-primary-button" onClick={onNext} disabled={!canAdvance || isBusy}>
              {isBusy ? <LoaderCircle className="ht-s1-spinner" aria-hidden="true" /> : null}{isBusy ? "演示回流中" : nextLabel}{!isBusy ? <ChevronRight aria-hidden="true" /> : null}</button>
          </div>
        </>}
      </footer>
    </div>
  );
}
