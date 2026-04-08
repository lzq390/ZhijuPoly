import { Gauge, Search, SlidersHorizontal } from "lucide-react";
import type { SmilesQueryRequest } from "../types";
import { cn } from "../lib/utils";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "./ui/card";
import { Input } from "./ui/input";
import { Select } from "./ui/select";

type QueryPanelProps = {
  request: SmilesQueryRequest;
  onChange: (request: SmilesQueryRequest) => void;
  onSubmit: () => void;
  disabled?: boolean;
  isLoading?: boolean;
  className?: string;
};

function ParamCard({
  title,
  meta,
  children,
  footer
}: {
  title: string;
  meta?: React.ReactNode;
  children: React.ReactNode;
  footer?: React.ReactNode;
}) {
  return (
    <div className="flex min-h-[124px] flex-col rounded-[20px] border border-slate-200 bg-slate-50/70 p-4">
      <div className="flex min-h-[20px] items-start justify-between gap-3">
        <div className="text-sm font-medium text-slate-900">{title}</div>
        {meta}
      </div>
      <div className="mt-2.5">{children}</div>
      <div className="mt-1.5 flex-1 text-xs leading-5 text-mutedForeground">{footer}</div>
    </div>
  );
}

export function QueryPanel({
  request,
  onChange,
  onSubmit,
  disabled = false,
  isLoading = false,
  className
}: QueryPanelProps) {
  const isSimilarity = request.match_mode === "similarity";

  return (
    <Card className={cn("overflow-hidden rounded-[28px] border-slate-200/90", className)}>
      <CardHeader className="min-h-[96px] gap-3 border-b border-slate-200/80 bg-[linear-gradient(180deg,#ffffff_0%,#f8fafc_100%)]">
        <div className="flex items-center justify-between gap-4">
          <div className="space-y-2">
            <CardTitle className="text-xl">Query Controls</CardTitle>
            <CardDescription>调整匹配方式、阈值和返回规模，然后提交查询。</CardDescription>
          </div>
          <div className="flex h-11 w-11 items-center justify-center rounded-2xl bg-slate-950 text-white">
            <SlidersHorizontal className="h-4 w-4" />
          </div>
        </div>
      </CardHeader>

      <CardContent className="space-y-3.5 pt-4">
        <div className="grid auto-rows-fr gap-3 sm:grid-cols-2">
          <ParamCard
            title="Match Mode"
            meta={
              <Badge className={cn(isSimilarity ? "" : "bg-emerald-50 text-emerald-700")}>
                {isSimilarity ? "Similarity" : "Exact"}
              </Badge>
            }
            footer="决定本次查询的匹配逻辑。"
          >
            <Select
              value={request.match_mode}
              onChange={(event) =>
                onChange({
                  ...request,
                  match_mode: event.target.value as SmilesQueryRequest["match_mode"]
                })
              }
              className="h-11 rounded-xl border-slate-200 bg-white"
            >
              <option value="exact">精确匹配</option>
              <option value="similarity">相似度匹配</option>
            </Select>
          </ParamCard>

          <ParamCard
            title="Similarity Threshold"
            meta={<Gauge className="h-4 w-4 text-blue-600" />}
            footer="用于控制相似度模式的召回强度。"
          >
            <Input
              type="number"
              step="0.01"
              min="0"
              max="1"
              value={request.similarity_threshold}
              onChange={(event) =>
                onChange({
                  ...request,
                  similarity_threshold: Number(event.target.value)
                })
              }
              className="h-11 rounded-xl border-slate-200 bg-white"
            />
          </ParamCard>

          <ParamCard title="Top K" footer="控制返回结果数量。">
            <Input
              type="number"
              min="1"
              value={request.top_k}
              onChange={(event) =>
                onChange({
                  ...request,
                  top_k: Number(event.target.value)
                })
              }
              className="h-11 rounded-xl border-slate-200 bg-white"
            />
          </ParamCard>

          <ParamCard
            title="当前执行配置"
            footer="确认当前参数后即可直接运行查询。"
          >
            <div className="flex flex-wrap gap-2 text-xs text-mutedForeground">
              <span className="rounded-full bg-white px-3 py-1">Mode {isSimilarity ? "Similarity" : "Exact"}</span>
              <span className="rounded-full bg-white px-3 py-1">Threshold {request.similarity_threshold}</span>
              <span className="rounded-full bg-white px-3 py-1">Top K {request.top_k}</span>
            </div>
          </ParamCard>
        </div>

        <div className="flex flex-col gap-3 border-t border-slate-200 pt-3.5 sm:flex-row sm:items-center sm:justify-between">
          <div className="text-sm leading-6 text-mutedForeground">同步结构后，使用当前参数提交一次完整查询。</div>
          <Button className="min-w-[192px]" size="lg" onClick={onSubmit} disabled={disabled}>
            <Search className="mr-2 h-4 w-4" />
            {isLoading ? "查询中..." : "Run Query"}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
