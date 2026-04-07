import type { SmilesQueryResponse } from "../types";
import { PolymerCard } from "./PolymerCard";
import { Alert } from "./ui/alert";

type ResultsDisplayProps = {
  data: SmilesQueryResponse | null;
  error: string | null;
};

export function ResultsDisplay({ data, error }: ResultsDisplayProps) {
  if (error) {
    return <Alert variant="destructive">{error}</Alert>;
  }

  if (!data) {
    return <Alert>Run a query to inspect polymer results.</Alert>;
  }

  return (
    <div className="space-y-4">
      <div className="text-sm text-mutedForeground">
        {data.match_type} match, {data.total} result(s), {data.query_time_ms.toFixed(1)} ms
      </div>
      {data.results.map((result) => (
        <PolymerCard key={result.polymer_id} result={result} />
      ))}
    </div>
  );
}
