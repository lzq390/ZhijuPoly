import {
  Activity,
  ArrowLeft,
  Atom,
  Ban,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Clipboard,
  FlaskConical,
  History,
  Info,
  Loader2,
  Play,
  RefreshCw,
  RotateCcw,
  Server,
  Settings2,
  Trash2,
  TriangleAlert,
  XCircle
} from "lucide-react";
import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import {
  isMonomerDftTerminal,
  useMonomerDftJob,
  validateMonomerDftRequest
} from "../hooks/useMonomerDftJob";
import { cn } from "../lib/utils";
import { hasInvalidMonomerDftJobSearch } from "../lib/monomerDftRouting";
import { labelMonomerDftStage } from "../lib/monomerDftPresentation";
import type {
  MonomerDftCalculationType,
  MonomerDftJobCreateRequest,
  MonomerDftJobStatus,
  MonomerDftModelCapability,
  MonomerDftModelName,
  MonomerDftPostOptimizationProperty,
  MonomerDftProperty,
  StructureWorkspaceContext
} from "../types";
import { CurrentStructurePanel } from "./StructureWorkbenchPage";
import { MonomerDftResults } from "./MonomerDftResults";
import { Button } from "./ui/button";
import { Input } from "./ui/input";
import { Select } from "./ui/select";

type MonomerDftPageProps = {
  structure: StructureWorkspaceContext;
  initialJobId: string | null;
  onJobIdChange: (jobId: string | null) => void;
  onEditStructure: () => void;
  onBackHome: () => void;
};

export function selectableMonomerDftModels(
  models: MonomerDftModelCapability[]
): MonomerDftModelCapability[] {
  return models.filter((model) => model.deprecated !== true);
}

const STATUS_LABELS: Record<MonomerDftJobStatus, string> = {
  pending: "等待入队",
  queued: "排队中",
  running: "计算中",
  cancel_requested: "取消中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消"
};

const STATUS_STYLES: Record<MonomerDftJobStatus, string> = {
  pending: "bg-slate-100 text-slate-700",
  queued: "bg-amber-50 text-amber-800",
  running: "bg-sky-50 text-sky-800",
  cancel_requested: "bg-orange-50 text-orange-800",
  completed: "bg-emerald-50 text-emerald-800",
  failed: "bg-red-50 text-red-800",
  cancelled: "bg-slate-100 text-slate-600"
};

const PROPERTY_OPTIONS: { value: MonomerDftProperty; label: string; detail: string }[] = [
  { value: "energy", label: "能量", detail: "eV，必选" },
  { value: "forces", label: "原子力", detail: "eV/Å" },
  { value: "charges", label: "原子电荷", detail: "e" },
  { value: "hessian", label: "Hessian", detail: "完整矩阵作为产物" },
  { value: "frequencies", label: "振动频率", detail: "自动隐式计算 Hessian" }
];

function formatDate(value: string | null | undefined): string {
  if (!value) return "--";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false });
}

function statusIcon(status: MonomerDftJobStatus) {
  if (status === "completed") return <CheckCircle2 className="h-3.5 w-3.5" />;
  if (status === "failed") return <XCircle className="h-3.5 w-3.5" />;
  if (status === "cancelled") return <Ban className="h-3.5 w-3.5" />;
  if (["running", "cancel_requested"].includes(status)) return <Loader2 className="h-3.5 w-3.5 animate-spin" />;
  return <Activity className="h-3.5 w-3.5" />;
}

function StatusBadge({ status }: { status: MonomerDftJobStatus }) {
  return <span className={cn("inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-semibold", STATUS_STYLES[status])}>{statusIcon(status)}{STATUS_LABELS[status]}</span>;
}

function PropertyChoices({ values, onChange, supported, compact = false }: { values: MonomerDftProperty[]; onChange: (values: MonomerDftProperty[]) => void; supported: MonomerDftProperty[]; compact?: boolean }) {
  const options = compact ? PROPERTY_OPTIONS.filter((option) => ["hessian", "frequencies"].includes(option.value)) : PROPERTY_OPTIONS;
  return <div className={cn("grid gap-2", compact ? "sm:grid-cols-2" : "sm:grid-cols-2")}>{options.map((option) => {
    const checked = values.includes(option.value);
    const disabled = (!compact && option.value === "energy") || !supported.includes(option.value);
    return <label key={option.value} className={cn("flex cursor-pointer items-start gap-2 rounded-lg border px-3 py-2 text-xs", checked ? "border-sky-200 bg-sky-50" : "border-slate-200 bg-white", disabled && !checked ? "cursor-not-allowed opacity-45" : "")}><input type="checkbox" className="mt-0.5 accent-sky-600" checked={checked} disabled={disabled} onChange={(event) => { if (event.target.checked) onChange([...values, option.value]); else onChange(values.filter((item) => item !== option.value)); }} /><span><span className="block font-semibold text-slate-800">{option.label}</span><span className="mt-0.5 block text-[11px] text-slate-500">{option.detail}</span></span></label>;
  })}</div>;
}

export function MonomerDftPage({ structure, initialJobId, onJobIdChange, onEditStructure, onBackHome }: MonomerDftPageProps) {
  const dft = useMonomerDftJob({ initialJobId, onJobIdChange });
  const [calculationType, setCalculationType] = useState<MonomerDftCalculationType>("single_point");
  const [modelId, setModelId] = useState<MonomerDftModelName | "">("");
  const [netChargeText, setNetChargeText] = useState("");
  const [multiplicity, setMultiplicity] = useState(1);
  const [psmilesMode, setPsmilesMode] = useState<"close" | "cap" | null>(null);
  const [seed, setSeed] = useState(1);
  const [maxIterations, setMaxIterations] = useState(500);
  const [properties, setProperties] = useState<MonomerDftProperty[]>(["energy", "forces", "charges"]);
  const [postOptimizationProperties, setPostOptimizationProperties] = useState<MonomerDftPostOptimizationProperty[]>([]);
  const [fmax, setFmax] = useState(0.01);
  const [maxSteps, setMaxSteps] = useState(50);
  const [isAdvancedOpen, setIsAdvancedOpen] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const defaultsInitializedRef = useRef(false);
  const selectableModels = useMemo(
    () => selectableMonomerDftModels(dft.capabilities?.models ?? []),
    [dft.capabilities]
  );

  useEffect(() => {
    if (!dft.capabilities || dft.capabilities.schema_ready !== true) {
      defaultsInitializedRef.current = false;
      setModelId("");
      setSeed(1);
      setMaxIterations(500);
      setProperties(["energy", "forces", "charges"]);
      setFmax(0.01);
      setMaxSteps(50);
      setPostOptimizationProperties([]);
      setFormError(null);
      return;
    }
    setModelId((current) => current && selectableModels.some((model) => model.id === current)
      ? current
      : selectableModels.find((model) => model.id === dft.capabilities?.default_model)?.id
        ?? selectableModels.find((model) => model.available)?.id
        ?? "");
    if (!defaultsInitializedRef.current) {
      const defaults = dft.capabilities.defaults;
      setSeed(defaults.conformer.seed);
      setMaxIterations(defaults.conformer.max_iterations);
      setProperties(defaults.single_point.properties);
      setFmax(defaults.optimization.fmax_eV_per_A);
      setMaxSteps(defaults.optimization.max_steps);
      setPostOptimizationProperties(defaults.optimization.post_optimization_properties);
      defaultsInitializedRef.current = true;
    }
  }, [dft.capabilities, selectableModels]);

  useEffect(() => {
    if (!structure.smiles.includes("*")) {
      setPsmilesMode(null);
    }
  }, [structure.smiles]);

  const selectedModel = selectableModels.find((model) => model.id === modelId) ?? null;
  const netCharge = netChargeText.trim() === "" ? null : Number(netChargeText);
  const validationIssues = useMemo(() => validateMonomerDftRequest({
    smiles: structure.smiles,
    netCharge,
    multiplicity,
    psmilesMode,
    calculationType,
    modelId,
    properties: calculationType === "single_point" ? properties : ["energy", "forces", "charges", ...postOptimizationProperties],
    fmax,
    maxSteps,
    seed,
    maxIterations
  }, dft.capabilities), [calculationType, dft.capabilities, fmax, maxIterations, maxSteps, modelId, multiplicity, netCharge, postOptimizationProperties, properties, psmilesMode, seed, structure.smiles]);

  const serviceReady = Boolean(
    dft.serviceStatus?.schema_ready && dft.capabilities?.schema_ready &&
    dft.serviceStatus.enabled && dft.serviceStatus.available && dft.serviceStatus.runtime_ready !== false &&
    !dft.serviceStatus.draining && dft.capabilities?.available
  );
  const submissionDisabled = dft.serviceStatus?.enabled === false || dft.capabilities?.enabled === false;
  const activeJob = dft.job && !isMonomerDftTerminal(dft.job.status);
  const canSubmit = serviceReady && validationIssues.length === 0 && !dft.isSubmitting && !activeJob;
  const historyPageCount = Math.max(1, Math.ceil((dft.history?.total ?? 0) / (dft.history?.page_size ?? 20)));
  const invalidJobDeepLink = typeof window !== "undefined" && hasInvalidMonomerDftJobSearch(window.location.search);
  const maxRunningJobs = dft.capabilities?.limits.max_concurrent_jobs;
  const maxQueuedJobs = dft.capabilities?.limits.max_queued_jobs;
  const maxActiveJobs = dft.serviceStatus?.max_active_jobs ?? dft.capabilities?.limits.max_active_jobs;
  const pollStatusLabel = dft.pollState === "degraded"
    ? "连接中断，正在自动重试"
    : dft.pollState === "stopped"
      ? "自动同步已停止"
      : dft.pollState === "polling"
        ? "每 1.5 秒同步"
        : null;

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setFormError(null);
    const currentSmiles = (await structure.getCurrentSmiles()).trim();
    const issues = validateMonomerDftRequest({
      smiles: currentSmiles,
      netCharge,
      multiplicity,
      psmilesMode,
      calculationType,
      modelId,
      properties: calculationType === "single_point" ? properties : ["energy", "forces", "charges", ...postOptimizationProperties],
      fmax,
      maxSteps,
      seed,
      maxIterations
    }, dft.capabilities);
    if (issues.length > 0) {
      setFormError(issues[0].message);
      return;
    }
    const submitModel = selectableModels.find((model) => model.id === modelId);
    if (!submitModel) {
      setFormError("请选择能力目录中的模型。");
      return;
    }
    const common = {
      input: { smiles: currentSmiles, net_charge: netCharge, multiplicity, psmiles_mode: psmilesMode },
      model: submitModel.id,
      conformer: { seed, max_iterations: maxIterations }
    };
    const request: MonomerDftJobCreateRequest = calculationType === "single_point"
      ? { ...common, calculation_type: "single_point", single_point: { properties } }
      : { ...common, calculation_type: "optimization", optimization: { fmax_eV_per_A: fmax, max_steps: maxSteps, post_optimization_properties: postOptimizationProperties } };
    await dft.submit(request);
  }

  function copyJobLink() {
    if (!dft.job || typeof navigator === "undefined") return;
    const url = new URL(window.location.href);
    url.pathname = "/monomer-dft";
    url.search = new URLSearchParams({ job: dft.job.job_id }).toString();
    void navigator.clipboard.writeText(url.toString());
  }

  return <div className="min-h-full bg-slate-50">
    <header className="border-b border-slate-200 bg-white px-4 py-4 md:px-6">
      <div className="mx-auto flex max-w-[1640px] flex-wrap items-start justify-between gap-4">
        <div className="flex items-start gap-3">
          <Button type="button" variant="outline" className="h-9 w-9 shrink-0 rounded-lg p-0" onClick={onBackHome} aria-label="返回首页"><ArrowLeft className="h-4 w-4" /></Button>
          <div><div className="flex flex-wrap items-center gap-2"><h1 className="text-xl font-semibold text-slate-950">单体 DFT（AIMNet2）</h1><span className="rounded-full bg-violet-50 px-2.5 py-1 text-[11px] font-semibold text-violet-700">GPU Broker 调度 · 独立 Worker</span></div><p className="mt-1 max-w-3xl text-sm leading-6 text-slate-600">使用拟合 DFT 参考数据的 AIMNet2 机器学习势预测能量和响应属性；<strong className="font-semibold text-slate-800">它不是传统 SCF / 从头算 DFT</strong>。</p></div>
        </div>
        <div className="flex flex-col items-end gap-2"><div className="flex items-center gap-2"><span className={cn("inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-semibold", serviceReady ? "bg-emerald-50 text-emerald-700" : submissionDisabled ? "bg-slate-100 text-slate-700" : dft.serviceStatus?.draining ? "bg-amber-50 text-amber-800" : "bg-red-50 text-red-700")}><Server className="h-3.5 w-3.5" />{dft.isServiceLoading ? "检查服务" : serviceReady ? "Worker 就绪" : submissionDisabled ? "功能尚未开放" : dft.serviceStatus?.draining ? "发布排空中" : "服务不可用"}</span><Button type="button" variant="outline" className="h-8 rounded-md px-2.5 text-xs" onClick={() => void dft.refreshStatus()} disabled={dft.isServiceLoading}><RefreshCw className={cn("mr-1.5 h-3.5 w-3.5", dft.isServiceLoading && "animate-spin")} />刷新</Button></div><div className="flex flex-wrap justify-end gap-x-3 gap-y-1 text-[11px] text-slate-500"><span>队列容量：{maxRunningJobs ?? "--"} running + {maxQueuedJobs ?? "--"} queued</span><span>当前活跃：{dft.serviceStatus?.active_jobs ?? "--"} / {maxActiveJobs ?? "--"} active</span></div></div>
      </div>
    </header>

    <main className="mx-auto max-w-[1640px] space-y-4 p-4 md:p-6">
      {dft.serviceStatus?.schema_ready === false ? <div className="rounded-xl border border-amber-200 bg-amber-50 p-3 text-xs text-amber-900">单体 DFT 数据库迁移尚未完成。历史记录、任务深链接和提交功能会保持关闭，迁移就绪后自动恢复。</div> : null}
      {invalidJobDeepLink ? <div className="rounded-xl border border-red-200 bg-red-50 p-3 text-xs text-red-800">任务深链接无效：<code>job</code> 必须是 UUID。已安全忽略该参数，未发送任务查询。</div> : null}
      {submissionDisabled ? <div className="rounded-xl border border-slate-200 bg-white p-3 text-xs leading-5 text-slate-700"><strong className="font-semibold text-slate-900">功能尚未开放。</strong> 当前发布仅提供历史记录和既有结果查看；新任务提交、重跑和 Worker 计算将在独立生产启用后开放。</div> : null}
      <CurrentStructurePanel structure={structure} onEditStructure={onEditStructure} compact />
      <section className="rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-xs leading-5 text-amber-950">
        <div className="flex items-start gap-2"><Info className="mt-0.5 h-4 w-4 shrink-0 text-amber-700" /><div><div className="font-semibold">适用范围与共享环境提示</div><ul className="mt-1 grid list-disc gap-x-8 pl-4 md:grid-cols-2"><li>元素、净电荷和多重度必须落在所选模型的能力域，最终以服务端校验为准。</li><li>PSMILES 的 close/cap 只生成有限代理分子，不代表完整聚合物周期环境。</li><li>几何优化从一个确定性构象出发，是单构象局部优化，不保证全局最低能构象。</li><li>不同模型家族的绝对能量不可直接横向比较或混用于同一能量差。</li><li className="md:col-span-2">当前全局历史面向可信单租户环境：所有能访问此页面的访问者都能查看任务，并可取消任务或删除产物。</li></ul></div></div>
      </section>
      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_390px]">
        <form onSubmit={handleSubmit} className="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm md:p-5">
          <div className="flex items-center gap-2 text-sm font-semibold text-slate-950"><FlaskConical className="h-4 w-4 text-violet-600" />计算设置</div>
          <div className="mt-4 grid gap-3 sm:grid-cols-2">
            {(["single_point", "optimization"] as const).map((type) => <button key={type} type="button" disabled={Boolean(activeJob)} onClick={() => setCalculationType(type)} className={cn("rounded-xl border p-3 text-left", calculationType === type ? "border-violet-300 bg-violet-50" : "border-slate-200 bg-white hover:bg-slate-50")}><span className="block text-sm font-semibold text-slate-900">{type === "single_point" ? "单点计算" : "几何优化"}</span><span className="mt-1 block text-xs leading-5 text-slate-500">{type === "single_point" ? "固定构象计算能量、力、电荷、Hessian 或频率。" : "BFGS 优化并返回逐步能量、Fmax 与显式坐标轨迹。"}</span></button>)}
          </div>
          <div className="mt-4 grid gap-4 md:grid-cols-2">
            <label className="block"><span className="mb-1.5 block text-xs font-medium text-slate-600">共享 SMILES</span><Input value={structure.smiles} onChange={(event) => structure.setSmiles(event.target.value)} disabled={Boolean(activeJob)} className="h-10 rounded-lg border-slate-200 bg-white font-mono text-xs" placeholder="从结构工作台同步或直接输入" /></label>
            <label className="block"><span className="mb-1.5 block text-xs font-medium text-slate-600">AIMNet 模型</span><Select value={modelId} onChange={(event) => setModelId(event.target.value as MonomerDftModelName | "")} disabled={Boolean(activeJob) || !dft.capabilities} className="h-10 rounded-lg border-slate-200 bg-white"><option value="">等待能力目录</option>{selectableModels.map((model) => <option key={model.id} value={model.id} disabled={!model.available}>{model.label}{!model.available ? "（不可用）" : ""}</option>)}</Select></label>
          </div>
          {selectedModel ? <div className={cn("mt-3 rounded-lg border px-3 py-2 text-xs leading-5", selectedModel.deprecated ? "border-amber-200 bg-amber-50 text-amber-900" : "border-slate-100 bg-slate-50 text-slate-600")}><span className="font-semibold">{selectedModel.label}</span>：{selectedModel.description ?? "由后端能力目录提供的 AIMNet 模型。"}{selectedModel.deprecation_message ? ` ${selectedModel.deprecation_message}` : ""}<div className="mt-1 text-[11px]">支持元素：{selectedModel.supported_elements.join("、") || "由服务端校验"} · {selectedModel.supports_spin ? "支持开放壳层" : "仅闭壳层"}</div></div> : null}
          <div className="mt-4 grid gap-4 sm:grid-cols-3">
            <label><span className="mb-1.5 block text-xs font-medium text-slate-600">净电荷</span><Input type="number" step={1} value={netChargeText} onChange={(event) => setNetChargeText(event.target.value)} disabled={Boolean(activeJob)} className="h-10 rounded-lg border-slate-200 bg-white" placeholder="留空自动" /></label>
            <label><span className="mb-1.5 block text-xs font-medium text-slate-600">多重度（2S+1）</span><Input type="number" min={1} max={7} step={1} value={multiplicity} onChange={(event) => setMultiplicity(Number(event.target.value))} disabled={Boolean(activeJob)} className="h-10 rounded-lg border-slate-200 bg-white" /></label>
            <label><span className="mb-1.5 block text-xs font-medium text-slate-600">PSMILES 处理</span><Select value={psmilesMode ?? ""} onChange={(event) => setPsmilesMode(event.target.value === "close" || event.target.value === "cap" ? event.target.value : null)} disabled={Boolean(activeJob) || !structure.smiles.includes("*")} className="h-10 rounded-lg border-slate-200 bg-white"><option value="">普通单体 / 未选择</option><option value="close">闭环（close）</option><option value="cap">封端（cap）</option></Select></label>
          </div>

          <div className="mt-4"><div className="mb-2 text-xs font-medium text-slate-600">{calculationType === "single_point" ? "请求属性" : "优化后高级属性"}</div>{calculationType === "single_point" ? <PropertyChoices values={properties} onChange={setProperties} supported={selectedModel?.supported_properties ?? []} /> : <><div className="grid gap-4 sm:grid-cols-2"><label><span className="mb-1.5 block text-xs font-medium text-slate-600">Fmax 阈值 / eV Å⁻¹</span><Input type="number" min={0.001} step={0.001} value={fmax} onChange={(event) => setFmax(Number(event.target.value))} disabled={Boolean(activeJob)} className="h-10 rounded-lg border-slate-200 bg-white" /></label><label><span className="mb-1.5 block text-xs font-medium text-slate-600">最大优化步数（10–50）</span><Input type="number" min={10} max={50} step={1} value={maxSteps} onChange={(event) => setMaxSteps(Number(event.target.value))} disabled={Boolean(activeJob)} className="h-10 rounded-lg border-slate-200 bg-white" /></label></div><div className="mt-3"><PropertyChoices compact values={postOptimizationProperties} onChange={(values) => setPostOptimizationProperties(values.filter((value): value is MonomerDftPostOptimizationProperty => value === "hessian" || value === "frequencies"))} supported={selectedModel?.supported_properties ?? []} /></div><p className="mt-2 text-[11px] text-slate-500">优化始终返回最终能量、力和电荷；这里只选择额外 Hessian / 频率。</p></>}
          </div>

          <button type="button" className="mt-4 inline-flex items-center gap-1.5 text-xs font-medium text-slate-600 hover:text-slate-900" onClick={() => setIsAdvancedOpen((value) => !value)}><Settings2 className="h-3.5 w-3.5" />构象生成高级设置 · {isAdvancedOpen ? "收起" : "展开"}</button>
          {isAdvancedOpen ? <div className="mt-3 grid gap-4 rounded-xl border border-slate-100 bg-slate-50 p-3 sm:grid-cols-2"><label><span className="mb-1.5 block text-xs font-medium text-slate-600">RDKit seed</span><Input type="number" step={1} value={seed} onChange={(event) => setSeed(Number(event.target.value))} disabled={Boolean(activeJob)} className="h-9 rounded-lg border-slate-200 bg-white" /></label><label><span className="mb-1.5 block text-xs font-medium text-slate-600">最大嵌入迭代</span><Input type="number" min={1} step={1} value={maxIterations} onChange={(event) => setMaxIterations(Number(event.target.value))} disabled={Boolean(activeJob)} className="h-9 rounded-lg border-slate-200 bg-white" /></label></div> : null}

          {validationIssues.length > 0 ? <div className="mt-4 rounded-xl border border-amber-200 bg-amber-50 p-3 text-xs leading-5 text-amber-900"><div className="flex items-start gap-2"><TriangleAlert className="mt-0.5 h-4 w-4 shrink-0" /><ul className="space-y-1">{validationIssues.map((issue, index) => <li key={`${issue.field}-${index}`}>{issue.message}</li>)}</ul></div></div> : null}
          {formError || dft.jobError ? <div className="mt-3 rounded-xl border border-red-200 bg-red-50 p-3 text-xs text-red-800">{formError ?? dft.jobError}</div> : null}
          {dft.serviceError ? <div className="mt-3 rounded-xl border border-red-200 bg-red-50 p-3 text-xs text-red-800">状态接口：{dft.serviceError}</div> : null}
          <div className="mt-4 flex flex-wrap gap-2"><Button type="submit" disabled={!canSubmit} className="h-10 rounded-lg px-4 shadow-none">{dft.isSubmitting ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Play className="mr-2 h-4 w-4" />}{dft.isSubmitting ? "正在提交" : submissionDisabled ? "功能尚未开放" : "提交计算"}</Button><Button type="button" variant="outline" className="h-10 rounded-lg border-slate-200 px-4" onClick={onEditStructure} disabled={Boolean(activeJob)}><Atom className="mr-2 h-4 w-4" />打开 Ketcher</Button></div>
        </form>

        <aside className="space-y-4">
          <section className="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm">
            <div className="flex items-center justify-between gap-3"><div className="flex items-center gap-2 text-sm font-semibold text-slate-950"><Activity className="h-4 w-4 text-sky-600" />当前任务</div>{dft.job ? <StatusBadge status={dft.job.status} /> : null}</div>
            {dft.job ? <div className="mt-4 space-y-3"><div className="rounded-lg bg-slate-50 p-3"><div className="flex items-center justify-between gap-2"><span className="truncate font-mono text-xs text-slate-700" title={dft.job.job_id}>{dft.job.job_id}</span><button type="button" onClick={copyJobLink} className="shrink-0 text-slate-400 hover:text-slate-700" title="复制任务链接"><Clipboard className="h-3.5 w-3.5" /></button></div><div className="mt-2 text-[11px] text-slate-500">{dft.job.calculation_type === "single_point" ? "单点" : "几何优化"} · {dft.job.request.model} · {formatDate(dft.job.created_at)}</div></div><div><div className="mb-1 flex items-center justify-between text-xs text-slate-500"><span>{labelMonomerDftStage(dft.job.stage)}{dft.job.queue_position != null ? ` · 队列第 ${dft.job.queue_position} 位` : ""}</span><span>{Math.round(dft.job.progress_percent)}%</span></div><div className="h-2 overflow-hidden rounded-full bg-slate-100"><div className={cn("h-full rounded-full transition-all", dft.job.status === "failed" ? "bg-red-500" : "bg-sky-500")} style={{ width: `${Math.max(0, Math.min(100, dft.job.progress_percent))}%` }} /></div></div>{dft.job.error ? <div className="rounded-lg border border-red-200 bg-red-50 p-3 text-xs leading-5 text-red-800"><div className="font-semibold">{dft.job.error.code}</div><div>{dft.job.error.message}</div><div className="mt-1">{dft.job.error.retryable ? "可重试" : "不可重试，请修改输入或环境。"}</div></div> : null}<div className="flex flex-wrap gap-2">{!isMonomerDftTerminal(dft.job.status) ? <Button type="button" variant="outline" className="h-9 rounded-md border-red-200 px-3 text-xs text-red-700 hover:bg-red-50" onClick={() => void dft.cancel()} disabled={dft.isCancelling || dft.job.status === "cancel_requested"}>{dft.isCancelling ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : <Ban className="mr-1.5 h-3.5 w-3.5" />}取消</Button> : <><Button type="button" variant="outline" className="h-9 rounded-md border-slate-200 px-3 text-xs" onClick={() => void dft.rerun()} disabled={dft.isSubmitting || !serviceReady}><RotateCcw className="mr-1.5 h-3.5 w-3.5" />{submissionDisabled ? "功能尚未开放" : "重跑同参数"}</Button><Button type="button" variant="outline" className="h-9 rounded-md border-red-200 px-3 text-xs text-red-700 hover:bg-red-50" disabled={dft.deletingJobIds.includes(dft.job.job_id)} onClick={() => { const selected = dft.job; if (selected && window.confirm("删除后，任务参数、结果、深链接和在线存储都无法在产品中恢复。确定继续吗？")) void dft.deleteJobRecord(selected); }}>{dft.deletingJobIds.includes(dft.job.job_id) ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : <Trash2 className="mr-1.5 h-3.5 w-3.5" />}删除记录</Button></>}<Button type="button" variant="outline" className="h-9 rounded-md border-slate-200 px-3 text-xs" onClick={dft.clearJob} disabled={Boolean(activeJob)}>清空当前</Button></div></div> : <div className="mt-6 rounded-xl border border-dashed border-slate-200 p-6 text-center text-xs leading-5 text-slate-500">提交任务或从全局历史选择记录。URL 中的 <code>?job=uuid</code> 可直接恢复查看与轮询。</div>}
          </section>

          <section className="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm">
            <div className="flex items-center justify-between gap-3"><div className="flex items-center gap-2 text-sm font-semibold text-slate-950"><History className="h-4 w-4 text-violet-600" />全局任务历史</div><Button type="button" variant="outline" className="h-8 w-8 rounded-md p-0" onClick={() => void dft.refreshHistory()} disabled={dft.isHistoryLoading} aria-label="刷新历史"><RefreshCw className={cn("h-3.5 w-3.5", dft.isHistoryLoading && "animate-spin")} /></Button></div>
            <div className="mt-3 grid grid-cols-2 gap-2"><Select value={dft.historyQuery.status ?? ""} onChange={(event) => dft.changeHistoryQuery({ page: 1, status: event.target.value as MonomerDftJobStatus | "" })} className="h-9 rounded-lg border-slate-200 bg-white px-2 text-xs"><option value="">全部状态</option>{Object.entries(STATUS_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</Select><Select value={dft.historyQuery.calculation_type ?? ""} onChange={(event) => dft.changeHistoryQuery({ page: 1, calculation_type: event.target.value as MonomerDftCalculationType | "" })} className="h-9 rounded-lg border-slate-200 bg-white px-2 text-xs"><option value="">全部类型</option><option value="single_point">单点</option><option value="optimization">几何优化</option></Select></div>
            {dft.historyError ? <div className="mt-3 rounded-lg border border-amber-200 bg-amber-50 p-2 text-xs text-amber-900">{dft.historyError}</div> : null}
            <div className="mt-3 max-h-[430px] space-y-2 overflow-auto pr-1">{dft.history?.items.length ? dft.history.items.map((item) => <div key={item.job_id} className={cn("rounded-lg border p-3", dft.job?.job_id === item.job_id ? "border-sky-300 bg-sky-50" : "border-slate-100 bg-white")}><button type="button" onClick={() => dft.loadJob(item.job_id)} className="w-full text-left hover:opacity-80"><div className="flex items-center justify-between gap-2"><span className="truncate font-mono text-[11px] text-slate-600">{item.job_id}</span><StatusBadge status={item.status} /></div><div className="mt-2 truncate text-xs font-medium text-slate-800">{item.request.input.smiles}</div><div className="mt-1 text-[11px] text-slate-500">{item.request.calculation_type === "single_point" ? "单点" : "优化"} · {item.request.model} · {formatDate(item.created_at)}</div></button>{isMonomerDftTerminal(item.status) ? <Button type="button" variant="outline" className="mt-2 h-8 rounded-md border-red-200 px-2.5 text-xs text-red-700 hover:bg-red-50" disabled={dft.deletingJobIds.includes(item.job_id)} onClick={() => { if (window.confirm("删除后，任务参数、结果、深链接和在线存储都无法在产品中恢复。确定继续吗？")) void dft.deleteJobRecord(item); }}>{dft.deletingJobIds.includes(item.job_id) ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : <Trash2 className="mr-1.5 h-3.5 w-3.5" />}删除记录</Button> : null}{dft.deleteJobErrors[item.job_id] ? <div className="mt-2 text-[11px] text-red-700">{dft.deleteJobErrors[item.job_id]}</div> : null}</div>) : <div className="rounded-lg border border-dashed border-slate-200 p-5 text-center text-xs text-slate-500">{dft.isHistoryLoading ? "读取历史…" : "没有符合条件的任务"}</div>}</div>
            <div className="mt-3 flex items-center justify-between text-xs text-slate-500"><Button type="button" variant="outline" className="h-8 w-8 rounded-md p-0" disabled={dft.historyQuery.page <= 1} onClick={() => dft.changeHistoryQuery({ page: dft.historyQuery.page - 1 })}><ChevronLeft className="h-3.5 w-3.5" /></Button><span>第 {dft.historyQuery.page} / {historyPageCount} 页 · 共 {dft.history?.total ?? 0} 项</span><Button type="button" variant="outline" className="h-8 w-8 rounded-md p-0" disabled={dft.historyQuery.page >= historyPageCount} onClick={() => dft.changeHistoryQuery({ page: dft.historyQuery.page + 1 })}><ChevronRight className="h-3.5 w-3.5" /></Button></div>
          </section>
        </aside>
      </div>

      <section className="space-y-3"><div className="flex items-center justify-between"><div><h2 className="text-base font-semibold text-slate-950">结果与产物</h2><p className="mt-1 text-xs text-slate-500">显示后端返回的真实值、实际耗时和可复现性信息。</p></div>{pollStatusLabel ? <span className={cn("inline-flex items-center gap-1.5 text-xs", dft.pollState === "degraded" ? "text-amber-700" : dft.pollState === "stopped" ? "text-red-700" : "text-sky-700")}>{dft.isJobLoading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <TriangleAlert className="h-3.5 w-3.5" />}{pollStatusLabel}</span> : null}</div>{dft.job ? <MonomerDftResults job={dft.job} onDeleteArtifacts={() => { if (window.confirm("确定删除该任务在服务器上的计算产物吗？任务元数据会保留。")) void dft.deleteArtifacts(); }} isDeletingArtifacts={dft.isDeletingArtifacts} /> : <div className="rounded-2xl border border-slate-200 bg-white p-10 text-center text-sm text-slate-500"><Info className="mx-auto mb-2 h-6 w-6 text-slate-300" />尚未选择任务。</div>}</section>
    </main>
  </div>;
}
