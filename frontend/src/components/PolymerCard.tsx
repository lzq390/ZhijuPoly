import { Atom } from "lucide-react";
import { PREDICT_PROPERTY_META, type MatchMode, type PolymerResult, type PredictableProperty } from "../types";
import { Card } from "./ui/card";

type PolymerCardProps = {
  result: PolymerResult;
  matchType: MatchMode;
  selectedProperty: PredictableProperty | null;
};

export function PolymerCard({ result, matchType, selectedProperty }: PolymerCardProps) {
  const displaySmiles = result.canonical_smiles || result.smiles;
  const similarityText = result.similarity_score !== null ? result.similarity_score.toFixed(3) : "N/A";
  const propertyMeta = selectedProperty ? PREDICT_PROPERTY_META[selectedProperty] : null;
  const propertyValueText =
    result.matched_property_value !== null
      ? `${Number(result.matched_property_value).toPrecision(6)} ${result.matched_property_unit || propertyMeta?.unit || ""}`.trim()
      : "N/A";
  const metricLabel = matchType === "property" ? propertyMeta?.label || "所选性质值" : "相似度";
  const metricValue = matchType === "property" ? propertyValueText : similarityText;
  const propertyGroups = Object.values(result.properties);
  const sourceText =
    matchType === "property"
      ? result.matched_property_source || "N/A"
      : Array.from(
          new Set(
            propertyGroups
              .flat()
              .map((item) => item.label_source)
              .filter((source): source is string => Boolean(source))
          )
        ).join(" / ") || "N/A";

  return (
    <Card className="overflow-hidden rounded-[24px] border-white/70 bg-[linear-gradient(180deg,rgba(255,255,255,0.98)_0%,rgba(244,248,249,0.88)_100%)] p-3">
      <div className="overflow-hidden rounded-[18px] border border-white/80 bg-white/90 p-2.5 shadow-sm">
        <div className="mb-1.5 flex items-center gap-2 text-[10px] font-medium uppercase tracking-[0.18em] text-mutedForeground">
          <Atom className="h-3.5 w-3.5 text-teal-600" />
          2D Structure
        </div>
        {result.structure_svg ? (
          <div
            className="[&_svg]:mx-auto [&_svg]:h-auto [&_svg]:max-h-[170px] [&_svg]:w-full [&_svg]:max-w-full"
            dangerouslySetInnerHTML={{ __html: result.structure_svg }}
          />
        ) : (
          <div className="flex min-h-[150px] items-center justify-center rounded-[14px] bg-slate-50 px-3 text-center font-mono-ui text-xs leading-5 text-mutedForeground">
            {displaySmiles}
          </div>
        )}
      </div>

      <div className="mt-2.5 rounded-[16px] border border-white/80 bg-white/75 px-3 py-2.5 shadow-sm">
        <div className="font-mono-ui break-all text-xs leading-5 text-slate-800">{displaySmiles}</div>
        <div className="mt-1.5 flex items-center justify-between gap-3 text-xs">
          <span className="text-mutedForeground">{metricLabel}</span>
          <span className="text-right font-semibold text-teal-700">{metricValue}</span>
        </div>
        <div className="mt-1.5 flex items-center justify-between gap-3 text-xs">
          <span className="text-mutedForeground">来源</span>
          <span className="text-right font-semibold text-slate-700">{sourceText}</span>
        </div>
      </div>
    </Card>
  );
}
