import { SlidersHorizontal, Sparkles } from "lucide-react";
import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
  type CSSProperties
} from "react";
import { REVERSE_DESIGN_DEMO_SMILES } from "../constants/reverseDesignDefaults";
import type { TgAssistantSession } from "../hooks/useTgAssistant";
import { useReverseDesign } from "../hooks/useReverseDesign";
import {
  useTgStructureCanvas,
  type TgSmilesDraftState
} from "../hooks/useTgStructureCanvas";
import type {
  KnowledgeNavigationRequest,
  ReverseDesignTgRequest,
  StructureWorkspaceContext,
  TgAssistantOperation,
  TgAssistantPageContext
} from "../types";
import "../styles/structure-workbench.css";
import "../styles/reverse-design.css";
import "../styles/tg-reverse-design-workbench.css";
import type { StructureCanvasOwnerHandle } from "./StructureWorkbenchPage";
import { ReverseDesignDrawer } from "./reverse-design/ReverseDesignDrawer";
import {
  ReverseDesignUtilityPanels,
  type ReverseDesignOpenPanel
} from "./reverse-design/ReverseDesignUtilityPanels";
import { StructureCanvasSurface } from "./structure-workbench/StructureCanvasSurface";

type ReverseDesignPageProps = {
  structure: StructureWorkspaceContext;
  onOpenKnowledge: (request: KnowledgeNavigationRequest) => void;
  assistant: TgAssistantSession;
};

type TgAssistantSuggestionContext = {
  isLoading: boolean;
  searchFailed: boolean;
  hasResultData: boolean;
  resultCount: number;
  parametersDirty: boolean;
  editorReady: boolean;
  smilesState: TgSmilesDraftState;
  hasSmiles: boolean;
  validationMessage: string | null;
  targetTg: number | null;
};

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

function validateRequest(request: ReverseDesignTgRequest) {
  if (request.target_tg === null || !Number.isFinite(request.target_tg)) {
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
  if (!submitted) return false;
  return (
    draft.target_tg !== submitted.target_tg ||
    draft.similarity_threshold !== submitted.similarity_threshold ||
    draft.candidate_size !== submitted.candidate_size
  );
}

export const ReverseDesignPage = forwardRef<
  StructureCanvasOwnerHandle,
  ReverseDesignPageProps
>(function ReverseDesignPage({ structure, onOpenKnowledge, assistant }, forwardedRef) {
  const reverseDesign = useReverseDesign();
  const [openPanel, setOpenPanel] = useState<ReverseDesignOpenPanel>(null);
  const [hasActivated3D, setHasActivated3D] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [hasRun, setHasRun] = useState(false);
  const [drawerWidth, setDrawerWidth] = useState(380);
  const [resultPage, setResultPage] = useState(1);
  const parameterPanelRef = useRef<HTMLElement | null>(null);
  const assistantPanelRef = useRef<HTMLElement | null>(null);
  const parameterButtonRef = useRef<HTMLButtonElement | null>(null);
  const assistantButtonRef = useRef<HTMLButtonElement | null>(null);
  const restoreFocusFrameRef = useRef<number | null>(null);
  const revisionRef = useRef(globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-tg`);
  const lastCanvasRevisionRef = useRef<string | null>(null);

  const handleStructureChanged = useCallback(() => {
    revisionRef.current = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-structure`;
    reverseDesign.reset();
    setHasRun(false);
    setDrawerOpen(false);
    setResultPage(1);
    assistant.addDivider("结构已变化");
  }, [assistant, reverseDesign]);

  const canvas = useTgStructureCanvas({ structure, onStructureChanged: handleStructureChanged });

  function markAssistantRevision(label: string) {
    revisionRef.current = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${label}`;
  }

  const closePanel = useCallback((restoreFocus = true) => {
    const panel = openPanel;
    setOpenPanel(null);
    if (!panel || !restoreFocus) return;
    if (restoreFocusFrameRef.current !== null) {
      window.cancelAnimationFrame(restoreFocusFrameRef.current);
    }
    restoreFocusFrameRef.current = window.requestAnimationFrame(() => {
      restoreFocusFrameRef.current = null;
      (panel === "parameters" ? parameterButtonRef.current : assistantButtonRef.current)?.focus();
    });
  }, [openPanel]);

  useEffect(() => {
    return () => {
      if (restoreFocusFrameRef.current !== null) {
        window.cancelAnimationFrame(restoreFocusFrameRef.current);
      }
    };
  }, []);

  useEffect(() => {
    if (!openPanel) return;
    const panel = openPanel === "parameters" ? parameterPanelRef.current : assistantPanelRef.current;
    const frame = window.requestAnimationFrame(() => {
      panel
        ?.querySelector<HTMLElement>(
          openPanel === "parameters"
            ? ".np-tg-field input"
            : 'textarea[aria-label="发送给 AI 助手的消息"]'
        )
        ?.focus();
    });

    function handlePointerDown(event: PointerEvent) {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (
        openPanel === "assistant" &&
        target instanceof Element &&
        target.closest(".np-sw-drawer")
      ) {
        return;
      }
      const trigger = openPanel === "parameters" ? parameterButtonRef.current : assistantButtonRef.current;
      if (!panel?.contains(target) && !trigger?.contains(target)) closePanel(false);
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key !== "Escape") return;
      event.preventDefault();
      closePanel(true);
    }

    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      window.cancelAnimationFrame(frame);
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [closePanel, openPanel]);

  useImperativeHandle(
    forwardedRef,
    () => ({
      syncBeforeLeave: canvas.syncBeforeLeave
    }),
    [canvas]
  );

  const validationMessage = validateRequest(reverseDesign.request);
  const parametersDirty = requestsDiffer(reverseDesign.request, reverseDesign.submittedRequest);
  const operationBusy = canvas.isBusy || reverseDesign.isLoading;
  const smilesSyncBlocked = canvas.smilesDraftState !== "synced";
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
    canvas.smilesDraft,
    canvas.smilesDraftState,
    canvas.smilesDraftError,
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
    reverseDesign.setRequest({ ...reverseDesign.request, ...partial });
  }

  function togglePanel(panel: Exclude<ReverseDesignOpenPanel, null>) {
    if (openPanel === panel) closePanel(true);
    else setOpenPanel(panel);
  }

  function openDrawer() {
    setOpenPanel((current) => current === "parameters" ? null : current);
    setDrawerOpen(true);
  }

  async function toggle3D() {
    const activating = !canvas.isFlipped;
    const changed = await canvas.toggle3D();
    if (changed && activating) setHasActivated3D(true);
  }

  async function performSearch(draft: ReverseDesignTgRequest) {
    if (smilesSyncBlocked) {
      canvas.setFeedback("请等待 SMILES 同步完成，或先修正当前输入。");
      return false;
    }
    if (validateRequest(draft) || canvas.isBusy || reverseDesign.isLoading) return false;
    const smiles = await canvas.resolveSmilesForSearch();
    if (!smiles) return false;
    canvas.setFeedback(null);

    const request: ReverseDesignTgRequest = { ...draft, smiles };
    setResultPage(1);
    setHasRun(true);
    openDrawer();
    assistant.addDivider("已开始新的 Tg 候选搜索");
    void reverseDesign.submit(request);
    return true;
  }

  async function handleSearch() {
    if (!(await performSearch(reverseDesign.request))) setOpenPanel("parameters");
  }

  async function captureAssistantContext(): Promise<TgAssistantPageContext> {
    const peek = await canvas.peekCanvasState();
    if (lastCanvasRevisionRef.current !== null && peek.revisionKey !== lastCanvasRevisionRef.current) {
      revisionRef.current = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-canvas`;
    }
    lastCanvasRevisionRef.current = peek.revisionKey;
    const request = reverseDesign.request;
    const validationError = canvas.smilesDraftError
      ? { field: "structure" as const, message: canvas.smilesDraftError }
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
        smiles: (smilesSyncBlocked ? canvas.smilesDraft.trim() : peek.smiles) || null,
        canvas_dirty: peek.canvasDirty || smilesSyncBlocked,
        editor_ready: peek.editorReady,
        view_mode: peek.viewMode,
        busy: peek.busy || canvas.smilesDraftState === "syncing"
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
        drawer_open: drawerOpen,
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
        if (
          patch.similarity_threshold !== undefined &&
          (!Number.isFinite(patch.similarity_threshold) || patch.similarity_threshold < 0 || patch.similarity_threshold > 1)
        ) {
          return { status: "failed", detail: "相似度阈值参数无效。" };
        }
        if (
          patch.candidate_size !== undefined &&
          (!Number.isInteger(patch.candidate_size) || patch.candidate_size < 1 || patch.candidate_size > 200)
        ) {
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
    getStructureSmiles: () => canvas.smilesDraftState === "synced"
      ? (canvas.smilesDraft.trim() || null)
      : null,
    navigate: (target) => {
      if (target === "parameters") setOpenPanel("parameters");
      else setDrawerOpen(true);
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
    smilesState: canvas.smilesDraftState,
    hasSmiles: Boolean(canvas.smilesDraft.trim()),
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
            : !canvas.smilesDraft.trim()
              ? "先绘制或导入结构，再设置 Tg、相似度阈值和候选数量。"
              : validationMessage
                ? validationMessage
                : "当前结构和参数已准备完成，可以开始搜索或向 AI 提问。";

  function parameterStatusText() {
    if (canvas.smilesDraftError) return canvas.smilesDraftError;
    if (canvas.smilesDraftState === "pending") return "等待 SMILES 输入完成后自动同步…";
    if (canvas.smilesDraftState === "syncing") return "正在校验并同步结构，请稍候…";
    if (validationMessage) return validationMessage;
    if (parametersDirty) return "参数已修改，需要重新搜索。";
    return "参数已就绪 · 搜索结果按 Tg 距离和结构相似度筛选。";
  }

  const parameterHasError = Boolean(canvas.smilesDraftError || validationMessage);
  const workbenchStyle = { "--np-sw-drawer-width": `${drawerWidth}px` } as CSSProperties;

  return (
    <div
      className="np-structure-workbench np-tg-reverse-design"
      data-module="tg-reverse-design"
      style={workbenchStyle}
    >
      <div className={`np-sw-page${drawerOpen ? " has-open-drawer" : ""}`}>
        <h1 className="np-sw-page-title">Tg 逆向设计</h1>

        <div className={`np-sw-layout${drawerOpen ? " has-open-drawer" : ""}`}>
          <main className="np-sw-workspace">
            <StructureCanvasSurface
              structure={structure}
              canvas={canvas}
              hasActivated3D={hasActivated3D}
              operationBusy={operationBusy}
              editorTitle="Tg 逆向设计结构编辑器"
              utilityActions={[
                {
                  id: "tg-search-parameters",
                  label: "搜索参数",
                  icon: <SlidersHorizontal aria-hidden="true" />,
                  active: openPanel === "parameters",
                  buttonRef: parameterButtonRef,
                  controls: "tg-parameter-panel",
                  onClick: () => togglePanel("parameters")
                },
                {
                  id: "tg-assistant",
                  label: "AI 助手",
                  icon: <Sparkles aria-hidden="true" />,
                  active: openPanel === "assistant",
                  busy: assistant.isStreaming,
                  buttonRef: assistantButtonRef,
                  controls: "tg-assistant-panel",
                  onClick: () => togglePanel("assistant")
                }
              ]}
              onLoadExample={() => canvas.loadStructure(REVERSE_DESIGN_DEMO_SMILES)}
              onImportFile={(file) => canvas.importImageFile(file)}
              onClear={() => canvas.clearCanvas()}
              onSync={() => canvas.syncSmilesFromCanvas()}
              onToggle3D={toggle3D}
              onSmilesDraftChange={() => markAssistantRevision("smiles-draft")}
            />

            <ReverseDesignUtilityPanels
              openPanel={openPanel}
              parameterPanelRef={parameterPanelRef}
              assistantPanelRef={assistantPanelRef}
              request={reverseDesign.request}
              parameterStatus={parameterStatusText()}
              parameterHasError={parameterHasError}
              searching={reverseDesign.isLoading}
              canSearch={!validationMessage && !operationBusy && !smilesSyncBlocked}
              assistant={assistant}
              assistantContextLabels={[
                canvas.smilesDraftState === "syncing"
                  ? "结构同步中"
                  : smilesSyncBlocked
                    ? "结构输入待修正"
                    : canvas.smilesDraft.trim()
                      ? "结构已准备"
                      : "尚未添加结构",
                `Tg ${reverseDesign.request.target_tg ?? "—"} °C`,
                resultStatus
              ]}
              assistantLocalDiagnostic={localDiagnostic}
              assistantSuggestions={assistantSuggestions}
              onClose={closePanel}
              onRequestChange={updateRequest}
              onSearch={() => void handleSearch()}
            />
          </main>

          <ReverseDesignDrawer
            open={drawerOpen}
            hasRun={hasRun}
            width={drawerWidth}
            status={resultStatus}
            data={reverseDesign.data}
            error={reverseDesign.error}
            loading={reverseDesign.isLoading}
            job={reverseDesign.job}
            submittedRequest={reverseDesign.submittedRequest}
            page={resultPage}
            onPageChange={setResultPage}
            onOpenKnowledge={onOpenKnowledge}
            onWidthChange={setDrawerWidth}
            onClose={() => setDrawerOpen(false)}
            onOpen={openDrawer}
          />
        </div>
      </div>
    </div>
  );
});
