import { Braces, Fingerprint, Layers3 } from "lucide-react";
import type { PolymerResult } from "../types";
import { PropertyGroupCard } from "./PropertyGroupCard";
import { Badge } from "./ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "./ui/card";

type PolymerCardProps = {
  result: PolymerResult;
};

export function PolymerCard({ result }: PolymerCardProps) {
  const groups = [
    { title: "Thermal", items: result.properties.thermal },
    { title: "Mechanical", items: result.properties.mechanical },
    { title: "Electrical", items: result.properties.electrical },
    { title: "Chemical", items: result.properties.chemical },
    { title: "Optical", items: result.properties.optical },
    { title: "Other", items: result.properties.other }
  ];
  const propertyCount = groups.reduce((sum, group) => sum + group.items.length, 0);
  const populatedGroups = groups.filter((group) => group.items.length > 0).length;

  return (
    <Card className="overflow-hidden rounded-[26px] border-slate-200/90">
      <CardHeader className="min-h-[208px] gap-5 border-b border-slate-200/80 bg-[linear-gradient(180deg,#ffffff_0%,#f8fafc_100%)]">
        <div className="flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between">
          <div className="space-y-2">
            <div className="flex flex-wrap items-center gap-2">
              <CardTitle className="text-xl">{result.polymer_name}</CardTitle>
              {result.similarity_score !== null ? (
                <Badge>{`Similarity ${result.similarity_score.toFixed(3)}`}</Badge>
              ) : (
                <Badge className="bg-emerald-50 text-emerald-700">精确命中</Badge>
              )}
            </div>
            <div className="text-sm leading-6 text-mutedForeground">
              查询命中实体与属性将以下方分组形式展示。
            </div>
          </div>
          <div className="rounded-[20px] border border-slate-200 bg-white px-4 py-3 text-sm text-mutedForeground">
            Polymer ID <span className="ml-2 font-medium text-slate-900">{result.polymer_id}</span>
          </div>
        </div>

        <div className="grid auto-rows-fr gap-3 lg:grid-cols-3">
          <div className="flex min-h-[116px] flex-col rounded-[18px] border border-slate-200 bg-white p-4">
            <div className="flex items-center gap-2 text-xs font-medium uppercase tracking-[0.16em] text-mutedForeground">
              <Braces className="h-4 w-4 text-blue-600" />
              Matched SMILES
            </div>
            <div className="mt-3 flex-1 font-mono text-[13px] leading-6 text-slate-700">{result.smiles}</div>
          </div>
          <div className="flex min-h-[116px] flex-col rounded-[18px] border border-slate-200 bg-white p-4">
            <div className="flex items-center gap-2 text-xs font-medium uppercase tracking-[0.16em] text-mutedForeground">
              <Fingerprint className="h-4 w-4 text-blue-600" />
              Similarity
            </div>
            <div className="mt-3 flex-1 text-lg font-semibold text-slate-950">
              {result.similarity_score !== null ? result.similarity_score.toFixed(3) : "Exact"}
            </div>
          </div>
          <div className="flex min-h-[116px] flex-col rounded-[18px] border border-slate-200 bg-white p-4">
            <div className="flex items-center gap-2 text-xs font-medium uppercase tracking-[0.16em] text-mutedForeground">
              <Layers3 className="h-4 w-4 text-blue-600" />
              Property Coverage
            </div>
            <div className="mt-3 flex-1 text-lg font-semibold text-slate-950">
              {propertyCount} 项属性 / {populatedGroups} 个分组
            </div>
          </div>
        </div>
      </CardHeader>

      <CardContent className="pt-6">
        <div className="grid auto-rows-fr gap-4 xl:grid-cols-3">
          {groups.map((group) => (
            <PropertyGroupCard key={group.title} title={group.title} items={group.items} />
          ))}
        </div>
      </CardContent>
    </Card>
  );
}
