import { Check, Search, SlidersHorizontal, Sparkles, X } from "lucide-react";
import { useState } from "react";
import {
  PREDICTABLE_PROPERTIES,
  PREDICT_PROPERTY_META,
  type PredictableProperty,
  type SmilesQueryRequest,
  type WorkspaceMode
} from "../types";
import { cn } from "../lib/utils";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "./ui/card";

type QueryPanelProps = {
  mode: WorkspaceMode;
  onModeChange: (mode: WorkspaceMode) => void;
  request: SmilesQueryRequest;
  onChange: (request: SmilesQueryRequest) => void;
  onQuerySubmit: () => void;
  onPredictSubmit: () => void;
  selectedProperties: PredictableProperty[];
  onSelectedPropertiesChange: (properties: PredictableProperty[]) => void;
  queryDisabled?: boolean;
  predictDisabled?: boolean;
  isQueryLoading?: boolean;
  isPredicting?: boolean;
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

export function QueryPanel({
  mode,
  onModeChange,
  request,
  onChange,
  onQuerySubmit,
  onPredictSubmit,
  selectedProperties,
  onSelectedPropertiesChange,
  queryDisabled = false,
  predictDisabled = false,
  isQueryLoading = false,
  isPredicting = false,
  className
}: QueryPanelProps) {
  const [isPropertyDialogOpen, setIsPropertyDialogOpen] = useState(false);
  const isPropertyMatch = request.match_mode === "property";
  const isQueryMode = mode === "query";
  const selectedSummary = selectedProperties
    .slice(0, 2)
    .map((property) => PREDICT_PROPERTY_META[property].label)
    .join("、");

  function applyPreset(matchMode: SmilesQueryRequest["match_mode"]) {
    onChange({
      ...request,
      match_mode: matchMode,
      similarity_threshold: matchMode === "property" ? 0.72 : 1,
      top_k: matchMode === "property" ? 12 : 10
    });
  }

  function toggleProperty(property: PredictableProperty) {
    if (selectedProperties.includes(property)) {
      onSelectedPropertiesChange(selectedProperties.filter((item) => item !== property));
      return;
    }

    onSelectedPropertiesChange([...selectedProperties, property]);
  }

  return (
    <>
      <Card className={cn("overflow-hidden rounded-[30px] border-white/70", className)}>
      <CardHeader className="gap-3 border-b border-white/70 bg-[linear-gradient(180deg,rgba(255,255,255,0.96)_0%,rgba(244,248,249,0.86)_100%)]">
        <div className="flex items-center justify-between gap-4">
          <div className="space-y-2">
            <div className="text-[11px] font-medium uppercase tracking-[0.22em] text-teal-700/80">
              {isQueryMode ? "Similarity Controls" : "Prediction Controls"}
            </div>
            <CardTitle className="text-[1.35rem] tracking-tight">
              {isQueryMode ? "相似匹配控制" : "预测控制"}
            </CardTitle>
            <CardDescription>
              {isQueryMode ? "在这里选择结构或性质相似匹配方式。" : "选择待预测性质，并用当前结构发起模型推理。"}
            </CardDescription>
          </div>
          <div className="flex h-11 w-11 items-center justify-center rounded-2xl bg-slate-950 text-white shadow-[0_12px_30px_rgba(8,17,31,0.18)]">
            {isQueryMode ? <SlidersHorizontal className="h-4 w-4" /> : <Sparkles className="h-4 w-4" />}
          </div>
        </div>
      </CardHeader>

      <CardContent className="flex flex-col gap-[0.72rem] pt-[0.96rem]">
        <div className="grid gap-[0.62rem] lg:grid-cols-2">
          <ModeButton
            active={isQueryMode}
            title="相似匹配"
            detail="按结构或性质相似性匹配现有聚合物记录。"
            onClick={() => onModeChange("query")}
          />
          <ModeButton
            active={!isQueryMode}
            title="预测"
            detail="调用模型直接计算当前结构的目标性质。"
            onClick={() => onModeChange("predict")}
          />
        </div>

        {isQueryMode ? (
          <>
            <div className="grid gap-[0.62rem] lg:grid-cols-2">
              <ModeButton
                active={!isPropertyMatch}
                title="结构相似匹配"
                detail="按当前结构寻找相近的聚合物记录。"
                onClick={() => applyPreset("structure")}
              />
              <ModeButton
                active={isPropertyMatch}
                title="性质相似匹配"
                detail="按性质相关性扩展候选范围。"
                onClick={() => applyPreset("property")}
              />
            </div>

            <div className="flex flex-col gap-[0.5rem] border-t border-slate-200/70 pt-[0.8rem] sm:flex-row sm:items-center sm:justify-between">
              <div className="text-sm leading-6 text-mutedForeground">
                {queryDisabled
                  ? "先在编辑器中输入结构，再发起相似匹配。"
                  : "结构已就绪，可以立即提交相似匹配并刷新结果面板。"}
              </div>
              <Button className="min-h-[44px] min-w-[192px]" size="lg" onClick={onQuerySubmit} disabled={queryDisabled}>
                <Search className="mr-2 h-4 w-4" />
                {isQueryLoading ? "匹配中..." : "立即匹配"}
              </Button>
            </div>
          </>
        ) : (
          <>
            <div className="rounded-[24px] border border-white/80 bg-[linear-gradient(180deg,rgba(255,255,255,0.92)_0%,rgba(244,248,249,0.86)_100%)] p-4 shadow-sm">
              <div className="flex min-h-[94px] flex-col justify-between gap-4 sm:flex-row sm:items-center">
                <div>
                  <div className="text-sm font-semibold tracking-tight text-slate-950">选择待预测性质</div>
                  <div className="mt-1 text-sm leading-6 text-mutedForeground">
                    {selectedProperties.length > 0
                      ? `已选择 ${selectedProperties.length} 项：${selectedSummary}${selectedProperties.length > 2 ? " 等" : ""}`
                      : "打开弹窗选择 1 个或多个性质，主控制卡高度保持稳定。"}
                  </div>
                </div>
                <div className="flex flex-wrap items-center gap-2 sm:justify-end">
                  <Badge className="bg-slate-100 text-slate-700">{`${selectedProperties.length} selected`}</Badge>
                  <Button
                    type="button"
                    variant="outline"
                    className="min-h-[42px]"
                    onClick={() => setIsPropertyDialogOpen(true)}
                  >
                    <Sparkles className="mr-2 h-4 w-4" />
                    选择性质
                  </Button>
                </div>
              </div>
            </div>

            <div className="flex flex-col gap-[0.5rem] border-t border-slate-200/70 pt-[0.8rem] sm:flex-row sm:items-center sm:justify-between">
              <div className="text-sm leading-6 text-mutedForeground">
                {predictDisabled
                  ? "需要结构输入并至少勾选一个性质，才能发起预测。"
                  : "结构与性质已就绪，可以提交模型推理并切换到预测结果。"}
              </div>
              <Button className="min-h-[44px] min-w-[192px]" size="lg" onClick={onPredictSubmit} disabled={predictDisabled}>
                <Sparkles className="mr-2 h-4 w-4" />
                {isPredicting ? "预测中..." : "立即预测"}
              </Button>
            </div>
          </>
        )}
      </CardContent>

      </Card>

      {isPropertyDialogOpen ? (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/45 px-4 py-6 backdrop-blur-sm">
          <div className="w-full max-w-3xl overflow-hidden rounded-[30px] border border-white/70 bg-[linear-gradient(180deg,rgba(255,255,255,0.98)_0%,rgba(242,248,250,0.94)_100%)] shadow-[0_32px_90px_rgba(8,17,31,0.28)]">
            <div className="flex items-start justify-between gap-4 border-b border-slate-200/80 px-5 py-5 md:px-6">
              <div>
                <div className="text-[11px] font-medium uppercase tracking-[0.22em] text-teal-700/80">
                  Prediction Properties
                </div>
                <div className="font-heading mt-2 text-[1.45rem] font-semibold tracking-tight text-slate-950">
                  选择预测性质
                </div>
                <div className="mt-1 text-sm leading-6 text-mutedForeground">
                  当前开放 9 个 RDKit 描述符兼容模型，可多选后关闭弹窗继续预测。
                </div>
              </div>
              <button
                type="button"
                onClick={() => setIsPropertyDialogOpen(false)}
                className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full border border-white/80 bg-white/80 text-slate-600 shadow-sm transition-colors hover:text-slate-950"
                aria-label="关闭性质选择弹窗"
              >
                <X className="h-4 w-4" />
              </button>
            </div>

            <div className="max-h-[62vh] overflow-y-auto px-5 py-5 md:px-6">
              <div className="grid gap-3 md:grid-cols-2">
                {PREDICTABLE_PROPERTIES.map((property) => {
                  const selected = selectedProperties.includes(property);
                  const meta = PREDICT_PROPERTY_META[property];

                  return (
                    <label
                      key={property}
                      className={cn(
                        "flex cursor-pointer items-start gap-3 rounded-[20px] border px-4 py-3 transition-all duration-200",
                        selected
                          ? "border-teal-500/30 bg-teal-50/80 shadow-[0_12px_30px_rgba(15,118,110,0.08)]"
                          : "border-white/80 bg-white/75 hover:border-slate-200"
                      )}
                    >
                      <input
                        type="checkbox"
                        className="sr-only"
                        checked={selected}
                        onChange={() => toggleProperty(property)}
                      />
                      <span
                        className={cn(
                          "mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-md border",
                          selected ? "border-teal-600 bg-teal-600 text-white" : "border-slate-300 bg-white"
                        )}
                      >
                        {selected ? <Check className="h-3.5 w-3.5" /> : null}
                      </span>
                      <span className="min-w-0">
                        <span className="block text-sm font-medium tracking-tight text-slate-950">{meta.label}</span>
                        <span className="mt-1 block text-xs uppercase tracking-[0.16em] text-mutedForeground">
                          {meta.unit}
                        </span>
                      </span>
                    </label>
                  );
                })}
              </div>
            </div>

            <div className="flex flex-col gap-3 border-t border-slate-200/80 px-5 py-4 sm:flex-row sm:items-center sm:justify-between md:px-6">
              <div className="text-sm text-mutedForeground">{`已选择 ${selectedProperties.length} / ${PREDICTABLE_PROPERTIES.length} 项性质`}</div>
              <div className="flex flex-wrap gap-2 sm:justify-end">
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => onSelectedPropertiesChange([])}
                  disabled={selectedProperties.length === 0}
                >
                  清空选择
                </Button>
                <Button type="button" onClick={() => setIsPropertyDialogOpen(false)}>
                  完成
                </Button>
              </div>
            </div>
          </div>
        </div>
      ) : null}
    </>
  );
}
