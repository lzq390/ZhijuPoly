import { ChevronDown, ChevronRight, TriangleAlert } from "lucide-react";
import { type RefObject, useId, useState } from "react";
import type { HighThroughputCandidate, HighThroughputTarget } from "../../constants/highThroughputDemoScenario";
import { cn } from "../../lib/utils";
import { displayTargetUnit, fallbackCandidateSmiles, formatPriorValue, validationNumber } from "./prior-hotspot-model";
import type { RecommendationValidationValues } from "./types";
import "./recommendation-validation.css";

export function RecommendationValidationCards({ target, recommendations, selectedId, values, disabled, readOnly = false,
  onSelect, onChange, cardRefs }: {
  target: HighThroughputTarget;
  recommendations: HighThroughputCandidate[];
  selectedId?: string;
  values: RecommendationValidationValues;
  disabled: boolean;
  readOnly?: boolean;
  onSelect: (candidateId: string) => void;
  onChange: (candidateId: string, value: string) => void;
  cardRefs: RefObject<Map<string, HTMLElement>>;
}) {
  const id = useId();
  return <div className={cn("ht-s2-candidate-grid", recommendations.length === 1 && "ht-validation-single")} aria-label={`${target.shortLabel} 推荐验证点`}>
    {recommendations.map((candidate, index) => {
      const inputId = `${id}-${target.key}-${candidate.id}-value`;
      const value = values[`${target.key}:${candidate.id}`] ?? "";
      const valid = validationNumber(values, target.key, candidate.id) !== null;
      return <article key={candidate.id} ref={(element) => { if (element) cardRefs.current.set(candidate.id, element); else cardRefs.current.delete(candidate.id); }}
        className={cn("ht-s2-validation-candidate", candidate.id === selectedId && "selected")}>
        <button className="ht-s2-candidate-select" type="button" aria-pressed={candidate.id === selectedId} disabled={disabled}
          aria-label={`查看 ${candidate.id} 推荐详情`} onClick={() => onSelect(candidate.id)}>
          <span className="ht-s2-candidate-number">{String(index + 1).padStart(2, "0")}</span>
          <span><strong>{candidate.id}</strong><small>{candidate.monomerA} + {candidate.monomerB}</small></span>
          <span className="ht-s2-predicted-value">预测 <b>{formatPriorValue(target, candidate.scores[target.key])}</b> {displayTargetUnit(target)}</span>
          <ChevronRight aria-hidden="true" />
        </button>
        <div className="ht-s2-value-field">
          {readOnly ? <>
            <div className="ht-validation-readonly-label">演示验证值 <span>本轮已确认 · 只读</span></div>
            <div className="ht-validation-readonly-value" aria-label={`${candidate.id} ${target.shortLabel} 已确认演示验证值`}>
              <strong>{value || "—"}</strong><span>{displayTargetUnit(target)}</span>
            </div>
          </> : <>
            <label htmlFor={inputId}>演示验证值 <span>{valid ? "已填值 · 可编辑" : "待补值"}</span></label>
            <div className={cn("ht-s2-value-control", !valid && "invalid")}>
              <input id={inputId} type="number" inputMode="decimal" step={target.key === "modulus" ? "0.1" : "1"}
                value={value} disabled={disabled} aria-label={`${candidate.id} ${target.shortLabel} 演示验证值`}
                aria-invalid={!valid} aria-describedby={!valid ? `${inputId}-error` : undefined}
                onFocus={() => { if (!disabled) onSelect(candidate.id); }}
                onChange={(event) => onChange(candidate.id, event.currentTarget.value)} />
              <span>{displayTargetUnit(target)}</span>
            </div>
            {!valid ? <p className="ht-s2-field-error" id={`${inputId}-error`} role="alert"><TriangleAlert aria-hidden="true" />请输入有效数值后再确认本批</p> : null}
          </>}
        </div>
      </article>;
    })}
  </div>;
}

export function CandidateStructureDetails({ candidate, sourceLabel, disabled }: {
  candidate: HighThroughputCandidate;
  sourceLabel: string;
  disabled: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const id = useId();
  const smiles = fallbackCandidateSmiles(candidate);
  return <section className="ht-s2-structure-detail" aria-label={`${candidate.id} 候选详情`}>
    <div className="ht-s2-structure-summary">
      <div><span>当前候选 <strong>{candidate.id}</strong></span><p>来源：{sourceLabel} · 结构类别：<span>{candidate.cluster}</span>
        {candidate.sourcePiId !== undefined ? <> · PI 来源 <b>{candidate.sourcePiId}</b></> : null}</p></div>
      <button type="button" className="ht-s1-detail-toggle" aria-expanded={expanded} aria-controls={id} disabled={disabled} onClick={() => setExpanded((value) => !value)}>
        {expanded ? "收起结构详情" : "展开结构详情"}<ChevronDown aria-hidden="true" />
      </button>
    </div>
    <div id={id} className="ht-s2-structure-fields" hidden={!expanded}>
      <p>预设结构用于演示候选追踪，不代表真实实验产物。</p>
      <dl>{[["Polymer SMILES", smiles.polymerSmiles], ["单体 A SMILES", smiles.monomerASmiles], ["单体 B SMILES", smiles.monomerBSmiles]].map(([label, value]) =>
        <div key={label}><dt>{label}</dt><dd><code>{value}</code></dd></div>)}</dl>
    </div>
  </section>;
}
