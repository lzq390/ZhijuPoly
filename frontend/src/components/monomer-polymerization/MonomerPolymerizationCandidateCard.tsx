import { Check, ChevronDown, Copy, FlaskConical } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { MonomerPolymerizationCandidate } from "../../types";
import { StructureSvg } from "../StructureSvg";

export function MonomerPolymerizationCandidateCard({
  candidate
}: {
  candidate: MonomerPolymerizationCandidate;
}) {
  const [smilesExpanded, setSmilesExpanded] = useState(false);
  const [copyState, setCopyState] = useState<"copied" | "failed" | null>(null);
  const copyTimerRef = useRef<number | null>(null);

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
        <div className="np-mp-candidate-card__rank">RANK {candidate.rank}</div>
        <div>
          <h3>{candidate.polymer_class}</h3>
          <span>Reaction ID {candidate.reaction_id ?? "--"}</span>
        </div>
        <FlaskConical aria-hidden="true" />
      </header>

      <div className="np-mp-candidate-card__structure">
        {candidate.structure_svg ? (
          <StructureSvg
            svg={candidate.structure_svg}
            alt={`Rank ${candidate.rank} 聚合物候选结构`}
            className="np-mp-candidate-card__structure-svg"
            imageClassName="np-mp-candidate-card__structure-image"
            transparentBackground
          />
        ) : (
          <div className="np-mp-candidate-card__structure-empty">该候选未返回结构图</div>
        )}
      </div>

      <div className="np-mp-candidate-card__smiles">
        <div>
          <span>POLYMER SMILES</span>
          <div className={smilesExpanded ? "is-expanded" : ""}>{candidate.polymer_smiles}</div>
        </div>
        <div className="np-mp-candidate-card__smiles-actions">
          <button
            type="button"
            className="np-mp-icon-action"
            aria-label={`${smilesExpanded ? "收起" : "展开"} Rank ${candidate.rank} 聚合物 SMILES`}
            aria-expanded={smilesExpanded}
            onClick={() => setSmilesExpanded((current) => !current)}
          >
            <ChevronDown className={smilesExpanded ? "is-rotated" : ""} />
          </button>
          <button
            type="button"
            className="np-mp-icon-action"
            aria-label={`复制 Rank ${candidate.rank} 聚合物 SMILES`}
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
          <div><dt>Monomer A</dt><dd>{candidate.monomer_a_smiles}</dd></div>
          <div><dt>Monomer B</dt><dd>{candidate.monomer_b_smiles ?? "--"}</dd></div>
          <div><dt>React set</dt><dd>{candidate.reactset.length ? candidate.reactset.join(" + ") : "--"}</dd></div>
          <div><dt>Reaction Name</dt><dd>{candidate.reaction_name ?? "--"}</dd></div>
        </dl>
      </details>
    </article>
  );
}
