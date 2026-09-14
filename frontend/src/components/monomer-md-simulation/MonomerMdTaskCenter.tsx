import {
  Ban,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Clock3,
  Filter,
  LoaderCircle,
  RefreshCw,
  Search,
  Trash2
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent
} from "react";
import { createPortal } from "react-dom";
import { useModuleTransitioning } from "../../hooks/ModuleTransitionContext";
import type {
  MonomerMdJobListQuery,
  MonomerMdJobPageResponse,
  MonomerMdJobResponse,
  MonomerMdJobStatus,
  MonomerMdProtocol
} from "../../types";
import { FORMAL_PROTOCOLS } from "./config";
import {
  clampMonomerMdProgress,
  formatDateTime,
  JOB_STATUS_LABELS,
  PROTOCOL_LABELS,
  translateMonomerMdMessage
} from "./presentation";

const TERMINAL = new Set<MonomerMdJobStatus>(["completed", "failed", "cancelled"]);
const HISTORY_PAGE_SIZE = 10;
type TaskFilterOption = { value: string; label: string };
const PROTOCOL_FILTERS: TaskFilterOption[] = [
  { value: "", label: "全部协议" },
  ...FORMAL_PROTOCOLS.map((protocol) => ({ value: protocol, label: protocol }))
];
const STATUS_FILTERS: Array<{ value: MonomerMdJobStatus | ""; label: string }> = [
  { value: "", label: "全部状态" },
  { value: "submitted", label: "已排队" },
  { value: "running", label: "运行中" },
  { value: "cancel_requested", label: "取消中" },
  { value: "completed", label: "已完成" },
  { value: "failed", label: "失败" },
  { value: "cancelled", label: "已取消" }
];

type MonomerMdTaskCenterProps = {
  selectedJob: MonomerMdJobResponse | null;
  activeJobs: MonomerMdJobResponse[];
  isActiveJobsLoading: boolean;
  activeJobsError: string | null;
  history: MonomerMdJobPageResponse | null;
  historyQuery: MonomerMdJobListQuery;
  isHistoryLoading: boolean;
  historyError: string | null;
  cancellingJobIds: string[];
  deletingJobIds: string[];
  deleteJobErrors: Record<string, string>;
  onRefresh: () => void;
  onSelect: (job: MonomerMdJobResponse) => void;
  onCancel: (job: MonomerMdJobResponse) => void;
  onDelete: (job: MonomerMdJobResponse) => void;
  onChangeQuery: (patch: Partial<MonomerMdJobListQuery>) => void;
};

export function MonomerMdTaskCenter({
  selectedJob,
  activeJobs,
  isActiveJobsLoading,
  activeJobsError,
  history,
  historyQuery,
  isHistoryLoading,
  historyError,
  cancellingJobIds,
  deletingJobIds,
  deleteJobErrors,
  onRefresh,
  onSelect,
  onCancel,
  onDelete,
  onChangeQuery
}: MonomerMdTaskCenterProps) {
  const page = history?.page ?? historyQuery.page ?? 1;
  const total = history?.total ?? 0;
  const pageCount = Math.max(1, Math.ceil(total / HISTORY_PAGE_SIZE));
  const historyItems = history?.items.slice(0, HISTORY_PAGE_SIZE) ?? [];

  return (
    <div className="np-mmd-task-center">
      <div className="np-mmd-task-center__notice">
        <Clock3 />
        <div>
          <strong>全局正式任务</strong>
          <span>此处只列正式协议历史；快速演示同样由 Worker 真实执行，但不进入历史列表，可通过当前结果或任务深链恢复。</span>
        </div>
        <button type="button" onClick={onRefresh} disabled={isActiveJobsLoading || isHistoryLoading}>
          <RefreshCw className={isActiveJobsLoading || isHistoryLoading ? "np-mmd-spin" : ""} />刷新
        </button>
      </div>

      {selectedJob ? (
        <section className="np-mmd-selected-task" aria-labelledby="monomer-md-selected-task-title">
          <div className="np-mmd-section-heading">
            <div><span className="np-mmd-eyebrow">SELECTED TASK</span><h3 id="monomer-md-selected-task-title">当前选中任务</h3></div>
            <span className={`np-mmd-status-pill is-${selectedJob.status}`}>{JOB_STATUS_LABELS[selectedJob.status]}</span>
          </div>
          <JobSummary job={selectedJob} />
        </section>
      ) : null}

      <section className="np-mmd-active-tasks" aria-labelledby="monomer-md-active-tasks-title">
        <div className="np-mmd-section-heading">
          <div><span className="np-mmd-eyebrow">ACTIVE QUEUE</span><h3 id="monomer-md-active-tasks-title">活跃任务与排队</h3></div>
          <span className="np-mmd-section-note">每 5 秒刷新状态与排队位置</span>
        </div>
        {activeJobsError ? <div className="np-mmd-inline-error" role="alert">{translateMonomerMdMessage(activeJobsError)}</div> : null}
        {isActiveJobsLoading && activeJobs.length === 0 ? (
          <div className="np-mmd-empty-state"><LoaderCircle className="np-mmd-spin" />正在读取活跃任务</div>
        ) : activeJobs.length === 0 ? (
          <div className="np-mmd-empty-state">当前没有正式运行或排队任务。</div>
        ) : (
          <div className="np-mmd-task-list">
            {activeJobs.map((job) => (
              <TaskRow
                key={job.job_id}
                job={job}
                selected={selectedJob?.job_id === job.job_id}
                cancelling={cancellingJobIds.includes(job.job_id)}
                deleting={false}
                error={null}
                onSelect={() => onSelect(job)}
                onCancel={() => onCancel(job)}
                onDelete={() => undefined}
              />
            ))}
          </div>
        )}
      </section>

      <section className="np-mmd-history" aria-labelledby="monomer-md-history-title">
        <div className="np-mmd-section-heading np-mmd-history__heading">
          <div><span className="np-mmd-eyebrow">FORMAL HISTORY</span><h3 id="monomer-md-history-title">正式任务历史</h3></div>
          <div className="np-mmd-history-filters">
            <span className="np-mmd-history-filters__mark"><Filter aria-hidden="true" /></span>
            <TaskFilterSelect
              ariaLabel="按协议筛选"
              caption="PROTOCOL"
              value={historyQuery.protocol ?? ""}
              options={PROTOCOL_FILTERS}
              onChange={(value) => onChangeQuery({ page: 1, protocol: value as MonomerMdProtocol | "" })}
            />
            <TaskFilterSelect
              ariaLabel="按状态筛选"
              caption="STATUS"
              value={historyQuery.status ?? ""}
              options={STATUS_FILTERS}
              onChange={(value) => onChangeQuery({ page: 1, status: value as MonomerMdJobStatus | "" })}
            />
          </div>
        </div>
        {historyError ? <div className="np-mmd-inline-error" role="alert">{translateMonomerMdMessage(historyError)}</div> : null}
        {isHistoryLoading && !history ? (
          <div className="np-mmd-empty-state"><LoaderCircle className="np-mmd-spin" />正在读取正式任务历史</div>
        ) : !historyItems.length ? (
          <div className="np-mmd-empty-state">当前筛选条件下没有正式任务记录。</div>
        ) : (
          <div className="np-mmd-task-list">
            {historyItems.map((job) => (
              <TaskRow
                key={job.job_id}
                job={job}
                selected={selectedJob?.job_id === job.job_id}
                cancelling={cancellingJobIds.includes(job.job_id)}
                deleting={deletingJobIds.includes(job.job_id)}
                error={deleteJobErrors[job.job_id] ?? null}
                onSelect={() => onSelect(job)}
                onCancel={() => onCancel(job)}
                onDelete={() => onDelete(job)}
              />
            ))}
          </div>
        )}
        <div className="np-mmd-pagination">
          <button type="button" aria-label="上一页" disabled={page <= 1 || isHistoryLoading} onClick={() => onChangeQuery({ page: page - 1 })}><ChevronLeft />上一页</button>
          <span>第 {page} / {pageCount} 页 · 共 {total} 项 · 每页 {HISTORY_PAGE_SIZE} 条</span>
          <button type="button" aria-label="下一页" disabled={page >= pageCount || isHistoryLoading} onClick={() => onChangeQuery({ page: page + 1 })}>下一页<ChevronRight /></button>
        </div>
      </section>
    </div>
  );
}

function TaskFilterSelect({
  ariaLabel,
  caption,
  value,
  options,
  onChange
}: {
  ariaLabel: string;
  caption: string;
  value: string;
  options: TaskFilterOption[];
  onChange: (value: string) => void;
}) {
  const menuId = useId();
  const transitioning = useModuleTransitioning();
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const [open, setOpen] = useState(false);
  const [menuStyle, setMenuStyle] = useState<CSSProperties | null>(null);
  const selected = options.find((option) => option.value === value) ?? options[0];
  useEffect(() => { if (transitioning) setOpen(false); }, [transitioning]);

  const updateMenuPosition = useCallback(() => {
    const trigger = triggerRef.current;
    if (!trigger) return;
    const rect = trigger.getBoundingClientRect();
    const estimatedHeight = menuRef.current?.offsetHeight || 58 + options.length * 42;
    const width = Math.max(rect.width, 160);
    const left = Math.max(12, Math.min(rect.left, window.innerWidth - width - 12));
    const openAbove = rect.bottom + estimatedHeight + 8 > window.innerHeight && rect.top > estimatedHeight;
    setMenuStyle({
      left,
      top: openAbove ? Math.max(12, rect.top - estimatedHeight - 8) : rect.bottom + 8,
      width
    });
  }, [options.length]);

  useEffect(() => {
    if (!open || transitioning) return;
    updateMenuPosition();
    const focusFrame = window.requestAnimationFrame(() => {
      const selectedOption = menuRef.current?.querySelector<HTMLButtonElement>('[role="option"][aria-selected="true"]');
      (selectedOption ?? menuRef.current?.querySelector<HTMLButtonElement>('[role="option"]'))?.focus();
    });
    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target as Node;
      if (!triggerRef.current?.contains(target) && !menuRef.current?.contains(target)) setOpen(false);
    };
    const handleEscape = (event: globalThis.KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setOpen(false);
      triggerRef.current?.focus();
    };
    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleEscape);
    window.addEventListener("resize", updateMenuPosition);
    document.addEventListener("scroll", updateMenuPosition, true);
    return () => {
      window.cancelAnimationFrame(focusFrame);
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleEscape);
      window.removeEventListener("resize", updateMenuPosition);
      document.removeEventListener("scroll", updateMenuPosition, true);
    };
  }, [open, transitioning, updateMenuPosition]);

  function handleTriggerKeyDown(event: ReactKeyboardEvent<HTMLButtonElement>) {
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    event.preventDefault();
    if (!open) {
      updateMenuPosition();
      setOpen(true);
    }
  }

  function handleMenuKeyDown(event: ReactKeyboardEvent<HTMLDivElement>) {
    const optionButtons = [...(menuRef.current?.querySelectorAll<HTMLButtonElement>('[role="option"]') ?? [])];
    const currentIndex = optionButtons.indexOf(event.target as HTMLButtonElement);
    let nextIndex: number | null = null;
    if (event.key === "ArrowDown") nextIndex = (currentIndex + 1 + optionButtons.length) % optionButtons.length;
    if (event.key === "ArrowUp") nextIndex = (currentIndex - 1 + optionButtons.length) % optionButtons.length;
    if (event.key === "Home") nextIndex = 0;
    if (event.key === "End") nextIndex = optionButtons.length - 1;
    if (event.key === "Tab") setOpen(false);
    if (nextIndex == null) return;
    event.preventDefault();
    optionButtons[nextIndex]?.focus();
  }

  const menu = open && !transitioning && menuStyle ? (
    <div
      ref={menuRef}
      id={menuId}
      className="np-mmd-filter-menu"
      data-module-owned-portal="true"
      role="listbox"
      aria-label={ariaLabel}
      style={menuStyle}
      onKeyDown={handleMenuKeyDown}
    >
      <div className="np-mmd-filter-menu__header" aria-hidden="true">
        <span>{caption}</span>
        <small>{ariaLabel}</small>
      </div>
      <div className="np-mmd-filter-menu__options">
        {options.map((option) => {
          const isSelected = option.value === selected.value;
          return (
            <button
              key={option.value || "all"}
              type="button"
              role="option"
              aria-selected={isSelected}
              className={isSelected ? "is-selected" : ""}
              onClick={() => {
                onChange(option.value);
                setOpen(false);
                triggerRef.current?.focus();
              }}
            >
              <i aria-hidden="true">{isSelected ? <Check /> : null}</i>
              <span>{option.label}</span>
            </button>
          );
        })}
      </div>
    </div>
  ) : null;

  return (
    <div className="np-mmd-filter-control">
      <button
        ref={triggerRef}
        type="button"
        role="combobox"
        aria-label={`${ariaLabel}：${selected.label}`}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={menuId}
        className={open ? "is-open" : ""}
        onClick={() => {
          updateMenuPosition();
          setOpen((current) => !current);
        }}
        onKeyDown={handleTriggerKeyDown}
      >
        <span><small>{caption}</small><strong>{selected.label}</strong></span>
        <ChevronDown aria-hidden="true" />
      </button>
      {typeof document !== "undefined" ? createPortal(menu, document.body) : null}
    </div>
  );
}

function JobSummary({ job }: { job: MonomerMdJobResponse }) {
  const progress = clampMonomerMdProgress(job.progress_percent ?? job.progress);
  return (
    <div className="np-mmd-selected-task__grid">
      <div><span>任务 ID</span><code title={job.job_id}>{job.job_id}</code></div>
      <div><span>模式 / 协议</span><strong>{job.run_mode === "demo" ? "真实快速演示" : "正式"} · {PROTOCOL_LABELS[job.protocol ?? "DensityDemo"]}</strong></div>
      <div><span>真实阶段</span><strong>{job.progress_stage || job.progress_message || "--"}</strong></div>
      <div><span>进度</span><strong className="np-mmd-selected-task__progress">{Math.round(progress)}%{job.queue_position != null ? ` · 队列第 ${job.queue_position} 位` : ""}</strong></div>
    </div>
  );
}

function TaskRow({
  job,
  selected,
  cancelling,
  deleting,
  error,
  onSelect,
  onCancel,
  onDelete
}: {
  job: MonomerMdJobResponse;
  selected: boolean;
  cancelling: boolean;
  deleting: boolean;
  error: string | null;
  onSelect: () => void;
  onCancel: () => void;
  onDelete: () => void;
}) {
  const terminal = TERMINAL.has(job.status);
  const progress = clampMonomerMdProgress(job.progress_percent ?? job.progress);
  return (
    <article className={`np-mmd-task-row${selected ? " is-selected" : ""}`}>
      <button type="button" className="np-mmd-task-row__select" onClick={onSelect} aria-label={`查看任务 ${job.job_id} 结果`}>
        <Search />
        <span className="np-mmd-task-row__identity">
          <code title={job.job_id}>{job.job_id}</code>
          <small>{PROTOCOL_LABELS[job.protocol ?? "Density"]} · <time dateTime={job.created_at}>{formatDateTime(job.created_at)}</time></small>
        </span>
        <span className="np-mmd-task-row__progress">
          <span><i style={{ width: `${progress}%` }} /></span>
          <small>{Math.round(progress)}%{job.queue_position != null ? ` · 队列第 ${job.queue_position} 位` : ""}</small>
        </span>
        <span className={`np-mmd-status-pill is-${job.status}`}>{JOB_STATUS_LABELS[job.status]}</span>
      </button>
      <div className="np-mmd-task-row__actions">
        {!terminal && job.status !== "cancel_requested" ? (
          <button type="button" onClick={onCancel} disabled={cancelling} aria-label={job.status === "submitted" ? "取消排队" : "取消任务"}>
            {cancelling ? <LoaderCircle className="np-mmd-spin" /> : <Ban />}{job.status === "submitted" ? "取消排队" : "取消任务"}
          </button>
        ) : null}
        {terminal ? (
          <button type="button" className="is-danger" onClick={onDelete} disabled={deleting} aria-label="删除记录">
            {deleting ? <LoaderCircle className="np-mmd-spin" /> : <Trash2 />}删除记录
          </button>
        ) : null}
      </div>
      {error ? <p className="np-mmd-task-row__error" role="alert">{translateMonomerMdMessage(error)}</p> : null}
    </article>
  );
}
