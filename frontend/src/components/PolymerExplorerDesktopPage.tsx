import {
  BarChart3,
  Box,
  ChevronDown,
  Copy,
  Eraser,
  ImagePlus,
  LoaderCircle,
  RefreshCcw,
  Search,
  Settings,
  SlidersHorizontal,
  Sparkles,
  Trash2,
  X
} from "lucide-react";
import { useEffect, useMemo, useRef, useState, type MouseEvent as ReactMouseEvent, type RefObject } from "react";
import { recognizeStructureImage } from "../services/api";
import type { PolymerResult, PredictableProperty, PredictResponse, SmilesQueryRequest, SmilesQueryResponse } from "../types";
import { StructurePreview3D } from "./StructurePreview3D";
import { StructureSvg } from "./StructureSvg";
import "../styles/polymer-desktop.css";

type ExplorerMode = "similarity" | "property" | "predict";
type SortOrder = "desc" | "asc";
type KetcherApi = NonNullable<Window["ketcher"]>;
type KetcherSnapshot = { smiles: string; molfile: string };

type PolymerExplorerDesktopPageProps = {
  smiles: string;
  setSmiles: (value: string) => void;
  iframeRef: RefObject<HTMLIFrameElement | null>;
  setIsReady: (ready: boolean) => void;
  getCurrentSmiles: () => Promise<string>;
  request: SmilesQueryRequest;
  setRequest: (request: SmilesQueryRequest) => void;
  isQueryLoading: boolean;
  queryError: string | null;
  queryData: SmilesQueryResponse | null;
  submitQuery: (request?: SmilesQueryRequest) => Promise<void>;
  predict: {
    isLoading: boolean;
    error: string | null;
    data: PredictResponse | null;
    submit: (request: { smiles: string; properties: PredictableProperty[] }) => Promise<PredictResponse>;
  };
  selectedProperties: PredictableProperty[];
  setSelectedProperties: (properties: PredictableProperty[]) => void;
};

type PropertyOption = { key: PredictableProperty; label: string; unit: string; shortLabel: string };

const PROPERTY_OPTIONS: PropertyOption[] = [
  { key: "Glass transition temperature", label: "玻璃化转变温度", unit: "°C", shortLabel: "Tg" },
  { key: "Melting temperature", label: "熔融温度", unit: "°C", shortLabel: "Tm" },
  { key: "Thermal decomposition temperature", label: "热分解温度", unit: "°C", shortLabel: "Td" },
  { key: "Thermal decomposition weight loss", label: "热分解失重率", unit: "%", shortLabel: "Wloss" },
  { key: "Elongation at break", label: "断裂伸长率", unit: "%", shortLabel: "ε" },
  { key: "Tensile stress strength at break", label: "断裂拉伸强度", unit: "MPa", shortLabel: "σ" },
  { key: "O2 Permeability Barrer", label: "O2 渗透率", unit: "Barrer", shortLabel: "PO2" },
  { key: "Co2 Permeability Barrer", label: "CO2 渗透率", unit: "Barrer", shortLabel: "PCO2" },
  { key: "H2 Permeability Barrer", label: "H2 渗透率", unit: "Barrer", shortLabel: "PH2" }
];

const DEFAULT_PROPERTY = PROPERTY_OPTIONS[0].key;
const modeConfig: Record<ExplorerMode, { label: string }> = {
  similarity: { label: "结构相似探索" },
  property: { label: "性能相似探索" },
  predict: { label: "性能预测探索" }
};

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}

function getPropertyOption(key: PredictableProperty | null | undefined) {
  return PROPERTY_OPTIONS.find((item) => item.key === key) ?? PROPERTY_OPTIONS[0];
}

function ModeIcon({ mode }: { mode: ExplorerMode }) {
  if (mode === "predict") return <BarChart3 width={12} height={12} />;
  if (mode === "property") return <SlidersHorizontal width={12} height={12} />;
  return <Search width={12} height={12} />;
}

function formatOrigin(value: string | null | undefined) {
  const normalized = value?.trim().toLowerCase();
  if (!normalized || normalized === "n/a" || normalized === "na") return "未标注";
  if (normalized === "exp" || normalized === "experimental") return "实验";
  if (normalized === "sim" || normalized === "simulated") return "模拟";
  return value?.trim() || "未标注";
}

function formatNumber(value: number | null | undefined, digits = 3) {
  if (value === null || value === undefined || Number.isNaN(value)) return "暂无";
  return value.toFixed(digits);
}

function wildcardCount(value: string) {
  return value.match(/\*/g)?.length ?? 0;
}

function shouldAdoptEditorSmiles(sourceSmiles: string, editorSmiles: string) {
  const sourceWildcardCount = wildcardCount(sourceSmiles);
  if (sourceWildcardCount === 0) return editorSmiles.trim().length > 0;
  return wildcardCount(editorSmiles) >= sourceWildcardCount;
}

function getEditorLoadCandidates(sourceSmiles: string) {
  const normalized = sourceSmiles.trim();
  const stripped = normalized.replace(/\*/g, "").trim();
  return Array.from(new Set([normalized, stripped])).filter(Boolean);
}

function sourceFromResult(result: PolymerResult, mode: ExplorerMode) {
  if (mode === "property") return formatOrigin(result.matched_property_source);
  const sources = Object.values(result.properties)
    .flat()
    .map((item) => formatOrigin(item.label_source))
    .filter((item) => item !== "未标注");
  return Array.from(new Set(sources)).join(" / ") || "未标注";
}

function ResultSkeleton() {
  return (
    <div className="results-skeleton">
      <div className="skeleton-card-container" style={{ padding: 12 }}>
        <div className="skeleton-header">
          <div className="skeleton-block title-block shimmer" style={{ width: "60%" }} />
          <div className="skeleton-block desc-block shimmer" style={{ width: "85%" }} />
        </div>
        <div className="skeleton-grid" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <div className="skeleton-item shimmer" style={{ height: 60, width: "100%" }} />
          <div className="skeleton-item shimmer" style={{ height: 60, width: "100%" }} />
          <div className="skeleton-item shimmer" style={{ height: 60, width: "100%" }} />
        </div>
      </div>
    </div>
  );
}

function ResultsEmptyState({ title, description }: { title: string; description: string }) {
  return (
    <div className="results-empty-state">
      <div className="empty-graphic"><Sparkles width={48} height={48} strokeWidth={1.5} /></div>
      <h3>{title}</h3>
      <p>{description}</p>
    </div>
  );
}

function ErrorState({ message }: { message: string }) {
  return (
    <div className="results-empty-state">
      <div className="empty-graphic polymer-error-graphic"><X width={42} height={42} strokeWidth={1.5} /></div>
      <h3>请求未完成</h3>
      <p>{message}</p>
    </div>
  );
}
function SimilarityResultCard({ result, mode, selectedProperty }: { result: PolymerResult; mode: ExplorerMode; selectedProperty: PredictableProperty }) {
  const displaySmiles = result.canonical_smiles || result.smiles;
  const property = getPropertyOption(selectedProperty);
  const value =
    mode === "property"
      ? result.matched_property_value !== null
        ? `${Number(result.matched_property_value).toPrecision(6)} ${result.matched_property_unit || property.unit}`
        : "暂无"
      : formatNumber(result.similarity_score, 3);
  const label = mode === "property" ? property.shortLabel : "相似度";
  const source = sourceFromResult(result, mode);

  return (
    <div className="similarity-result-card" data-smiles={displaySmiles}>
      <div className="flip-card-inner">
        <div className="flip-card-front">
          <div className="card-top-info"><span className="card-title-2d">2D STRUCTURE</span></div>
          <div className="result-structure-img">
            {result.structure_svg ? <StructureSvg svg={result.structure_svg} alt={`2D structure for ${displaySmiles}`} className="polymer-result-svg" imageClassName="h-full max-h-full" /> : <div className="polymer-result-smiles-fallback">{displaySmiles}</div>}
          </div>
          <div className="result-smiles-box">{displaySmiles}</div>
          <div className="card-bottom-row">
            <div className="bottom-prop-group"><span className="bottom-prop-label">{label}</span><span className="bottom-prop-val">{value}</span></div>
            <div className="bottom-source-group"><span className="bottom-source-label">数据来源</span><span className="bottom-source-val">{source}</span></div>
          </div>
        </div>
      </div>
    </div>
  );
}

function PredictionCards({ data }: { data: PredictResponse }) {
  const entries = PROPERTY_OPTIONS.filter((property) => data.predictions[property.key] !== undefined).map((property) => ({ ...property, value: data.predictions[property.key] ?? 0 }));
  if (entries.length === 0) return <ResultsEmptyState title="暂无预测结果" description="服务未返回所选性能的预测值。" />;

  return (
    <div className="prediction-grid-wrapper">
      {entries.map((entry) => (
        <div className="pred-card-v2" key={entry.key}>
          <div className="pred-card-header"><span className="pred-type-badge">PREDICTION</span><span className="pred-name-label">{entry.label}</span></div>
          <div className="pred-card-body">
            <div className="pred-val-line"><span className="pred-number-val">{entry.value.toFixed(2)}</span><span className="pred-unit-capsule">{entry.unit}</span></div>
            <div className="pred-widget-area"><div className="temp-track-wrapper"><div className="temp-gradient-track" /><div className="temp-pointer" style={{ left: `${clamp(Math.abs(entry.value) % 100, 8, 92)}%` }} /></div></div>
          </div>
          <div className="pred-card-footer"><span>模型输出</span><span>{data.query_time_ms.toFixed(1)} ms</span></div>
        </div>
      ))}
    </div>
  );
}

export function PolymerExplorerDesktopPage({
  smiles,
  setSmiles,
  iframeRef,
  setIsReady,
  getCurrentSmiles,
  request,
  setRequest,
  isQueryLoading,
  queryError,
  queryData,
  submitQuery,
  predict,
  selectedProperties,
  setSelectedProperties
}: PolymerExplorerDesktopPageProps) {
  const [mode, setMode] = useState<ExplorerMode>("similarity");
  const [activeRunMode, setActiveRunMode] = useState<ExplorerMode>("similarity");
  const [isDropdownOpen, setIsDropdownOpen] = useState(false);
  const [submenuMode, setSubmenuMode] = useState<"property" | "predict" | null>(null);
  const [selectedProperty, setSelectedProperty] = useState<PredictableProperty>(DEFAULT_PROPERTY);
  const [isFlipped, setIsFlipped] = useState(false);
  const [isFlipping, setIsFlipping] = useState(false);
  const [isAnalysisOpen, setIsAnalysisOpen] = useState(false);
  const [hasAnalysisRun, setHasAnalysisRun] = useState(false);
  const [panelWidth, setPanelWidth] = useState(380);
  const [sortOrder, setSortOrder] = useState<SortOrder>("desc");
  const [isImportingImage, setIsImportingImage] = useState(false);
  const [isSyncing, setIsSyncing] = useState(false);
  const [isClearing, setIsClearing] = useState(false);
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");
  const [feedback, setFeedback] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const debounceTimerRef = useRef<number | null>(null);
  const hasSmiles = smiles.trim().length > 0;
  const isRunning = isQueryLoading || predict.isLoading || isImportingImage || isSyncing || isClearing;
  const predictProperties = selectedProperties.length > 0 ? selectedProperties : [DEFAULT_PROPERTY];
  const activeProperty = getPropertyOption(selectedProperty);

  useEffect(() => {
    let cancelled = false;
    let attempts = 0;
    const timer = window.setInterval(() => {
      attempts += 1;
      const ketcher = iframeRef.current?.contentWindow?.ketcher;
      if (ketcher) {
        if (!cancelled) setIsReady(true);
        window.clearInterval(timer);
      }
      if (attempts >= 50) window.clearInterval(timer);
    }, 300);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [iframeRef, setIsReady]);

  useEffect(() => {
    const sourceSmiles = smiles.trim();
    if (!sourceSmiles) return;

    let cancelled = false;
    let attempts = 0;
    let isChecking = false;

    const timer = window.setInterval(() => {
      if (isChecking) return;
      attempts += 1;
      const ketcher = iframeRef.current?.contentWindow?.ketcher;
      if (!ketcher || typeof ketcher.getSmiles !== "function" || typeof ketcher.setMolecule !== "function") {
        if (attempts >= 50) window.clearInterval(timer);
        return;
      }

      isChecking = true;
      void (async () => {
        const editorSmiles = (await ketcher.getSmiles()).trim();
        if (!editorSmiles) {
          const { adoptedSmiles } = await loadBestEffortStructureToEditor(ketcher, sourceSmiles);
          if (!cancelled && adoptedSmiles && adoptedSmiles !== sourceSmiles) {
            setSmiles(adoptedSmiles);
            setRequest({ ...request, smiles: adoptedSmiles });
          }
        }
        if (!cancelled) setIsReady(true);
        window.clearInterval(timer);
      })().catch((error) => {
        console.error("Failed to hydrate empty Ketcher editor from shared SMILES", error);
        if (attempts >= 50) {
          window.clearInterval(timer);
          return;
        }
        isChecking = false;
      });
    }, 300);

    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [iframeRef, request, setIsReady, setRequest, setSmiles, smiles]);

  useEffect(() => {
    return () => {
      if (debounceTimerRef.current !== null) window.clearTimeout(debounceTimerRef.current);
    };
  }, []);

  function getKetcher() { return iframeRef.current?.contentWindow?.ketcher; }
  function refreshKetcherFrame() {
    const frameWindow = iframeRef.current?.contentWindow;
    if (!frameWindow) return;
    const FrameEvent = (frameWindow as Window & typeof globalThis).Event;
    frameWindow.dispatchEvent(new FrameEvent("resize"));
    frameWindow.scrollTo(0, 0);
  }
  async function waitForKetcherCommit() {
    await new Promise((resolve) => window.setTimeout(resolve, 80));
    refreshKetcherFrame();
    await new Promise((resolve) => window.setTimeout(resolve, 80));
  }
  async function waitForKetcher(timeoutMs = 4000): Promise<KetcherApi | null> {
    const startedAt = Date.now();
    while (Date.now() - startedAt < timeoutMs) {
      const ketcher = getKetcher();
      if (ketcher && typeof ketcher.setMolecule === "function") return ketcher;
      await new Promise((resolve) => window.setTimeout(resolve, 120));
    }
    return null;
  }
  async function readEditorSmiles(ketcher: KetcherApi) {
    if (typeof ketcher.getSmiles !== "function") throw new Error("结构编辑器无法返回 SMILES。");
    const editorSmiles = (await ketcher.getSmiles()).trim();
    return editorSmiles && editorSmiles !== "{}" ? editorSmiles : "";
  }
  async function readEditorMolfile(ketcher: KetcherApi) {
    if (typeof ketcher.getMolfile !== "function") return "";
    return (await ketcher.getMolfile()).trim();
  }
  async function captureEditorSnapshot(ketcher: KetcherApi): Promise<KetcherSnapshot> {
    const fallbackSmiles = smiles.trim();
    let editorSmiles = fallbackSmiles;
    try {
      editorSmiles = (await readEditorSmiles(ketcher)) || fallbackSmiles;
    } catch {
      editorSmiles = fallbackSmiles;
    }

    let molfile = "";
    try {
      molfile = await readEditorMolfile(ketcher);
    } catch {
      molfile = "";
    }

    return { smiles: editorSmiles, molfile };
  }
  async function waitForEditorSmilesState(ketcher: KetcherApi, matches: (value: string) => boolean, timeoutMs = 1200) {
    const startedAt = Date.now();
    let latest = await readEditorSmiles(ketcher);
    while (Date.now() - startedAt < timeoutMs) {
      if (matches(latest)) return latest;
      await new Promise((resolve) => window.setTimeout(resolve, 80));
      latest = await readEditorSmiles(ketcher);
    }
    return latest;
  }
  async function writeStructureToEditor(ketcher: KetcherApi, structure: string, fallbackSmiles: string) {
    if (typeof ketcher.setMolecule !== "function") throw new Error("结构编辑器无法加载分子。");
    await ketcher.setMolecule(structure);
    await waitForKetcherCommit();
    if (typeof ketcher.getSmiles !== "function") return fallbackSmiles;
    return (await ketcher.getSmiles()).trim();
  }
  async function clearEditorForImageImport(ketcher: KetcherApi) {
    if (typeof ketcher.clear === "function") await ketcher.clear();
    else if (typeof ketcher.setMolecule === "function") await ketcher.setMolecule("");
    else throw new Error("结构编辑器无法清空画布。");
    await waitForKetcherCommit();

    let clearedSmiles = await waitForEditorSmilesState(ketcher, (value) => !value);
    if (clearedSmiles && typeof ketcher.setMolecule === "function") {
      await ketcher.setMolecule("");
      await waitForKetcherCommit();
      clearedSmiles = await waitForEditorSmilesState(ketcher, (value) => !value);
    }
    if (clearedSmiles) throw new Error("旧画布未能清空，图片结构未写入。保存当前结构后请手动清空画布再重试。");
  }
  async function writeImageStructureToEditor(ketcher: KetcherApi, structure: string) {
    if (typeof ketcher.setMolecule !== "function") throw new Error("结构编辑器无法加载分子。因无法确认画布写入结果，本次图片导入已取消。");
    await clearEditorForImageImport(ketcher);
    await ketcher.setMolecule(structure);
    await waitForKetcherCommit();
    const editorSmiles = await waitForEditorSmilesState(ketcher, (value) => Boolean(value), 1800);
    if (!editorSmiles) throw new Error("Ketcher 未接受识别出的结构，图片结构未写入画布。");
    return editorSmiles;
  }
  async function restoreEditorSnapshot(ketcher: KetcherApi, snapshot: KetcherSnapshot) {
    const restoreStructure = snapshot.molfile || snapshot.smiles;
    if (!restoreStructure) {
      if (typeof ketcher.clear === "function") await ketcher.clear();
      else if (typeof ketcher.setMolecule === "function") await ketcher.setMolecule("");
      await waitForKetcherCommit();
      updateSmiles("");
      return;
    }
    if (typeof ketcher.setMolecule !== "function") throw new Error("结构编辑器无法恢复原画布。");

    await ketcher.setMolecule(restoreStructure);
    await waitForKetcherCommit();

    let restoredSmiles = snapshot.smiles;
    try {
      const editorSmiles = await waitForEditorSmilesState(ketcher, (value) => Boolean(value));
      restoredSmiles = snapshot.smiles && !shouldAdoptEditorSmiles(snapshot.smiles, editorSmiles) ? snapshot.smiles : editorSmiles || snapshot.smiles;
    } catch {
      restoredSmiles = snapshot.smiles;
    }
    updateSmiles(restoredSmiles);
  }
  async function loadBestEffortStructureToEditor(ketcher: KetcherApi, sourceSmiles: string) {
    const normalizedSmiles = sourceSmiles.trim();
    let lastError: unknown = null;
    for (const candidate of getEditorLoadCandidates(normalizedSmiles)) {
      try {
        const editorSmiles = await writeStructureToEditor(ketcher, candidate, normalizedSmiles);
        if (editorSmiles) {
          return {
            editorSmiles,
            adoptedSmiles: shouldAdoptEditorSmiles(normalizedSmiles, editorSmiles) ? editorSmiles : normalizedSmiles
          };
        }
      } catch (error) {
        lastError = error;
      }
    }
    throw lastError instanceof Error ? lastError : new Error("Ketcher 未接受识别出的结构。");
  }
  async function loadStructureIntoKetcher(molfile: string, smilesValue: string, activeKetcher?: KetcherApi) {
    const ketcher = activeKetcher ?? await waitForKetcher();
    if (!ketcher) {
      setIsReady(false);
      throw new Error("结构编辑器尚未就绪，请稍后重试。");
    }
    const normalizedSmiles = smilesValue.trim();
    if (molfile.trim()) {
      try {
        const editorSmiles = await writeImageStructureToEditor(ketcher, molfile);
        if (editorSmiles) return shouldAdoptEditorSmiles(normalizedSmiles, editorSmiles) ? editorSmiles : normalizedSmiles || editorSmiles;
      } catch (error) {
        console.error("Failed to load recognized molfile into Ketcher", error);
        if (!normalizedSmiles) throw error;
      }
    }
    let lastError: unknown = null;
    for (const candidate of getEditorLoadCandidates(normalizedSmiles)) {
      try {
        const editorSmiles = await writeImageStructureToEditor(ketcher, candidate);
        return shouldAdoptEditorSmiles(normalizedSmiles, editorSmiles) ? editorSmiles : normalizedSmiles || editorSmiles;
      } catch (error) {
        lastError = error;
      }
    }
    throw lastError instanceof Error ? lastError : new Error("Ketcher 未接受识别出的结构。");
  }

  function updateSmiles(value: string) {
    setSmiles(value);
    setRequest({ ...request, smiles: value });
  }
  function scheduleLoadSmiles(value: string) {
    if (debounceTimerRef.current !== null) window.clearTimeout(debounceTimerRef.current);
    debounceTimerRef.current = window.setTimeout(() => {
      const ketcher = getKetcher();
      const sourceSmiles = value.trim();
      if (!ketcher || typeof ketcher.setMolecule !== "function" || !sourceSmiles) return;
      void loadBestEffortStructureToEditor(ketcher, sourceSmiles).catch(() => undefined);
    }, 300);
  }
  async function syncSmilesFromCanvas(options: { preserveExisting?: boolean } = {}) {
    const ketcher = getKetcher();
    if (!ketcher || typeof ketcher.getSmiles !== "function") {
      setIsReady(false);
      setFeedback("结构编辑器尚未就绪。");
      return options.preserveExisting ? smiles.trim() : "";
    }
    setIsSyncing(true);
    try {
      const fallbackSmiles = smiles.trim();
      const editorSmiles = (await ketcher.getSmiles()).trim();
      const canvasSmiles = editorSmiles && editorSmiles !== "{}" ? editorSmiles : "";
      if (canvasSmiles) {
        const nextSmiles = shouldAdoptEditorSmiles(fallbackSmiles, canvasSmiles) ? canvasSmiles : fallbackSmiles || canvasSmiles;
        updateSmiles(nextSmiles);
        setIsReady(true);
        setFeedback(nextSmiles === canvasSmiles ? "SMILES 已从画布同步。" : "已保留当前聚合物 SMILES。");
        return nextSmiles;
      }
      if (options.preserveExisting && fallbackSmiles) {
        setIsReady(true);
        setFeedback("画布暂无结构，已使用当前 SMILES。");
        return fallbackSmiles;
      }
      updateSmiles("");
      setIsReady(true);
      setFeedback("画布暂无可同步结构。");
      return "";
    } catch (error) {
      console.error("Failed to sync SMILES from Ketcher", error);
      setFeedback("SMILES 同步失败。");
      return options.preserveExisting ? smiles.trim() : "";
    } finally {
      setIsSyncing(false);
    }
  }
  async function clearCanvas() {
    setIsClearing(true);
    try {
      const ketcher = getKetcher();
      if (ketcher && typeof ketcher.clear === "function") await ketcher.clear();
      else if (ketcher && typeof ketcher.setMolecule === "function") await ketcher.setMolecule("");
      updateSmiles("");
      setIsFlipped(false);
      setIsAnalysisOpen(false);
      setFeedback("画布已清空。");
    } catch (error) {
      console.error("Failed to clear Ketcher canvas", error);
      setFeedback("画布清空失败。");
    } finally {
      setIsClearing(false);
    }
  }
  async function copySmiles() {
    const value = smiles.trim();
    if (!value) return;
    try {
      await navigator.clipboard.writeText(value);
      setCopyState("copied");
      window.setTimeout(() => setCopyState("idle"), 1200);
    } catch (error) {
      console.error("Failed to copy SMILES", error);
      setCopyState("failed");
      window.setTimeout(() => setCopyState("idle"), 1200);
    }
  }
  async function importImageFile(file: File) {
    if (!file.type.startsWith("image/")) {
      setFeedback("请选择图片文件。");
      return;
    }
    let previousSnapshot: KetcherSnapshot | null = null;
    const previousFlipped = isFlipped;
    setIsImportingImage(true);
    setFeedback("正在识别结构图片...");
    try {
      const result = await recognizeStructureImage(file);
      const molfile = result.molfile?.trim() ?? "";
      const recognizedSmiles = result.smiles.trim();
      if (!molfile && !recognizedSmiles) throw new Error("识别结果未返回结构。");
      const ketcher = await waitForKetcher();
      if (!ketcher) {
        setIsReady(false);
        throw new Error("结构编辑器尚未就绪，请稍后重试。");
      }
      previousSnapshot = await captureEditorSnapshot(ketcher);
      setIsFlipped(false);
      setFeedback("正在写入结构画布...");
      const nextSmiles = await loadStructureIntoKetcher(molfile, recognizedSmiles, ketcher);
      updateSmiles(nextSmiles);
      setIsReady(true);
      setFeedback(result.warnings.length > 0 ? `图片结构已导入：${result.warnings[0]}` : "图片结构已导入。");
    } catch (error) {
      console.error("Failed to import structure image", error);
      const message = error instanceof Error ? error.message : "图片导入失败。";
      if (previousSnapshot) {
        const ketcher = await waitForKetcher(1000);
        if (ketcher) {
          try {
            await restoreEditorSnapshot(ketcher, previousSnapshot);
            setIsFlipped(previousFlipped);
            setIsReady(true);
            setFeedback(`${message} 已恢复原画布。`);
          } catch (restoreError) {
            console.error("Failed to restore Ketcher canvas after image import failure", restoreError);
            setFeedback(`${message} 原画布恢复失败，请手动检查。`);
          }
        } else {
          setFeedback(`${message} 结构编辑器不可用，无法恢复原画布。`);
        }
      } else {
        setFeedback(message);
      }
    } finally {
      setIsImportingImage(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  }
  async function toggle3D() {
    if (isFlipping || isSyncing || isImportingImage || isClearing) return;
    setIsFlipping(true);
    try {
      if (!isFlipped) {
        const nextSmiles = await syncSmilesFromCanvas({ preserveExisting: true });
        if (!nextSmiles) {
          setFeedback("请先绘制、导入或输入 SMILES 结构。");
          return;
        }
      }
      setIsFlipped((current) => !current);
    } finally {
      window.setTimeout(() => setIsFlipping(false), 620);
    }
  }
  async function resolveSmilesForRun(currentSmiles: string) {
    if (wildcardCount(currentSmiles) > 0) return currentSmiles;
    const synchronizedSmiles = (await getCurrentSmiles()) || currentSmiles;
    return shouldAdoptEditorSmiles(currentSmiles, synchronizedSmiles) ? synchronizedSmiles : currentSmiles;
  }
  async function runMode(nextMode: ExplorerMode = mode) {
    setMode(nextMode);
    setIsDropdownOpen(false);
    setSubmenuMode(null);
    let currentSmiles = smiles.trim();
    if (!currentSmiles) currentSmiles = await syncSmilesFromCanvas();
    if (!currentSmiles) {
      setFeedback("请先绘制、导入或输入 SMILES 结构。");
      return;
    }
    const standardizedSmiles = await resolveSmilesForRun(currentSmiles);
    setHasAnalysisRun(true);
    setIsAnalysisOpen(true);
    setActiveRunMode(nextMode);
    if (nextMode === "predict") {
      try {
        await predict.submit({ smiles: standardizedSmiles, properties: predictProperties });
      } catch {
        // The hook owns the error state and the panel renders it.
      }
      return;
    }
    const nextRequest: SmilesQueryRequest = {
      ...request,
      smiles: standardizedSmiles,
      match_mode: nextMode === "property" ? "property" : "structure",
      property_name: nextMode === "property" ? selectedProperty : null
    };
    setRequest(nextRequest);
    await submitQuery(nextRequest);
  }
  function togglePredictProperty(property: PredictableProperty) {
    if (selectedProperties.includes(property)) {
      const next = selectedProperties.filter((item) => item !== property);
      if (next.length > 0) setSelectedProperties(next);
      return;
    }
    setSelectedProperties([...selectedProperties, property]);
  }
  function startResize(event: ReactMouseEvent<HTMLDivElement>) {
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = panelWidth;
    function handleMove(moveEvent: MouseEvent) {
      setPanelWidth(clamp(startWidth + startX - moveEvent.clientX, 320, 560));
    }
    function handleUp() {
      document.removeEventListener("mousemove", handleMove);
      document.removeEventListener("mouseup", handleUp);
    }
    document.addEventListener("mousemove", handleMove);
    document.addEventListener("mouseup", handleUp);
  }

  const sortedResults = useMemo(() => {
    const results = queryData?.results ?? [];
    return [...results].sort((a, b) => {
      const left = a.similarity_score ?? -1;
      const right = b.similarity_score ?? -1;
      return sortOrder === "desc" ? right - left : left - right;
    });
  }, [queryData?.results, sortOrder]);

  const mainButtonLabel =
    mode === "predict" ? `${modeConfig[mode].label} (${predictProperties.length})` : mode === "property" ? `${modeConfig[mode].label} (1)` : modeConfig[mode].label;
  const panelShowsQuery = activeRunMode !== "predict";
  const showSort = panelShowsQuery && queryData && queryData.total > 0 && !isQueryLoading && !queryError;

  return (
    <div className="polymer-desktop-page polymer-desktop-page--embedded">
      <h1 className="polymer-page-title">聚合物性能探索</h1>
      <div className="polymer-centered-shell">
        <div className="polymer-centered-column">
          <div className="app-container">
            <div className="main-layout">
          <main className="main-content">
            <div className="polymer-module-header">
              <div className="header-actions" id="polymer-header-actions">
                <input ref={fileInputRef} type="file" accept="image/*" style={{ display: "none" }} onChange={(event) => { const file = event.currentTarget.files?.[0]; if (file) void importImageFile(file); }} />
                <button className="btn btn--outline btn--sm" id="btn-import-img" type="button" onClick={() => fileInputRef.current?.click()}>
                  {isImportingImage ? <LoaderCircle width={14} height={14} className="spin-icon" /> : <ImagePlus width={14} height={14} />}
                  <span>导入图片</span>
                </button>
                <button className="btn btn--outline btn--sm" id="btn-clear-canvas" type="button" onClick={() => void clearCanvas()}>
                  {isClearing ? <LoaderCircle width={14} height={14} className="spin-icon" /> : <Eraser width={14} height={14} />}
                  <span>清空画布</span>
                </button>
                <button className="btn btn--outline btn--sm" id="btn-sync-canvas" type="button" onClick={() => void syncSmilesFromCanvas()}>
                  {isSyncing ? <LoaderCircle width={14} height={14} className="spin-icon" /> : <RefreshCcw width={14} height={14} />}
                  <span>生成SMILES</span>
                </button>
                <button className="btn btn--outline btn--sm" id="btn-toggle-3d" type="button" title="在 2D 画布与 3D 构象之间翻转切换" onClick={() => void toggle3D()} disabled={isFlipping || isSyncing || isImportingImage || isClearing}>
                  <Box width={14} height={14} />
                  <span>{isFlipped ? "2D画布" : "3D构象"}</span>
                </button>
              </div>
            </div>

            <div className="polymer-workspace">
              <div className="workspace-left">
                <div className={`workspace-card editor-card flip-card-container${isFlipped ? " is-flipped" : ""}${isFlipping ? " is-flipping" : ""}`} id="flip-card-container">
                  <div className="flip-card-inner">
                    <div className="flip-card-front"><div className="editor-iframe-container"><iframe title="Ketcher 编辑器" src="/ketcher/index.html" id="ketcher-iframe" ref={iframeRef} className="ketcher-iframe" /></div></div>
                    <div className="flip-card-back">
                      <div className="canvas-3d-container-full real-3d-container" id="canvas-container">
                        <StructurePreview3D smiles={smiles} variant="bare" visualStyle="polished-atoms" className="polymer-real-3d-preview" contentClassName="polymer-real-3d-content" previewClassName="polymer-real-3d-frame" viewerClassName="polymer-real-3d-viewer" />
                        <div className="canvas-tip">真实 3D 构象，支持拖拽旋转与缩放</div>
                      </div>
                    </div>
                  </div>
                </div>

                <div className={`smiles-smart-capsule${hasSmiles ? " has-content" : ""}`} id="smiles-smart-capsule">
                  <div className="smiles-textarea-floating-actions">
                    <button className={`smiles-floating-action-btn btn-copy${copyState === "copied" ? " copied" : ""}`} id="btn-copy-smiles" type="button" title="一键复制 SMILES 文本" onClick={() => void copySmiles()} disabled={!hasSmiles}><Copy width={13} height={13} /></button>
                    <button className="smiles-floating-action-btn btn-clear" id="btn-clear-smiles-input" type="button" title="清空输入内容" onClick={() => updateSmiles("")} disabled={!hasSmiles}><Trash2 width={13} height={13} /></button>
                  </div>
                  <textarea
                    className="smiles-textarea"
                    id="smiles-input-field"
                    value={smiles}
                    placeholder="输入高分子单体 SMILES 结构式（例如：*CC*、CCO），或在上方画板绘制后点击同步"
                    onChange={(event) => { updateSmiles(event.target.value); scheduleLoadSmiles(event.target.value); }}
                  />
                  <div className="capsule-toolbar">
                    <div className="toolbar-actions-group">
                      <div className={`split-button-container${isDropdownOpen ? " menu-open" : ""}${submenuMode ? " submenu-open" : ""}${isRunning ? " is-calculating" : ""}${!hasSmiles ? " is-disabled" : ""}`} id="split-button-container">
                        <button className="btn-generate-main" id="btn-generate" type="button" disabled={!hasSmiles || isRunning} onClick={() => void runMode()} title="开始计算当前模式">
                          {isRunning ? <span className="dots-loader"><span className="dot dot-1" /><span className="dot dot-2" /><span className="dot dot-3" /></span> : <><span className="btn-icon"><ModeIcon mode={mode} /></span><span className="btn-text">{mainButtonLabel}</span></>}
                        </button>
                        <button className="btn-generate-dropdown" id="btn-generate-dropdown" type="button" disabled={!hasSmiles || isRunning} title="选择探索模式" onClick={(event) => { event.stopPropagation(); setIsDropdownOpen((current) => !current); setSubmenuMode(null); }}><ChevronDown width={10} height={10} /></button>
                        <div className="generate-dropdown-menu" id="btn-generate-dropdown-menu" style={{ display: isDropdownOpen ? "flex" : "none" }}>
                          {(["similarity", "property", "predict"] as ExplorerMode[]).map((item) => (
                            <div key={item} className={`dropdown-item${mode === item ? " active" : ""}`} data-mode={item} title={modeConfig[item].label} onClick={() => { setMode(item); setIsDropdownOpen(false); setSubmenuMode(null); }}>
                              <ModeIcon mode={item} />
                              <span>{item === "predict" ? `${modeConfig[item].label} (${predictProperties.length})` : item === "property" ? `${modeConfig[item].label} (1)` : modeConfig[item].label}</span>
                              {item !== "similarity" ? <button className="dropdown-item-config" type="button" data-config-mode={item} title="配置性能指标" onClick={(event) => { event.stopPropagation(); setSubmenuMode(item as "property" | "predict"); setIsDropdownOpen(false); }}><Settings className="icon-gear" width={10} height={10} /></button> : null}
                            </div>
                          ))}
                        </div>
                        <div className={`generate-submenu-panel${submenuMode === "property" ? " single-select-mode" : ""}`} id="property-submenu-panel" style={{ display: submenuMode ? "flex" : "none" }}>
                          <div className="submenu-header"><span>{submenuMode === "predict" ? "配置预测性能指标" : "配置探索性能指标"}</span></div>
                          <div className="submenu-list">
                            {PROPERTY_OPTIONS.map((property) => {
                              const checked = submenuMode === "predict" ? predictProperties.includes(property.key) : selectedProperty === property.key;
                              return <label className="submenu-item" key={property.key}><input type="checkbox" checked={checked} onChange={() => { if (submenuMode === "predict") togglePredictProperty(property.key); else setSelectedProperty(property.key); }} /><span>{property.label} ({property.shortLabel})</span></label>;
                            })}
                          </div>
                          <div className="submenu-footer"><div className="submenu-footer-left">{submenuMode === "predict" ? <><input type="checkbox" className="submenu-select-all-checkbox" checked={predictProperties.length === PROPERTY_OPTIONS.length} onChange={(event) => setSelectedProperties(event.target.checked ? PROPERTY_OPTIONS.map((item) => item.key) : [DEFAULT_PROPERTY])} /><span className="submenu-select-all">全选</span></> : null}<span className="submenu-count" id="val-submenu-count">(已选 {submenuMode === "predict" ? predictProperties.length : 1} 项)</span></div><button className="btn btn--primary btn--sm" id="btn-submenu-save" type="button" onClick={() => setSubmenuMode(null)}>确定</button></div>
                        </div>
                      </div>
                    </div>
                  </div>
                  {feedback ? <div className="polymer-capsule-feedback">{feedback}</div> : null}
                </div>
              </div>
            </div>
          </main>
            </div>
          </div>
        </div>
      </div>

      <button className="btn-expand-analysis" id="btn-expand-analysis" type="button" title="展开分析面板" style={{ display: hasAnalysisRun && !isAnalysisOpen ? "flex" : "none" }} onClick={() => setIsAnalysisOpen(true)}><Sparkles width={14} height={14} /></button>
      <div className="analysis-resizer" id="analysis-resizer" title="拖拽调整宽度" style={{ display: isAnalysisOpen ? "flex" : "none", height: "100%", right: panelWidth }} onMouseDown={startResize}><div className="resizer-line" /></div>
      <div className="workspace-analysis-panel workspace-card" id="analysis-panel" style={{ display: isAnalysisOpen ? "flex" : "none", height: "100%", margin: 0, width: panelWidth }}>
            <div className="analysis-panel-header"><div className="panel-header-title"><h3>分析工作台</h3>{showSort ? <button className="btn-sort-similarity" id="btn-sort-similarity" type="button" title="点击切换排序" onClick={() => setSortOrder((current) => current === "desc" ? "asc" : "desc")}><span>相似度 {sortOrder === "desc" ? "降序" : "升序"}</span></button> : null}</div><button className="btn-close-analysis" id="btn-close-analysis" type="button" title="收起分析面板" onClick={() => setIsAnalysisOpen(false)}><X width={14} height={14} /></button></div>
            <div className="analysis-panel-body"><section className="results-section" id="results-section" style={{ display: "block" }}>
              {activeRunMode === "predict" ? (
                predict.isLoading ? <ResultSkeleton /> : predict.error ? <ErrorState message={predict.error} /> : predict.data ? <div className="results-content-area" id="results-content-area" style={{ display: "block" }}><PredictionCards data={predict.data} /></div> : <ResultsEmptyState title="暂无预测数据" description="请选择性能指标并运行预测探索。" />
              ) : isQueryLoading ? <ResultSkeleton /> : queryError ? <ErrorState message={queryError} /> : queryData && queryData.total > 0 ? (
                <div className="results-content-area" id="results-content-area" style={{ display: "block" }}><div className="similarity-grid">{sortedResults.map((result) => <SimilarityResultCard key={result.polymer_id} result={result} mode={activeRunMode} selectedProperty={activeProperty.key} />)}</div></div>
              ) : queryData ? <ResultsEmptyState title="未找到匹配结果" description="请检查当前 SMILES，或切换探索模式后重新运行。" /> : <ResultsEmptyState title="暂无探索数据" description="请在左侧绘制、导入或输入聚合物结构式，并点击运行按钮。" />}
            </section></div>
          </div>
    </div>
  );
}
