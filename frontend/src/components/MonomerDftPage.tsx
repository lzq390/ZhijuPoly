import { ModulePageHeader } from "./ModulePageHeader";
import { useContentMotion } from "../hooks/useContentMotion";
import {
  Activity,
  Atom,
  Ban,
  CheckCircle2,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Download,
  FlaskConical,
  Gauge,
  History,
  Info,
  Loader2,
  Play,
  RefreshCw,
  RotateCcw,
  Settings2,
  Trash2,
  TriangleAlert,
  XCircle
} from "lucide-react";
import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent
} from "react";
import {
  isMonomerDftTerminal,
  MONOMER_DFT_HISTORY_PAGE_SIZE,
  useMonomerDftJob,
  validateMonomerDftRequest
} from "../hooks/useMonomerDftJob";
import {
  hasAvailableMonomerDftArtifacts,
  labelMonomerDftStage,
  userFacingMonomerDftMessage
} from "../lib/monomerDftPresentation";
import { hasInvalidMonomerDftJobSearch } from "../lib/monomerDftRouting";
import {
  downloadMonomerDftBundle,
  MonomerDftApiError,
  standardizeSmiles
} from "../services/api";
import type {
  MonomerDftCalculationType,
  MonomerDftJobCreateRequest,
  MonomerDftJobResponse,
  MonomerDftJobStatus,
  MonomerDftModelCapability,
  MonomerDftModelName,
  MonomerDftPostOptimizationProperty,
  MonomerDftProperty,
  StructureWorkspaceContext
} from "../types";
import { MonomerDftStructureInput } from "./monomer-dft/MonomerDftStructureInput";
import {
  MonomerDftSelect,
  type MonomerDftSelectOption
} from "./monomer-dft/MonomerDftSelect";
import { MonomerDftResults } from "./MonomerDftResults";
import "../styles/structure-workbench.css";
import "../styles/monomer-dft.css";

type MonomerDftPageProps = {
  structure: StructureWorkspaceContext;
  initialJobId: string | null;
  onJobIdChange: (jobId: string | null) => void;
  onEditStructure: () => void;
};

type PrimaryTab = "config" | "tasks" | "results";
type DftController = ReturnType<typeof useMonomerDftJob>;

const PRIMARY_TABS: Array<{
  id: PrimaryTab;
  label: string;
  description: string;
  surfaceTitle: string;
  surfaceDescription: string;
  icon: typeof FlaskConical;
}> = [
  {
    id: "config",
    label: "计算配置",
    description: "结构与方法",
    surfaceTitle: "计算配置",
    surfaceDescription: "设置结构、计算方式与输出内容。",
    icon: FlaskConical
  },
  {
    id: "tasks",
    label: "任务中心",
    description: "进度与历史",
    surfaceTitle: "任务中心",
    surfaceDescription: "跟踪任务进度，查看历史记录。",
    icon: History
  },
  {
    id: "results",
    label: "结果分析",
    description: "结构与性质",
    surfaceTitle: "结果分析",
    surfaceDescription: "分析结构、性质与原子数据。",
    icon: Gauge
  }
];

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

const PROPERTY_OPTIONS: Array<{
  value: MonomerDftProperty;
  label: string;
  detail: string;
}> = [
  { value: "energy", label: "能量", detail: "eV · 必选" },
  { value: "forces", label: "原子力", detail: "eV/Å" },
  { value: "charges", label: "原子电荷", detail: "e" },
  { value: "hessian", label: "二阶力常数", detail: "用于结构稳定性分析" },
  { value: "frequencies", label: "振动频率", detail: "自动包含二阶力常数" }
];

type ModelPurpose = {
  label: string;
  description: string;
};

export function describeMonomerDftModelPurpose(model: MonomerDftModelCapability): ModelPurpose {
  if (model.id === "aimnet2-nse") {
    return { label: "带电与多重态体系", description: "适合离子、自由基和键解离结构" };
  }
  if (model.id === "aimnet2-pd") {
    return { label: "含钯催化体系", description: "适合包含钯元素的催化结构" };
  }
  if (model.id === "aimnet2-rxn") {
    return { label: "反应路径与活性结构", description: "适合由氢、碳、氮、氧组成的反应体系" };
  }
  return { label: "通用有机分子", description: "适合常见有机分子与单重态结构" };
}

function formatDate(value: string | null | undefined): string {
  if (!value) return "--";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleString("zh-CN", { hour12: false });
}

function statusIcon(status: MonomerDftJobStatus) {
  if (status === "completed") return <CheckCircle2 />;
  if (status === "failed") return <XCircle />;
  if (status === "cancelled") return <Ban />;
  if (status === "running" || status === "cancel_requested") {
    return <Loader2 className="np-dft-spin" />;
  }
  return <Activity />;
}

function StatusBadge({ status }: { status: MonomerDftJobStatus }) {
  return (
    <span className={`np-dft-status-badge is-${status}`}>
      {statusIcon(status)}
      {STATUS_LABELS[status]}
    </span>
  );
}

function PropertyChoices({
  values,
  onChange,
  supported,
  locked,
  compact = false
}: {
  values: MonomerDftProperty[];
  onChange: (values: MonomerDftProperty[]) => void;
  supported: MonomerDftProperty[];
  locked: boolean;
  compact?: boolean;
}) {
  const options = compact
    ? PROPERTY_OPTIONS.filter((option) => option.value === "hessian" || option.value === "frequencies")
    : PROPERTY_OPTIONS;

  return (
    <div className={`np-dft-property-grid${compact ? " is-compact" : ""}`}>
      {options.map((option) => {
        const checked = values.includes(option.value);
        const fixed = !compact && option.value === "energy";
        const unavailable = !supported.includes(option.value);
        const disabled = locked || fixed || (unavailable && !checked);
        return (
          <label
            key={option.value}
            className={`${checked ? "is-selected" : ""}${unavailable ? " is-unavailable" : ""}`}
          >
            <input
              type="checkbox"
              checked={checked}
              disabled={disabled}
              onChange={(event) => {
                if (event.target.checked) onChange([...values, option.value]);
                else onChange(values.filter((item) => item !== option.value));
              }}
            />
            <span>
              <strong>{option.label}</strong>
              <small>{unavailable ? `${option.detail} · 当前方案不支持` : option.detail}</small>
            </span>
          </label>
        );
      })}
    </div>
  );
}

function SelectedJobCard({
  dft,
  serviceReady,
  submissionDisabled,
  requestCreating,
  onShowTasks,
  onRerun
}: {
  dft: DftController;
  serviceReady: boolean;
  submissionDisabled: boolean;
  requestCreating: boolean;
  onShowTasks?: () => void;
  onRerun: () => void;
}) {
  const selectedJobId = dft.job?.job_id ?? null;
  const downloadAbortRef = useRef<AbortController | null>(null);
  const [isDownloading, setIsDownloading] = useState(false);
  const [downloadFeedback, setDownloadFeedback] = useState<string | null>(null);
  const [downloadError, setDownloadError] = useState<string | null>(null);

  useEffect(() => {
    downloadAbortRef.current?.abort();
    downloadAbortRef.current = null;
    setIsDownloading(false);
    setDownloadFeedback(null);
    setDownloadError(null);
    return () => {
      downloadAbortRef.current?.abort();
      downloadAbortRef.current = null;
    };
  }, [selectedJobId]);

  const job = dft.job;
  if (!job) {
    return (
      <div className="np-dft-empty-state is-compact">
        <Activity />
        <div>
          <strong>尚未选择任务</strong>
          <span>提交新任务，或从历史记录中选择一项。</span>
        </div>
      </div>
    );
  }

  const jobId = job.job_id;
  const progress = Math.max(0, Math.min(100, job.progress_percent));
  const deleting = dft.deletingJobIds.includes(job.job_id);
  const canDownloadResults = isMonomerDftTerminal(job.status) &&
    hasAvailableMonomerDftArtifacts(job);
  const jobFailureMessage = job.error
    ? userFacingMonomerDftMessage(job.error.message, {
      code: job.error.code,
      fallback: "计算任务未能完成，请检查输入后重试。"
    })
    : null;
  const actionErrorMessage = dft.jobError
    ? userFacingMonomerDftMessage(dft.jobError)
    : null;

  async function downloadResults() {
    if (!canDownloadResults || isDownloading) return;
    downloadAbortRef.current?.abort();
    const controller = new AbortController();
    downloadAbortRef.current = controller;
    setIsDownloading(true);
    setDownloadFeedback(null);
    setDownloadError(null);
    let objectUrl: string | null = null;
    try {
      const bundle = await downloadMonomerDftBundle(jobId, controller.signal);
      if (controller.signal.aborted) return;
      objectUrl = URL.createObjectURL(bundle);
      const link = document.createElement("a");
      link.href = objectUrl;
      link.download = `monomer-dft-${jobId}.zip`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      const completedObjectUrl = objectUrl;
      objectUrl = null;
      window.setTimeout(() => URL.revokeObjectURL(completedObjectUrl), 0);
      setDownloadFeedback("结果下载已开始。");
    } catch (error) {
      if (controller.signal.aborted || (error instanceof Error && error.name === "AbortError")) return;
      setDownloadError(error instanceof Error
        ? userFacingMonomerDftMessage(error.message, {
          code: error instanceof MonomerDftApiError ? error.code : null,
          fallback: "结果下载失败，请稍后重试。"
        })
        : "结果下载失败，请稍后重试。");
    } finally {
      if (objectUrl) URL.revokeObjectURL(objectUrl);
      if (downloadAbortRef.current === controller) {
        downloadAbortRef.current = null;
        setIsDownloading(false);
      }
    }
  }

  return (
    <section className="np-dft-selected-job" aria-labelledby="np-dft-selected-job-title">
      <header className="np-dft-selected-job__header">
        <div className="np-dft-job-identity">
          <span>当前任务</span>
          <code id="np-dft-selected-job-title" tabIndex={0} title={job.job_id}>{job.job_id}</code>
          <p>本次提交参数已固定</p>
        </div>
        <StatusBadge status={job.status} />
      </header>

      <dl className="np-dft-job-facts">
        <div><dt>计算类型</dt><dd className="is-ui-value">{job.calculation_type === "single_point" ? "单点计算" : "几何优化"}</dd></div>
        <div><dt>创建时间</dt><dd>{formatDate(job.created_at)}</dd></div>
        <div><dt>队列位置</dt><dd className="is-ui-value">{job.queue_position == null ? "--" : `第 ${job.queue_position} 位`}</dd></div>
      </dl>

      <div className="np-dft-job-progress">
        <div>
          <span>{labelMonomerDftStage(job.stage)}</span>
          <strong>{Math.round(progress)}%</strong>
        </div>
        <div
          className={`np-dft-progress-track${job.status === "failed" ? " is-error" : ""}`}
          role="progressbar"
          aria-label="任务进度"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={Math.round(progress)}
        >
          <i style={{ width: `${progress}%` }} />
        </div>
      </div>

      {job.error && jobFailureMessage ? (
        <div className="np-dft-inline-message is-error" role="alert">
          <TriangleAlert />
          <div>
            <strong>计算未完成</strong>
            <span>{jobFailureMessage}</span>
            <small>{job.error.retryable ? "可以重新尝试" : "请修改输入后重新提交。"}</small>
          </div>
        </div>
      ) : null}
      {actionErrorMessage && actionErrorMessage !== jobFailureMessage ? (
        <div className="np-dft-inline-message is-error" role="alert">
          <TriangleAlert />
          <div>
            <strong>操作未完成</strong>
            <span>{actionErrorMessage}</span>
          </div>
        </div>
      ) : null}

      <div className="np-dft-job-actions">
        {!isMonomerDftTerminal(job.status) ? (
          <button
            type="button"
            className="is-danger"
            onClick={() => void dft.cancel()}
            disabled={dft.isCancelling || job.status === "cancel_requested"}
          >
            {dft.isCancelling ? <Loader2 className="np-dft-spin" /> : <Ban />}
            {dft.isCancelling ? "正在取消" : "取消任务"}
          </button>
        ) : (
          <>
            <button
              type="button"
              onClick={onRerun}
              disabled={requestCreating || isDownloading || !serviceReady}
            >
              {dft.isSubmitting ? <Loader2 className="np-dft-spin" /> : <RotateCcw />}
              {submissionDisabled ? "功能尚未开放" : "重跑同参数"}
            </button>
            <button
              type="button"
              className="is-danger"
              disabled={deleting || requestCreating || isDownloading}
              onClick={() => {
                if (window.confirm("删除后，本次任务的参数、结果和分享链接都无法恢复。确定继续吗？")) {
                  void dft.deleteJobRecord(job);
                }
              }}
            >
              {deleting ? <Loader2 className="np-dft-spin" /> : <Trash2 />}
              {deleting ? "正在删除" : "删除记录"}
            </button>
          </>
        )}
        <button
          type="button"
          onClick={dft.clearJob}
          disabled={!isMonomerDftTerminal(job.status) || requestCreating || isDownloading}
        >
          取消选择
        </button>
        {onShowTasks ? <button type="button" onClick={onShowTasks}><History />任务中心</button> : null}
        <button
          type="button"
          className="np-dft-download-results"
          disabled={!canDownloadResults || isDownloading || requestCreating}
          title={!canDownloadResults
            ? isMonomerDftTerminal(job.status) ? "结果文件不可用" : "任务完成后可下载"
            : undefined}
          onClick={() => void downloadResults()}
        >
          {isDownloading ? <Loader2 className="np-dft-spin" /> : <Download />}
          {isDownloading ? "正在准备下载" : "下载结果"}
        </button>
      </div>
      {downloadFeedback ? <p className="np-dft-action-feedback" role="status">{downloadFeedback}</p> : null}
      {downloadError ? <p className="np-dft-action-feedback is-error" role="alert">{downloadError}</p> : null}
    </section>
  );
}

function TaskCenter({
  dft,
  serviceReady,
  submissionDisabled,
  requestCreating,
  onSelectJob,
  onRerun
}: {
  dft: DftController;
  serviceReady: boolean;
  submissionDisabled: boolean;
  requestCreating: boolean;
  onSelectJob: (jobId: string) => void;
  onRerun: () => void;
}) {
  const pageCount = Math.max(1, Math.ceil(
    (dft.history?.total ?? 0) / (dft.history?.page_size ?? MONOMER_DFT_HISTORY_PAGE_SIZE)
  ));

  return (
    <div className="np-dft-task-center">
      <SelectedJobCard
        dft={dft}
        serviceReady={serviceReady}
        submissionDisabled={submissionDisabled}
        requestCreating={requestCreating}
        onRerun={onRerun}
      />

      <div className="np-dft-trust-notice">
        <Info />
        <div>
          <strong>共享任务记录</strong>
          <span>此工作区中的访问者都可以查看和管理这些任务。</span>
        </div>
      </div>

      <section className="np-dft-history" aria-labelledby="np-dft-history-title">
        <header className="np-dft-section-heading is-row">
          <div>
            <span className="np-dft-eyebrow">TASK HISTORY</span>
            <h3 id="np-dft-history-title">任务历史</h3>
            <p>每页 10 条；筛选不切换当前结果。</p>
          </div>
          <button
            type="button"
            className="np-dft-icon-button"
            aria-label="刷新任务历史"
            title="刷新任务历史"
            onClick={() => void dft.refreshHistory()}
            disabled={dft.isHistoryLoading}
          >
            <RefreshCw className={dft.isHistoryLoading ? "np-dft-spin" : ""} />
          </button>
        </header>

        <div className="np-dft-history-filters">
          <div className="np-dft-field">
            <span id="np-dft-history-status-label">任务状态</span>
            <MonomerDftSelect
              id="np-dft-history-status"
              ariaLabelledBy="np-dft-history-status-label"
              value={(dft.historyQuery.status ?? "") as MonomerDftJobStatus | ""}
              options={[
                { value: "", label: "全部状态" },
                ...Object.entries(STATUS_LABELS).map(([value, label]) => ({
                  value: value as MonomerDftJobStatus,
                  label
                }))
              ]}
              onChange={(status) => dft.changeHistoryQuery({ page: 1, status })}
            />
          </div>
          <div className="np-dft-field">
            <span id="np-dft-history-type-label">计算类型</span>
            <MonomerDftSelect
              id="np-dft-history-type"
              ariaLabelledBy="np-dft-history-type-label"
              value={(dft.historyQuery.calculation_type ?? "") as MonomerDftCalculationType | ""}
              options={[
                { value: "", label: "全部类型" },
                { value: "single_point", label: "单点计算" },
                { value: "optimization", label: "几何优化" }
              ]}
              onChange={(calculation_type) => dft.changeHistoryQuery({ page: 1, calculation_type })}
            />
          </div>
        </div>

        {dft.historyError ? (
          <div className="np-dft-inline-message is-warning" role="alert">
            <TriangleAlert />
            <span>{userFacingMonomerDftMessage(dft.historyError)}</span>
          </div>
        ) : null}

        <div className="np-dft-history-list" aria-live="polite">
          {dft.history?.items.length ? dft.history.items.map((item) => {
            const selected = dft.job?.job_id === item.job_id;
            const deleting = dft.deletingJobIds.includes(item.job_id);
            return (
              <article key={item.job_id} className={selected ? "is-selected" : ""}>
                <button
                  type="button"
                  className="np-dft-history-select"
                  disabled={requestCreating}
                  title={`${item.job_id}\n${item.request.input.smiles}`}
                  onClick={() => onSelectJob(item.job_id)}
                >
                  <div className="np-dft-history-row">
                    <code>{item.job_id}</code>
                    <StatusBadge status={item.status} />
                  </div>
                  <strong>{item.request.input.smiles}</strong>
                  <span>
                    {item.calculation_type === "single_point" ? "单点计算" : "几何优化"}
                    {" · "}{formatDate(item.created_at)}
                  </span>
                </button>
                {isMonomerDftTerminal(item.status) ? (
                  <button
                    type="button"
                    className="np-dft-history-delete"
                    disabled={deleting || requestCreating}
                    onClick={() => {
                      if (window.confirm("删除后，本次任务的参数、结果和分享链接都无法恢复。确定继续吗？")) {
                        void dft.deleteJobRecord(item);
                      }
                    }}
                  >
                    {deleting ? <Loader2 className="np-dft-spin" /> : <Trash2 />}
                    {deleting ? "正在删除" : "删除记录"}
                  </button>
                ) : null}
                {dft.deleteJobErrors[item.job_id] ? (
                  <p className="np-dft-action-feedback is-error" role="alert">{userFacingMonomerDftMessage(dft.deleteJobErrors[item.job_id])}</p>
                ) : null}
              </article>
            );
          }) : (
            <div className="np-dft-empty-state is-compact">
              {dft.isHistoryLoading ? <Loader2 className="np-dft-spin" /> : <History />}
              <div>
                <strong>{dft.isHistoryLoading ? "正在读取历史" : "没有符合条件的任务"}</strong>
                <span>{dft.isHistoryLoading ? "请稍候…" : "可以调整筛选条件或提交新任务。"}</span>
              </div>
            </div>
          )}
        </div>

        <nav className="np-dft-pagination" aria-label="任务历史分页">
          <button
            type="button"
            aria-label="上一页"
            disabled={dft.historyQuery.page <= 1}
            onClick={() => dft.changeHistoryQuery({ page: dft.historyQuery.page - 1 })}
          >
            <ChevronLeft />
          </button>
          <span>第 <strong>{dft.historyQuery.page}</strong> / {pageCount} 页 · 共 {dft.history?.total ?? 0} 项</span>
          <button
            type="button"
            aria-label="下一页"
            disabled={dft.historyQuery.page >= pageCount}
            onClick={() => dft.changeHistoryQuery({ page: dft.historyQuery.page + 1 })}
          >
            <ChevronRight />
          </button>
        </nav>
      </section>
    </div>
  );
}

function samePropertySelection<T extends string>(left: T[], right: T[]): boolean {
  return left.length === right.length && left.every((item) => right.includes(item));
}

export function monomerDftRequestsMatch(
  job: MonomerDftJobResponse | null,
  request: MonomerDftJobCreateRequest
) {
  if (!job) return true;
  const submitted = job.request;
  if (
    submitted.calculation_type !== request.calculation_type ||
    submitted.input.smiles !== request.input.smiles ||
    submitted.input.net_charge !== request.input.net_charge ||
    submitted.input.multiplicity !== request.input.multiplicity ||
    submitted.input.psmiles_mode !== request.input.psmiles_mode ||
    submitted.model !== request.model ||
    submitted.conformer.seed !== request.conformer.seed ||
    submitted.conformer.max_iterations !== request.conformer.max_iterations
  ) return false;
  if (submitted.calculation_type === "single_point" && request.calculation_type === "single_point") {
    return samePropertySelection(submitted.single_point.properties, request.single_point.properties);
  }
  if (submitted.calculation_type === "optimization" && request.calculation_type === "optimization") {
    return submitted.optimization.fmax_eV_per_A === request.optimization.fmax_eV_per_A &&
      submitted.optimization.max_steps === request.optimization.max_steps &&
      samePropertySelection(
        submitted.optimization.post_optimization_properties,
        request.optimization.post_optimization_properties
      );
  }
  return false;
}

export function MonomerDftPage({
  structure,
  initialJobId,
  onJobIdChange,
  onEditStructure
}: MonomerDftPageProps) {
  const dft = useMonomerDftJob({ initialJobId, onJobIdChange });
  const [activeTab, setActiveTab] = useState<PrimaryTab>(initialJobId ? "results" : "config");
  const tabContentRef = useRef<HTMLDivElement | null>(null);
  useContentMotion(tabContentRef, activeTab, "tab");
  const [smilesDraft, setSmilesDraft] = useState(structure.smiles);
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
  const [isPreparingSubmission, setIsPreparingSubmission] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const defaultsInitializedRef = useRef(false);
  const submissionPreparingRef = useRef(false);
  const submissionPreparationAbortRef = useRef<AbortController | null>(null);
  const scrollRegionRef = useRef<HTMLDivElement | null>(null);

  const selectableModels = useMemo(
    () => selectableMonomerDftModels(dft.capabilities?.models ?? []),
    [dft.capabilities]
  );

  useEffect(() => {
    setSmilesDraft(structure.smiles);
  }, [structure.smiles]);

  useEffect(() => {
    if (initialJobId) {
      setActiveTab("results");
      if (scrollRegionRef.current) scrollRegionRef.current.scrollTop = 0;
    }
  }, [initialJobId]);

  useEffect(() => () => {
    submissionPreparationAbortRef.current?.abort();
    submissionPreparationAbortRef.current = null;
  }, []);

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
    setModelId((current) => current && selectableModels.some((model) => model.id === current && model.available)
      ? current
      : selectableModels.find((model) => model.id === dft.capabilities?.default_model && model.available)?.id
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
    if (!smilesDraft.includes("*")) setPsmilesMode(null);
  }, [smilesDraft]);

  const selectedModel = selectableModels.find((model) => model.id === modelId) ?? null;
  const purposeModels = useMemo(() => {
    const isGeneralPurpose = (model: MonomerDftModelCapability) =>
      model.id === "aimnet2" || model.id === "aimnet2-2025";
    const generalModels = selectableModels.filter(isGeneralPurpose);
    const generalChoice = generalModels.find((model) => model.id === modelId && model.available)
      ?? generalModels.find((model) => model.id === dft.capabilities?.default_model && model.available)
      ?? generalModels.find((model) => model.available)
      ?? generalModels.find((model) => model.id === modelId)
      ?? generalModels.find((model) => model.id === dft.capabilities?.default_model)
      ?? generalModels[0];
    let generalAdded = false;
    return selectableModels.flatMap((model) => {
      if (!isGeneralPurpose(model)) return [model];
      if (generalAdded || !generalChoice) return [];
      generalAdded = true;
      return [generalChoice];
    });
  }, [dft.capabilities?.default_model, modelId, selectableModels]);
  const modelOptions = useMemo<Array<MonomerDftSelectOption<MonomerDftModelName | "">>>(() => [
    {
      value: "",
      label: dft.capabilities ? "请选择计算用途" : "正在载入计算方案",
      description: dft.capabilities ? "根据分子类型选择适用范围" : "请稍候",
      disabled: true
    },
    ...purposeModels.map((model) => {
      const purpose = describeMonomerDftModelPurpose(model);
      return {
        value: model.id,
        label: purpose.label,
        description: model.available ? purpose.description : `${purpose.description} · 暂不可选择`,
        disabled: !model.available
      };
    })
  ], [dft.capabilities, purposeModels]);
  const netCharge = netChargeText.trim() === "" ? null : Number(netChargeText);
  const requestProperties: MonomerDftProperty[] = calculationType === "single_point"
    ? properties
    : ["energy", "forces", "charges", ...postOptimizationProperties];
  const validationIssues = useMemo(() => validateMonomerDftRequest({
    smiles: smilesDraft,
    netCharge,
    multiplicity,
    psmilesMode,
    calculationType,
    modelId,
    properties: requestProperties,
    fmax,
    maxSteps,
    seed,
    maxIterations
  }, dft.capabilities), [
    calculationType,
    dft.capabilities,
    fmax,
    maxIterations,
    maxSteps,
    modelId,
    multiplicity,
    netCharge,
    postOptimizationProperties,
    properties,
    psmilesMode,
    seed,
    smilesDraft
  ]);

  const maxActiveJobs = dft.serviceStatus?.max_active_jobs ?? dft.capabilities?.limits.max_active_jobs;
  const minOptimizationSteps = dft.capabilities?.limits.min_optimization_steps ?? 10;
  const maxOptimizationSteps = dft.capabilities?.limits.max_optimization_steps ?? 50;
  const capabilitiesRuntimeReady = dft.capabilities?.worker?.runtime_ready;
  const capabilitiesDraining = dft.capabilities?.worker?.draining;
  const serviceDraining = dft.serviceStatus?.draining === true || capabilitiesDraining === true;
  const capacityFull = maxActiveJobs != null && (dft.serviceStatus?.active_jobs ?? 0) >= maxActiveJobs;
  const submissionDisabled = dft.serviceStatus?.enabled === false || dft.capabilities?.enabled === false;
  const serviceReady = Boolean(
    !dft.serviceError &&
    dft.serviceStatus?.schema_ready &&
    dft.capabilities?.schema_ready &&
    dft.serviceStatus.enabled &&
    dft.capabilities.enabled &&
    dft.serviceStatus.available &&
    dft.capabilities.available &&
    dft.serviceStatus.runtime_ready === true &&
    dft.serviceStatus.draining === false &&
    capabilitiesRuntimeReady !== false &&
    capabilitiesDraining !== true &&
    !capacityFull
  );
  const activeJob = dft.job && !isMonomerDftTerminal(dft.job.status);
  const configLocked = Boolean(activeJob) || isPreparingSubmission || dft.isSubmitting;
  const canSubmit = serviceReady && validationIssues.length === 0 && !configLocked;
  const invalidJobDeepLink = typeof window !== "undefined" && hasInvalidMonomerDftJobSearch(window.location.search);
  const activeTabDefinition = PRIMARY_TABS.find((tab) => tab.id === activeTab) ?? PRIMARY_TABS[0];
  const SurfaceIcon = activeTabDefinition.icon;

  const currentRequest = useMemo<MonomerDftJobCreateRequest | null>(() => {
    if (!selectedModel) return null;
    const common = {
      input: {
        smiles: smilesDraft.trim(),
        net_charge: netCharge,
        multiplicity,
        psmiles_mode: psmilesMode
      },
      model: selectedModel.id,
      conformer: { seed, max_iterations: maxIterations }
    };
    return calculationType === "single_point"
      ? { ...common, calculation_type: "single_point", single_point: { properties } }
      : {
        ...common,
        calculation_type: "optimization",
        optimization: {
          fmax_eV_per_A: fmax,
          max_steps: maxSteps,
          post_optimization_properties: postOptimizationProperties
        }
      };
  }, [
    calculationType,
    fmax,
    maxIterations,
    maxSteps,
    multiplicity,
    netCharge,
    postOptimizationProperties,
    properties,
    psmilesMode,
    seed,
    selectedModel,
    smilesDraft
  ]);
  const resultUsesPreviousSnapshot = Boolean(currentRequest && !monomerDftRequestsMatch(dft.job, currentRequest));

  function showResultsAtTop() {
    setActiveTab("results");
    if (scrollRegionRef.current) scrollRegionRef.current.scrollTop = 0;
  }

  function changeSmilesDraft(value: string) {
    setSmilesDraft(value);
    structure.setSmiles(value);
    setFormError(null);
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (submissionPreparingRef.current) return;
    submissionPreparingRef.current = true;
    submissionPreparationAbortRef.current?.abort();
    const preparationController = new AbortController();
    submissionPreparationAbortRef.current = preparationController;
    setIsPreparingSubmission(true);
    setFormError(null);
    try {
      const rawSmiles = smilesDraft.trim();
      let standardizedSmiles = rawSmiles;
      if (rawSmiles) {
        const standardized = await standardizeSmiles({ smiles: rawSmiles }, preparationController.signal);
        if (preparationController.signal.aborted) return;
        standardizedSmiles = standardized.standardized_smiles.trim();
        if (standardizedSmiles && standardizedSmiles !== rawSmiles) {
          setSmilesDraft(standardizedSmiles);
          structure.setSmiles(standardizedSmiles);
        }
      }
      const issues = validateMonomerDftRequest({
        smiles: standardizedSmiles,
        netCharge,
        multiplicity,
        psmilesMode,
        calculationType,
        modelId,
        properties: requestProperties,
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
        setFormError("请选择适合当前分子的计算用途。");
        return;
      }
      const common = {
        input: {
          smiles: standardizedSmiles,
          net_charge: netCharge,
          multiplicity,
          psmiles_mode: psmilesMode
        },
        model: submitModel.id,
        conformer: { seed, max_iterations: maxIterations }
      };
      const request: MonomerDftJobCreateRequest = calculationType === "single_point"
        ? { ...common, calculation_type: "single_point", single_point: { properties } }
        : {
          ...common,
          calculation_type: "optimization",
          optimization: {
            fmax_eV_per_A: fmax,
            max_steps: maxSteps,
            post_optimization_properties: postOptimizationProperties
          }
      };
      const jobId = await dft.submit(request);
      if (preparationController.signal.aborted) return;
      if (jobId) showResultsAtTop();
    } catch (error) {
      if (preparationController.signal.aborted || (error instanceof Error && error.name === "AbortError")) return;
      setFormError("SMILES 标准化失败，任务未提交。请检查结构后重试。");
    } finally {
      if (submissionPreparationAbortRef.current === preparationController) {
        submissionPreparationAbortRef.current = null;
        submissionPreparingRef.current = false;
        setIsPreparingSubmission(false);
      }
    }
  }

  async function rerunSelectedJob() {
    const jobId = await dft.rerun();
    if (jobId) showResultsAtTop();
  }

  function handleTabKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const currentIndex = PRIMARY_TABS.findIndex((tab) => tab.id === activeTab);
    const nextIndex = event.key === "Home"
      ? 0
      : event.key === "End"
        ? PRIMARY_TABS.length - 1
        : (currentIndex + (event.key === "ArrowRight" ? 1 : -1) + PRIMARY_TABS.length) % PRIMARY_TABS.length;
    const nextTab = PRIMARY_TABS[nextIndex];
    setActiveTab(nextTab.id);
    document.getElementById(`monomer-dft-main-tab-${nextTab.id}`)?.focus();
  }

  const serviceTone = dft.isServiceLoading
    ? "loading"
    : dft.serviceError
      ? "error"
      : dft.serviceStatus?.schema_ready === false || dft.capabilities?.schema_ready === false
      ? "warning"
      : submissionDisabled
        ? "disabled"
        : serviceDraining
          ? "warning"
          : capacityFull
            ? "capacity"
            : serviceReady
              ? "ready"
              : "error";
  const serviceLabel = dft.isServiceLoading
    ? "正在检查"
    : dft.serviceError
      ? "状态检查失败"
      : dft.serviceStatus?.schema_ready === false || dft.capabilities?.schema_ready === false
      ? "服务准备中"
      : submissionDisabled
        ? "暂未开放功能"
        : serviceDraining
          ? "暂缓新任务"
          : capacityFull
            ? "任务繁忙"
            : serviceReady
              ? "准备就绪"
              : "暂时不可用";
  const pollStatusLabel = dft.pollState === "degraded"
    ? "连接中断，正在自动重试"
    : dft.pollState === "stopped"
      ? "任务进度同步已暂停"
      : dft.pollState === "polling"
        ? "正在同步任务进度"
        : null;
  const structureIssue = validationIssues.find((issue) => issue.field === "smiles")?.message ?? null;

  return (
    <div className="np-module-page np-structure-workbench np-monomer-dft" data-module="monomer-dft">
      <ModulePageHeader>单体 DFT</ModulePageHeader>
      <div className="np-dft-page np-module-page-body">
        <div className="np-dft-module-toolbar" aria-label="单体 DFT 服务状态">
          <div className="np-dft-service-status">
            <span className={`is-${serviceTone}`} role="status">
              {serviceTone === "ready" ? <i className="np-dft-ready-dot" aria-hidden="true" /> : null}
              {serviceTone === "disabled" ? <i className="np-dft-disabled-dot" aria-hidden="true" /> : null}
              {serviceTone === "loading" ? <Loader2 className="np-dft-spin" /> : null}
              {serviceTone === "warning" || serviceTone === "capacity" ? <TriangleAlert /> : null}
              {serviceTone === "error" ? <XCircle /> : null}
              <strong>{serviceLabel}</strong>
            </span>
            <button
              type="button"
              onClick={() => void dft.refreshStatus()}
              disabled={dft.isServiceLoading}
            >
              <RefreshCw className={dft.isServiceLoading ? "np-dft-spin" : ""} />刷新
            </button>
          </div>
        </div>

        <div ref={scrollRegionRef} className="np-dft-scroll-region">
          <div className="np-dft-content-column">
            {dft.serviceStatus?.schema_ready === false ? (
              <div className="np-dft-page-message is-warning" role="alert">
                <TriangleAlert />
                <div><strong>服务正在准备</strong><span>历史记录与计算功能暂不可用，请稍后刷新。</span></div>
              </div>
            ) : null}
            {invalidJobDeepLink ? (
              <div className="np-dft-page-message is-error" role="alert">
                <TriangleAlert />
                <div><strong>任务链接无效</strong><span>链接中的任务编号格式不正确，已返回默认页面。</span></div>
              </div>
            ) : null}
            {submissionDisabled ? (
              <div className="np-dft-page-message" role="status">
                <Info />
                <div><strong>计算功能暂未开放</strong><span>当前可以查看历史记录和已有结果，暂不能提交或重新计算。</span></div>
              </div>
            ) : null}

            <main className="np-dft-workbench-surface np-sw-accented-surface" aria-label="单体 DFT 主工作区">
              <header className="np-dft-view-header">
                <div className="np-dft-view-heading">
                  <span className="np-dft-surface-mark"><SurfaceIcon /></span>
                  <div>
                    <h2>{activeTabDefinition.surfaceTitle}</h2>
                    <p>{activeTabDefinition.surfaceDescription}</p>
                  </div>
                </div>
              </header>

              <div className="np-dft-main-tabs" role="tablist" aria-label="单体 DFT 主工作区" onKeyDown={handleTabKeyDown}>
                {PRIMARY_TABS.map((tab) => {
                  const Icon = tab.icon;
                  return (
                    <button
                      key={tab.id}
                      id={`monomer-dft-main-tab-${tab.id}`}
                      type="button"
                      role="tab"
                      aria-selected={activeTab === tab.id}
                      aria-controls={`monomer-dft-main-panel-${tab.id}`}
                      tabIndex={activeTab === tab.id ? 0 : -1}
                      className={activeTab === tab.id ? "is-active" : ""}
                      onClick={() => setActiveTab(tab.id)}
                    >
                      <Icon />
                      <span><strong>{tab.label}</strong><small>{tab.description}</small></span>
                      {tab.id === "results" && dft.job ? <i className={`is-${dft.job.status}`} /> : null}
                    </button>
                  );
                })}
              </div>

              <section
                id={`monomer-dft-main-panel-${activeTab}`}
                ref={tabContentRef}
                role="tabpanel"
                aria-labelledby={`monomer-dft-main-tab-${activeTab}`}
                className="np-dft-main-panel"
              >
                {activeTab === "config" ? (
                  <form className="np-dft-config" onSubmit={handleSubmit} noValidate>
                    <section className="np-dft-config-section" aria-labelledby="np-dft-structure-title">
                      <header className="np-dft-section-heading">
                        <span className="np-dft-step">01</span>
                        <div>
                          <h3 id="np-dft-structure-title">结构输入</h3>
                          <p>编辑提交结构并核对 2D 预览。</p>
                        </div>
                      </header>
                      <MonomerDftStructureInput
                        value={smilesDraft}
                        disabled={configLocked}
                        error={structureIssue}
                        onChange={changeSmilesDraft}
                        onEditStructure={onEditStructure}
                      />
                    </section>

                    <section className="np-dft-config-section" aria-labelledby="np-dft-method-title">
                      <header className="np-dft-section-heading">
                        <span className="np-dft-step">02</span>
                        <div>
                          <h3 id="np-dft-method-title">计算方法</h3>
                          <p>选择计算用途，并设置分子电荷与自旋状态。</p>
                        </div>
                      </header>

                      <div className="np-dft-mode-switch" role="group" aria-label="计算类型">
                        {(["single_point", "optimization"] as const).map((type) => (
                          <button
                            key={type}
                            type="button"
                            aria-pressed={calculationType === type}
                            disabled={configLocked}
                            className={calculationType === type ? "is-active" : ""}
                            onClick={() => setCalculationType(type)}
                          >
                            {type === "single_point" ? <Activity /> : <Atom />}
                            <span>
                              <strong>{type === "single_point" ? "单点计算" : "几何优化"}</strong>
                              <small>{type === "single_point" ? "计算自动生成的初始三维结构，不进行几何优化" : "优化分子构型，记录完整变化轨迹"}</small>
                            </span>
                          </button>
                        ))}
                      </div>

                      <div className="np-dft-fields is-method">
                        <div className="np-dft-field is-wide">
                          <span id="np-dft-model-purpose-label">适用体系</span>
                          <MonomerDftSelect
                            id="np-dft-model-purpose"
                            ariaLabelledBy="np-dft-model-purpose-label"
                            value={modelId}
                            disabled={configLocked || !dft.capabilities}
                            options={modelOptions}
                            onChange={setModelId}
                          />
                        </div>
                        <label className="np-dft-field">
                          <span>净电荷</span>
                          <input
                            type="number"
                            step={1}
                            value={netChargeText}
                            disabled={configLocked}
                            placeholder="留空自动"
                            onChange={(event) => setNetChargeText(event.target.value)}
                          />
                        </label>
                        <label className="np-dft-field">
                          <span>自旋多重度</span>
                          <input
                            type="number"
                            min={1}
                            max={7}
                            step={1}
                            value={multiplicity}
                            disabled={configLocked}
                            onChange={(event) => setMultiplicity(Number(event.target.value))}
                          />
                        </label>
                        <div className="np-dft-field">
                          <span id="np-dft-psmiles-mode-label">连接位点处理</span>
                          <MonomerDftSelect
                            id="np-dft-psmiles-mode"
                            ariaLabelledBy="np-dft-psmiles-mode-label"
                            value={psmilesMode ?? ""}
                            disabled={configLocked || !smilesDraft.includes("*")}
                            options={[
                              {
                                value: "",
                                label: smilesDraft.includes("*") ? "请选择处理方式" : "无需处理",
                                description: smilesDraft.includes("*") ? "当前结构包含连接位点" : "当前结构为普通分子"
                              },
                              { value: "close", label: "连接两端", description: "将两个连接位点闭合为环状结构" },
                              { value: "cap", label: "补全两端", description: "补全连接位点，形成有限分子" }
                            ]}
                            onChange={(value) => setPsmilesMode(
                              value === "close" || value === "cap"
                                ? value
                                : null
                            )}
                          />
                        </div>
                      </div>

                      {selectedModel ? (
                        <div className="np-dft-model-summary">
                          <div><strong>{describeMonomerDftModelPurpose(selectedModel).label}</strong><span>{selectedModel.available ? "可用" : "暂不可用"}</span></div>
                          <p>{describeMonomerDftModelPurpose(selectedModel).description}</p>
                          <small>适用元素：<code>{selectedModel.supported_elements.join("、") || "提交时检查"}</code> · {selectedModel.supports_spin ? "支持多重态" : "仅支持单重态"}</small>
                        </div>
                      ) : null}
                    </section>

                    <section className="np-dft-config-section" aria-labelledby="np-dft-properties-title">
                      <header className="np-dft-section-heading">
                        <span className="np-dft-step">03</span>
                        <div>
                          <h3 id="np-dft-properties-title">性质与收敛</h3>
                          <p>{calculationType === "single_point" ? "选择输出性质。" : "设置收敛条件与附加计算。"}</p>
                        </div>
                      </header>

                      {calculationType === "single_point" ? (
                        <PropertyChoices
                          values={properties}
                          onChange={setProperties}
                          supported={selectedModel?.supported_properties ?? []}
                          locked={configLocked}
                        />
                      ) : (
                        <>
                          <div className="np-dft-fields is-optimization">
                            <label className="np-dft-field">
                              <span>收敛力阈值 / eV·Å⁻¹</span>
                              <input
                                type="number"
                                min={0.001}
                                max={1}
                                step={0.001}
                                value={fmax}
                                disabled={configLocked}
                                onChange={(event) => setFmax(Number(event.target.value))}
                              />
                            </label>
                            <label className="np-dft-field">
                              <span>最大步数（{minOptimizationSteps}–{maxOptimizationSteps}）</span>
                              <input
                                type="number"
                                min={minOptimizationSteps}
                                max={maxOptimizationSteps}
                                step={1}
                                value={maxSteps}
                                disabled={configLocked}
                                onChange={(event) => setMaxSteps(Number(event.target.value))}
                              />
                            </label>
                          </div>
                          <PropertyChoices
                            compact
                            values={postOptimizationProperties}
                            onChange={(values) => setPostOptimizationProperties(values.filter(
                              (value): value is MonomerDftPostOptimizationProperty => value === "hessian" || value === "frequencies"
                            ))}
                            supported={selectedModel?.supported_properties ?? []}
                            locked={configLocked}
                          />
                          <p className="np-dft-field-hint">最终能量、原子力和电荷默认返回；还可计算二阶力常数或振动频率。</p>
                        </>
                      )}

                      <button
                        type="button"
                        className="np-dft-advanced-toggle"
                        aria-expanded={isAdvancedOpen}
                        aria-controls="np-dft-conformer-settings"
                        onClick={() => setIsAdvancedOpen((value) => !value)}
                      >
                        <Settings2 />
                        <span><strong>初始构型</strong><small>调整随机种子与初步优化步数</small></span>
                        <ChevronDown className={isAdvancedOpen ? "is-open" : ""} />
                      </button>
                      {isAdvancedOpen ? (
                        <div id="np-dft-conformer-settings" className="np-dft-fields is-advanced">
                          <label className="np-dft-field">
                            <span>随机种子</span>
                            <input
                              type="number"
                              min={0}
                              max={2147483647}
                              step={1}
                              value={seed}
                              disabled={configLocked}
                              onChange={(event) => setSeed(Number(event.target.value))}
                            />
                          </label>
                          <label className="np-dft-field">
                            <span>初始构型最大优化步数</span>
                            <input
                              type="number"
                              min={1}
                              max={5000}
                              step={1}
                              value={maxIterations}
                              disabled={configLocked}
                              onChange={(event) => setMaxIterations(Number(event.target.value))}
                            />
                          </label>
                        </div>
                      ) : null}
                    </section>

                    <details className="np-dft-scope-notice">
                      <summary><Info />科学说明<ChevronDown /></summary>
                      <ul>
                        <li>不同计算方案适用的元素、电荷和自旋状态不同，提交时会自动检查。</li>
                        <li>闭环或补全两端仅生成用于计算的有限分子，不代表完整聚合物周期环境。</li>
                        <li>几何优化从一个确定的初始构型出发，不保证得到全局最低能结构。</li>
                        <li>不同计算方案得到的绝对能量不可直接比较，也不应混合计算能量差。</li>
                      </ul>
                    </details>

                    {activeJob ? (
                      <div className="np-dft-inline-message is-warning" role="status">
                        <Activity />
                        <span>任务运行中，配置已锁定；前往任务中心查看进度。</span>
                      </div>
                    ) : null}
                    {validationIssues.length > 0 ? (
                      <div className="np-dft-validation" role="status">
                        <TriangleAlert />
                        <ul>{validationIssues.map((issue, index) => <li key={`${issue.field}-${index}`}>{issue.message}</li>)}</ul>
                      </div>
                    ) : null}
                    {formError || dft.jobError ? (
                      <div className="np-dft-inline-message is-error" role="alert">
                        <TriangleAlert /><span>{userFacingMonomerDftMessage(formError ?? dft.jobError ?? "")}</span>
                      </div>
                    ) : null}
                    {dft.serviceError ? (
                      <div className="np-dft-inline-message is-error" role="alert">
                        <TriangleAlert /><span>{userFacingMonomerDftMessage(dft.serviceError)}</span>
                      </div>
                    ) : null}

                    <div className="np-dft-submit-bar">
                      <div>
                        <span>任务提交</span>
                        <strong>{activeJob ? "任务运行中" : serviceReady ? "准备就绪" : serviceLabel}</strong>
                        <small>{canSubmit ? "提交后将固定当前结构与参数" : validationIssues[0]?.message ?? "当前无法创建任务"}</small>
                      </div>
                      <button type="submit" disabled={!canSubmit}>
                        {isPreparingSubmission || dft.isSubmitting ? <Loader2 className="np-dft-spin" /> : <Play />}
                        {isPreparingSubmission || dft.isSubmitting ? "正在创建任务" : submissionDisabled ? "功能尚未开放" : "提交计算"}
                      </button>
                    </div>
                  </form>
                ) : null}

                {activeTab === "tasks" ? (
                  <TaskCenter
                    dft={dft}
                    serviceReady={serviceReady}
                    submissionDisabled={submissionDisabled}
                    requestCreating={isPreparingSubmission || dft.isSubmitting}
                    onRerun={() => void rerunSelectedJob()}
                    onSelectJob={(jobId) => {
                      showResultsAtTop();
                      dft.loadJob(jobId);
                    }}
                  />
                ) : null}

                {activeTab === "results" ? (
                  <div className="np-dft-results-workspace">
                    {dft.job ? (
                      <>
                        <SelectedJobCard
                          dft={dft}
                          serviceReady={serviceReady}
                          submissionDisabled={submissionDisabled}
                          requestCreating={isPreparingSubmission || dft.isSubmitting}
                          onRerun={() => void rerunSelectedJob()}
                          onShowTasks={() => setActiveTab("tasks")}
                        />
                        {resultUsesPreviousSnapshot ? (
                          <div className="np-dft-inline-message" role="status">
                            <Info /><span>当前结果属于上次提交的参数；页面中的配置已经改变。</span>
                          </div>
                        ) : null}
                        {pollStatusLabel ? (
                          <div className={`np-dft-poll-state is-${dft.pollState}`} role="status">
                            {dft.isJobLoading ? <Loader2 className="np-dft-spin" /> : <TriangleAlert />}
                            {pollStatusLabel}
                          </div>
                        ) : null}
                        <MonomerDftResults job={dft.job} />
                      </>
                    ) : (
                      <>
                        {dft.jobError ? (
                          <div className="np-dft-inline-message is-error" role="alert">
                            <TriangleAlert />
                            <span>{userFacingMonomerDftMessage(dft.jobError)}</span>
                          </div>
                        ) : null}
                        <div className="np-dft-empty-state">
                          {dft.isJobLoading ? <Loader2 className="np-dft-spin" /> : <Gauge />}
                          <div>
                            <strong>{dft.isJobLoading ? "正在读取任务" : "尚未选择计算结果"}</strong>
                            <span>{dft.isJobLoading
                              ? "正在同步任务状态与结果，请稍候…"
                              : "提交任务，或前往任务中心选择历史任务。"}</span>
                          </div>
                          {!dft.isJobLoading ? (
                            <div className="np-dft-empty-actions">
                              <button type="button" onClick={() => setActiveTab("config")}><FlaskConical />计算配置</button>
                              <button type="button" onClick={() => setActiveTab("tasks")}><History />任务中心</button>
                            </div>
                          ) : null}
                        </div>
                      </>
                    )}
                  </div>
                ) : null}
              </section>
            </main>
          </div>
        </div>
      </div>
    </div>
  );
}
