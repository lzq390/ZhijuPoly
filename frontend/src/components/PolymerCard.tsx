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

  return (
    <Card className="bg-card">
      <CardHeader className="gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <CardTitle>{result.polymer_name}</CardTitle>
            {result.similarity_score !== null ? (
            <Badge>{`Similarity ${result.similarity_score.toFixed(3)}`}</Badge>
            ) : null}
        </div>
        <div className="text-sm text-mutedForeground">SMILES: {result.smiles}</div>
      </CardHeader>
      <CardContent>
        <div className="grid gap-4 md:grid-cols-2">
          {groups.map((group) => (
            <div key={group.title}>
              <PropertyGroupCard title={group.title} items={group.items} />
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}
