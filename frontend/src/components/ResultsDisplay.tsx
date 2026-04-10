import type { ReactNode } from "react";
import { Database, LoaderCircle, ScanSearch, SearchX, Timer, TriangleAlert } from "lucide-react";
import type { SmilesQueryRequest, SmilesQueryResponse } from "../types";
import { PolymerCard } from "./PolymerCard";
import { SummaryMetric } from "./SummaryMetric";
import { Alert } from "./ui/alert";
import { Badge } from "./ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "./ui/card";

type ResultsDisplayProps = {
  data: SmilesQueryResponse | null;
  error: string | null;
  isLoading?: boolean;
  request: SmilesQueryRequest;
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


export function ResultsDisplay({
  data,
  error,
  isLoading = false,
  request
}: ResultsDisplayProps) {
  if (error) {
    return (
      <Card className="overflow-hidden rounded-[28px] border-destructive/20 shadow-none">
        <CardHeader className="min-h-[112px] border-b border-destructive/10 bg-destructiveForeground">
          <CardTitle className="flex items-center gap-2 text-lg text-destructive">
            <TriangleAlert className="h-5 w-5" />
            查询失败
          </CardTitle>
          <CardDescription>请求未成功返回，请检查结构输入、参数组合或服务可用性。</CardDescription>
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
          <CardTitle className="text-xl">Query Results</CardTitle>
          <CardDescription>正在执行查询并汇总属性数据。</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4 pt-5">
          <div className="flex items-center gap-3 rounded-[20px] border border-white/80 bg-white/80 px-4 py-3 text-sm text-slate-700">
            <LoaderCircle className="h-4 w-4 animate-spin" />
            正在加载结果，面板会在结果返回后自动刷新。
          </div>
          <div className="grid gap-4">
            {[0, 1].map((index) => (
              <div
                key={index}
                className="space-y-4 rounded-[24px] border border-slate-200 bg-white p-5"
              >
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
          <CardTitle className="text-xl">Query Results</CardTitle>
          <CardDescription>当前暂无查询结果。</CardDescription>
        </CardHeader>
        <CardContent className="pt-5">
          <EmptyState
            icon={<Database className="h-6 w-6" />}
            title="结果区已准备"
            description="运行查询后，这里会显示摘要、命中记录和属性分组。"
          />
        </CardContent>
      </Card>
    );
  }

  if (data.total === 0) {
    return (
      <Card className="overflow-hidden rounded-[28px] border-white/70 shadow-none">
        <CardHeader className="min-h-[112px] border-b border-white/70 bg-[linear-gradient(180deg,rgba(255,255,255,0.98)_0%,rgba(244,248,249,0.88)_100%)]">
          <CardTitle className="text-xl">Query Results</CardTitle>
          <CardDescription>本次查询执行成功，但没有命中任何可展示结果。</CardDescription>
        </CardHeader>
        <CardContent className="pt-5">
          <EmptyState
            icon={<SearchX className="h-6 w-6" />}
            title="未找到匹配结果"
            description="可以尝试放宽相似度阈值、调整返回数量，或检查当前 SMILES。"
          />
        </CardContent>
      </Card>
    );
  }

  return (
    <Card className="overflow-hidden rounded-[28px] border-white/70 shadow-none">
      <CardHeader className="min-h-[120px] gap-4 border-b border-white/70 bg-[linear-gradient(180deg,rgba(255,255,255,0.98)_0%,rgba(244,248,249,0.88)_100%)]">
        <div className="flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between">
          <div className="space-y-2">
            <div className="text-[11px] font-medium uppercase tracking-[0.22em] text-teal-700/80">
              Retrieved Dataset
            </div>
            <CardTitle className="text-[1.4rem] tracking-tight">Query Results</CardTitle>
            <CardDescription>摘要、命中记录与属性分组会按顺序展示在这里。</CardDescription>
          </div>
          <div className="flex flex-wrap gap-2">
            <Badge>{`${data.match_type === "similarity" ? "相似度" : "精确"}匹配`}</Badge>
            <Badge className="text-slate-700">{`${data.total} 条结果`}</Badge>
            <Badge className="text-slate-700">{`${data.query_time_ms.toFixed(1)} ms`}</Badge>
          </div>
        </div>

        <div className="grid auto-rows-fr gap-3 md:grid-cols-2 xl:grid-cols-4">
          <SummaryMetric
            icon={<ScanSearch className="h-4 w-4 text-teal-600" />}
            label="Match Mode"
            value={data.match_type === "similarity" ? "Similarity" : "Exact"}
            detail="当前查询的匹配方式。"
          />
          <SummaryMetric label="Result Count" value={String(data.total)} detail="命中的聚合物记录总数。" />
          <SummaryMetric
            icon={<Timer className="h-4 w-4 text-teal-600" />}
            label="Elapsed Time"
            value={`${data.query_time_ms.toFixed(1)} ms`}
            detail="本次检索与聚合耗时。"
          />
          <SummaryMetric
            label="Query SMILES"
            value={request.smiles || "N/A"}
            detail="当前查询输入。"
            mono
          />
        </div>
      </CardHeader>

      <CardContent className="space-y-5 pt-5">
        {data.results.map((result) => (
          <PolymerCard key={result.polymer_id} result={result} />
        ))}
      </CardContent>
    </Card>
  );
}
