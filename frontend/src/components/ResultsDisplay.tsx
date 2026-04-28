import type { ReactNode } from "react";
import { Database, LoaderCircle, ScanSearch, SearchX, Timer, TriangleAlert } from "lucide-react";
import type {
  PredictableProperty,
  PredictResponse,
  ResultsTab,
  SmilesQueryRequest,
  SmilesQueryResponse
} from "../types";
import { PREDICT_PROPERTY_META } from "../types";
import { cn } from "../lib/utils";
import { PolymerCard } from "./PolymerCard";
import { PredictionResults } from "./PredictionResults";
import { SummaryMetric } from "./SummaryMetric";
import { Alert } from "./ui/alert";
import { Badge } from "./ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "./ui/card";

type ResultsDisplayProps = {
  data: SmilesQueryResponse | null;
  error: string | null;
  isLoading?: boolean;
  request: SmilesQueryRequest;
  predictData: PredictResponse | null;
  isPredicting?: boolean;
  predictError: string | null;
  activeTab: ResultsTab;
  onTabChange: (tab: ResultsTab) => void;
};

function EmptyState({
  icon,
  title,
  description
}: {
  icon: ReactNode;
  title: string;
  description: string;
}) {
  return (
    <div className="flex min-h-[320px] flex-col items-center justify-center rounded-[24px] border border-dashed border-white bg-[linear-gradient(180deg,rgba(255,255,255,0.72)_0%,rgba(244,248,249,0.78)_100%)] px-6 py-12 text-center">
      <div className="flex h-14 w-14 items-center justify-center rounded-2xl border border-white bg-white/85 text-slate-600 shadow-sm">
        {icon}
      </div>
      <div className="mt-5 text-lg font-semibold text-slate-900">{title}</div>
      <div className="mt-2 max-w-xl text-sm leading-6 text-mutedForeground">{description}</div>
    </div>
  );
}

function QueryResultsPanel({
  data,
  error,
  isLoading = false,
  request
}: {
  data: SmilesQueryResponse | null;
  error: string | null;
  isLoading?: boolean;
  request: SmilesQueryRequest;
}) {
  if (error) {
    return (
      <Card className="overflow-hidden rounded-[28px] border-destructive/20 shadow-none">
        <CardHeader className="min-h-[112px] border-b border-destructive/10 bg-destructiveForeground">
          <CardTitle className="flex items-center gap-2 text-lg text-destructive">
            <TriangleAlert className="h-5 w-5" />
            Match Failed
          </CardTitle>
          <CardDescription>The request did not complete. Check the structure input, match mode, or service status.</CardDescription>
        </CardHeader>
        <CardContent className="pt-5">
          <Alert variant="destructive">{error}</Alert>
        </CardContent>
      </Card>
    );
  }

  if (isLoading) {
    return (
      <Card className="overflow-hidden rounded-[28px] border-white/70 shadow-none">
        <CardHeader className="min-h-[112px] border-b border-white/70 bg-[linear-gradient(180deg,rgba(255,255,255,0.98)_0%,rgba(244,248,249,0.88)_100%)]">
          <CardTitle className="text-xl">Similarity Matching Results</CardTitle>
          <CardDescription>Finding similar polymer records and preparing their property summaries.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4 pt-5">
          <div className="flex items-center gap-3 rounded-[20px] border border-white/80 bg-white/80 px-4 py-3 text-sm text-slate-700">
            <LoaderCircle className="h-4 w-4 animate-spin" />
            Loading results. This panel will refresh when the response returns.
          </div>
          <div className="grid gap-4">
            {[0, 1].map((index) => (
              <div key={index} className="space-y-4 rounded-[24px] border border-slate-200 bg-white p-5">
                <div className="grid auto-rows-fr gap-3 md:grid-cols-2 xl:grid-cols-4">
                  {[0, 1, 2, 3].map((item) => (
                    <div key={item} className="h-[144px] animate-pulse rounded-[18px] bg-slate-100/90" />
                  ))}
                </div>
                <div className="grid gap-4 xl:grid-cols-3">
                  {[0, 1, 2, 3, 4, 5].map((item) => (
                    <div key={item} className="h-40 animate-pulse rounded-[18px] bg-slate-50/90" />
                  ))}
                </div>
              </div>
            ))}
          </div>
        </CardContent>
      </Card>
    );
  }

  if (!data) {
    return (
      <Card className="overflow-hidden rounded-[28px] border-white/70 shadow-none">
        <CardHeader className="min-h-[112px] border-b border-white/70 bg-[linear-gradient(180deg,rgba(255,255,255,0.98)_0%,rgba(244,248,249,0.88)_100%)]">
          <CardTitle className="text-xl">Similarity Matching Results</CardTitle>
          <CardDescription>No similarity match results yet.</CardDescription>
        </CardHeader>
        <CardContent className="pt-5">
          <EmptyState
            icon={<Database className="h-6 w-6" />}
            title="Results Panel Ready"
            description="Run similarity matching to see summaries, matching records, and property groups."
          />
        </CardContent>
      </Card>
    );
  }

  if (data.total === 0) {
    return (
      <Card className="overflow-hidden rounded-[28px] border-white/70 shadow-none">
        <CardHeader className="min-h-[112px] border-b border-white/70 bg-[linear-gradient(180deg,rgba(255,255,255,0.98)_0%,rgba(244,248,249,0.88)_100%)]">
          <CardTitle className="text-xl">Similarity Matching Results</CardTitle>
          <CardDescription>The match completed successfully, but no displayable records were found.</CardDescription>
        </CardHeader>
        <CardContent className="pt-5">
          <EmptyState
            icon={<SearchX className="h-6 w-6" />}
            title="No Matches Found"
            description="Check the current SMILES or switch to another similarity matching mode."
          />
        </CardContent>
      </Card>
    );
  }

  const predictedPropertyMeta =
    data.predicted_property_name ? PREDICT_PROPERTY_META[data.predicted_property_name as PredictableProperty] : null;
  const predictedPropertyText =
    data.predicted_property_value !== null
      ? `${data.predicted_property_value.toPrecision(6)} ${
          data.predicted_property_unit || predictedPropertyMeta?.unit || ""
        }`.trim()
      : null;
  const predictedPropertyLabel =
    predictedPropertyMeta?.label || data.predicted_property_name || "Selected property";

  return (
    <Card className="overflow-hidden rounded-[28px] border-white/70 shadow-none">
      <CardHeader className="min-h-[120px] gap-4 border-b border-white/70 bg-[linear-gradient(180deg,rgba(255,255,255,0.98)_0%,rgba(244,248,249,0.88)_100%)]">
        <div className="flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between">
          <div className="space-y-2">
            <div className="text-[11px] font-medium uppercase tracking-[0.22em] text-teal-700/80">
              Similarity Dataset
            </div>
            <CardTitle className="text-[1.4rem] tracking-tight">Similarity Matching Results</CardTitle>
            <CardDescription>
              {data.match_type === "property"
                ? "Summaries, 2D structures, SMILES, and selected property values are shown here."
                : "Summaries, 2D structures, SMILES, and similarity scores are shown here."}
            </CardDescription>
          </div>
          <div className="flex flex-wrap gap-2">
            <Badge>{data.match_type === "property" ? "Property Similarity" : "Structural Similarity"}</Badge>
            {data.match_type === "property" && predictedPropertyText ? (
              <Badge className="text-slate-700">{`${predictedPropertyLabel} ${predictedPropertyText}`}</Badge>
            ) : null}
            <Badge className="text-slate-700">{`${data.total} results`}</Badge>
            <Badge className="text-slate-700">{`${data.query_time_ms.toFixed(1)} ms`}</Badge>
          </div>
        </div>

        <div className="grid auto-rows-fr gap-3 md:grid-cols-2 xl:grid-cols-4">
          <SummaryMetric
            icon={<ScanSearch className="h-4 w-4 text-teal-600" />}
            label="Match Mode"
            value={data.match_type === "property" ? "Property Similarity" : "Structural Similarity"}
            detail="Current similarity matching mode."
          />
          <SummaryMetric label="Result Count" value={String(data.total)} detail="Total matched polymer records." />
          <SummaryMetric
            icon={<Timer className="h-4 w-4 text-teal-600" />}
            label="Elapsed Time"
            value={`${data.query_time_ms.toFixed(1)} ms`}
            detail="Time spent finding and preparing results."
          />
          <SummaryMetric label="Input SMILES" value={request.smiles || "Not available"} detail="Current structure input." mono />
        </div>
      </CardHeader>

      <CardContent className="grid gap-4 pt-5 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
        {data.results.map((result) => (
          <PolymerCard
            key={result.polymer_id}
            result={result}
            matchType={data.match_type}
            selectedProperty={request.property_name}
          />
        ))}
      </CardContent>
    </Card>
  );
}

export function ResultsDisplay({
  data,
  error,
  isLoading = false,
  request,
  predictData,
  isPredicting = false,
  predictError,
  activeTab,
  onTabChange
}: ResultsDisplayProps) {
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          onClick={() => onTabChange("query")}
          className={cn(
            "rounded-full border px-4 py-2 text-sm font-medium transition-colors duration-200",
            activeTab === "query"
              ? "border-teal-500/30 bg-teal-50 text-teal-800"
              : "border-white/80 bg-white/80 text-slate-600 hover:border-slate-200"
          )}
        >
          Similarity Results
        </button>
        <button
          type="button"
          onClick={() => onTabChange("predict")}
          className={cn(
            "rounded-full border px-4 py-2 text-sm font-medium transition-colors duration-200",
            activeTab === "predict"
              ? "border-teal-500/30 bg-teal-50 text-teal-800"
              : "border-white/80 bg-white/80 text-slate-600 hover:border-slate-200"
          )}
        >
          Prediction Results
        </button>
      </div>

      {activeTab === "query" ? (
        <QueryResultsPanel data={data} error={error} isLoading={isLoading} request={request} />
      ) : (
        <PredictionResults data={predictData} isLoading={isPredicting} error={predictError} />
      )}
    </div>
  );
}
