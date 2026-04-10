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

function ModeButton({
  active,
  title,
  detail,
  onClick
}: {
  active: boolean;
  title: string;
  detail: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "min-h-[90px] rounded-[22px] border px-4 py-[0.92rem] text-left transition-all duration-300",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        active
          ? "border-teal-500/30 bg-[linear-gradient(180deg,rgba(15,118,110,0.12)_0%,rgba(255,255,255,0.96)_100%)] shadow-[0_16px_40px_rgba(15,118,110,0.12)]"
          : "border-white/80 bg-[linear-gradient(180deg,rgba(255,255,255,0.9)_0%,rgba(244,248,249,0.8)_100%)] shadow-sm hover:border-white hover:shadow-panel"
      )}
    >
      <div className="flex items-center justify-between gap-3">
        <div className="text-sm font-semibold tracking-tight text-slate-950">{title}</div>
        <div
          className={cn(
            "rounded-full px-2.5 py-1 text-[11px] font-medium uppercase tracking-[0.16em]",
            active ? "bg-teal-600 text-white" : "bg-slate-100 text-slate-500"
          )}
        >
          {active ? "当前" : "切换"}
        </div>
      </div>
      <div className="mt-2 text-sm leading-6 text-mutedForeground">{detail}</div>
    </button>
  );
}

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
    <div className="flex min-h-[109px] flex-col rounded-[22px] border border-white/80 bg-[linear-gradient(180deg,rgba(255,255,255,0.88)_0%,rgba(244,248,249,0.82)_100%)] p-[0.98rem] shadow-sm">
      <div className="flex min-h-[20px] items-start justify-between gap-3">
        <div className="text-sm font-medium tracking-tight text-slate-900">{title}</div>
        {meta}
      </div>
      <div className="mt-2">{children}</div>
      <div className="mt-2 flex-1 text-xs leading-5 text-mutedForeground">{footer}</div>
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
  const thresholdValue = Number.isFinite(request.similarity_threshold)
    ? request.similarity_threshold.toFixed(2)
    : "0.00";

  function applyPreset(mode: SmilesQueryRequest["match_mode"]) {
    onChange({
      ...request,
      match_mode: mode,
      similarity_threshold: mode === "similarity" ? Math.max(request.similarity_threshold, 0.72) : 1,
      top_k: mode === "similarity" ? Math.max(request.top_k, 12) : 10
    });
  }

  return (
    <Card className={cn("overflow-hidden rounded-[30px] border-white/70", className)}>
      <CardHeader className="gap-3 border-b border-white/70 bg-[linear-gradient(180deg,rgba(255,255,255,0.96)_0%,rgba(244,248,249,0.86)_100%)]">
        <div className="flex items-center justify-between gap-4">
          <div className="space-y-2">
            <div className="text-[11px] font-medium uppercase tracking-[0.22em] text-teal-700/80">
              Retrieval Controls
            </div>
            <CardTitle className="text-[1.35rem] tracking-tight">查询控制</CardTitle>
            <CardDescription>在这里切换检索方式并调整关键参数。</CardDescription>
          </div>
          <div className="flex h-11 w-11 items-center justify-center rounded-2xl bg-slate-950 text-white shadow-[0_12px_30px_rgba(8,17,31,0.18)]">
            <SlidersHorizontal className="h-4 w-4" />
          </div>
        </div>
      </CardHeader>

      <CardContent className="flex flex-col gap-[0.72rem] pt-[0.96rem]">
        <div className="grid gap-[0.62rem] lg:grid-cols-2">
          <ModeButton
            active={!isSimilarity}
            title="精确匹配"
            detail="适合做明确对照。"
            onClick={() => applyPreset("exact")}
          />
          <ModeButton
            active={isSimilarity}
            title="相似度检索"
            detail="适合扩展候选范围。"
            onClick={() => applyPreset("similarity")}
          />
        </div>

        <div className="grid gap-[0.62rem] lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
          <ParamCard
            title="相似度阈值"
            meta={
              <Badge className={cn(isSimilarity ? "" : "bg-slate-100 text-slate-500")}>
                {isSimilarity ? thresholdValue : "Exact"}
              </Badge>
            }
            footer="数值越高，结果越接近目标结构。"
          >
            <label className="sr-only" htmlFor="similarity-threshold">
              相似度阈值
            </label>
            <Input
              id="similarity-threshold"
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
              className="h-11"
            />
          </ParamCard>

          <ParamCard
            title="返回数量"
            meta={<Gauge className="h-4 w-4 text-teal-600" />}
            footer={`当前 Top ${request.top_k}。控制结果规模。`}
          >
            <label className="sr-only" htmlFor="top-k">
              返回数量
            </label>
            <Input
              id="top-k"
              type="number"
              min="1"
              value={request.top_k}
              onChange={(event) =>
                onChange({
                  ...request,
                  top_k: Number(event.target.value)
                })
              }
              className="h-11"
            />
          </ParamCard>
        </div>

        <div className="flex flex-col gap-[0.5rem] border-t border-slate-200/70 pt-[0.8rem] sm:flex-row sm:items-center sm:justify-between">
          <div className="text-sm leading-6 text-mutedForeground">
            {disabled
              ? "先在编辑器中输入结构，再用当前参数发起检索。"
              : "结构已就绪，可以立即提交查询并刷新结果面板。"}
          </div>
          <Button className="min-h-[44px] min-w-[192px]" size="lg" onClick={onSubmit} disabled={disabled}>
            <Search className="mr-2 h-4 w-4" />
            {isLoading ? "查询中..." : "立即查询"}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
