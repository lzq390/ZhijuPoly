import { Atom } from "lucide-react";
import type { PolymerResult } from "../types";
import { Card } from "./ui/card";

type PolymerCardProps = {
  result: PolymerResult;
};

export function PolymerCard({ result }: PolymerCardProps) {
  const displaySmiles = result.canonical_smiles || result.smiles;
  const similarityText = result.similarity_score !== null ? result.similarity_score.toFixed(3) : "N/A";

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
          <span className="text-mutedForeground">相似度</span>
          <span className="font-semibold text-teal-700">{similarityText}</span>
        </div>
      </div>
    </Card>
  );
}
