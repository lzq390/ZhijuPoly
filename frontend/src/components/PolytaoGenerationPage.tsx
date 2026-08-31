import {
  Atom,
  Box,
  Check,
  ChevronDown,
  CircleAlert,
  Copy,
  Edit3,
  Filter,
  Info,
  Link2,
  LoaderCircle,
  PanelRightOpen,
  Play,
  RefreshCw,
  RotateCcw,
  SlidersHorizontal,
  Sparkles,
  Trash2,
  Wand2,
  X
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type PointerEvent as ReactPointerEvent,
  type RefObject
} from "react";
import {
  DEFAULT_POLYTAO_DESCRIPTORS,
  EMPTY_POLYTAO_DESCRIPTORS,
  getPolytaoRuntimeDisplayState,
  polytaoDescriptorMapFromEntries,
  type PolytaoRuntimeDisplayState,
  usePolytaoGeneration
} from "../hooks/usePolytaoGeneration";
import { calculatePolytaoDescriptors, fetchStructure2D } from "../services/api";
import {
  POLYTAO_DESCRIPTOR_NAMES,
  type PolytaoCandidate,
  type PolytaoDescriptorMap,
  type PolytaoDescriptorName,
  type PolytaoGenerationRequest,
  type PolytaoGenerationResponse,
  type PolytaoJobStatusResponse,
  type PolytaoStatusResponse,
  type StructureWorkspaceContext
} from "../types";
import { StructurePreview3D } from "./StructurePreview3D";
import { StructureSvg } from "./StructureSvg";
import "../styles/polytao-generation.css";

type PolytaoGenerationPageProps = {
  structure: StructureWorkspaceContext;
  onEditStructure: () => void;
  onBackHome: () => void;
};

type DescriptorSource = "empty" | "structure" | "manual" | "sample";

type DescriptorItem = {
  name: PolytaoDescriptorName;
  label: string;
  step: number;
};

type DescriptorGroup = {
  title: string;
  description: string;
  items: DescriptorItem[];
};

const DESCRIPTOR_GROUPS: DescriptorGroup[] = [
  {
    title: "规模与柔性",
    description: "质量、原子组成与链段活动度",
    items: [
      { name: "MolWt", label: "分子量", step: 0.1 },
      { name: "HeavyAtomCount", label: "重原子数", step: 1 },
      { name: "NumHeteroatoms", label: "杂原子数", step: 1 },
      { name: "NumRotatableBonds", label: "可旋转键数", step: 1 }
    ]
  },
  {
    title: "氢键能力",
    description: "氮氧组成及供体、受体数量",
    items: [
      { name: "NHOHCount", label: "N / O–H 数", step: 1 },
      { name: "NOCount", label: "氮氧原子数", step: 1 },
      { name: "NumHAcceptors", label: "氢键受体数", step: 1 },
      { name: "NumHDonors", label: "氢键供体数", step: 1 }
    ]
  },
  {
    title: "环结构组成",
    description: "脂肪族与芳香环系统分布",
    items: [
      { name: "NumAliphaticCarbocycles", label: "脂肪族碳环数", step: 1 },
      { name: "NumAliphaticHeterocycles", label: "脂肪族杂环数", step: 1 },
      { name: "NumAliphaticRings", label: "脂肪族环总数", step: 1 },
      { name: "NumAromaticCarbocycles", label: "芳香碳环数", step: 1 },
      { name: "NumAromaticHeterocycles", label: "芳香杂环数", step: 1 },
      { name: "NumAromaticRings", label: "芳香环总数", step: 1 },
      { name: "RingCount", label: "环总数", step: 1 }
    ]
  }
];

const DRAWER_MIN_WIDTH = 320;
const DRAWER_MAX_WIDTH = 560;
const DRAWER_DEFAULT_WIDTH = 380;
const DRAWER_KEYBOARD_STEP = 16;
const DRAWER_INLINE_MIN_WIDTH = 1160;
const TWO_K_MEDIA_QUERY = "(min-width: 2000px) and (min-height: 1120px)";

const TWO_K_DRAWER_MIN_WIDTH = 480;
const TWO_K_DRAWER_MAX_WIDTH = 720;
const TWO_K_DRAWER_DEFAULT_WIDTH = 540;
const TWO_K_DRAWER_KEYBOARD_STEP = 24;

type DrawerMode = "inline" | "overlay";

type DrawerProfile = {
  min: number;
  max: number;
  defaultWidth: number;
  keyboardStep: number;
};

const STANDARD_DRAWER_PROFILE: DrawerProfile = {
  min: DRAWER_MIN_WIDTH,
  max: DRAWER_MAX_WIDTH,
  defaultWidth: DRAWER_DEFAULT_WIDTH,
  keyboardStep: DRAWER_KEYBOARD_STEP
};

const TWO_K_DRAWER_PROFILE: DrawerProfile = {
  min: TWO_K_DRAWER_MIN_WIDTH,
  max: TWO_K_DRAWER_MAX_WIDTH,
  defaultWidth: TWO_K_DRAWER_DEFAULT_WIDTH,
  keyboardStep: TWO_K_DRAWER_KEYBOARD_STEP
};

function isTwoKViewport() {
  return (
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia(TWO_K_MEDIA_QUERY).matches
  );
}

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}

function parseNumber(value: string) {
  if (!value.trim()) {
    return Number.NaN;
  }
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : Number.NaN;
}

function formatDescriptorValue(value: number) {
  if (!Number.isFinite(value)) {
    return "";
  }
  return Math.abs(value - Math.round(value)) < 1e-9
    ? String(Math.round(value))
    : String(Number(value.toPrecision(6)));
}

function filledDescriptorCount(descriptors: PolytaoDescriptorMap) {
  return POLYTAO_DESCRIPTOR_NAMES.reduce(
    (count, name) => count + (Number.isFinite(descriptors[name]) ? 1 : 0),
    0
  );
}

async function writeClipboardText(value: string) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(value);
    return;
  }

  const textarea = document.createElement("textarea");
  textarea.value = value;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.left = "-9999px";
  document.body.appendChild(textarea);
  textarea.select();
  document.execCommand("copy");
  document.body.removeChild(textarea);
}

function runtimePresentation(
  displayState: PolytaoRuntimeDisplayState,
  isGenerating: boolean,
  activeJobs: number | null | undefined
) {
  if (isGenerating) {
    return {
      className: "is-running",
      label: `PolyTAO 正在生成${activeJobs != null ? ` · 活动作业 ${activeJobs}` : ""}`
    };
  }

  switch (displayState) {
    case "checking":
      return { className: "is-running", label: "正在检查 PolyTAO 模型" };
    case "ready":
      return { className: "", label: "PolyTAO 模型就绪" };
    case "cold":
      return { className: "is-warning", label: "PolyTAO 模型待加载" };
    case "loading":
      return { className: "is-running", label: "PolyTAO 模型加载中" };
    case "disabled":
      return { className: "is-error", label: "PolyTAO 模型未启用" };
    case "db_unavailable":
      return { className: "is-error", label: "PolyTAO 依赖不可用" };
    default:
      return { className: "is-error", label: "PolyTAO 运行时异常" };
  }
}

function sourcePresentation(source: DescriptorSource, filled: number) {
  if (source === "structure") {
    return { icon: <Link2 />, label: "目标特征源自参考结构", className: "" };
  }
  if (source === "sample") {
    return { icon: <Wand2 />, label: "示例向量 · 无骨架关联", className: "is-manual" };
  }
  if (source === "manual" && filled > 0) {
    return { icon: <Edit3 />, label: "目标特征已人工调整", className: "is-manual" };
  }
  return { icon: <CircleAlert />, label: "描述符待填写", className: "is-empty" };
}

export function PolytaoGenerationPage({
  structure,
  onEditStructure
}: PolytaoGenerationPageProps) {
  const polytao = usePolytaoGeneration();
  const [descriptorError, setDescriptorError] = useState<string | null>(null);
  const [isDescriptorLoading, setIsDescriptorLoading] = useState(false);
  const [descriptorSource, setDescriptorSource] = useState<DescriptorSource>("empty");
  const [parameterOpen, setParameterOpen] = useState(false);
  const [referenceOpen, setReferenceOpen] = useState(false);
  const [structureFlipped, setStructureFlipped] = useState(false);
  const [referenceSmilesExpanded, setReferenceSmilesExpanded] = useState(false);
  const [referenceSvg, setReferenceSvg] = useState<string | null>(null);
  const [referenceSvgError, setReferenceSvgError] = useState<string | null>(null);
  const [isReferenceSvgLoading, setIsReferenceSvgLoading] = useState(false);
  const [hasGenerationAttempt, setHasGenerationAttempt] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [drawerMode, setDrawerMode] = useState<DrawerMode>("inline");
  const [isTwoK, setIsTwoK] = useState(isTwoKViewport);
  const [drawerWidth, setDrawerWidth] = useState(() =>
    isTwoKViewport() ? TWO_K_DRAWER_DEFAULT_WIDTH : DRAWER_DEFAULT_WIDTH
  );
  const [isDrawerResizing, setIsDrawerResizing] = useState(false);
  const [toastMessage, setToastMessage] = useState<string | null>(null);
  const pageRef = useRef<HTMLDivElement | null>(null);
  const parameterAnchorRef = useRef<HTMLDivElement | null>(null);
  const parameterButtonRef = useRef<HTMLButtonElement | null>(null);
  const drawerCloseRef = useRef<HTMLButtonElement | null>(null);
  const drawerReopenRef = useRef<HTMLButtonElement | null>(null);
  const drawerRef = useRef<HTMLElement | null>(null);
  const drawerReturnFocusRef = useRef<HTMLElement | null>(null);
  const drawerResizeCleanupRef = useRef<(() => void) | null>(null);
  const toastTimerRef = useRef<number | null>(null);
  const drawerProfile = isTwoK ? TWO_K_DRAWER_PROFILE : STANDARD_DRAWER_PROFILE;
  const previousDrawerProfileRef = useRef<DrawerProfile>(drawerProfile);
  const hasStructure = structure.smiles.trim().length > 0;
  const filledCount = useMemo(
    () => filledDescriptorCount(polytao.request.descriptors),
    [polytao.request.descriptors]
  );
  const runtimeDisplayState = getPolytaoRuntimeDisplayState(
    polytao.serviceStatus,
    polytao.statusError,
    polytao.isStatusLoading
  );
  const runtime = runtimePresentation(
    runtimeDisplayState,
    polytao.isLoading,
    polytao.serviceStatus?.active_jobs
  );
  const source = sourcePresentation(descriptorSource, filledCount);

  const samplingValid =
    polytao.request.candidate_count >= 1 &&
    polytao.request.candidate_count <= 50 &&
    polytao.request.temperature >= 0.1 &&
    polytao.request.temperature <= 2 &&
    polytao.request.top_k >= 1 &&
    polytao.request.top_k <= 500 &&
    polytao.request.top_p > 0 &&
    polytao.request.top_p <= 1 &&
    polytao.request.max_length >= 16 &&
    polytao.request.max_length <= 512;
  const descriptorReady = filledCount === POLYTAO_DESCRIPTOR_NAMES.length;
  const canSubmit =
    !polytao.isLoading &&
    polytao.serviceStatus?.available === true &&
    descriptorReady &&
    samplingValid;

  const showToast = useCallback((message: string) => {
    if (toastTimerRef.current !== null) {
      window.clearTimeout(toastTimerRef.current);
    }
    setToastMessage(message);
    toastTimerRef.current = window.setTimeout(() => {
      setToastMessage(null);
      toastTimerRef.current = null;
    }, 1800);
  }, []);

  const copyText = useCallback(async (value: string, successMessage: string) => {
    try {
      await writeClipboardText(value);
      showToast(successMessage);
    } catch {
      showToast("复制失败，请手动复制");
    }
  }, [showToast]);

  useEffect(() => {
    return () => {
      if (toastTimerRef.current !== null) {
        window.clearTimeout(toastTimerRef.current);
      }
      drawerResizeCleanupRef.current?.();
    };
  }, []);

  useEffect(() => {
    if (typeof window.matchMedia !== "function") {
      return;
    }
    const query = window.matchMedia(TWO_K_MEDIA_QUERY);
    const update = () => setIsTwoK(query.matches);
    update();
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);

  useEffect(() => {
    const previousProfile = previousDrawerProfileRef.current;
    if (previousProfile === drawerProfile) {
      return;
    }
    setDrawerWidth((currentWidth) => {
      const ratio = clamp(
        (currentWidth - previousProfile.min) / (previousProfile.max - previousProfile.min),
        0,
        1
      );
      return Math.round(drawerProfile.min + ratio * (drawerProfile.max - drawerProfile.min));
    });
    previousDrawerProfileRef.current = drawerProfile;
  }, [drawerProfile]);

  useEffect(() => {
    const page = pageRef.current;
    if (!page) {
      return;
    }

    const updateMode = (width: number) => {
      if (width > 0) {
        setDrawerMode(width >= DRAWER_INLINE_MIN_WIDTH ? "inline" : "overlay");
      }
    };
    const measure = () => updateMode(page.getBoundingClientRect().width);
    measure();

    if (typeof ResizeObserver === "undefined") {
      window.addEventListener("resize", measure);
      return () => window.removeEventListener("resize", measure);
    }

    const observer = new ResizeObserver((entries) => {
      updateMode(entries[0]?.contentRect.width ?? 0);
    });
    observer.observe(page);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    setStructureFlipped(false);
    setReferenceSmilesExpanded(false);
    setReferenceSvg(null);
    setReferenceSvgError(null);
    setIsReferenceSvgLoading(false);
  }, [structure.smiles]);

  useEffect(() => {
    const smiles = structure.smiles.trim();
    if (!referenceOpen || !smiles || referenceSvg) {
      return;
    }

    const controller = new AbortController();
    setIsReferenceSvgLoading(true);
    void fetchStructure2D(smiles, controller.signal)
      .then((response) => {
        if (!controller.signal.aborted) {
          setReferenceSvg(response.structure_svg);
        }
      })
      .catch((error) => {
        if (!controller.signal.aborted) {
          setReferenceSvgError(error instanceof Error ? error.message : "二维结构渲染失败");
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setIsReferenceSvgLoading(false);
        }
      });

    return () => controller.abort();
  }, [referenceOpen, referenceSvg, structure.smiles]);

  const closeParameterPanel = useCallback((restoreFocus = false) => {
    setParameterOpen(false);
    if (restoreFocus) {
      window.requestAnimationFrame(() => parameterButtonRef.current?.focus());
    }
  }, []);

  useEffect(() => {
    if (!parameterOpen) {
      return;
    }

    const closeOnOutsidePointer = (event: PointerEvent) => {
      if (!parameterAnchorRef.current?.contains(event.target as Node)) {
        closeParameterPanel(false);
      }
    };
    document.addEventListener("pointerdown", closeOnOutsidePointer);
    return () => document.removeEventListener("pointerdown", closeOnOutsidePointer);
  }, [closeParameterPanel, parameterOpen]);

  const closeDrawer = useCallback(() => {
    setDrawerOpen(false);
    window.requestAnimationFrame(() => {
      const returnTarget = drawerReturnFocusRef.current;
      if (returnTarget?.isConnected) {
        returnTarget.focus();
      } else {
        drawerReopenRef.current?.focus();
      }
    });
  }, []);

  const openDrawer = useCallback(() => {
    drawerReturnFocusRef.current =
      document.activeElement instanceof HTMLElement ? document.activeElement : drawerReopenRef.current;
    setDrawerOpen(true);
  }, []);

  useEffect(() => {
    const handleEscape = (event: KeyboardEvent) => {
      if (
        event.key === "Tab" &&
        drawerOpen &&
        drawerMode === "overlay" &&
        drawerRef.current
      ) {
        const focusable = Array.from(
          drawerRef.current.querySelectorAll<HTMLElement>(
            'button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])'
          )
        ).filter((element) => !element.hidden && element.getAttribute("aria-hidden") !== "true");
        if (focusable.length === 0) {
          return;
        }
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (event.shiftKey && (document.activeElement === first || !drawerRef.current.contains(document.activeElement))) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && (document.activeElement === last || !drawerRef.current.contains(document.activeElement))) {
          event.preventDefault();
          first.focus();
        }
        return;
      }
      if (event.key !== "Escape") {
        return;
      }
      if (parameterOpen) {
        closeParameterPanel(true);
        return;
      }
      if (drawerOpen) {
        closeDrawer();
      }
    };
    window.addEventListener("keydown", handleEscape);
    return () => window.removeEventListener("keydown", handleEscape);
  }, [closeDrawer, closeParameterPanel, drawerMode, drawerOpen, parameterOpen]);

  function updateRequest(partial: Partial<PolytaoGenerationRequest>) {
    polytao.setRequest({
      ...polytao.request,
      ...partial,
      descriptors: partial.descriptors ?? polytao.request.descriptors
    });
  }

  function updateDescriptor(name: PolytaoDescriptorName, value: number) {
    const descriptors = {
      ...polytao.request.descriptors,
      [name]: value
    };
    updateRequest({ descriptors, input_smiles: null });
    setDescriptorSource(filledDescriptorCount(descriptors) > 0 ? "manual" : "empty");
    setDescriptorError(null);
  }

  async function handleDescriptorPrefill() {
    setDescriptorError(null);
    setIsDescriptorLoading(true);
    try {
      const currentSmiles = (await structure.getCurrentSmiles()).trim();
      if (!currentSmiles) {
        setDescriptorError("请先在结构工作台设置参考结构。");
        return;
      }
      const response = await calculatePolytaoDescriptors({ smiles: currentSmiles });
      polytao.setRequest({
        ...polytao.request,
        descriptors: polytaoDescriptorMapFromEntries(response.descriptors),
        input_smiles: response.canonical_smiles
      });
      setDescriptorSource("structure");
      showToast("已从参考结构提取 15 项描述符");
    } catch (error) {
      setDescriptorError(error instanceof Error ? error.message : "PolyTAO 描述符计算失败。");
      setReferenceOpen(true);
    } finally {
      setIsDescriptorLoading(false);
    }
  }

  function handleLoadSample() {
    updateRequest({ descriptors: { ...DEFAULT_POLYTAO_DESCRIPTORS }, input_smiles: null });
    setDescriptorSource("sample");
    setDescriptorError(null);
  }

  function handleClearDescriptors() {
    updateRequest({ descriptors: { ...EMPTY_POLYTAO_DESCRIPTORS }, input_smiles: null });
    setDescriptorSource("empty");
    setDescriptorError(null);
  }

  async function handleSubmit() {
    setDescriptorError(null);
    if (polytao.serviceStatus && !polytao.serviceStatus.available) {
      setDescriptorError(polytao.serviceStatus.message);
      return;
    }

    setParameterOpen(false);
    drawerReturnFocusRef.current = parameterButtonRef.current;
    setHasGenerationAttempt(true);
    setDrawerOpen(true);
    await polytao.submit({
      ...polytao.request,
      input_smiles: polytao.request.input_smiles?.trim() || null
    });
  }

  function beginDrawerResize(event: ReactPointerEvent<HTMLDivElement>) {
    event.preventDefault();
    drawerResizeCleanupRef.current?.();
    const startX = event.clientX;
    const startWidth = drawerWidth;
    const previousUserSelect = document.body.style.userSelect;
    setIsDrawerResizing(true);
    document.body.style.userSelect = "none";

    const handleMove = (moveEvent: PointerEvent) => {
      setDrawerWidth(clamp(startWidth + startX - moveEvent.clientX, drawerProfile.min, drawerProfile.max));
    };
    const cleanupResize = () => {
      document.body.style.userSelect = previousUserSelect;
      window.removeEventListener("pointermove", handleMove);
      window.removeEventListener("pointerup", handleEnd);
      window.removeEventListener("pointercancel", handleEnd);
      drawerResizeCleanupRef.current = null;
    };
    const handleEnd = () => {
      cleanupResize();
      setIsDrawerResizing(false);
    };

    drawerResizeCleanupRef.current = cleanupResize;
    window.addEventListener("pointermove", handleMove);
    window.addEventListener("pointerup", handleEnd);
    window.addEventListener("pointercancel", handleEnd);
  }

  function handleDrawerResizeKey(event: React.KeyboardEvent<HTMLDivElement>) {
    let nextWidth = drawerWidth;
    if (event.key === "ArrowLeft") {
      nextWidth += drawerProfile.keyboardStep;
    } else if (event.key === "ArrowRight") {
      nextWidth -= drawerProfile.keyboardStep;
    } else if (event.key === "Home") {
      nextWidth = drawerProfile.min;
    } else if (event.key === "End") {
      nextWidth = drawerProfile.max;
    } else {
      return;
    }
    event.preventDefault();
    setDrawerWidth(clamp(nextWidth, drawerProfile.min, drawerProfile.max));
  }

  const pageStyle = {
    "--polytao-drawer-width": `${drawerWidth}px`
  } as CSSProperties;

  return (
    <div
      ref={pageRef}
      className={`polytao-page is-drawer-${drawerMode}${drawerOpen ? " is-drawer-open" : ""}`}
      style={pageStyle}
    >
      <div
        className="polytao-page-scroll"
        inert={drawerOpen && drawerMode === "overlay"}
      >
        <header className="polytao-page-heading">
          <h1>聚合物生成</h1>
        </header>

        <main className="polytao-workbench-shell">
          <div className="polytao-module-toolbar" aria-label="PolyTAO 模块状态">
            <span className="polytao-model-label">
              <Sparkles />
              PolyTAO · 15 项 RDKit 描述符
            </span>
            <span className={`polytao-status-chip ${runtime.className}`}>
              <i />
              <span>{runtime.label}</span>
            </span>
          </div>

          <section className="polytao-generation-surface" aria-labelledby="polytao-generation-title">
          <header className="polytao-surface-header">
            <div className="polytao-surface-heading">
              <span className="polytao-surface-mark"><Sparkles /></span>
              <div className="polytao-surface-copy">
                <h2 id="polytao-generation-title">目标生成设置</h2>
                <p>PolyTAO 模型 · 根据 15 项目标分子特征生成聚合物重复单元</p>
              </div>
            </div>

            <div className="polytao-surface-actions">
              <span className={`polytao-source-chip ${source.className}`}>
                {source.icon}
                <span>{source.label}</span>
              </span>
              <div className="polytao-parameter-anchor" ref={parameterAnchorRef}>
                <button
                  ref={parameterButtonRef}
                  className="polytao-secondary-button polytao-parameter-trigger"
                  type="button"
                  aria-expanded={parameterOpen}
                  aria-controls="polytao-parameter-panel"
                  onClick={() => setParameterOpen((open) => !open)}
                >
                  <SlidersHorizontal />
                  参数配置
                </button>
                <ParameterPanel
                  open={parameterOpen}
                  request={polytao.request}
                  canSubmit={canSubmit}
                  descriptorReady={descriptorReady}
                  samplingValid={samplingValid}
                  runtimeAvailable={polytao.serviceStatus?.available === true}
                  isLoading={polytao.isLoading}
                  onClose={() => closeParameterPanel(true)}
                  onRequestChange={updateRequest}
                  onSubmit={() => void handleSubmit()}
                />
              </div>
            </div>
          </header>

          <section
            className={`polytao-condition-section polytao-reference-section${referenceOpen ? " is-open" : ""}`}
            aria-labelledby="polytao-source-title"
          >
            <div className="polytao-section-heading">
              <div className="polytao-section-title">
                <span className="polytao-section-number">01</span>
                <div>
                  <h3 id="polytao-source-title">参考结构（可选）</h3>
                  <p>从共享结构一键计算目标分子特征，也可以跳过并直接设定目标</p>
                </div>
              </div>
              <button
                className="polytao-reference-toggle"
                type="button"
                aria-expanded={referenceOpen}
                aria-controls="polytao-reference-content"
                onClick={() => setReferenceOpen((open) => !open)}
              >
                <span className={`polytao-reference-status${hasStructure ? " is-set" : ""}`}>
                  {hasStructure ? <Link2 /> : <CircleAlert />}
                  {hasStructure ? "已设置 · 共享结构" : "未设置"}
                </span>
                <span className="polytao-reference-toggle-action">
                  {referenceOpen ? "收起" : "展开"}
                  <ChevronDown />
                </span>
              </button>
            </div>

            {referenceOpen ? (
              <div className="polytao-reference-content" id="polytao-reference-content">
                <div className="polytao-reference-content-toolbar">
                  <button
                    className="polytao-small-button polytao-structure-view-toggle"
                    type="button"
                    aria-pressed={structureFlipped}
                    aria-controls="polytao-structure-flip"
                    disabled={!hasStructure}
                    onClick={() => setStructureFlipped((flipped) => !flipped)}
                  >
                    {structureFlipped ? <RotateCcw /> : <Box />}
                    {structureFlipped ? "返回 2D" : "查看 3D"}
                  </button>
                </div>

                <div className="polytao-structure-source">
                  <div
                    id="polytao-structure-flip"
                    className={`polytao-structure-flip${structureFlipped ? " is-flipped" : ""}`}
                  >
                    <div className="polytao-structure-flip-inner">
                      <div className="polytao-structure-face polytao-structure-face-front">
                        <span className="polytao-structure-face-label">2D 结构</span>
                        <div className="polytao-structure-canvas" aria-label="共享聚合物重复单元二维结构">
                          <ReferenceStructure2D
                            hasStructure={hasStructure}
                            svg={referenceSvg}
                            isLoading={isReferenceSvgLoading}
                            error={referenceSvgError}
                          />
                        </div>
                      </div>
                      <div className="polytao-structure-face polytao-structure-face-back">
                        <span className="polytao-structure-face-label">3D 构象</span>
                        <div className="polytao-structure-3d-canvas" aria-label="共享结构三维构象">
                          {structureFlipped && hasStructure ? (
                            <StructurePreview3D
                              smiles={structure.smiles}
                              variant="bare"
                              visualStyle="polished-atoms"
                              backgroundColor="#f7fbff"
                              className="polytao-reference-3d"
                              previewClassName="polytao-reference-3d-preview"
                            />
                          ) : null}
                          <span className="polytao-structure-3d-hint">拖动旋转 · 滚轮缩放</span>
                        </div>
                      </div>
                    </div>
                  </div>

                  <div className="polytao-source-detail">
                    <div className={`polytao-smiles-disclosure${referenceSmilesExpanded ? " is-open" : ""}`}>
                      <button
                        className="polytao-smiles-toggle"
                        type="button"
                        aria-expanded={referenceSmilesExpanded}
                        aria-controls="polytao-reference-smiles-content"
                        disabled={!hasStructure}
                        onClick={() => setReferenceSmilesExpanded((expanded) => !expanded)}
                      >
                        <span className="polytao-field-eyebrow">共享结构 SMILES</span>
                        <span className="polytao-smiles-toggle-action">
                          {hasStructure ? (referenceSmilesExpanded ? "收起" : "展开") : "未设置"}
                          <ChevronDown />
                        </span>
                      </button>
                      {referenceSmilesExpanded && hasStructure ? (
                        <div className="polytao-smiles-content" id="polytao-reference-smiles-content">
                          <span className="polytao-smiles-value">{structure.smiles}</span>
                          <button
                            className="polytao-copy-inline"
                            type="button"
                            aria-label="复制共享结构 SMILES"
                            onClick={() => {
                              void copyText(structure.smiles, "共享结构 SMILES 已复制");
                            }}
                          >
                            <Copy />
                          </button>
                        </div>
                      ) : null}
                    </div>
                    <div className="polytao-semantic-note">
                      <Info />
                      <span>
                        <strong>参考结构不是生成骨架。</strong> PolyTAO 只读取由它计算出的分子特征，生成结果不会复刻或保留该结构。
                      </span>
                    </div>
                    <div className="polytao-source-actions">
                      <button className="polytao-secondary-button" type="button" onClick={onEditStructure}>
                        <Edit3 />
                        编辑结构
                      </button>
                      <button
                        className="polytao-secondary-button"
                        type="button"
                        onClick={() => void handleDescriptorPrefill()}
                        disabled={isDescriptorLoading || !hasStructure}
                      >
                        {isDescriptorLoading ? <LoaderCircle className="polytao-spinner" /> : <Wand2 />}
                        提取描述符
                      </button>
                    </div>
                    {descriptorError ? (
                      <div className="polytao-inline-error" role="alert">
                        <CircleAlert />
                        <span>{descriptorError}</span>
                      </div>
                    ) : null}
                  </div>
                </div>
              </div>
            ) : null}
          </section>

          <section className="polytao-condition-section" aria-labelledby="polytao-descriptor-title">
            <div className="polytao-section-heading">
              <div className="polytao-section-title">
                <span className="polytao-section-number">02</span>
                <div>
                  <h3 id="polytao-descriptor-title">目标分子特征</h3>
                  <p>15 项 RDKit 描述符共同定义期望生成的聚合物特征</p>
                </div>
              </div>
              <div className="polytao-descriptor-tools">
                <span className={`polytao-completion-chip${descriptorReady ? "" : " is-incomplete"}`}>
                  {descriptorReady ? <Check /> : <CircleAlert />}
                  <span>{filledCount} / 15 {descriptorReady ? "完整" : "已填写"}</span>
                </span>
                <button className="polytao-small-button" type="button" onClick={handleLoadSample}>
                  <Wand2 />
                  载入示例
                </button>
                <button className="polytao-small-button" type="button" onClick={handleClearDescriptors}>
                  <Trash2 />
                  清空
                </button>
              </div>
            </div>

            <div className="polytao-descriptor-grid" aria-label="目标分子特征描述符编辑器">
              {DESCRIPTOR_GROUPS.map((group, groupIndex) => (
                <DescriptorGroupEditor
                  key={group.title}
                  group={group}
                  groupIndex={groupIndex}
                  descriptors={polytao.request.descriptors}
                  onDescriptorChange={updateDescriptor}
                />
              ))}
            </div>
          </section>
          </section>
        </main>
      </div>

      {hasGenerationAttempt ? (
        <ResultsDrawer
          open={drawerOpen}
          mode={drawerMode}
          width={drawerWidth}
          profile={drawerProfile}
          isResizing={isDrawerResizing}
          data={polytao.data}
          job={polytao.job}
          error={polytao.error}
          isLoading={polytao.isLoading}
          runtimeDisplayState={runtimeDisplayState}
          status={polytao.serviceStatus}
          requestedCount={polytao.request.candidate_count}
          drawerRef={drawerRef}
          closeButtonRef={drawerCloseRef}
          reopenButtonRef={drawerReopenRef}
          onClose={closeDrawer}
          onOpen={openDrawer}
          onRetry={() => void handleSubmit()}
          onRefreshRuntime={() => void polytao.refreshStatus()}
          onResizeStart={beginDrawerResize}
          onResizeKeyDown={handleDrawerResizeKey}
          onCopy={(value) => {
            void copyText(value, "候选 SMILES 已复制");
          }}
        />
      ) : null}

      <div className={`polytao-toast${toastMessage ? " is-visible" : ""}`} role="status" aria-live="polite">
        <Check />
        <span>{toastMessage}</span>
      </div>
    </div>
  );
}

function ReferenceStructure2D({
  hasStructure,
  svg,
  isLoading,
  error
}: {
  hasStructure: boolean;
  svg: string | null;
  isLoading: boolean;
  error: string | null;
}) {
  if (svg) {
    return (
      <StructureSvg
        svg={svg}
        alt="共享聚合物重复单元二维结构"
        className="polytao-reference-svg"
        imageClassName="polytao-reference-svg-image"
        transparentBackground
      />
    );
  }
  if (isLoading) {
    return (
      <div className="polytao-reference-placeholder">
        <LoaderCircle className="polytao-spinner" />
        <span>正在渲染 2D 结构</span>
      </div>
    );
  }
  return (
    <div className="polytao-reference-placeholder">
      {error ? <CircleAlert /> : <Atom />}
      <span>{error ?? (hasStructure ? "暂无可用的 2D 结构" : "请先在结构工作台设置参考结构")}</span>
    </div>
  );
}

function DescriptorGroupEditor({
  group,
  groupIndex,
  descriptors,
  onDescriptorChange
}: {
  group: DescriptorGroup;
  groupIndex: number;
  descriptors: PolytaoDescriptorMap;
  onDescriptorChange: (name: PolytaoDescriptorName, value: number) => void;
}) {
  const filled = group.items.reduce(
    (count, item) => count + (Number.isFinite(descriptors[item.name]) ? 1 : 0),
    0
  );
  const groupComplete = filled === group.items.length;

  return (
    <section className="polytao-descriptor-group" aria-labelledby={`polytao-descriptor-group-${groupIndex}`}>
      <div className="polytao-descriptor-group-head">
        <span className="polytao-descriptor-group-index">0{groupIndex + 1}</span>
        <div className="polytao-descriptor-group-title">
          <h4 id={`polytao-descriptor-group-${groupIndex}`}>{group.title}</h4>
          <p>{group.description}</p>
          <span className={`polytao-group-progress ${groupComplete ? "is-complete" : "is-incomplete"}`}>
            {filled} / {group.items.length} 已填写
          </span>
        </div>
      </div>
      <div className="polytao-descriptor-fields">
        {group.items.map((item) => (
          <label className="polytao-descriptor-field" key={item.name}>
            <span className="polytao-descriptor-label">{item.label}</span>
            <span className="polytao-descriptor-key" title={item.name}>{item.name}</span>
            <input
              className="polytao-number-input"
              type="number"
              step={item.step}
              value={formatDescriptorValue(descriptors[item.name])}
              aria-label={`${item.label} ${item.name}`}
              onChange={(event) => onDescriptorChange(item.name, parseNumber(event.currentTarget.value))}
            />
          </label>
        ))}
      </div>
    </section>
  );
}

function ParameterPanel({
  open,
  request,
  canSubmit,
  descriptorReady,
  samplingValid,
  runtimeAvailable,
  isLoading,
  onClose,
  onRequestChange,
  onSubmit
}: {
  open: boolean;
  request: PolytaoGenerationRequest;
  canSubmit: boolean;
  descriptorReady: boolean;
  samplingValid: boolean;
  runtimeAvailable: boolean;
  isLoading: boolean;
  onClose: () => void;
  onRequestChange: (partial: Partial<PolytaoGenerationRequest>) => void;
  onSubmit: () => void;
}) {
  let readinessTitle = "生成目标已就绪";
  let readinessDetail = "15 项目标特征完整 · PolyTAO 可用";
  if (isLoading) {
    readinessTitle = "生成任务执行中";
    readinessDetail = "参数已锁定 · 正在轮询作业状态";
  } else if (!runtimeAvailable) {
    readinessTitle = "模型运行时不可用";
    readinessDetail = "当前输入已保留，恢复后可直接重试";
  } else if (!descriptorReady) {
    readinessTitle = "目标分子特征尚未完整";
    readinessDetail = "PolyTAO 需要完整的 15 维描述符向量";
  } else if (!samplingValid) {
    readinessTitle = "采样参数超出范围";
    readinessDetail = "请修正参数后再提交";
  }

  return (
    <section
      id="polytao-parameter-panel"
      className={`polytao-parameter-panel${open ? " is-open" : ""}`}
      role="dialog"
      aria-modal="false"
      aria-hidden={!open}
      aria-labelledby="polytao-parameter-title"
    >
      <header className="polytao-parameter-panel-head">
        <div className="polytao-parameter-panel-title">
          <span className="polytao-section-number">03</span>
          <div>
            <h3 id="polytao-parameter-title">参数配置</h3>
            <p>设置 PolyTAO 候选采样范围</p>
          </div>
        </div>
        <button className="polytao-icon-button" type="button" aria-label="关闭参数配置" onClick={onClose}>
          <X />
        </button>
      </header>
      <div className="polytao-sampling-grid">
        <SamplingField
          label="候选数量"
          range="1–50"
          min={1}
          max={50}
          step={1}
          value={request.candidate_count}
          onChange={(candidate_count) => onRequestChange({ candidate_count })}
        />
        <SamplingField
          label="Temperature"
          range="0.1–2.0"
          min={0.1}
          max={2}
          step={0.05}
          value={request.temperature}
          onChange={(temperature) => onRequestChange({ temperature })}
        />
        <SamplingField
          label="Top-K"
          range="1–500"
          min={1}
          max={500}
          step={1}
          value={request.top_k}
          onChange={(top_k) => onRequestChange({ top_k })}
        />
        <SamplingField
          label="Top-P"
          range="(0, 1]"
          min={0.001}
          max={1}
          step={0.001}
          value={request.top_p}
          onChange={(top_p) => onRequestChange({ top_p })}
        />
        <SamplingField
          label="最大长度"
          range="16–512"
          min={16}
          max={512}
          step={1}
          value={request.max_length}
          onChange={(max_length) => onRequestChange({ max_length })}
        />
      </div>
      <div className="polytao-generation-action-row">
        <div className="polytao-run-readiness">
          <strong>{readinessTitle}</strong>
          <span>{readinessDetail}</span>
        </div>
        <button className="polytao-primary-button" type="button" disabled={!canSubmit} onClick={onSubmit}>
          {isLoading ? <LoaderCircle className="polytao-spinner" /> : <Play />}
          {isLoading ? "正在生成" : "开始生成"}
        </button>
      </div>
    </section>
  );
}

function SamplingField({
  label,
  range,
  value,
  min,
  max,
  step,
  onChange
}: {
  label: string;
  range: string;
  value: number;
  min: number;
  max: number;
  step: number;
  onChange: (value: number) => void;
}) {
  const valid = Number.isFinite(value) && value >= min && value <= max && (label !== "Top-P" || value > 0);
  return (
    <label className="polytao-sampling-field">
      <span className="polytao-sampling-label">
        <span>{label}</span>
        <span className="polytao-range-hint">{range}</span>
      </span>
      <input
        className={`polytao-number-input${valid ? "" : " is-invalid"}`}
        type="number"
        min={min}
        max={max}
        step={step}
        value={Number.isFinite(value) ? value : ""}
        aria-label={label}
        onChange={(event) => onChange(parseNumber(event.currentTarget.value))}
      />
    </label>
  );
}

type ResultsDrawerProps = {
  open: boolean;
  mode: DrawerMode;
  width: number;
  profile: DrawerProfile;
  isResizing: boolean;
  data: PolytaoGenerationResponse | null;
  job: PolytaoJobStatusResponse | null;
  error: string | null;
  isLoading: boolean;
  runtimeDisplayState: PolytaoRuntimeDisplayState;
  status: PolytaoStatusResponse | null;
  requestedCount: number;
  drawerRef: RefObject<HTMLElement | null>;
  closeButtonRef: RefObject<HTMLButtonElement | null>;
  reopenButtonRef: RefObject<HTMLButtonElement | null>;
  onClose: () => void;
  onOpen: () => void;
  onRetry: () => void;
  onRefreshRuntime: () => void;
  onResizeStart: (event: ReactPointerEvent<HTMLDivElement>) => void;
  onResizeKeyDown: (event: React.KeyboardEvent<HTMLDivElement>) => void;
  onCopy: (value: string) => void;
};

function ResultsDrawer({
  open,
  mode,
  width,
  profile,
  isResizing,
  data,
  job,
  error,
  isLoading,
  runtimeDisplayState,
  status,
  requestedCount,
  drawerRef,
  closeButtonRef,
  reopenButtonRef,
  onClose,
  onOpen,
  onRetry,
  onRefreshRuntime,
  onResizeStart,
  onResizeKeyDown,
  onCopy
}: ResultsDrawerProps) {
  useEffect(() => {
    if (!open) {
      return;
    }
    window.requestAnimationFrame(() => closeButtonRef.current?.focus());
  }, [closeButtonRef, open]);

  const completedWithoutData = job?.status === "completed" && !data;
  const runtimeUnavailable =
    runtimeDisplayState === "disabled" ||
    runtimeDisplayState === "db_unavailable" ||
    runtimeDisplayState === "runtime_error";
  let subtitle = "等待提交 · PolyTAO";
  if (isLoading) {
    subtitle = `${jobStatusLabel(job?.status)} · ${Math.round(job?.progress_percent ?? 0)}%`;
  } else if (error) {
    subtitle = "生成失败 · 输入已保留";
  } else if (data) {
    subtitle = `${data.returned_count} 个候选 · 仅显示结构、SMILES 与 SA Score`;
  } else if (completedWithoutData) {
    subtitle = "0 个候选 · 生成完成";
  } else if (runtimeUnavailable) {
    subtitle = "PolyTAO 运行时不可用";
  }

  return (
    <>
      <button
        className={`polytao-drawer-backdrop${mode === "overlay" ? " is-overlay" : ""}${open ? " is-open" : ""}`}
        type="button"
        aria-label="关闭聚合物生成结果"
        aria-hidden={!open || mode !== "overlay"}
        tabIndex={-1}
        onClick={onClose}
      />
      <button
        ref={reopenButtonRef}
        className={`polytao-drawer-reopen${open ? "" : " is-visible"}`}
        type="button"
        aria-label="打开聚合物生成结果"
        title="打开聚合物生成结果"
        aria-hidden={open}
        tabIndex={open ? -1 : 0}
        onClick={onOpen}
      >
        <PanelRightOpen />
      </button>
      <aside
        ref={drawerRef}
        className={`polytao-detail-drawer is-${mode}${open ? " is-open" : ""}`}
        role="dialog"
        aria-modal={mode === "overlay"}
        aria-labelledby="polytao-drawer-title"
        aria-hidden={!open}
        inert={!open}
      >
        {mode === "inline" ? (
          <div
            className={`polytao-drawer-resizer${isResizing ? " is-dragging" : ""}`}
            role="separator"
            tabIndex={open ? 0 : -1}
            aria-label="调整聚合物生成结果侧栏宽度"
            aria-orientation="vertical"
            aria-valuemin={profile.min}
            aria-valuemax={profile.max}
            aria-valuenow={Math.round(width)}
            onPointerDown={onResizeStart}
            onKeyDown={onResizeKeyDown}
          />
        ) : null}
        <header className="polytao-drawer-header">
          <div className="polytao-drawer-heading">
            <span className="polytao-drawer-mark"><Sparkles /></span>
            <div className="polytao-drawer-copy">
              <span className="polytao-drawer-eyebrow">PolyTAO Output</span>
              <h2 id="polytao-drawer-title">聚合物生成结果</h2>
              <p>{subtitle}</p>
            </div>
          </div>
          <button
            ref={closeButtonRef}
            className="polytao-icon-button"
            type="button"
            aria-label="关闭聚合物生成结果"
            onClick={onClose}
          >
            <X />
          </button>
        </header>

        <div className="polytao-drawer-body" aria-live="polite">
          {isLoading ? <DrawerProgress job={job} /> : null}
          {!isLoading && error ? (
            <DrawerState
              icon={<CircleAlert />}
              tone="danger"
              title="生成任务执行失败"
              message={error}
              actionLabel="重新提交"
              onAction={onRetry}
            />
          ) : null}
          {!isLoading && !error && data?.results.length ? (
            <div className="polytao-drawer-results-list">
              {data.results.map((candidate) => (
                <CandidateCard
                  key={`${candidate.rank}-${candidate.generated_smiles}`}
                  candidate={candidate}
                  onCopy={onCopy}
                />
              ))}
            </div>
          ) : null}
          {!isLoading && !error && data && data.results.length === 0 ? (
            <DrawerState
              icon={<Filter />}
              tone="warning"
              title="没有可用候选"
              message="本次生成未返回通过结构校验的聚合物，目标特征与参数已保留。"
              actionLabel="重新生成"
              onAction={onRetry}
            />
          ) : null}
          {!isLoading && !error && !data && completedWithoutData ? (
            <DrawerState
              icon={<Filter />}
              tone="warning"
              title="没有可用候选"
              message="生成任务已完成，但没有候选结构通过校验。"
              actionLabel="重新生成"
              onAction={onRetry}
            />
          ) : null}
          {!isLoading && !error && !data && !completedWithoutData && runtimeUnavailable ? (
            <DrawerState
              icon={<CircleAlert />}
              tone="danger"
              title="模型运行时不可用"
              message={status?.message ?? "当前输入不会丢失，恢复运行时后可直接重试。"}
              actionLabel="重新检查"
              onAction={onRefreshRuntime}
            />
          ) : null}
          {!isLoading && !error && !data && !completedWithoutData && !runtimeUnavailable ? (
            <DrawerState
              icon={<Sparkles />}
              title="等待生成聚合物"
              message="打开右上角参数配置，确认 15 项目标特征后开始生成。"
            />
          ) : null}
        </div>

        <footer className="polytao-drawer-footer">
          <span>候选 {data?.returned_count ?? job?.returned_count ?? 0} / {requestedCount}</span>
          <span>{status?.model_id ?? "PolyTAO"}</span>
        </footer>
      </aside>
    </>
  );
}

function jobStatusLabel(status: PolytaoJobStatusResponse["status"] | undefined) {
  switch (status) {
    case "pending":
      return "等待中";
    case "submitted":
      return "已提交";
    case "running":
      return "正在生成";
    case "completed":
      return "生成完成";
    case "failed":
      return "作业失败";
    case "cancelled":
      return "已取消";
    default:
      return "正在提交";
  }
}

function DrawerProgress({ job }: { job: PolytaoJobStatusResponse | null }) {
  const progress = clamp(job?.progress_percent ?? 0, 0, 100);
  return (
    <div className="polytao-drawer-empty-shell">
      <div className="polytao-progress-card">
        <div className="polytao-progress-head">
          <span className="polytao-progress-stage">
            <LoaderCircle className="polytao-spinner" />
            {jobStatusLabel(job?.status)}
          </span>
          <span className="polytao-progress-percent">{Math.round(progress)}%</span>
        </div>
        <div className="polytao-progress-track">
          <div className="polytao-progress-fill" style={{ width: `${progress}%` }} />
        </div>
        <div className="polytao-progress-message">
          {job?.progress_message ?? "正在创建 PolyTAO 生成任务。"}
        </div>
      </div>
    </div>
  );
}

function DrawerState({
  icon,
  tone,
  title,
  message,
  actionLabel,
  onAction
}: {
  icon: React.ReactNode;
  tone?: "danger" | "warning";
  title: string;
  message: string;
  actionLabel?: string;
  onAction?: () => void;
}) {
  return (
    <div className="polytao-drawer-empty-shell">
      <div className="polytao-state-panel">
        <div className="polytao-state-content">
          <span className={`polytao-state-icon${tone ? ` is-${tone}` : ""}`}>{icon}</span>
          <h3>{title}</h3>
          <p>{message}</p>
          {actionLabel && onAction ? (
            <button className="polytao-secondary-button" type="button" onClick={onAction}>
              <RefreshCw />
              {actionLabel}
            </button>
          ) : null}
        </div>
      </div>
    </div>
  );
}

function CandidateCard({ candidate, onCopy }: { candidate: PolytaoCandidate; onCopy: (value: string) => void }) {
  const [smilesExpanded, setSmilesExpanded] = useState(false);

  return (
    <article className="polytao-drawer-result-card" aria-labelledby={`polytao-candidate-${candidate.rank}`}>
      <div className="polytao-drawer-result-head">
        <div className="polytao-candidate-identity">
          <span className="polytao-candidate-number">{String(candidate.rank).padStart(2, "0")}</span>
          <div>
            <strong id={`polytao-candidate-${candidate.rank}`}>候选结构</strong>
            <small>CANDIDATE {String(candidate.rank).padStart(2, "0")}</small>
          </div>
        </div>
        <div className="polytao-score-meter">
          <span>SA</span>
          <strong>{candidate.sa_score == null ? "—" : candidate.sa_score.toFixed(2)}</strong>
        </div>
      </div>
      <div className="polytao-candidate-visual">
        {candidate.structure_svg ? (
          <StructureSvg
            svg={candidate.structure_svg}
            alt={`PolyTAO 候选结构 ${candidate.rank}`}
            className="polytao-candidate-svg"
            imageClassName="polytao-candidate-svg-image"
          />
        ) : (
          <div className="polytao-candidate-fallback">{candidate.generated_smiles}</div>
        )}
      </div>
      <div className="polytao-drawer-result-smiles">
        <button
          className="polytao-result-smiles-toggle"
          type="button"
          aria-expanded={smilesExpanded}
          aria-controls={`polytao-candidate-${candidate.rank}-smiles`}
          aria-label={`${smilesExpanded ? "收起" : "展开"}候选 ${candidate.rank} SMILES`}
          onClick={() => setSmilesExpanded((expanded) => !expanded)}
        >
          <span>SMILES</span>
          <span>
            {smilesExpanded ? "收起" : "展开"}
            <ChevronDown />
          </span>
        </button>
        {smilesExpanded ? (
          <div className="polytao-result-smiles-content" id={`polytao-candidate-${candidate.rank}-smiles`}>
            <code>{candidate.generated_smiles}</code>
            <button
              className="polytao-result-copy-button"
              type="button"
              aria-label={`复制候选 ${candidate.rank} SMILES`}
              onClick={() => onCopy(candidate.generated_smiles)}
            >
              <Copy />
            </button>
          </div>
        ) : null}
      </div>
    </article>
  );
}
