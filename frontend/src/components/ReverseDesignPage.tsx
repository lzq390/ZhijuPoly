import {
  Box,
  Check,
  Copy,
  Eraser,
  ImagePlus,
  LoaderCircle,
  MessageSquareText,
  RefreshCcw,
  Search,
  SlidersHorizontal,
  Sparkles,
  X
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type PointerEvent as ReactPointerEvent
} from "react";
import { useReverseDesign } from "../hooks/useReverseDesign";
import { useTgStructureCanvas } from "../hooks/useTgStructureCanvas";
import { standardizeSmiles } from "../services/api";
import type { TgAssistantSession } from "../hooks/useTgAssistant";
import type {
  KnowledgeNavigationRequest,
  ReverseDesignTgRequest,
  StructureWorkspaceContext,
  TgAssistantOperation,
  TgAssistantPageContext
} from "../types";
import { ReverseDesignResults } from "./ReverseDesignResults";
import { StructurePreview3D } from "./StructurePreview3D";
import { TgAssistantPanel } from "./TgAssistantPanel";
import "../styles/polymer-desktop.css";
import "../styles/reverse-design.css";

type ReverseDesignPageProps = {
  structure: StructureWorkspaceContext;
  onOpenKnowledge: (request: KnowledgeNavigationRequest) => void;
  assistant: TgAssistantSession;
};

type OpenPanel = "parameters" | "assistant" | null;
type SmilesSyncState = "synced" | "pending" | "syncing" | "error";

type TgAssistantSuggestionContext = {
  isLoading: boolean;
  searchFailed: boolean;
  hasResultData: boolean;
  resultCount: number;
  parametersDirty: boolean;
  editorReady: boolean;
  smilesState: SmilesSyncState;
  hasSmiles: boolean;
  validationMessage: string | null;
  targetTg: number | null;
};

const DRAWER_MIN_WIDTH = 320;
const DRAWER_MAX_WIDTH = 560;

export function getTgAssistantSuggestions({
  isLoading,
  searchFailed,
  hasResultData,
  resultCount,
  parametersDirty,
  editorReady,
  smilesState,
  hasSmiles,
  validationMessage,
  targetTg
}: TgAssistantSuggestionContext) {
  if (isLoading) {
    return [
      "根据当前扫描进度判断搜索是否正常",
      "当前命中率反映了哪些筛选限制？",
      "搜索完成后应优先比较哪些候选？"
    ];
  }
  if (searchFailed) {
    return [
      "根据当前错误定位搜索失败原因",
      "当前结构或参数中哪一项最可能导致失败？",
      "给出保留当前设置的安全重试方案"
    ];
  }
  if (!editorReady) {
    return [
      "围绕当前目标 Tg 规划结构设计方向",
      "为首轮搜索建议参数范围",
      "编辑器就绪后应优先检查哪些内容？"
    ];
  }
  if (smilesState === "error") {
    return [
      "检查当前 SMILES 为什么无效",
      "在保留设计意图的前提下修正这个 SMILES",
      "检查括号、化学键和环闭合是否完整"
    ];
  }
  if (smilesState === "pending" || smilesState === "syncing") {
    return [
      "同步完成后分析结构中影响 Tg 的关键片段",
      "同步完成后评估当前搜索参数是否合适",
      "同步完成后检查结构与参数是否可以搜索"
    ];
  }
  if (validationMessage) {
    return [
      `解释并修正当前参数错误：${validationMessage}`,
      "根据当前目标推荐一组有效搜索参数",
      "说明三个搜索参数的有效范围和取值权衡"
    ];
  }
  if (hasResultData && parametersDirty) {
    return [
      "比较当前参数与上次搜索参数的差异",
      "判断这些参数改动会怎样影响候选结果",
      "检查当前设置并生成重新搜索确认"
    ];
  }
  if (hasResultData && resultCount > 0) {
    return [
      "比较当前页候选并给出优先验证顺序",
      "解释 Tg 差与结构相似度之间的权衡",
      "分析排名靠前候选的关键结构差异"
    ];
  }
  if (hasResultData) {
    return [
      "分析本次没有候选的最可能原因",
      "建议下一轮相似度阈值和候选数量",
      "判断下一步应先调整参数还是修改结构"
    ];
  }
  if (!hasSmiles) {
    const target = typeof targetTg === "number" && Number.isFinite(targetTg)
      ? `${targetTg} °C`
      : "当前目标 Tg";
    return [
      `为 ${target} 推荐一个可编辑的起始 SMILES`,
      `哪些结构特征最可能帮助接近 ${target}？`,
      "为首轮搜索建议相似度阈值和候选数量"
    ];
  }
  return [
    "分析当前结构中影响 Tg 的关键片段",
    "根据当前结构评估搜索参数是否合适",
    "检查结构与参数后生成运行搜索确认"
  ];
}

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}

function validateRequest(request: ReverseDesignTgRequest) {
  if (
    request.target_tg === null ||
    !Number.isFinite(request.target_tg)
  ) {
    return "目标 Tg 必须为有效数值。";
  }
  if (
    !Number.isFinite(request.similarity_threshold) ||
    request.similarity_threshold < 0 ||
    request.similarity_threshold > 1
  ) {
    return "相似度阈值必须在 0–1 之间。";
  }
  if (
    !Number.isInteger(request.candidate_size) ||
    request.candidate_size < 1 ||
    request.candidate_size > 200
  ) {
    return "候选数量必须为 1–200 的整数。";
  }
  return null;
}

function requestsDiffer(
  draft: ReverseDesignTgRequest,
  submitted: ReverseDesignTgRequest | null
) {
  if (!submitted) {
    return false;
  }
  return (
    draft.target_tg !== submitted.target_tg ||
    draft.similarity_threshold !== submitted.similarity_threshold ||
    draft.candidate_size !== submitted.candidate_size
  );
}

export function ReverseDesignPage({
  structure,
  onOpenKnowledge,
  assistant
}: ReverseDesignPageProps) {
  const reverseDesign = useReverseDesign();
  const [openPanel, setOpenPanel] = useState<OpenPanel>(null);
  const [isDrawerOpen, setIsDrawerOpen] = useState(false);
  const [hasRun, setHasRun] = useState(false);
  const [drawerWidth, setDrawerWidth] = useState(380);
  const [resultPage, setResultPage] = useState(1);
  const [smilesDraft, setSmilesDraft] = useState(structure.smiles);
  const [smilesSyncState, setSmilesSyncState] = useState<SmilesSyncState>("synced");
  const [smilesSyncError, setSmilesSyncError] = useState<string | null>(null);
  const parameterPanelRef = useRef<HTMLElement | null>(null);
  const assistantPanelRef = useRef<HTMLElement | null>(null);
  const parameterButtonRef = useRef<HTMLButtonElement | null>(null);
  const assistantButtonRef = useRef<HTMLButtonElement | null>(null);
  const resizeStateRef = useRef<{ startX: number; startWidth: number } | null>(null);
  const revisionRef = useRef(globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-tg`);
  const lastCanvasRevisionRef = useRef<string | null>(null);
  const smilesDraftRef = useRef(structure.smiles);
  const lastSharedSmilesRef = useRef(structure.smiles);
  const smilesDraftRevisionRef = useRef(0);
  const smilesSyncTimerRef = useRef<number | null>(null);
  const pendingSmilesSyncRef = useRef<{ revision: number; value: string } | null>(null);
  const smilesSyncRunningRef = useRef(false);
  const activeSmilesSyncRevisionRef = useRef<number | null>(null);

  const handleStructureChanged = useCallback(() => {
    revisionRef.current = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-structure`;
    reverseDesign.reset();
    setHasRun(false);
    setIsDrawerOpen(false);
    setResultPage(1);
    assistant.addDivider("结构已变化");
  }, [assistant, reverseDesign]);

  const canvas = useTgStructureCanvas({
    structure,
    onStructureChanged: handleStructureChanged
  });

  function markAssistantRevision(label: string) {
    revisionRef.current = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${label}`;
  }

  function cancelPendingSmilesSync() {
    if (smilesSyncTimerRef.current !== null) {
      window.clearTimeout(smilesSyncTimerRef.current);
      smilesSyncTimerRef.current = null;
    }
    pendingSmilesSyncRef.current = null;
    smilesDraftRevisionRef.current += 1;
  }

  async function drainSmilesSyncQueue() {
    if (smilesSyncRunningRef.current) return;
    smilesSyncRunningRef.current = true;
    try {
      while (pendingSmilesSyncRef.current) {
        const task = pendingSmilesSyncRef.current;
        pendingSmilesSyncRef.current = null;
        if (task.revision !== smilesDraftRevisionRef.current) continue;
        activeSmilesSyncRevisionRef.current = task.revision;
        setSmilesSyncState("syncing");
        setSmilesSyncError(null);
        const isCurrent = () => task.revision === smilesDraftRevisionRef.current;
        let applied = false;
        let synchronizedSmiles = "";
        if (!task.value.trim()) {
          applied = await canvas.clearCanvas({ isCurrent });
        } else {
          try {
            const result = await standardizeSmiles({ smiles: task.value });
            if (!isCurrent()) continue;
            synchronizedSmiles = result.standardized_smiles.trim();
            applied = await canvas.loadStructure(synchronizedSmiles, { isCurrent });
          } catch (error) {
            if (!isCurrent()) continue;
            console.error("Failed to standardize editable Tg SMILES", error);
            setSmilesSyncState("error");
            setSmilesSyncError("SMILES 无效或尚未完整，原画板未修改。");
            continue;
          }
        }
        if (!isCurrent()) continue;
        if (!applied) {
          setSmilesSyncState("error");
          setSmilesSyncError("结构未能同步到画板，请检查 SMILES 或编辑器状态。");
          continue;
        }
        const peek = await canvas.peekCanvasState();
        if (!isCurrent()) continue;
        const nextValue = task.value.trim() ? (peek.smiles || synchronizedSmiles) : "";
        lastSharedSmilesRef.current = nextValue;
        smilesDraftRef.current = nextValue;
        setSmilesDraft(nextValue);
        setSmilesSyncState("synced");
        setSmilesSyncError(null);
      }
    } finally {
      activeSmilesSyncRevisionRef.current = null;
      smilesSyncRunningRef.current = false;
      if (pendingSmilesSyncRef.current) queueMicrotask(() => void drainSmilesSyncQueue());
    }
  }

  function updateSmilesDraft(nextValue: string) {
    if (nextValue.length > 8000) return;
    if (smilesSyncTimerRef.current !== null) {
      window.clearTimeout(smilesSyncTimerRef.current);
      smilesSyncTimerRef.current = null;
    }
    const nextRevision = smilesDraftRevisionRef.current + 1;
    smilesDraftRevisionRef.current = nextRevision;
    smilesDraftRef.current = nextValue;
    setSmilesDraft(nextValue);
    setSmilesSyncError(null);
    markAssistantRevision("smiles-draft");
    if (nextValue.trim() === lastSharedSmilesRef.current.trim()) {
      pendingSmilesSyncRef.current = null;
      setSmilesSyncState("synced");
      return;
    }
    setSmilesSyncState("pending");
    smilesSyncTimerRef.current = window.setTimeout(() => {
      smilesSyncTimerRef.current = null;
      pendingSmilesSyncRef.current = { revision: nextRevision, value: nextValue };
      void drainSmilesSyncQueue();
    }, 500);
  }

  async function adoptCanvasSmiles() {
    const peek = await canvas.peekCanvasState();
    const nextValue = peek.smiles;
    lastSharedSmilesRef.current = nextValue;
    smilesDraftRef.current = nextValue;
    setSmilesDraft(nextValue);
    setSmilesSyncState("synced");
    setSmilesSyncError(null);
  }

  async function clearCanvasFromToolbar() {
    cancelPendingSmilesSync();
    if (await canvas.clearCanvas()) await adoptCanvasSmiles();
  }

  async function importImageFromToolbar(file: File) {
    cancelPendingSmilesSync();
    if (await canvas.importImageFile(file)) await adoptCanvasSmiles();
  }

  async function syncCanvasFromToolbar() {
    cancelPendingSmilesSync();
    await canvas.syncSmilesFromCanvas();
    await adoptCanvasSmiles();
  }

  useEffect(() => {
    const sharedSmiles = structure.smiles;
    if (sharedSmiles === lastSharedSmilesRef.current) return;
    lastSharedSmilesRef.current = sharedSmiles;
    const activeRevision = activeSmilesSyncRevisionRef.current;
    if (activeRevision !== null && activeRevision !== smilesDraftRevisionRef.current) return;
    if (activeRevision === null) cancelPendingSmilesSync();
    smilesDraftRef.current = sharedSmiles;
    setSmilesDraft(sharedSmiles);
    setSmilesSyncState("synced");
    setSmilesSyncError(null);
  }, [structure.smiles]);

  useEffect(() => () => {
    if (smilesSyncTimerRef.current !== null) {
      window.clearTimeout(smilesSyncTimerRef.current);
    }
    pendingSmilesSyncRef.current = null;
    smilesDraftRevisionRef.current += 1;
  }, []);

  const validationMessage = validateRequest(reverseDesign.request);
  const parametersDirty = requestsDiffer(
    reverseDesign.request,
    reverseDesign.submittedRequest
  );
  const operationBusy = canvas.isBusy || reverseDesign.isLoading;
  const smilesSyncBlocked = smilesSyncState !== "synced";
  const searchFailed = Boolean(reverseDesign.error) ||
    reverseDesign.job?.status === "failed" ||
    reverseDesign.job?.status === "cancelled";
  const resultCount = reverseDesign.data?.total ?? reverseDesign.job?.matched_count ?? 0;
  const resultStatus = reverseDesign.isLoading
    ? "候选搜索中"
    : searchFailed
      ? "搜索需要检查"
      : reverseDesign.data
        ? `${resultCount} 个候选`
        : "尚未搜索";

  const revisionKey = JSON.stringify([
    structure.smiles,
    reverseDesign.request.target_tg,
    reverseDesign.request.similarity_threshold,
    reverseDesign.request.candidate_size,
    reverseDesign.submittedRequest?.smiles,
    reverseDesign.submittedRequest?.target_tg,
    reverseDesign.job?.status,
    reverseDesign.data?.query_time_ms,
    reverseDesign.data?.total,
    resultPage,
    canvas.isEditorReady,
    canvas.isFlipped,
    canvas.isBusy
  ]);

  useEffect(() => {
    revisionRef.current = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-revision`;
  }, [revisionKey]);

  useEffect(() => {
    setResultPage(1);
  }, [reverseDesign.data]);

  function updateRequest(partial: Partial<ReverseDesignTgRequest>) {
    reverseDesign.setRequest({
      ...reverseDesign.request,
      ...partial
    });
  }

  function restorePanelFocus(panel: Exclude<OpenPanel, null>) {
    const target =
      panel === "parameters" ? parameterButtonRef.current : assistantButtonRef.current;
    window.requestAnimationFrame(() => target?.focus());
  }

  function closePanel(restoreFocus = true) {
    if (openPanel && restoreFocus) {
      restorePanelFocus(openPanel);
    }
    setOpenPanel(null);
  }

  function togglePanel(panel: Exclude<OpenPanel, null>) {
    setOpenPanel((current) => (current === panel ? null : panel));
  }

  function openDrawer() {
    setOpenPanel((current) => (current === "parameters" ? null : current));
    setIsDrawerOpen(true);
  }

  useEffect(() => {
    if (!openPanel) {
      return;
    }

    function handlePointerDown(event: PointerEvent) {
      const target = event.target;
      if (!(target instanceof Node)) {
        return;
      }
      if (
        openPanel === "assistant" &&
        target instanceof Element &&
        target.closest(".tg-results-drawer")
      ) {
        return;
      }
      const panel =
        openPanel === "parameters" ? parameterPanelRef.current : assistantPanelRef.current;
      const trigger =
        openPanel === "parameters" ? parameterButtonRef.current : assistantButtonRef.current;
      if (!panel?.contains(target) && !trigger?.contains(target)) {
        closePanel(false);
      }
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        closePanel(true);
      }
    }

    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [openPanel]);

  useEffect(() => {
    function handlePointerMove(event: PointerEvent) {
      const resizeState = resizeStateRef.current;
      if (!resizeState) {
        return;
      }
      const nextWidth = resizeState.startWidth + resizeState.startX - event.clientX;
      setDrawerWidth(clamp(nextWidth, DRAWER_MIN_WIDTH, DRAWER_MAX_WIDTH));
    }

    function stopResize() {
      resizeStateRef.current = null;
      document.body.classList.remove("tg-is-resizing");
    }

    document.addEventListener("pointermove", handlePointerMove);
    document.addEventListener("pointerup", stopResize);
    document.addEventListener("pointercancel", stopResize);
    return () => {
      document.removeEventListener("pointermove", handlePointerMove);
      document.removeEventListener("pointerup", stopResize);
      document.removeEventListener("pointercancel", stopResize);
      document.body.classList.remove("tg-is-resizing");
    };
  }, []);

  function startDrawerResize(event: ReactPointerEvent<HTMLDivElement>) {
    event.preventDefault();
    resizeStateRef.current = {
      startX: event.clientX,
      startWidth: drawerWidth
    };
    document.body.classList.add("tg-is-resizing");
  }

  async function performSearch(draft: ReverseDesignTgRequest) {
    if (smilesSyncBlocked) {
      canvas.setFeedback("请等待 SMILES 同步完成，或先修正当前输入。");
      return false;
    }
    if (validateRequest(draft) || canvas.isBusy || reverseDesign.isLoading) return false;
    const smiles = await canvas.resolveSmilesForSearch();
    if (!smiles) {
      return false;
    }

    const request: ReverseDesignTgRequest = {
      ...draft,
      smiles
    };
    setResultPage(1);
    setHasRun(true);
    openDrawer();
    assistant.addDivider("已开始新的 Tg 候选搜索");
    void reverseDesign.submit(request);
    return true;
  }

  async function handleSearch() {
    if (!(await performSearch(reverseDesign.request))) {
      setOpenPanel("parameters");
    }
  }

  async function captureAssistantContext(): Promise<TgAssistantPageContext> {
    const peek = await canvas.peekCanvasState();
    if (lastCanvasRevisionRef.current !== null && peek.revisionKey !== lastCanvasRevisionRef.current) {
      revisionRef.current = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-canvas`;
    }
    lastCanvasRevisionRef.current = peek.revisionKey;
    const request = reverseDesign.request;
    const validationError = smilesSyncError
      ? { field: "structure" as const, message: smilesSyncError }
      : request.target_tg === null || !Number.isFinite(request.target_tg)
      ? { field: "target_tg" as const, message: "目标 Tg 必须为有效数值。" }
      : !Number.isFinite(request.similarity_threshold) || request.similarity_threshold < 0 || request.similarity_threshold > 1
        ? { field: "similarity_threshold" as const, message: "相似度阈值必须在 0–1 之间。" }
        : !Number.isInteger(request.candidate_size) || request.candidate_size < 1 || request.candidate_size > 200
          ? { field: "candidate_size" as const, message: "候选数量必须为 1–200 的整数。" }
          : null;
    const start = (resultPage - 1) * 5;
    const candidates = reverseDesign.data?.results.slice(start, start + 5) ?? [];
    const submitted = reverseDesign.submittedRequest;
    return {
      type: "tg_reverse_design",
      version: 1,
      captured_at: new Date().toISOString(),
      action_context_revision: revisionRef.current,
      structure: {
        smiles: (smilesSyncBlocked ? smilesDraftRef.current.trim() : peek.smiles) || null,
        canvas_dirty: peek.canvasDirty || smilesSyncBlocked,
        editor_ready: peek.editorReady,
        view_mode: peek.viewMode,
        busy: peek.busy || smilesSyncState === "syncing"
      },
      draft_parameters: {
        target_tg: Number.isFinite(request.target_tg) ? request.target_tg : null,
        similarity_threshold:
          Number.isFinite(request.similarity_threshold) &&
          request.similarity_threshold >= 0 &&
          request.similarity_threshold <= 1
            ? request.similarity_threshold
            : null,
        candidate_size:
          Number.isInteger(request.candidate_size) &&
          request.candidate_size >= 1 &&
          request.candidate_size <= 200
            ? request.candidate_size
            : null
      },
      submitted_request: submitted?.target_tg == null ? null : {
        smiles: submitted.smiles,
        target_tg: submitted.target_tg,
        similarity_threshold: submitted.similarity_threshold,
        candidate_size: submitted.candidate_size
      },
      parameters_dirty: parametersDirty,
      validation_error: validationError,
      job: reverseDesign.job ? {
        status: reverseDesign.job.status,
        scanned_rows: reverseDesign.job.scanned_rows,
        matched_count: reverseDesign.job.matched_count,
        current_tg_radius: reverseDesign.job.current_tg_radius,
        best_similarity_score: reverseDesign.job.best_similarity_score,
        message: reverseDesign.job.status === "pending"
          ? "Tg 搜索已排队。"
          : reverseDesign.job.status === "running"
            ? "正在按目标 Tg 绝对差向两侧扫描。"
            : reverseDesign.job.status === "found_enough"
              ? "已达到请求的候选数量。"
              : reverseDesign.job.status === "exhausted"
                ? "PI 数据库已扫描完成，但可能未达到请求数量。"
                : reverseDesign.job.status === "cancelled"
                  ? "Tg 搜索已取消。"
                  : null
      } : null,
      result_view: reverseDesign.data ? {
        total: reverseDesign.data.total,
        page: resultPage,
        page_size: 5,
        drawer_open: isDrawerOpen,
        visible_candidates: candidates.map((candidate) => ({
          rank: candidate.rank,
          polymer_smiles: candidate.canonical_polym || candidate.polymer_smiles || null,
          monomer_a_smiles: candidate.monomer_a_smiles || null,
          monomer_b_smiles: candidate.monomer_b_smiles || null,
          monomer_a_iupac: candidate.monomer_a_iupac,
          monomer_b_iupac: candidate.monomer_b_iupac,
          tg_value: candidate.tg_value,
          tg_difference: candidate.tg_difference,
          similarity_score: candidate.similarity_score
        }))
      } : null,
      error: reverseDesign.error ? "Tg 搜索失败，请检查结构与服务状态后重试。" : null
    };
  }

  async function applyAssistantOperations(
    operations: TgAssistantOperation[],
    basisRevision: string
  ): Promise<{ status: "applied" | "expired" | "failed"; detail?: string }> {
    const latestPeek = await canvas.peekCanvasState();
    if (lastCanvasRevisionRef.current !== null && latestPeek.revisionKey !== lastCanvasRevisionRef.current) {
      revisionRef.current = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-canvas-confirm`;
    }
    lastCanvasRevisionRef.current = latestPeek.revisionKey;
    if (basisRevision !== revisionRef.current) {
      return { status: "expired", detail: "页面状态已变化，请重新生成操作。" };
    }
    if (reverseDesign.isLoading) {
      return { status: "expired", detail: "搜索状态已变化，当前操作不能执行。" };
    }
    let nextRequest = { ...reverseDesign.request };
    let runSearch = false;
    let hasParameterChange = false;
    const operationTypes = operations.map((operation) => operation.type).join(",");
    if (!["set_parameters", "run_search", "set_parameters,run_search", "set_structure"].includes(operationTypes)) {
      return { status: "failed", detail: "操作组合无效，未修改页面。" };
    }
    if (operationTypes === "set_structure") {
      if (smilesSyncBlocked) {
        return { status: "expired", detail: "SMILES 输入状态已变化，请等待同步后重新生成操作。" };
      }
      const operation = operations[0];
      if (operation.type !== "set_structure" || !operation.smiles.trim() || operation.smiles.length > 8000) {
        return { status: "failed", detail: "建议结构无效，未修改画板。" };
      }
      if (!canvas.isEditorReady || canvas.isBusy) {
        return { status: "expired", detail: "结构编辑器当前不可用或正在处理其他操作。" };
      }
      const loaded = await canvas.loadStructure(operation.smiles);
      return loaded
        ? { status: "applied" }
        : { status: "failed", detail: "结构加载失败，原画板已恢复。" };
    }
    for (const operation of operations) {
      if (operation.type === "set_parameters") {
        const patch = operation.parameters;
        if (patch.target_tg !== undefined && (patch.target_tg === null || !Number.isFinite(patch.target_tg))) {
          return { status: "failed", detail: "目标 Tg 参数无效。" };
        }
        if (patch.similarity_threshold !== undefined && (!Number.isFinite(patch.similarity_threshold) || patch.similarity_threshold < 0 || patch.similarity_threshold > 1)) {
          return { status: "failed", detail: "相似度阈值参数无效。" };
        }
        if (patch.candidate_size !== undefined && (!Number.isInteger(patch.candidate_size) || patch.candidate_size < 1 || patch.candidate_size > 200)) {
          return { status: "failed", detail: "候选数量参数无效。" };
        }
        hasParameterChange = Object.entries(patch).some(
          ([key, value]) => reverseDesign.request[key as keyof ReverseDesignTgRequest] !== value
        );
        nextRequest = { ...nextRequest, ...patch };
      } else {
        runSearch = true;
      }
    }
    if (operations.some((operation) => operation.type === "set_parameters") && !hasParameterChange) {
      return { status: "failed", detail: "当前参数已经是建议值，未执行重复修改。" };
    }
    const error = validateRequest(nextRequest);
    if (error) return { status: "failed", detail: error };
    reverseDesign.setRequest(nextRequest);
    if (runSearch) {
      const submitted = await performSearch(nextRequest);
      return submitted
        ? { status: "applied" }
        : { status: "failed", detail: "结构同步或标准化失败，参数已保留但搜索未提交。" };
    }
    setOpenPanel("parameters");
    return { status: "applied" };
  }

  useEffect(() => assistant.registerAdapter({
    captureContext: captureAssistantContext,
    captureCanvasImage: canvas.captureCanvasImage,
    getRevision: () => revisionRef.current,
    getDraftParameters: () => ({
      target_tg: Number.isFinite(reverseDesign.request.target_tg) ? reverseDesign.request.target_tg : null,
      similarity_threshold:
        Number.isFinite(reverseDesign.request.similarity_threshold) &&
        reverseDesign.request.similarity_threshold >= 0 &&
        reverseDesign.request.similarity_threshold <= 1
        ? reverseDesign.request.similarity_threshold
        : null,
      candidate_size:
        Number.isInteger(reverseDesign.request.candidate_size) &&
        reverseDesign.request.candidate_size >= 1 &&
        reverseDesign.request.candidate_size <= 200
        ? reverseDesign.request.candidate_size
        : null
    }),
    getStructureSmiles: () => smilesSyncState === "synced" ? (smilesDraftRef.current.trim() || null) : null,
    navigate: (target) => {
      if (target === "parameters") setOpenPanel("parameters");
      else setIsDrawerOpen(true);
    },
    applyOperations: applyAssistantOperations
  }));

  const assistantSuggestions = getTgAssistantSuggestions({
    isLoading: reverseDesign.isLoading,
    searchFailed,
    hasResultData: Boolean(reverseDesign.data),
    resultCount: reverseDesign.data?.total ?? 0,
    parametersDirty,
    editorReady: canvas.isEditorReady,
    smilesState: smilesSyncState,
    hasSmiles: Boolean(smilesDraft.trim()),
    validationMessage,
    targetTg: Number.isFinite(reverseDesign.request.target_tg)
      ? reverseDesign.request.target_tg
      : null
  });

  const localDiagnostic = reverseDesign.isLoading
    ? `搜索正在进行：已检查 ${reverseDesign.job?.scanned_rows ?? 0} 条数据，找到 ${reverseDesign.job?.matched_count ?? 0} 个候选。`
    : searchFailed
      ? "当前搜索需要检查，可查看错误后再重试。"
      : reverseDesign.data && parametersDirty
        ? "当前结果属于上次提交；草稿参数已经变化。"
        : reverseDesign.data
          ? `当前有 ${reverseDesign.data.total} 个候选，可分析当前页 5 条。`
          : !canvas.isEditorReady
            ? "结构编辑器尚未就绪，请稍后重试。"
            : !smilesDraft.trim()
              ? "先绘制或导入结构，再设置 Tg、相似度阈值和候选数量。"
              : validationMessage
                ? validationMessage
                : "当前结构和参数已准备完成，可以开始搜索或向 AI 提问。";

  const rootStyle = {
    "--tg-drawer-width": `${drawerWidth}px`
  } as CSSProperties;

  return (
    <div
      className={`polymer-desktop-page polymer-desktop-page--embedded tg-reverse-page${isDrawerOpen ? " has-open-drawer" : ""}`}
      style={rootStyle}
    >
      <h1 className="tg-page-title">Tg 逆向设计</h1>

      <div className="tg-workbench-shell">
        <div className="tg-workbench-column">
          <header className="polymer-module-header tg-toolbar-row">
            <input
              ref={canvas.fileInputRef}
              className="tg-visually-hidden"
              type="file"
              accept="image/*"
              aria-label="导入结构图片"
              onChange={(event) => {
                const file = event.currentTarget.files?.[0];
                if (file) {
                  void importImageFromToolbar(file);
                }
              }}
            />
            <div className="header-actions tg-toolbar" aria-label="Tg 结构工具栏">
              <button
                type="button"
                className="btn btn--outline btn--sm tg-tool-button"
                id="btn-import-img"
                onClick={() => canvas.fileInputRef.current?.click()}
                disabled={operationBusy}
              >
                {canvas.isImportingImage ? <LoaderCircle className="animate-spin" /> : <ImagePlus />}
                导入图片
              </button>
              <button
                type="button"
                className="btn btn--outline btn--sm tg-tool-button"
                id="btn-clear-canvas"
                onClick={() => void clearCanvasFromToolbar()}
                disabled={operationBusy || !canvas.isEditorReady}
              >
                {canvas.isClearing ? <LoaderCircle className="animate-spin" /> : <Eraser />}
                清空画布
              </button>
              <button
                type="button"
                className="btn btn--outline btn--sm tg-tool-button"
                id="btn-sync-canvas"
                onClick={() => void syncCanvasFromToolbar()}
                disabled={operationBusy || !canvas.isEditorReady}
              >
                {canvas.isSyncing ? <LoaderCircle className="animate-spin" /> : <RefreshCcw />}
                生成SMILES
              </button>
              <button
                type="button"
                className={`btn btn--outline btn--sm tg-tool-button${canvas.isFlipped ? " active" : ""}`}
                id="btn-toggle-3d"
                onClick={() => void canvas.toggle3D()}
                disabled={operationBusy || smilesSyncBlocked || !canvas.isEditorReady}
              >
                {canvas.isFlipping ? <LoaderCircle className="animate-spin" /> : <Box />}
                {canvas.isFlipped ? "2D画布" : "3D构象"}
              </button>
              <span className="tg-toolbar-separator" aria-hidden="true" />
              <button
                ref={parameterButtonRef}
                type="button"
                className={`btn btn--outline btn--sm tg-icon-tool${openPanel === "parameters" ? " is-active" : ""}`}
                aria-label="搜索参数"
                title="搜索参数"
                aria-expanded={openPanel === "parameters"}
                aria-controls="tg-parameter-panel"
                onClick={() => togglePanel("parameters")}
              >
                <SlidersHorizontal />
              </button>
              <button
                ref={assistantButtonRef}
                type="button"
                className={`btn btn--outline btn--sm tg-icon-tool${openPanel === "assistant" ? " is-active" : ""}`}
                aria-label="AI 助手"
                title="AI 助手"
                aria-expanded={openPanel === "assistant"}
                aria-controls="tg-assistant-panel"
                onClick={() => togglePanel("assistant")}
              >
                <Sparkles />
              </button>
            </div>

            <section
              ref={parameterPanelRef}
              id="tg-parameter-panel"
              className={`tg-parameter-panel${openPanel === "parameters" ? " is-open" : ""}`}
              role="dialog"
              aria-modal="false"
              aria-labelledby="tg-parameter-title"
              aria-hidden={openPanel !== "parameters"}
              inert={openPanel !== "parameters"}
            >
              <header>
                <h2 id="tg-parameter-title">Tg 搜索参数</h2>
                <button type="button" aria-label="收起搜索参数" onClick={() => closePanel()}>
                  <X />
                </button>
              </header>
              <div className="tg-parameter-fields">
                <label>
                  <span>目标 Tg</span>
                  <span className="tg-input-shell">
                    <input
                      type="number"
                      step="0.1"
                      value={reverseDesign.request.target_tg ?? ""}
                      onChange={(event) =>
                        updateRequest({
                          target_tg:
                            event.currentTarget.value === ""
                              ? null
                              : Number(event.currentTarget.value)
                        })
                      }
                    />
                    <small>°C</small>
                  </span>
                </label>
                <label>
                  <span>相似度阈值</span>
                  <span className="tg-input-shell">
                    <input
                      type="number"
                      min="0"
                      max="1"
                      step="0.01"
                      value={
                        Number.isNaN(reverseDesign.request.similarity_threshold)
                          ? ""
                          : reverseDesign.request.similarity_threshold
                      }
                      onChange={(event) =>
                        updateRequest({
                          similarity_threshold:
                            event.currentTarget.value === ""
                              ? Number.NaN
                              : Number(event.currentTarget.value)
                        })
                      }
                    />
                  </span>
                </label>
                <label>
                  <span>候选数量</span>
                  <span className="tg-input-shell">
                    <input
                      type="number"
                      min="1"
                      max="200"
                      step="1"
                      value={
                        Number.isNaN(reverseDesign.request.candidate_size)
                          ? ""
                          : reverseDesign.request.candidate_size
                      }
                      onChange={(event) =>
                        updateRequest({
                          candidate_size:
                            event.currentTarget.value === ""
                              ? Number.NaN
                              : Number(event.currentTarget.value)
                        })
                      }
                    />
                    <small>个</small>
                  </span>
                </label>
              </div>
              <div className="tg-parameter-validation" role="status" aria-live="polite">
                {validationMessage ||
                  (parametersDirty ? "参数已修改，需要重新搜索。" : "参数已就绪。")}
              </div>
              <button
                type="button"
                className="tg-search-button"
                onClick={() => void handleSearch()}
                disabled={Boolean(validationMessage) || operationBusy || smilesSyncBlocked}
              >
                {reverseDesign.isLoading ? <LoaderCircle className="animate-spin" /> : <Search />}
                搜索
              </button>
            </section>

            <section
              ref={assistantPanelRef}
              id="tg-assistant-panel"
              className={`tg-assistant-panel${openPanel === "assistant" ? " is-open" : ""}`}
              role="dialog"
              aria-modal="false"
              aria-labelledby="tg-assistant-title"
              aria-hidden={openPanel !== "assistant"}
              inert={openPanel !== "assistant"}
            >
              <TgAssistantPanel
                assistant={assistant}
                onClose={() => closePanel()}
                contextLabels={[
                  smilesSyncState === "syncing"
                    ? "结构同步中"
                    : smilesSyncBlocked
                      ? "结构输入待修正"
                      : smilesDraft.trim()
                        ? "结构已准备"
                        : "尚未添加结构",
                  `Tg ${reverseDesign.request.target_tg ?? "—"} °C`,
                  resultStatus
                ]}
                localDiagnostic={localDiagnostic}
                contextualSuggestions={assistantSuggestions}
              />
            </section>
          </header>

          <section className="tg-structure-surface" aria-label="结构画布">
            <div className={`tg-structure-flip${canvas.isFlipped ? " is-flipped" : ""}`}>
              <div className="tg-structure-face tg-structure-face-front">
                <iframe
                  ref={structure.iframeRef}
                  title="Tg 逆向设计结构编辑器"
                  src="/ketcher/index.html"
                  onLoad={canvas.handleEditorLoad}
                />
              </div>
              <div
                className="tg-structure-face tg-structure-face-back"
                aria-hidden={!canvas.isFlipped}
              >
                <StructurePreview3D
                  smiles={smilesSyncState === "synced" ? structure.smiles : ""}
                  variant="bare"
                  visualStyle="polished-atoms"
                  className="h-full"
                  previewClassName="h-full min-h-0"
                />
              </div>
            </div>
          </section>

          <section className="tg-smiles-capsule" aria-labelledby="tg-smiles-label">
            <label id="tg-smiles-label">SMILES</label>
            <textarea
              rows={2}
              value={smilesDraft}
              maxLength={8000}
              spellCheck={false}
              aria-invalid={smilesSyncState === "error"}
              onChange={(event) => updateSmilesDraft(event.currentTarget.value)}
              placeholder="输入 SMILES 后将自动校验并同步到上方画板。"
              aria-label="SMILES 输入，自动同步到画板"
            />
            <button
              type="button"
              onClick={() => void canvas.copySmiles(smilesDraft)}
              disabled={!smilesDraft.trim()}
              aria-label="复制当前 SMILES 输入"
              title="复制当前 SMILES 输入"
            >
              {canvas.copyState === "copied" ? <Check /> : <Copy />}
            </button>
            {smilesSyncError || canvas.feedback || smilesSyncState === "pending" || smilesSyncState === "syncing" ? (
              <p
                className={smilesSyncState === "error" ? "is-error" : ""}
                role={smilesSyncState === "error" ? "alert" : "status"}
                aria-live="polite"
              >
                {smilesSyncError || (smilesSyncState === "pending"
                  ? "等待输入完成后自动同步…"
                  : smilesSyncState === "syncing"
                    ? "正在校验并同步到画板…"
                    : canvas.feedback)}
              </p>
            ) : null}
          </section>
        </div>
      </div>

      <aside
        className={`tg-results-drawer${isDrawerOpen ? " is-open" : ""}`}
        aria-hidden={!isDrawerOpen}
        inert={!isDrawerOpen}
        aria-labelledby="tg-results-title"
      >
        <div
          className="tg-drawer-resizer"
          role="separator"
          tabIndex={isDrawerOpen ? 0 : -1}
          aria-label="调整候选结果抽屉宽度"
          aria-orientation="vertical"
          aria-valuemin={DRAWER_MIN_WIDTH}
          aria-valuemax={DRAWER_MAX_WIDTH}
          aria-valuenow={drawerWidth}
          onPointerDown={startDrawerResize}
          onKeyDown={(event) => {
            if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
              event.preventDefault();
              const amount = event.shiftKey ? 40 : 16;
              setDrawerWidth((current) =>
                clamp(
                  current + (event.key === "ArrowLeft" ? amount : -amount),
                  DRAWER_MIN_WIDTH,
                  DRAWER_MAX_WIDTH
                )
              );
            }
          }}
        />
        <header className="tg-results-header">
          <div>
            <span><MessageSquareText /></span>
            <div>
              <h2 id="tg-results-title">Tg 候选结果</h2>
              <p>{resultStatus}</p>
            </div>
          </div>
          <button
            type="button"
            aria-label="关闭候选结果"
            onClick={() => setIsDrawerOpen(false)}
          >
            <X />
          </button>
        </header>
        <div className="tg-results-body">
          <ReverseDesignResults
            data={reverseDesign.data}
            error={reverseDesign.error}
            isLoading={reverseDesign.isLoading}
            job={reverseDesign.job}
            submittedRequest={reverseDesign.submittedRequest}
            onOpenKnowledge={onOpenKnowledge}
            page={resultPage}
            onPageChange={setResultPage}
          />
        </div>
      </aside>

      {hasRun && !isDrawerOpen ? (
        <button
          type="button"
          className="btn-expand-analysis tg-drawer-reopen"
          onClick={openDrawer}
          aria-label="展开 Tg 候选结果"
          title="展开 Tg 候选结果"
        >
          <Search width={14} height={14} />
        </button>
      ) : null}
    </div>
  );
}
