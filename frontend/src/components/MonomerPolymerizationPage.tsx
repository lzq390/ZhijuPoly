import { ArrowLeft, Atom, Copy, FlaskConical, Loader2, Play, RefreshCw, Sparkles, TriangleAlert } from "lucide-react";
import { type FormEvent, useEffect, useMemo, useState } from "react";
import { cn } from "../lib/utils";
import { fetchMonomerPolymerizationStatus, runMonomerPolymerization } from "../services/api";
import type {
  MonomerPolymerizationCandidate,
  MonomerPolymerizationResponse,
  MonomerPolymerizationTargetClass,
  MonomerPolymerizationStatusResponse,
  StructureWorkspaceContext
} from "../types";
import { StructureSvg } from "./StructureSvg";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";
import { Input } from "./ui/input";
import { Textarea } from "./ui/textarea";

type MonomerPolymerizationPageProps = {
  structure: StructureWorkspaceContext;
  onEditStructure: () => void;
  onBackHome: () => void;
};

const TARGET_CLASS_LABELS: Record<MonomerPolymerizationTargetClass, string> = {
  polyolefin: "Polyolefin",
  polyester: "Polyester",
  polyether: "Polyether",
  polyamide: "Polyamide",
  polyimide: "Polyimide",
  polyurethane: "Polyurethane",
  polyoxazolidone: "Polyoxazolidone",
  all: "All classes"
};

const DEFAULT_TARGET_CLASSES: MonomerPolymerizationTargetClass[] = [
  "polyimide",
  "polyester",
  "polyamide",
  "polyurethane",
  "polyether",
  "polyolefin",
  "polyoxazolidone",
  "all"
];

export const SMIPOLY_POLYIMIDE_FIXTURE = {
  monomerA: "Nc1ccc(N)cc1",
  monomerB: "O=C1OC(=O)c2cc3c(cc21)C(=O)OC3=O"
} as const;

const DEFAULT_TARGET_REQUIREMENTS: Record<
  MonomerPolymerizationTargetClass,
  { min_monomers: number; max_monomers: number; monomer_b_required: boolean; note: string }
> = {
  polyolefin: {
    min_monomers: 1,
    max_monomers: 2,
    monomer_b_required: false,
    note: "当前类型允许先提交单体 A；提供单体 B 时会限制为两者共同参与的候选。"
  },
  polyester: {
    min_monomers: 2,
    max_monomers: 2,
    monomer_b_required: true,
    note: "当前类型需要两个互补单体。"
  },
  polyether: {
    min_monomers: 1,
    max_monomers: 2,
    monomer_b_required: false,
    note: "当前类型允许先提交单体 A；提供单体 B 时会限制为两者共同参与的候选。"
  },
  polyamide: {
    min_monomers: 2,
    max_monomers: 2,
    monomer_b_required: true,
    note: "当前类型需要两个互补单体。"
  },
  polyimide: {
    min_monomers: 2,
    max_monomers: 2,
    monomer_b_required: true,
    note: "Polyimide 需要二胺和二酐两个互补单体。"
  },
  polyurethane: {
    min_monomers: 2,
    max_monomers: 2,
    monomer_b_required: true,
    note: "当前类型需要两个互补单体。"
  },
  polyoxazolidone: {
    min_monomers: 1,
    max_monomers: 2,
    monomer_b_required: false,
    note: "当前类型允许先提交单体 A；提供单体 B 时会限制为两者共同参与的候选。"
  },
  all: {
    min_monomers: 1,
    max_monomers: 2,
    monomer_b_required: false,
    note: "All classes 会跨可用规则搜索；单体 B 可选。"
  }
};

function clampInteger(value: number, min: number, max: number) {
  if (!Number.isFinite(value)) {
    return min;
  }
  return Math.min(max, Math.max(min, Math.round(value)));
}

function statusTone(status: MonomerPolymerizationStatusResponse | null, error: string | null) {
  if (error || status?.available === false) {
    return "border-amber-200 bg-amber-50 text-amber-800";
  }
  if (status?.available) {
    return "border-emerald-200 bg-emerald-50 text-emerald-800";
  }
  return "border-slate-200 bg-slate-50 text-slate-600";
}

function CandidateCard({
  candidate,
  copyState,
  onCopy
}: {
  candidate: MonomerPolymerizationCandidate;
  copyState: "copied" | "failed" | null;
  onCopy: (value: string, key: string) => void;
}) {
  const copyKey = `candidate-${candidate.rank}`;
  return (
    <article className="flex min-h-[360px] flex-col overflow-hidden rounded-[14px] border border-slate-200 bg-white shadow-sm">
      <div className="flex items-center justify-between gap-3 border-b border-slate-100 px-4 py-3">
        <div className="flex min-w-0 items-center gap-2 text-sm font-semibold text-slate-950">
          <Atom className="h-4 w-4 shrink-0 text-teal-600" />
          <span className="truncate">{candidate.polymer_class}</span>
        </div>
        <Badge className="shrink-0 border-slate-200 bg-slate-50 text-slate-700">Rank {candidate.rank}</Badge>
      </div>
      <div className="flex min-h-[170px] items-center justify-center border-b border-slate-100 bg-slate-50/80 px-3 py-4">
        {candidate.structure_svg ? (
          <StructureSvg
            svg={candidate.structure_svg}
            alt={`Polymer candidate rank ${candidate.rank}`}
            className="w-full"
            imageClassName="max-h-[160px]"
          />
        ) : (
          <div className="max-h-[150px] overflow-auto rounded-lg border border-dashed border-slate-200 bg-white px-3 py-2 font-mono text-xs leading-5 text-slate-600">
            {candidate.polymer_smiles}
          </div>
        )}
      </div>
      <div className="flex flex-1 flex-col gap-3 p-4">
        <div className="rounded-lg border border-slate-100 bg-slate-50 px-3 py-2 font-mono text-xs leading-5 text-slate-800">
          <div className="max-h-[72px] overflow-auto break-all">{candidate.polymer_smiles}</div>
        </div>
        <div className="grid gap-2 text-xs text-slate-600">
          <div className="flex justify-between gap-3">
            <span>Reaction</span>
            <span className="font-semibold text-slate-900">{candidate.reaction_id ?? "--"}</span>
          </div>
          <div className="flex justify-between gap-3">
            <span>Monomer A</span>
            <span className="max-w-[70%] break-all text-right font-mono text-slate-900">{candidate.monomer_a_smiles}</span>
          </div>
          <div className="flex justify-between gap-3">
            <span>Monomer B</span>
            <span className="max-w-[70%] break-all text-right font-mono text-slate-900">{candidate.monomer_b_smiles ?? "--"}</span>
          </div>
          <div className="flex justify-between gap-3">
            <span>React set</span>
            <span className="max-h-[44px] max-w-[70%] overflow-auto break-all text-right font-mono text-slate-900">
              {candidate.reactset.length ? candidate.reactset.join(" + ") : "--"}
            </span>
          </div>
        </div>
        <Button
          type="button"
          variant="outline"
          onClick={() => onCopy(candidate.polymer_smiles, copyKey)}
          className="mt-auto h-9 rounded-md border-slate-200 bg-white px-3 text-slate-700 shadow-none hover:bg-slate-50"
        >
          <Copy className="mr-2 h-4 w-4" />
          {copyState === "copied" ? "已复制" : copyState === "failed" ? "复制失败" : "复制 SMILES"}
        </Button>
      </div>
    </article>
  );
}

export function MonomerPolymerizationPage({
  structure,
  onEditStructure,
  onBackHome
}: MonomerPolymerizationPageProps) {
  const [monomerA, setMonomerA] = useState(structure.smiles.trim());
  const [monomerB, setMonomerB] = useState("");
  const [targetClass, setTargetClass] = useState<MonomerPolymerizationTargetClass>("polyimide");
  const [maxResults, setMaxResults] = useState(10);
  const [status, setStatus] = useState<MonomerPolymerizationStatusResponse | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [data, setData] = useState<MonomerPolymerizationResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [syncError, setSyncError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [isStatusLoading, setIsStatusLoading] = useState(false);
  const [hasEditedMonomerA, setHasEditedMonomerA] = useState(false);
  const [copyState, setCopyState] = useState<{ key: string; status: "copied" | "failed" } | null>(null);

  useEffect(() => {
    const nextSmiles = structure.smiles.trim();
    if (!hasEditedMonomerA && nextSmiles) {
      setMonomerA(nextSmiles);
      setData(null);
    }
  }, [hasEditedMonomerA, structure.smiles]);

  async function refreshStatus() {
    setIsStatusLoading(true);
    try {
      const nextStatus = await fetchMonomerPolymerizationStatus();
      setStatus(nextStatus);
      setStatusError(null);
      if (!nextStatus.available_target_classes.includes(targetClass)) {
        setTargetClass(nextStatus.default_target_class);
        setData(null);
        setError(null);
      }
      setMaxResults((value) => clampInteger(value, 1, nextStatus.max_results_limit));
    } catch (statusFetchError) {
      setStatus(null);
      setStatusError(statusFetchError instanceof Error ? statusFetchError.message : "无法读取 SMiPoly 服务状态。");
    } finally {
      setIsStatusLoading(false);
    }
  }

  useEffect(() => {
    void refreshStatus();
  }, []);

  const targetOptions = useMemo(() => {
    const available = status?.available_target_classes?.length ? status.available_target_classes : DEFAULT_TARGET_CLASSES;
    return DEFAULT_TARGET_CLASSES.filter((target) => available.includes(target));
  }, [status]);

  const serviceUnavailable = status?.enabled === false || status?.available === false;
  const maxResultsLimit = status?.max_results_limit ?? 20;
  const localTargetRequirement = DEFAULT_TARGET_REQUIREMENTS[targetClass];
  const remoteTargetRequirement = status?.target_requirements?.[targetClass];
  const targetRequirement = {
    ...localTargetRequirement,
    ...remoteTargetRequirement
  };
  const targetRequirementNote =
    remoteTargetRequirement?.monomer_b_required === localTargetRequirement.monomer_b_required
      ? localTargetRequirement.note
      : remoteTargetRequirement?.note ?? localTargetRequirement.note;
  const isMonomerBRequired = targetRequirement.monomer_b_required;
  const isMissingRequiredMonomerB =
    isMonomerBRequired && monomerA.trim().length > 0 && monomerB.trim().length === 0;
  const monomerBHintId = "monomer-b-requirement-note";
  const monomerBMissingId = "monomer-b-required-missing";
  const canSubmit =
    !isLoading &&
    !serviceUnavailable &&
    monomerA.trim().length > 0 &&
    !isMissingRequiredMonomerB;

  async function syncSharedStructure() {
    setSyncError(null);
    let sharedSmiles = structure.smiles.trim();
    try {
      const editorSmiles = (await structure.getCurrentSmiles()).trim();
      if (editorSmiles) {
        sharedSmiles = editorSmiles;
      }
    } catch (syncFailure) {
      console.warn("Failed to read current structure from workbench", syncFailure);
    }
    if (!sharedSmiles) {
      setSyncError("共享结构为空，请先在结构工作台绘制或输入单体。");
      return;
    }
    setMonomerA(sharedSmiles);
    setHasEditedMonomerA(true);
    setData(null);
    setError(null);
  }

  async function submitPolymerization(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setSyncError(null);

    if (!monomerA.trim()) {
      setError("请输入单体 A 的 SMILES。");
      return;
    }
    if (isMonomerBRequired && !monomerB.trim()) {
      setError(`${TARGET_CLASS_LABELS[targetClass]} 需要单体 B，请输入互补单体 SMILES。`);
      return;
    }
    if (serviceUnavailable) {
      setError(status?.message ?? "SMiPoly 服务当前不可用。");
      return;
    }

    setIsLoading(true);
    try {
      const result = await runMonomerPolymerization({
        monomer_a_smiles: monomerA.trim(),
        monomer_b_smiles: monomerB.trim() || null,
        target_class: targetClass,
        max_results: clampInteger(maxResults, 1, maxResultsLimit)
      });
      setData(result);
    } catch (runError) {
      setData(null);
      setError(runError instanceof Error ? runError.message : "单体正向聚合失败。");
    } finally {
      setIsLoading(false);
    }
  }

  async function copyText(value: string, key: string) {
    try {
      await navigator.clipboard.writeText(value);
      setCopyState({ key, status: "copied" });
    } catch {
      setCopyState({ key, status: "failed" });
    } finally {
      window.setTimeout(() => setCopyState(null), 1300);
    }
  }

  return (
    <div className="min-h-full bg-[#f1f5f9] text-slate-950">
      <div className="mx-auto flex w-full max-w-[1480px] flex-col gap-4">
        <nav className="flex flex-col gap-3 rounded-[14px] border border-slate-200 bg-white px-4 py-3 shadow-sm md:flex-row md:items-center md:justify-between">
          <div className="flex min-w-0 items-center gap-3">
            <Button
              type="button"
              variant="outline"
              onClick={onBackHome}
              className="h-9 shrink-0 rounded-md border-slate-200 bg-white px-3 text-slate-700 shadow-none hover:border-slate-300 hover:bg-slate-50"
            >
              <ArrowLeft className="mr-2 h-4 w-4" />
              返回
            </Button>
            <div className="min-w-0">
              <div className="text-[11px] font-semibold uppercase text-slate-400">SMiPoly forward polymerization</div>
              <div className="truncate text-base font-semibold text-slate-950">单体正向聚合</div>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <span className={cn("inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1 font-medium", statusTone(status, statusError))}>
              {isStatusLoading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <FlaskConical className="h-3.5 w-3.5" />}
              {isStatusLoading ? "正在检查服务" : statusError ? "状态检查失败" : serviceUnavailable ? "规则服务不可用" : "规则服务可用"}
            </span>
            <Button
              type="button"
              variant="outline"
              onClick={() => void refreshStatus()}
              className="h-8 rounded-md border-slate-200 bg-white px-2.5 text-xs text-slate-600 shadow-none hover:bg-slate-50"
            >
              <RefreshCw className="mr-1.5 h-3.5 w-3.5" />
              刷新
            </Button>
          </div>
        </nav>

        <section className="grid gap-4 xl:grid-cols-[420px_minmax(0,1fr)]">
          <form onSubmit={(event) => void submitPolymerization(event)} className="rounded-[14px] border border-slate-200 bg-white p-4 shadow-sm">
            <div className="flex items-start justify-between gap-3">
              <div>
                <div className="text-[11px] font-semibold uppercase text-slate-400">输入</div>
                <h1 className="mt-1 text-base font-semibold text-slate-950">单次聚合设置</h1>
              </div>
              <Badge className="border-sky-200 bg-sky-50 text-sky-700">同步调用</Badge>
            </div>

            <div className="mt-4 rounded-lg border border-amber-100 bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-900">
              规则生成候选，不代表真实可合成性或性质验证。v1 每次最多处理两个用户提交的单体，不执行批量组合。
            </div>

            <label className="mt-4 block space-y-2">
              <span className="text-xs font-medium text-slate-600">单体 A SMILES</span>
              <Textarea
                value={monomerA}
                onChange={(event) => {
                  setMonomerA(event.target.value);
                  setHasEditedMonomerA(true);
                  setError(null);
                  setData(null);
                }}
                placeholder={`示例：${SMIPOLY_POLYIMIDE_FIXTURE.monomerA}`}
                spellCheck={false}
                className="min-h-[98px] rounded-lg border-slate-200 bg-white font-mono text-[13px] leading-5 text-slate-900 shadow-none placeholder:text-slate-400 focus-visible:ring-sky-200"
                disabled={isLoading}
              />
            </label>

            <div className="mt-2 flex flex-wrap items-center gap-2">
              <Button
                type="button"
                variant="outline"
                onClick={() => void syncSharedStructure()}
                disabled={isLoading}
                className="h-9 rounded-md border-slate-200 bg-white px-3 text-xs text-slate-700 shadow-none hover:bg-slate-50"
              >
                <Atom className="mr-2 h-3.5 w-3.5" />
                使用共享结构
              </Button>
              <Button
                type="button"
                variant="outline"
                onClick={onEditStructure}
                disabled={isLoading}
                className="h-9 rounded-md border-slate-200 bg-white px-3 text-xs text-slate-700 shadow-none hover:bg-slate-50"
              >
                打开结构工作台
              </Button>
            </div>
            {syncError ? <div className="mt-2 text-xs leading-5 text-red-600">{syncError}</div> : null}

            <label className="mt-4 block space-y-2">
              <span className="flex items-center justify-between gap-2 text-xs font-medium text-slate-600">
                <span>单体 B SMILES</span>
                <Badge
                  className={cn(
                    "shrink-0",
                    isMonomerBRequired ? "border-amber-200 bg-amber-50 text-amber-700" : "border-slate-200 bg-slate-50 text-slate-500"
                  )}
                >
                  {isMonomerBRequired ? "必填" : "可选"}
                </Badge>
              </span>
              <Textarea
                value={monomerB}
                onChange={(event) => {
                  setMonomerB(event.target.value);
                  setError(null);
                  setData(null);
                }}
                aria-describedby={
                  isMissingRequiredMonomerB ? `${monomerBHintId} ${monomerBMissingId}` : monomerBHintId
                }
                aria-invalid={isMissingRequiredMonomerB ? true : undefined}
                placeholder={`示例：${SMIPOLY_POLYIMIDE_FIXTURE.monomerB}`}
                spellCheck={false}
                className="min-h-[92px] rounded-lg border-slate-200 bg-white font-mono text-[13px] leading-5 text-slate-900 shadow-none placeholder:text-slate-400 focus-visible:ring-sky-200"
                disabled={isLoading}
              />
              <span
                id={monomerBHintId}
                className={cn("block text-xs leading-5", isMonomerBRequired ? "text-amber-700" : "text-slate-500")}
              >
                {targetRequirementNote}
              </span>
              {isMissingRequiredMonomerB ? (
                <span
                  id={monomerBMissingId}
                  aria-live="polite"
                  className="block rounded-md border border-amber-100 bg-amber-50 px-2 py-1 text-xs leading-5 text-amber-800"
                >
                  {TARGET_CLASS_LABELS[targetClass]} 需要单体 B，请补充互补单体后运行。
                </span>
              ) : null}
            </label>

            <div className="mt-4 grid gap-3 sm:grid-cols-2">
              <label className="block space-y-2">
                <span className="text-xs font-medium text-slate-600">目标聚合物类型</span>
                <select
                  value={targetClass}
                  onChange={(event) => {
                    setTargetClass(event.target.value as MonomerPolymerizationTargetClass);
                    setError(null);
                    setData(null);
                  }}
                  className="h-10 w-full rounded-md border border-slate-200 bg-white px-3 text-sm font-medium text-slate-800 shadow-none focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-200"
                  disabled={isLoading}
                >
                  {targetOptions.map((target) => (
                    <option key={target} value={target}>
                      {TARGET_CLASS_LABELS[target]}
                    </option>
                  ))}
                </select>
              </label>
              <label className="block space-y-2">
                <span className="text-xs font-medium text-slate-600">返回数量</span>
                <Input
                  type="number"
                  min={1}
                  max={maxResultsLimit}
                  step={1}
                  value={maxResults}
                  onChange={(event) => {
                    setMaxResults(clampInteger(Number(event.target.value), 1, maxResultsLimit));
                    setData(null);
                  }}
                  className="h-10 rounded-md border-slate-200 bg-white text-sm shadow-none focus-visible:ring-sky-200"
                  disabled={isLoading}
                />
              </label>
            </div>

            {statusError || serviceUnavailable ? (
              <div className="mt-4 flex gap-2 rounded-lg border border-amber-100 bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-900">
                <TriangleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                <span className="min-w-0 break-words [overflow-wrap:anywhere]">
                  {statusError ?? status?.message ?? "SMiPoly 服务当前不可用。"}
                </span>
              </div>
            ) : null}
            {error ? (
              <div className="mt-4 flex gap-2 rounded-lg border border-red-100 bg-red-50 px-3 py-2 text-xs leading-5 text-red-700">
                <TriangleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                <span className="min-w-0 break-words [overflow-wrap:anywhere]">{error}</span>
              </div>
            ) : null}

            <div className="mt-4 flex flex-wrap items-center gap-2">
              <Button type="submit" disabled={!canSubmit} className="h-10 rounded-md px-4 shadow-none disabled:opacity-[0.45]">
                {isLoading ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Play className="mr-2 h-4 w-4" />}
                {isLoading ? "运行中" : "运行聚合"}
              </Button>
              <Button
                type="button"
                variant="outline"
                onClick={() => {
                  setData(null);
                  setError(null);
                  setMonomerB("");
                }}
                disabled={isLoading}
                className="h-10 rounded-md border-slate-200 bg-white px-4 text-slate-700 shadow-none hover:bg-slate-50"
              >
                清空结果
              </Button>
            </div>
          </form>

          <div className="grid min-w-0 gap-4">
            <section className="rounded-[14px] border border-slate-200 bg-white p-4 shadow-sm">
              <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
                <div>
                  <div className="text-[11px] font-semibold uppercase text-slate-400">结果</div>
                  <h2 className="mt-1 text-base font-semibold text-slate-950">生成候选</h2>
                </div>
                <div className="flex flex-wrap gap-2">
                  <Badge className="border-slate-200 bg-slate-50 text-slate-700">{data ? `${data.total} 个候选` : "未运行"}</Badge>
                  <Badge className="border-slate-200 bg-slate-50 text-slate-700">
                    {data ? `${data.query_time_ms.toFixed(1)} ms` : TARGET_CLASS_LABELS[targetClass]}
                  </Badge>
                </div>
              </div>
              {data?.warnings.length ? (
                <div className="mt-4 space-y-2">
                  {data.warnings.map((warning) => (
                    <div key={warning} className="flex gap-2 rounded-lg border border-amber-100 bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-900">
                      <TriangleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                      <span className="min-w-0 break-words [overflow-wrap:anywhere]">{warning}</span>
                    </div>
                  ))}
                </div>
              ) : null}
            </section>

            {isLoading ? (
              <section className="flex min-h-[320px] items-center justify-center rounded-[14px] border border-dashed border-slate-200 bg-white text-sm font-medium text-slate-600">
                <Loader2 className="mr-2 h-5 w-5 animate-spin text-sky-600" />
                正在调用 SMiPoly 规则生成
              </section>
            ) : data && data.results.length > 0 ? (
              <section className="grid gap-4 md:grid-cols-2 2xl:grid-cols-3">
                {data.results.map((candidate) => {
                  const copyKey = `candidate-${candidate.rank}`;
                  return (
                    <CandidateCard
                      key={`${candidate.rank}-${candidate.polymer_smiles}`}
                      candidate={candidate}
                      copyState={copyState?.key === copyKey ? copyState.status : null}
                      onCopy={copyText}
                    />
                  );
                })}
              </section>
            ) : (
              <section className="flex min-h-[320px] flex-col items-center justify-center rounded-[14px] border border-dashed border-slate-200 bg-white px-6 py-10 text-center">
                <div className="flex h-12 w-12 items-center justify-center rounded-lg border border-slate-200 bg-slate-50 text-slate-600">
                  <Sparkles className="h-5 w-5" />
                </div>
                <div className="mt-4 text-sm font-semibold text-slate-950">{data ? "本次没有生成候选" : "等待单次聚合运行"}</div>
                <div className="mt-2 max-w-lg text-sm leading-6 text-slate-500">
                  {data
                    ? "可尝试补充第二个互补单体，或切换目标聚合物类型。"
                    : "输入一个或两个普通单体 SMILES，后端会同步返回少量规则生成候选。"}
                </div>
              </section>
            )}
          </div>
        </section>
      </div>
    </div>
  );
}
