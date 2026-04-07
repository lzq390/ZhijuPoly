import type { SmilesQueryRequest } from "../types";
import { Button } from "./ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "./ui/card";
import { Input } from "./ui/input";
import { Select } from "./ui/select";

type QueryPanelProps = {
  request: SmilesQueryRequest;
  onChange: (request: SmilesQueryRequest) => void;
  onSubmit: () => void;
  disabled?: boolean;
};

export function QueryPanel({
  request,
  onChange,
  onSubmit,
  disabled = false
}: QueryPanelProps) {
  return (
    <Card className="bg-accent">
      <CardHeader>
        <CardTitle>Query Controls</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="space-y-2">
          <label className="text-sm font-medium">Match Mode</label>
          <Select
            value={request.match_mode}
            onChange={(event) =>
              onChange({
                ...request,
                match_mode: event.target.value as SmilesQueryRequest["match_mode"]
              })
            }
          >
            <option value="exact">Exact</option>
            <option value="similarity">Similarity</option>
          </Select>
        </div>
        <div className="space-y-2">
          <label className="text-sm font-medium">Similarity Threshold</label>
          <Input
            type="number"
            value={request.similarity_threshold}
            onChange={(event) =>
              onChange({
                ...request,
                similarity_threshold: Number(event.target.value)
              })
            }
          />
        </div>
        <div className="space-y-2">
          <label className="text-sm font-medium">Top K</label>
          <Input
            type="number"
            value={request.top_k}
            onChange={(event) =>
              onChange({
                ...request,
                top_k: Number(event.target.value)
              })
            }
          />
        </div>
        <Button className="w-full" size="lg" onClick={onSubmit} disabled={disabled}>
          Run Query
        </Button>
      </CardContent>
    </Card>
  );
}
