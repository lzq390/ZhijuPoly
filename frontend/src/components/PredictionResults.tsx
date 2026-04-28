import { Database, LoaderCircle, Sparkles, TriangleAlert } from "lucide-react";
import { PREDICT_PROPERTY_META, PREDICTABLE_PROPERTIES, type PredictResponse } from "../types";
import { cn } from "../lib/utils";
import { Alert } from "./ui/alert";
import { Badge } from "./ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "./ui/card";

type PredictionResultsProps = {
  data: PredictResponse | null;
  isLoading?: boolean;
  error: string | null;
};

function EmptyPanel({
  title,
  description,
  loading = false,
  error = false
}: {
  title: string;
  description: string;
  loading?: boolean;
  error?: boolean;
}) {
  const Icon = error ? TriangleAlert : loading ? LoaderCircle : Database;

  return (
    <div className="flex min-h-[320px] flex-col items-center justify-center rounded-[24px] border border-dashed border-white bg-[linear-gradient(180deg,rgba(255,255,255,0.72)_0%,rgba(244,248,249,0.78)_100%)] px-6 py-12 text-center">
      <div
        className={cn(
          "flex h-14 w-14 items-center justify-center rounded-2xl border bg-white/85 shadow-sm",
          error ? "border-destructive/20 text-destructive" : "border-white text-slate-600"
        )}
      >
        <Icon className={cn("h-6 w-6", loading ? "animate-spin" : "")} />
      </div>
      <div className="mt-5 text-lg font-semibold text-slate-900">{title}</div>
      <div className="mt-2 max-w-xl text-sm leading-6 text-mutedForeground">{description}</div>
    </div>
  );
}

export function PredictionResults({
  data,
  isLoading = false,
  error
}: PredictionResultsProps) {
  if (error) {
    return (
      <Card className="overflow-hidden rounded-[28px] border-destructive/20 shadow-none">
        <CardHeader className="min-h-[112px] border-b border-destructive/10 bg-destructiveForeground">
          <CardTitle className="flex items-center gap-2 text-lg text-destructive">
            <TriangleAlert className="h-5 w-5" />
            Prediction Failed
          </CardTitle>
          <CardDescription>The prediction request did not complete. Check the structure input, property selection, or service status.</CardDescription>
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
          <CardTitle className="text-xl">Prediction Results</CardTitle>
          <CardDescription>Calculating the selected properties.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4 pt-5">
          <EmptyPanel
            loading
            title="Running Prediction"
            description="Result cards will refresh here after prediction completes."
          />
        </CardContent>
      </Card>
    );
  }

  if (!data) {
    return (
      <Card className="overflow-hidden rounded-[28px] border-white/70 shadow-none">
        <CardHeader className="min-h-[112px] border-b border-white/70 bg-[linear-gradient(180deg,rgba(255,255,255,0.98)_0%,rgba(244,248,249,0.88)_100%)]">
          <CardTitle className="text-xl">Prediction Results</CardTitle>
          <CardDescription>No prediction results yet.</CardDescription>
        </CardHeader>
        <CardContent className="pt-5">
          <EmptyPanel
            title="Prediction Panel Ready"
            description="Switch to prediction mode, select properties, and submit to view predicted values, units, and calculation time."
          />
        </CardContent>
      </Card>
    );
  }

  const predictionEntries = PREDICTABLE_PROPERTIES.filter((property) => property in data.predictions).map(
    (property) => ({
      property,
      value: data.predictions[property] ?? 0,
      ...PREDICT_PROPERTY_META[property]
    })
  );

  return (
    <Card className="overflow-hidden rounded-[28px] border-white/70 shadow-none">
      <CardHeader className="min-h-[120px] gap-4 border-b border-white/70 bg-[linear-gradient(180deg,rgba(255,255,255,0.98)_0%,rgba(244,248,249,0.88)_100%)]">
        <div className="flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between">
          <div className="space-y-2">
            <div className="text-[11px] font-medium uppercase tracking-[0.22em] text-teal-700/80">
              Property Prediction
            </div>
            <CardTitle className="text-[1.4rem] tracking-tight">Prediction Results</CardTitle>
            <CardDescription>Predicted values for selected properties are presented as cards for quick comparison.</CardDescription>
          </div>
          <div className="flex flex-wrap gap-2">
            <Badge>
              <Sparkles className="mr-1 h-3.5 w-3.5" />
              {`${predictionEntries.length} properties`}
            </Badge>
            <Badge className="text-slate-700">{`${data.query_time_ms.toFixed(1)} ms`}</Badge>
          </div>
        </div>
      </CardHeader>

      <CardContent className="pt-5">
        <div className="grid gap-4 md:grid-cols-2">
          {predictionEntries.map(({ property, label, unit, value }) => (
            <div
              key={property}
              className="rounded-[24px] border border-white/80 bg-[linear-gradient(180deg,rgba(255,255,255,0.96)_0%,rgba(244,248,249,0.86)_100%)] p-5 shadow-sm"
            >
              <div className="text-xs font-medium uppercase tracking-[0.18em] text-teal-700/80">Prediction</div>
              <div className="mt-3 text-lg font-semibold tracking-tight text-slate-950">{label}</div>
              <div className="mt-4 flex items-end justify-between gap-3">
                <div className="font-heading text-[2rem] font-semibold tracking-[-0.04em] text-slate-950">
                  {value.toFixed(2)}
                </div>
                <div className="rounded-full bg-slate-950 px-3 py-1 text-xs font-medium tracking-[0.18em] text-white">
                  {unit}
                </div>
              </div>
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}
