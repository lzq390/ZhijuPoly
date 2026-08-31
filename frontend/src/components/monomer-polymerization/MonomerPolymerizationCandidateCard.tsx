import { Check, ChevronDown, Copy, FlaskConical } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { MonomerPolymerizationCandidate } from "../../types";
import { StructureSvg } from "../StructureSvg";
import { TARGET_CLASS_LABELS } from "./config";

function formatPolymerClass(value: string) {
  const normalized = value.trim().toLowerCase() as keyof typeof TARGET_CLASS_LABELS;
  return TARGET_CLASS_LABELS[normalized] ?? (value.trim() || "未分类");
}

export function MonomerPolymerizationCandidateCard({
  candidate
}: {
  candidate: MonomerPolymerizationCandidate;
}) {
  const [smilesExpanded, setSmilesExpanded] = useState(false);
  const [copyState, setCopyState] = useState<"copied" | "failed" | null>(null);
  const copyTimerRef = useRef<number | null>(null);
  const polymerClassLabel = formatPolymerClass(candidate.polymer_class);

  useEffect(() => {
    return () => {
      if (copyTimerRef.current !== null) window.clearTimeout(copyTimerRef.current);
    };
  }, []);

  async function copyPolymerSmiles() {
    try {
      if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable");
      await navigator.clipboard.writeText(candidate.polymer_smiles);
      setCopyState("copied");
    } catch {
      setCopyState("failed");
    }
    if (copyTimerRef.current !== null) window.clearTimeout(copyTimerRef.current);
    copyTimerRef.current = window.setTimeout(() => setCopyState(null), 1300);
  }

  return (
    <article className="np-mp-candidate-card">
      <header>
        <div className="np-mp-candidate-card__rank">候选 {candidate.rank}</div>
        <div>
          <h3>{polymerClassLabel}</h3>
          <span>反应 ID {candidate.reaction_id ?? "未提供"}</span>
        </div>
        <FlaskConical aria-hidden="true" />
      </header>

      <div className="np-mp-candidate-card__structure">
        {candidate.structure_svg ? (
          <StructureSvg
            svg={candidate.structure_svg}
            alt={`候选 ${candidate.rank} 的聚合物结构`}
            className="np-mp-candidate-card__structure-svg"
            imageClassName="np-mp-candidate-card__structure-image"
            transparentBackground
          />
        ) : (
          <div className="np-mp-candidate-card__structure-empty">该候选没有结构图</div>
        )}
      </div>

      <div className="np-mp-candidate-card__smiles">
        <div>
          <span>聚合物 SMILES</span>
          <div className={smilesExpanded ? "is-expanded" : ""}>{candidate.polymer_smiles}</div>
        </div>
        <div className="np-mp-candidate-card__smiles-actions">
          <button
            type="button"
            className="np-mp-icon-action"
            aria-label={`${smilesExpanded ? "收起" : "展开"}候选 ${candidate.rank} 的聚合物 SMILES`}
            aria-expanded={smilesExpanded}
            onClick={() => setSmilesExpanded((current) => !current)}
          >
            <ChevronDown className={smilesExpanded ? "is-rotated" : ""} />
          </button>
          <button
            type="button"
            className="np-mp-icon-action"
            aria-label={`复制候选 ${candidate.rank} 的聚合物 SMILES`}
            title={copyState === "copied" ? "已复制" : copyState === "failed" ? "复制失败" : "复制 SMILES"}
            onClick={() => void copyPolymerSmiles()}
          >
            {copyState === "copied" ? <Check /> : <Copy />}
          </button>
        </div>
      </div>

      <details className="np-mp-candidate-card__details">
        <summary>反应与单体详情</summary>
        <dl>
          <div><dt>单体 A</dt><dd>{candidate.monomer_a_smiles}</dd></div>
          <div><dt>单体 B</dt><dd>{candidate.monomer_b_smiles ?? "未提供"}</dd></div>
          <div><dt>单体组合</dt><dd>{candidate.reactset.length ? candidate.reactset.join(" + ") : "未提供"}</dd></div>
          <div><dt>反应名称</dt><dd>{candidate.reaction_name ?? "未提供"}</dd></div>
        </dl>
      </details>
    </article>
  );
}
